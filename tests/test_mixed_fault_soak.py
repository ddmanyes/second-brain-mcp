"""Opt-in five-minute mixed fault rehearsal with explicitly synthetic dependencies."""

import asyncio
from collections import Counter
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import time

import pytest

pytest.importorskip("lcdda_ingest.queue")

from lcdda_ingest.queue import QueueDenied
from mcp_second_brain.identity import (
    _current,
    get_current_identity,
    hash_key,
    set_identity,
)
from mcp_second_brain.operational_alerts import QueryWindow
from mcp_second_brain.query_dispatch import QueryDispatcher
from mcp_second_brain.query_event_store import BoundedEventSink
from mcp_second_brain.query_events import ErrorCode, EventStatus, Outcome, mark_outcome
from tests.support.multiuser_load import SyntheticEnvironment


class FaultEnvironment(SyntheticEnvironment):
    def response(self, value):
        expected, _, sequence = value.partition(":")
        identity = get_current_identity()
        if identity.user_uuid != expected:
            raise PermissionError("actor mismatch")
        sequence = int(sequence)
        if sequence % 100 == 3:
            time.sleep(0.1)
        if sequence % 100 == 4:
            mark_outcome(Outcome(EventStatus.ERROR, ErrorCode.UNAVAILABLE))
            return "synthetic dependency unavailable"
        if sequence % 7 == 0:
            mark_outcome(Outcome(result_count=0))
            return "synthetic zero result"
        mark_outcome(Outcome(result_count=1))
        return f"synthetic result {identity.user_uuid}"

    def _register_query(self):
        @self.mcp.tool()
        def search_notes(query: str):
            return self.response(query)

        @self.mcp.tool()
        def read_note(path: str):
            return self.response(path)


