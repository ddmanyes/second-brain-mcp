"""Storage-free invocation telemetry security and lifecycle checks."""

import asyncio
import json
from contextvars import ContextVar
from uuid import UUID, uuid4

import pytest

from mcp_second_brain.query_events import (
    Actor,
    ActorRole,
    ErrorCode,
    EventStatus,
    Outcome,
    QueryEventRecorder,
    mark_outcome,
)


@pytest.fixture
def recorder():
    events = []
    actor = Actor(uuid4(), ActorRole.MEMBER)
    return (
        QueryEventRecorder(
            service="lcdda",
            actor_getter=lambda: actor,
            sink=events.append,
            enabled=True,
        ),
        events,
        actor,
    )


def test_success_allowlist_and_secrets(recorder):
    record, events, actor = recorder
    secret = "sk-secret /private/notes/alice Authorization: bearer password"

    @record.instrument(
        tool="search_notes",
        operation="query",
        classify_result=lambda result: Outcome(result_count=len(result)),
    )
    def search(query, **kwargs):
        return [query, kwargs]

    result = search(secret, headers=secret, actor_id="spoofed")
    event = events[0]
    payload = event.to_payload()
    assert result[0] == secret
    assert len(events) == 1
    assert secret not in json.dumps(payload)
    assert event.actor_id == actor.user_id
    assert event.result_count == 2
    assert event.duration_ms >= 0
    assert event.started_at.utcoffset().total_seconds() == 0
    assert UUID(payload["request_id"]) != UUID(payload["event_id"])
    assert set(payload) == {
        "event_id",
        "request_id",
        "actor_id",
        "actor_role",
        "service",
        "tool",
        "operation",
        "started_at",
        "duration_ms",
        "status",
        "error_code",
        "result_count",
        "revision",
        "retrieval_revision",
    }


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (RuntimeError("sk-secret"), EventStatus.ERROR, ErrorCode.INTERNAL),
        (PermissionError("/private/secret"), EventStatus.DENIED, ErrorCode.DENIED),
        (TimeoutError("password"), EventStatus.ERROR, ErrorCode.TIMEOUT),
        (asyncio.CancelledError("secret"), EventStatus.CANCELLED, ErrorCode.CANCELLED),
    ],
)
def test_async_failure_preserves_exception(recorder, exc, status, code):
    record, events, _ = recorder

    @record.instrument(tool="search_notes", operation="query")
    async def search():
        raise exc

    with pytest.raises(type(exc)) as caught:
        asyncio.run(search())
    assert caught.value is exc
    assert events[0].status == status
    assert events[0].error_code == code
    assert events[0].result_count is None
    assert str(exc) not in json.dumps(events[0].to_payload())


def test_sync_error_and_context_reset(recorder):
    record, events, _ = recorder

    @record.instrument(tool="search_notes", operation="query")
    def search(fail):
        if fail:
            raise RuntimeError("private")
        return 4

    with pytest.raises(RuntimeError):
        search(True)
    assert search(False) == 4
    assert [e.status for e in events] == [EventStatus.ERROR, EventStatus.UNKNOWN]
    assert len({e.request_id for e in events}) == 2


def test_disabled_does_not_call_dependencies():
    def forbidden(*args):
        pytest.fail("disabled dependency called")

    record = QueryEventRecorder(service="lcdda", actor_getter=forbidden, sink=forbidden)

    @record.instrument(tool="search", operation="query", classify_result=forbidden)
    def search():
        return "result"

    @record.instrument(tool="search", operation="query", classify_result=forbidden)
    async def async_search():
        return "async result"

    assert search() == "result"
    assert asyncio.run(async_search()) == "async result"
    assert QueryEventRecorder(service="lcdda").failure_count == 0


def test_nested_sync_and_async_helpers_emit_only_outer(recorder):
    record, events, _ = recorder

    @record.instrument(tool="helper", operation="read")
    def helper():
        return 3

    @record.instrument(tool="async_helper", operation="read")
    async def async_helper():
        return helper()

    @record.instrument(tool="search", operation="query")
    async def search():
        return await async_helper()

    assert asyncio.run(search()) == 3
    assert [e.tool for e in events] == ["search"]


def test_concurrent_actor_context_isolation():
    current = ContextVar("actor")
    events = []
    record = QueryEventRecorder(
        service="lcdda", actor_getter=current.get, sink=events.append, enabled=True
    )

    @record.instrument(tool="search", operation="query")
    async def search():
        await asyncio.sleep(0)
        return current.get().user_id

    async def run(actor):
        token = current.set(actor)
        try:
            return await search()
        finally:
            current.reset(token)

    actors = [Actor(uuid4(), ActorRole.MEMBER) for _ in range(20)]

    async def main():
        return await asyncio.gather(*(run(actor) for actor in actors))

    assert set(asyncio.run(main())) == {a.user_id for a in actors}
    assert {e.actor_id for e in events} == {a.user_id for a in actors}
    assert len({e.request_id for e in events}) == 20


def test_sink_failure_is_nonfatal_bounded_and_sanitized(caplog):
    def sink(event):
        raise RuntimeError("sk-secret /private/secret Authorization")

    record = QueryEventRecorder(
        service="lcdda", actor_getter=Actor, sink=sink, enabled=True
    )

    @record.instrument(tool="search", operation="query")
    def search():
        return 9

    for _ in range(100):
        assert search() == 9
    assert record.failure_count == 100
    assert len(caplog.records) == 7
    assert "sk-secret" not in caplog.text
    assert "/private/secret" not in caplog.text
    record._failures = 65535
    assert search() == 9
    assert record.failure_count == 65535


