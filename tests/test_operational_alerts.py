from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mcp_second_brain.operational_alerts import (
    ALERT_KEYS,
    OperationalAlertEvaluator,
    QueryWindow,
    main,
)
from mcp_second_brain.query_events import (
    ActorRole,
    ErrorCode,
    EventStatus,
    QueryEvent,
)


def snapshot(**changes):
    values = {
        "queue_oldest_seconds": 1.0,
        "query_p95_ms": 10.0,
        "query_timeout_rate": 0.0,
        "worker_heartbeat_ok": True,
        "storage_ok": True,
        "event_loss_count": 0,
        "event_written_count": 0,
        "backup_ok": True,
    }
    values.update(changes)
    return values


def evaluator():
    return OperationalAlertEvaluator(
        queue_oldest_seconds_threshold=60,
        query_p95_ms_threshold=250,
        query_timeout_rate_threshold=0.1,
    )


def event(*, operation="query", duration_ms=10.0, timeout=False):
    return QueryEvent(
        event_id=uuid4(),
        request_id=uuid4(),
        actor_id=uuid4(),
        actor_role=ActorRole.MEMBER,
        service="second_brain",
        tool="search_notes",
        operation=operation,
        started_at=datetime.now(UTC),
        duration_ms=duration_ms,
        status=EventStatus.ERROR if timeout else EventStatus.SUCCESS,
        error_code=ErrorCode.TIMEOUT if timeout else ErrorCode.NONE,
        result_count=None,
        revision=None,
        retrieval_revision=None,
    )


def test_three_bad_samples_trigger_once_and_three_good_samples_recover_once():
    alerts = evaluator()
    bad = snapshot(queue_oldest_seconds=61)

    assert alerts.evaluate(bad).triggered == ()
    assert alerts.evaluate(bad).triggered == ()
    third = alerts.evaluate(bad)
    assert third.triggered == ("queue_oldest_seconds",)
    assert third.codes == ("queue_oldest_seconds_active",)
    assert alerts.evaluate(bad).codes == ()

    assert alerts.evaluate(snapshot()).recovered == ()
    assert alerts.evaluate(snapshot()).recovered == ()
    recovered = alerts.evaluate(snapshot())
    assert recovered.recovered == ("queue_oldest_seconds",)
    assert recovered.codes == ("queue_oldest_seconds_recovered",)
    assert alerts.evaluate(snapshot()).codes == ()


def test_unknown_and_invalid_values_never_count_as_healthy_or_clear_active():
    alerts = evaluator()
    bad = snapshot(storage_ok=False)
    for _ in range(3):
        result = alerts.evaluate(bad)
    assert "storage_ok" in result.active

    for value in (None, "yes", 1):
        result = alerts.evaluate(snapshot(storage_ok=value))
        assert "storage_ok" in result.active
        assert "storage_ok" in result.unknown
        assert result.recovered == ()

    alerts.evaluate(snapshot(storage_ok=True))
    alerts.evaluate(snapshot(storage_ok=None))
    alerts.evaluate(snapshot(storage_ok=True))
    assert alerts.evaluate(snapshot(storage_ok=True)).recovered == ()
    recovered = alerts.evaluate(snapshot(storage_ok=True))
    assert recovered.recovered == ("storage_ok",)


def test_all_fixed_thresholds_and_boolean_checks_are_evaluated():
    alerts = evaluator()
    bad = snapshot(
        queue_oldest_seconds=60,
        query_p95_ms=250,
        query_timeout_rate=0.1,
        worker_heartbeat_ok=False,
        storage_ok=False,
        backup_ok=False,
    )
    for _ in range(3):
        result = alerts.evaluate(bad)
    assert result.active == (
        "queue_oldest_seconds",
        "query_p95_ms",
        "query_timeout_rate",
        "worker_heartbeat_ok",
        "storage_ok",
        "backup_ok",
    )


def test_event_loss_uses_delta_and_counter_reset_is_unknown():
    alerts = evaluator()
    first = alerts.evaluate(snapshot(event_loss_count=10, event_written_count=20))
    assert "event_loss_count" in first.unknown

    alerts.evaluate(snapshot(event_loss_count=11, event_written_count=21))
    alerts.evaluate(snapshot(event_loss_count=12, event_written_count=22))
    triggered = alerts.evaluate(snapshot(event_loss_count=13, event_written_count=23))
    assert triggered.triggered == ("event_loss_count",)

    reset = alerts.evaluate(snapshot(event_loss_count=2, event_written_count=2))
    assert "event_loss_count" in reset.unknown
    assert "event_loss_count" in reset.active
    alerts.evaluate(snapshot(event_loss_count=2, event_written_count=3))
    alerts.evaluate(snapshot(event_loss_count=2, event_written_count=4))
    recovered = alerts.evaluate(snapshot(event_loss_count=2, event_written_count=5))
    assert recovered.recovered == ("event_loss_count",)


