import asyncio
import json

from mcp_second_brain.identity import _current, set_identity
from tests.support.multiuser_load import (
    MIN_EVENT_SAMPLES,
    MIN_WARMUP,
    SyntheticEnvironment,
    run_acceptance,
    write_report,
)


def test_true_queue_and_shared_commit_persist_hash_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")
    environment = SyntheticEnvironment(
        tmp_path, client_count=2, events_enabled=True
    )
    first, second = environment.clients

    token = set_identity(first.identity)
    try:
        job = environment.queue.enqueue(
            "ingest", {"synthetic": True}, ["synthetic-worker"], str(tmp_path)
        )
    finally:
        _current.reset(token)
    token = set_identity(first.identity)
    try:
        assert environment.ingest(first, 0)[0] is False
    finally:
        _current.reset(token)
    token = set_identity(second.identity)
    try:
        assert environment.ingest(second, 0)[0] is True
    finally:
        _current.reset(token)

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert first.identity.credential_id in persisted
    assert first.raw_key not in persisted
    assert second.raw_key not in persisted
    article = (tmp_path / "vault/30-resources/synthetic-controlled.md").read_text()
    assert first.identity.user_uuid in article
    assert second.identity.user_uuid in article
    assert job["owner_id"] == first.identity.user_uuid
    lifecycle = asyncio.run(environment.close())
    assert lifecycle["queries_drained"] is True
    assert lifecycle["event_sink_closed"] is True


def test_short_fixed_rate_matrix_emits_complete_synthetic_report(tmp_path):
    report = asyncio.run(
        run_acceptance(
            duration_seconds=0.03,
            clients=(1, 2),
            mixtures=("query_only", "query+ingest"),
            rate_per_second=40,
            warmup_requests=MIN_WARMUP,
            event_samples=MIN_EVENT_SAMPLES,
            base_root=tmp_path / "roots",
        )
    )
    assert report["schema"] == "second-brain.synthetic-multiuser-load.v1"
    assert len(report["phases"]) == 4
    assert report["telemetry_comparison"]["on"]["samples"] >= 200
    assert report["telemetry_comparison"]["off"]["samples"] >= 200
    assert report["telemetry_comparison"]["p95_overhead_target_ms"] == 20
    assert "p95_overhead_ms" in report["telemetry_comparison"]
    assert "PostgreSQL latency, locks, RLS, connections, or storage metrics" in report["not_measured"]
    for phase in report["phases"]:
        assert phase["warmup_requests"] >= 200
        assert phase["pacing"] == "fixed_rate_monotonic_deadline"
        assert set(phase["latency_ms"]) == {"p50", "p95", "p99"}
        assert phase["count"] > 0
        assert phase["actor_isolation_failures"] == 0
        assert phase["event_loss"] == 0
        assert phase["lifecycle"]["queries_drained"] is True
        assert phase["lifecycle"]["event_sink_closed"] is True
        assert set(phase["ingest_stage_ms"]) == {
            "submit",
            "queue_wait",
            "job_run",
        }
        assert phase["ingest_every_queries"] == 20
        assert phase["queue_limits"] == {"capacity": 50, "per_owner": 5}

    output = tmp_path / "load-results.json"
    write_report(report, output)
    assert json.loads(output.read_text())["phases"][0]["resources"]["after"][
        "rss_max_bytes"
    ] > 0