def test_explicit_denied_result_classifier(recorder):
    record, events, _ = recorder

    @record.instrument(
        tool="search",
        operation="query",
        classify_result=lambda result: Outcome(EventStatus.DENIED, ErrorCode.DENIED),
    )
    def search():
        return {"error": "private reason"}

    assert search() == {"error": "private reason"}
    assert events[0].status == EventStatus.DENIED


def test_invalid_callback_values_do_not_leak(recorder):
    record, events, _ = recorder
    record._actor_getter = lambda: {"user_id": "secret"}

    @record.instrument(
        tool="search",
        operation="query",
        classify_result=lambda result: {"result_count": "secret"},
    )
    def search():
        return "private result"

    assert search() == "private result"
    assert events[0].actor_id is None
    assert events[0].actor_role == ActorRole.UNKNOWN
    assert events[0].result_count is None
    assert events[0].status == EventStatus.UNKNOWN
    assert events[0].error_code == ErrorCode.CLASSIFICATION_FAILED
    assert record.failure_count == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"service": "/private/secret"},
        {"service": "lcdda", "revision": "secret-token"},
        {"service": "lcdda", "enabled": True},
    ],
)
def test_reject_bad_configuration(kwargs):
    with pytest.raises(ValueError):
        QueryEventRecorder(**kwargs)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Actor("env:secret", ActorRole.ADMIN),
        lambda: Actor(uuid4(), "admin"),
        lambda: Outcome(result_count=True),
        lambda: Outcome(result_count=-1),
        lambda: Outcome(result_count=2**63),
        lambda: Outcome(status="success"),
        lambda: Outcome(EventStatus.SUCCESS, ErrorCode.INTERNAL),
    ],
)
def test_reject_non_schema_values(factory):
    with pytest.raises((ValueError, TypeError)):
        factory()


def test_duration_uses_monotonic(recorder, monkeypatch):
    record, events, _ = recorder
    times = iter([100.0, 100.125])
    monkeypatch.setattr(
        "mcp_second_brain.query_events.time.monotonic", lambda: next(times)
    )

    @record.instrument(tool="search", operation="query")
    def search():
        return None

    search()
    assert events[0].duration_ms == 125.0


def test_actual_task_cancellation_emits_once(recorder):
    record, events, _ = recorder

    async def scenario():
        started = asyncio.Event()

        @record.instrument(tool="search", operation="query")
        async def search():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(search())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(events) == 1
    assert events[0].status == EventStatus.CANCELLED


def test_sink_failure_preserves_original_tool_exception(recorder):
    record, _, _ = recorder
    original = PermissionError("private denial reason")

    def broken_sink(event):
        raise RuntimeError("sink secret")

    record._sink = broken_sink

    @record.instrument(tool="search", operation="query")
    def search():
        raise original

    with pytest.raises(PermissionError) as caught:
        search()
    assert caught.value is original
    assert record.failure_count == 1


def test_no_exception_message_classification(recorder):
    record, events, _ = recorder

    @record.instrument(tool="search", operation="query")
    def search():
        raise ValueError("permission denied cancelled timeout")

    with pytest.raises(ValueError):
        search()
    assert events[0].error_code == ErrorCode.INTERNAL


def test_source_marker_clears_next_invocation(recorder):
    record, events, _ = recorder

    @record.instrument(tool="search", operation="query")
    def search(denied):
        if denied:
            assert mark_outcome(Outcome(EventStatus.DENIED, ErrorCode.DENIED))
        return "opaque string"

    search(True)
    search(False)
    assert [e.status for e in events] == [EventStatus.DENIED, EventStatus.UNKNOWN]
    assert not mark_outcome(Outcome(result_count=999))


def test_nested_helper_cannot_overwrite_outer_marker(recorder):
    record, events, _ = recorder

    @record.instrument(tool="helper", operation="read")
    def helper():
        assert not mark_outcome(Outcome(result_count=99))

    @record.instrument(tool="search", operation="query")
    def search():
        assert mark_outcome(Outcome(result_count=2))
        helper()

    search()
    assert len(events) == 1
    assert events[0].result_count == 2


def test_concurrent_markers_are_isolated(recorder):
    record, events, _ = recorder

    @record.instrument(tool="search", operation="query")
    async def search(count):
        assert mark_outcome(Outcome(result_count=count))
        await asyncio.sleep(0)
        return count

    async def main():
        return await asyncio.gather(*(search(i) for i in range(20)))

    asyncio.run(main())
    assert sorted(e.result_count for e in events) == list(range(20))


def test_raised_failure_overrides_success_marker(recorder):
    record, events, _ = recorder

    @record.instrument(tool="search", operation="query")
    def search():
        mark_outcome(Outcome(result_count=9))
        raise PermissionError("private reason")

    with pytest.raises(PermissionError):
        search()
    assert events[0].status == EventStatus.DENIED
    assert events[0].result_count is None


def test_startup_sink_configuration_and_disable():
    events = []
    recorder = QueryEventRecorder(service="lcdda", actor_getter=Actor)
    recorder.configure_sink(events.append)

    @recorder.instrument(tool="search", operation="query")
    def search():
        return "unclassified opaque return"

    search()
    assert events[0].status == EventStatus.UNKNOWN
    assert events[0].error_code == ErrorCode.UNCLASSIFIED
    with pytest.raises(RuntimeError):
        recorder.configure_sink(events.append)
    recorder.disable()
    search()
    assert len(events) == 1