def test_event_loss_does_not_recover_without_new_written_events():
    alerts = evaluator()
    alerts.evaluate(snapshot(event_loss_count=0, event_written_count=10))
    for loss, written in ((1, 11), (2, 12), (3, 13)):
        result = alerts.evaluate(
            snapshot(event_loss_count=loss, event_written_count=written)
        )
    assert "event_loss_count" in result.active

    for _ in range(10):
        result = alerts.evaluate(snapshot(event_loss_count=3, event_written_count=13))
    assert "event_loss_count" in result.active
    assert "event_loss_count" in result.unknown
    assert result.recovered == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"queue_oldest_seconds_threshold": 0},
        {"query_p95_ms_threshold": float("inf")},
        {"query_timeout_rate_threshold": -0.1},
        {"query_timeout_rate_threshold": 1.1},
        {"query_p95_ms_threshold": True},
    ],
)
def test_thresholds_must_be_explicit_finite_values(kwargs):
    values = {
        "queue_oldest_seconds_threshold": 60,
        "query_p95_ms_threshold": 250,
        "query_timeout_rate_threshold": 0.1,
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match="invalid operational alert threshold"):
        OperationalAlertEvaluator(**values)


def test_evaluator_does_not_retain_raw_snapshot_values():
    alerts = evaluator()
    raw = "private query /vault/path API-key"
    result = alerts.evaluate(snapshot(query_p95_ms=raw, unexpected=raw))
    rendered = repr(alerts) + json.dumps(result.to_payload())
    assert raw not in rendered
    assert "query_p95_ms" in result.unknown


def test_query_window_filters_events_bounds_storage_and_computes_snapshot():
    now = [0.0]
    window = QueryWindow(clock=lambda: now[0])

    assert not window.observe(event(operation="write"))
    for index in range(100):
        assert window.observe(
            event(duration_ms=float(index + 1), timeout=index < 10)
        )
    assert window.snapshot() == {
        "query_p95_ms": 95.0,
        "query_timeout_rate": 0.1,
    }
    assert len(window) == 100
    assert "actor" not in repr(window)

    for _ in range(1_050):
        window.observe(event(operation="read"))
    assert len(window) == 1_000
    now[0] = 301.0
    assert window.snapshot() == {
        "query_p95_ms": None,
        "query_timeout_rate": None,
    }
    assert len(window) == 0


def test_query_window_requires_one_hundred_valid_samples():
    window = QueryWindow(clock=lambda: 5.0)
    for _ in range(99):
        window.observe(event())
    assert window.snapshot()["query_p95_ms"] is None
    assert not window.observe(event(duration_ms=float("nan")))


def test_cli_processes_bounded_fixed_schema_without_echoing_input():
    samples = [snapshot(storage_ok=False) for _ in range(3)]
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        [
            "--queue-oldest-seconds-threshold",
            "60",
            "--query-p95-ms-threshold",
            "250",
            "--query-timeout-rate-threshold",
            "0.1",
        ],
        stdin=io.StringIO(json.dumps({"samples": samples})),
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue()) == {
        "active": ["storage_ok"],
        "codes": ["storage_ok_active"],
        "unknown": ["event_loss_count"],
    }

    secret = "private-query-/vault/path"
    stdout = io.StringIO()
    code = main(
        [
            "--queue-oldest-seconds-threshold",
            "60",
            "--query-p95-ms-threshold",
            "250",
            "--query-timeout-rate-threshold",
            "0.1",
        ],
        stdin=io.StringIO(json.dumps({"samples": [snapshot(extra=secret)]})),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    assert code == 2
    assert json.loads(stdout.getvalue()) == {
        "active": [],
        "codes": ["invalid_input"],
        "unknown": list(ALERT_KEYS),
    }
    assert secret not in stdout.getvalue()


def test_cli_rejects_oversize_and_more_than_one_hundred_samples():
    argv = [
        "--queue-oldest-seconds-threshold",
        "60",
        "--query-p95-ms-threshold",
        "250",
        "--query-timeout-rate-threshold",
        "0.1",
    ]
    for payload in (
        "x" * (16 * 1024 + 1),
        json.dumps({"samples": [snapshot() for _ in range(101)]}),
    ):
        stdout = io.StringIO()
        assert (
            main(argv, stdin=io.StringIO(payload), stdout=stdout, stderr=io.StringIO())
            == 2
        )
        assert json.loads(stdout.getvalue())["codes"] == ["invalid_input"]
