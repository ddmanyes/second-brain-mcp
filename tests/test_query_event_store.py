"""Bounded, storage-free telemetry handoff tests; never connect to a database."""

from __future__ import annotations

import threading
import time

import pytest

from mcp_second_brain.query_event_store import BoundedEventSink
from mcp_second_brain.query_events import Actor, QueryEventRecorder


def make_query(sink, *, enabled=True):
    recorder = QueryEventRecorder(
        service="lcdda", actor_getter=Actor, sink=sink, enabled=enabled
    )

    @recorder.instrument(tool="search", operation="query")
    def query():
        return "result"

    return query


def test_disabled_never_starts_thread_or_writes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled telemetry spawned thread or wrote event")

    monkeypatch.setattr(threading.Thread, "start", forbidden)
    sink = BoundedEventSink(forbidden)
    assert make_query(sink, enabled=False)() == "result"
    assert sink.stats.accepted == sink.stats.rejected == 0
    assert sink.close(timeout=0)


def test_blocked_writer_does_not_stall_queries_and_capacity_is_bounded():
    entered = threading.Event()
    release = threading.Event()

    def writer(event):
        entered.set()
        assert release.wait(5)

    sink = BoundedEventSink(writer, capacity=2)
    sink.start()
    query = make_query(sink)
    try:
        assert query() == "result"
        assert entered.wait(1)
        start = time.monotonic()
        for _ in range(100):
            assert query() == "result"
        assert time.monotonic() - start < 1
        assert sink.stats.pending == 2
        assert sink.stats.accepted == 2
        assert sink.stats.overflow == 99
    finally:
        release.set()
        assert sink.close(timeout=2)
    assert sink.stats.written == 2
    assert sink.stats.pending == 0


def test_flush_and_close_deadlines_then_recover():
    entered = threading.Event()
    release = threading.Event()

    def writer(event):
        entered.set()
        release.wait(5)

    sink = BoundedEventSink(writer)
    sink.start()
    query = make_query(sink)
    query()
    assert entered.wait(1)
    try:
        start = time.monotonic()
        assert not sink.flush(timeout=0.01)
        assert not sink.close(timeout=0.01)
        assert time.monotonic() - start < 0.5
        assert query() == "result"
        assert sink.stats.rejected == 1
    finally:
        release.set()
        assert sink.close(timeout=2)
    assert sink.stats.written == 1


def test_writer_failure_count_sanitized_and_next_event_processed(caplog):
    calls = []

    def writer(event):
        calls.append(event.event_id)
        if len(calls) == 1:
            raise RuntimeError("API-key private/path password")

    sink = BoundedEventSink(writer)
    sink.start()
    query = make_query(sink)
    query()
    query()
    assert sink.flush(timeout=2)
    assert sink.close(timeout=2)
    assert sink.stats.failed == 1
    assert sink.stats.written == 1
    assert len(calls) == 2
    assert "password" not in caplog.text


def test_close_without_flush_drops_waiting_events():
    entered = threading.Event()
    release = threading.Event()

    def writer(event):
        entered.set()
        release.wait(5)

    sink = BoundedEventSink(writer, capacity=3)
    sink.start()
    query = make_query(sink)
    query()
    assert entered.wait(1)
    query()
    query()
    try:
        assert not sink.close(timeout=0, flush=False)
        assert sink.stats.dropped == 2
        assert sink.stats.pending == 1
    finally:
        release.set()
        assert sink.close(timeout=2)
    assert sink.stats.written == 1


def test_explicit_start_idempotent_and_closed_cannot_restart():
    events = []
    sink = BoundedEventSink(events.append)
    query = make_query(sink)
    query()
    assert sink.stats.rejected == 1
    sink.start()
    original = sink._thread
    sink.start()
    assert sink._thread is original
    query()
    assert sink.close(timeout=2)
    assert len(events) == 1
    with pytest.raises(RuntimeError, match="closed"):
        sink.start()


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_invalid_deadline_rejected(timeout):
    sink = BoundedEventSink(lambda event: None)
    with pytest.raises(ValueError):
        sink.flush(timeout=timeout)
    with pytest.raises(ValueError):
        sink.close(timeout=timeout)


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_invalid_capacity_rejected(capacity):
    with pytest.raises(ValueError):
        BoundedEventSink(lambda event: None, capacity=capacity)


def test_async_writer_rejected():
    async def writer(event):
        pass

    class AsyncWriter:
        async def __call__(self, event):
            pass

    for candidate in (writer, AsyncWriter()):
        with pytest.raises(TypeError, match="synchronous"):
            BoundedEventSink(candidate)


def test_accidental_awaitable_writer_is_failure_not_false_success():
    async def async_write():
        pass

    sink = BoundedEventSink(lambda event: async_write())
    sink.start()
    make_query(sink)()
    assert sink.close(timeout=2)
    assert sink.stats.failed == 1
    assert sink.stats.written == 0
