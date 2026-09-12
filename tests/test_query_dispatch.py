import asyncio
import threading
from contextvars import ContextVar

import pytest

from mcp_second_brain.query_dispatch import QueryBusy, QueryDispatcher
from mcp_second_brain.query_events import (
    Actor,
    ActorRole,
    Outcome,
    QueryEventRecorder,
    mark_outcome,
)


async def _event_is_set(event):
    assert await asyncio.to_thread(event.wait, .5)


def test_concurrent_reads_preserve_context_and_event_loop():
    dispatcher = QueryDispatcher(capacity=2, per_actor=1, timeout=1)
    identity = ContextVar("test_actor")
    barrier = threading.Barrier(2, timeout=.5)

    def read():
        barrier.wait()
        return identity.get()

    async def request(actor):
        identity.set(actor)
        return await dispatcher.run(actor, read)

    async def run():
        try:
            return await asyncio.gather(request("a"), request("b"))
        finally:
            assert await dispatcher.close(timeout=1)

    assert asyncio.run(run()) == ["a", "b"]


def test_timeout_retains_capacity_until_real_work_finishes():
    dispatcher = QueryDispatcher(capacity=1, per_actor=1, timeout=.02)
    release = threading.Event()

    async def run():
        try:
            with pytest.raises(TimeoutError):
                await dispatcher.run("a", release.wait, .5)
            assert dispatcher.active == 1
            with pytest.raises(QueryBusy):
                await dispatcher.run("b", lambda: None)
            release.set()
            for _ in range(100):
                if dispatcher.active == 0:
                    break
                await asyncio.sleep(.001)
            assert dispatcher.active == 0
            assert await dispatcher.run("b", lambda: "ok") == "ok"
        finally:
            release.set()
            assert await dispatcher.close(timeout=1)

    asyncio.run(run())


def test_cancellation_retains_slot_and_close_reports_undrained_work():
    dispatcher = QueryDispatcher(capacity=1, per_actor=1, timeout=1)
    started = threading.Event()
    release = threading.Event()

    def read():
        started.set()
        release.wait(.5)

    async def run():
        task = asyncio.create_task(dispatcher.run("a", read))
        await _event_is_set(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert dispatcher.active == 1
        assert not await dispatcher.close(timeout=0)
        with pytest.raises(QueryBusy):
            await dispatcher.run("b", lambda: None)

        release.set()
        assert await dispatcher.close(timeout=1)
        assert dispatcher.active == 0

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_per_actor_limit_does_not_consume_other_actors_capacity():
    dispatcher = QueryDispatcher(capacity=2, per_actor=1, timeout=1)
    started = threading.Event()
    release = threading.Event()

    def read():
        started.set()
        release.wait(.5)
        return "a"

    async def run():
        first = asyncio.create_task(dispatcher.run("a", read))
        await _event_is_set(started)
        with pytest.raises(QueryBusy):
            await dispatcher.run("a", lambda: "same actor")
        assert await dispatcher.run("b", lambda: "other actor") == "other actor"
        release.set()
        assert await first == "a"
        assert await dispatcher.close(timeout=1)

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_worker_outcome_crosses_copied_context_without_losing_identity():
    dispatcher = QueryDispatcher(capacity=1, per_actor=1, timeout=1)
    identity = ContextVar("test_actor")
    events = []
    actor = Actor(role=ActorRole.MEMBER)
    recorder = QueryEventRecorder(
        service="lcdda",
        actor_getter=lambda: actor,
        sink=events.append,
        enabled=True,
    )

    def read():
        assert identity.get() == "alice"
        mark_outcome(Outcome(result_count=7))
        return "ok"

    @recorder.instrument(tool="search_notes", operation="query")
    async def request():
        identity.set("alice")
        return await dispatcher.run("alice", read)

    async def run():
        try:
            assert await request() == "ok"
        finally:
            assert await dispatcher.close(timeout=1)

    asyncio.run(run())
    assert len(events) == 1
    assert events[0].result_count == 7


def test_closed_dispatcher_refuses_work():
    dispatcher = QueryDispatcher()

    async def run():
        assert await dispatcher.drain(timeout=0)
        assert await dispatcher.close(timeout=0)
        with pytest.raises(QueryBusy):
            await dispatcher.run("a", lambda: None)

    asyncio.run(run())