@pytest.mark.skipif(
    os.environ.get("SB_MANAGED_FAULT_SOAK") != "1",
    reason="explicit five-minute soak opt-in required",
)
def test_five_minute_mixed_fault_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")
    env = FaultEnvironment(tmp_path, client_count=10, events_enabled=False)
    env.mcp.query_dispatcher = QueryDispatcher(capacity=4, per_actor=2, timeout=0.03)
    outcomes, counts = Counter(), Counter()
    window = QueryWindow()
    writer_calls = 0

    def writer(event):
        nonlocal writer_calls
        writer_calls += 1
        if writer_calls % 101 == 0:
            raise OSError("synthetic event storage failure")
        env.event_counter(event)

    env.sink = BoundedEventSink(writer, capacity=256)
    env.sink.start()

    def observe(event):
        window.observe(event)
        outcomes[event.error_code.value] += 1
        env.sink(event)

    env.recorder.configure_sink(observe)
    env.queue.execution_guard = lambda: True

    async def query(sequence):
        client = env.clients[sequence % len(env.clients)]
        token = set_identity(client.identity)
        started = time.monotonic()
        try:
            method = "read_note" if sequence % 2 else "search_notes"
            parameter = "path" if method == "read_note" else "query"
            try:
                result = str(
                    await env.mcp.call_tool(
                        method, {parameter: f"{client.identity.user_uuid}:{sequence}"}
                    )
                )
            except Exception:
                counts[
                    "expected_timeouts" if sequence % 100 == 3 else "unexpected_errors"
                ] += 1
                return
            counts["queries_returned"] += 1
            assert all(
                other.identity.user_uuid not in result
                for other in env.clients
                if other.index != client.index
            )
            if sequence % 100 not in {3, 4} and sequence % 7:
                assert client.identity.user_uuid in result
        finally:
            counts["queries_finished"] += 1
            counts["query_duration_total_ms"] += (time.monotonic() - started) * 1000
            _current.reset(token)

    def queue_fault(sequence):
        client = env.clients[0]
        token = set_identity(client.identity)
        try:
            job = env.queue.enqueue("ingest", {"synthetic": True}, [], None)
            counts["accepted"] += 1
            env.allowed.pop(client.identity.credential_id)
            with pytest.raises(QueueDenied):
                env.queue.visible("ingest")
            assert (
                env.queue.dispatch_once(lambda job: pytest.fail("revoked job launched"))
                is None
            )
            assert env.workspace.read_job(job["id"])["status"] == "cancelled"
            counts["revoked_jobs_cancelled"] += 1
        finally:
            _current.reset(token)
        raw = f"synthetic-replacement-{sequence}"
        changed = replace(
            client,
            raw_key=raw,
            identity=replace(client.identity, credential_id=hash_key(raw)),
        )
        env.clients = (changed, *env.clients[1:])
        env.allowed[changed.identity.credential_id] = changed.identity.user_uuid
        token = set_identity(changed.identity)
        try:
            job = env.queue.enqueue("ingest", {"synthetic": True}, [], None)
            counts["accepted"] += 1
            env.workspace.update_job(job["id"], status="running", pid=0)
            assert env.queue.recover() == [job["id"]]
            assert env.queue.recover() == []
            assert env.workspace.read_job(job["id"])["status"] == "interrupted"
            counts["dead_workers_reconciled"] += 1
        finally:
            _current.reset(token)

    async def run():
        pending = set()
        failures = []
        started = time.monotonic()

        def completed(task):
            pending.discard(task)
            if task.exception() is not None:
                failures.append(True)

        try:
            for sequence in range(3000):
                await asyncio.sleep(max(0, started + sequence / 10 - time.monotonic()))
                task = asyncio.create_task(query(sequence))
                pending.add(task)
                task.add_done_callback(completed)
                if sequence % 20 == 0:
                    client = env.clients[(sequence // 20) % len(env.clients)]
                    token = set_identity(client.identity)
                    try:
                        duplicate, _ = await asyncio.to_thread(
                            env.ingest, client, sequence
                        )
                        counts["accepted"] += 1
                        counts["ingests"] += 1
                        counts["duplicates"] += int(duplicate)
                    finally:
                        _current.reset(token)
                if sequence % 100 == 0:
                    await asyncio.to_thread(queue_fault, sequence)
            if pending:
                await asyncio.gather(*pending)
            await asyncio.sleep(max(0, started + 300 - time.monotonic()))
        finally:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            lifecycle = await env.close()
        assert not failures
        assert counts["unexpected_errors"] == 0
        assert counts["queries_finished"] == 3000
        assert len(env.queue._jobs()) == counts["accepted"]
        assert all(
            job["status"] in {"succeeded", "cancelled", "interrupted"}
            for job in env.queue._jobs()
        )
        assert outcomes["timeout"] >= 1 and outcomes["unavailable"] == 30
        assert (
            env.sink.stats.failed > 0 and env.sink.stats.written > env.sink.stats.failed
        )
        assert env.sink.stats.pending == 0 and env.sink.stats.overflow == 0
        assert lifecycle["queries_drained"] and lifecycle["event_sink_closed"]
        report = {
            "schema": "synthetic-mixed-fault-soak.v1",
            "elapsed_seconds": time.monotonic() - started,
            "target_queries_per_second": 10,
            "clients": 10,
            "counts": dict(counts),
            "outcomes": dict(outcomes),
            "event_sink": asdict(env.sink.stats),
            "query_window": window.snapshot(),
            "actor_isolation_failures": 0,
            "accepted_jobs_retained": True,
            "lifecycle": lifecycle,
            "faults": [
                "slow query adapter",
                "unavailable database adapter",
                "event writer failure",
                "queued credential revocation",
                "dead worker PID reconciliation",
            ],
            "not_measured": [
                "real PostgreSQL or models",
                "HTTP/network",
                "real worker process restart",
                "production vault/NAS",
            ],
        }
        (Path(__file__).parents[1] / "docs/plans/load-fault-results.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )

    asyncio.run(run())
