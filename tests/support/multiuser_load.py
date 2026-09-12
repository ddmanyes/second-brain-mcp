"""Fixed-rate synthetic acceptance load for multi-user query and intake paths.

This is deliberately a test harness, not a production benchmark.  It exercises
the registered FastMCP wrapper, query dispatcher, bounded event sink, lcdda's
durable queue, and the shared-article commit helper against isolated temporary
directories.  Retrieval and resolver work are deterministic local fakes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5, NAMESPACE_URL

from lcdda_ingest.queue import DurableQueue, QueueActor, QueueDenied
from lcdda_ingest.workspace import HarvestWorkspace
from mcp_second_brain.article_attribution import commit_shared_article
from mcp_second_brain.identity import (
    Identity,
    _current,
    get_current_identity,
    hash_key,
    set_identity,
)
from mcp_second_brain.query_dispatch import QueryDispatcher
from mcp_second_brain.query_event_mcp import ObservedFastMCP
from mcp_second_brain.query_event_store import BoundedEventSink
from mcp_second_brain.query_events import (
    Actor,
    ActorRole,
    Outcome,
    QueryEvent,
    QueryEventRecorder,
    mark_outcome,
)

MIXTURES = ("query_only", "query+ingest")
DEFAULT_CLIENTS = (1, 5, 10, 20)
DEFAULT_DURATION_SECONDS = 300.0
DEFAULT_MIXED_DURATION_SECONDS = 1800.0
MIN_WARMUP = 200
MIN_EVENT_SAMPLES = 200
INGEST_EVERY = 20
SYNTHETIC_SOURCE = "https://synthetic.invalid/article/controlled"
SYNTHETIC_ARTICLE = """---
title: Synthetic controlled article
tags: []
---

Deterministic local content.  No network, model, database, or resolver is used.
"""


@dataclass(frozen=True)
class Client:
    index: int
    identity: Identity
    raw_key: str


class EventCounter:
    """Bounded-sink writer that retains counts, not event payloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.count = 0
        self.actors: set[str] = set()

    def __call__(self, event: QueryEvent) -> None:
        with self._lock:
            self.count += 1
            if event.actor_id is not None:
                self.actors.add(str(event.actor_id))


def _actor() -> Actor:
    identity = get_current_identity()
    if identity is None or identity.user_uuid is None:
        return Actor()
    return Actor(UUID(identity.user_uuid), ActorRole(identity.role))


def controlled_resolver(source: str) -> dict[str, str]:
    """Return fixed, already-verified metadata without external resolution."""
    if source != SYNTHETIC_SOURCE:
        raise ValueError("unexpected synthetic source")
    return {"canonical_url": SYNTHETIC_SOURCE}


class SyntheticEnvironment:
    """One isolated MCP/queue/vault composition for a single phase."""

    def __init__(
        self,
        root: Path,
        *,
        client_count: int,
        events_enabled: bool,
        event_capacity: int = 256,
    ) -> None:
        self.root = root
        self.workspace_root = root / "workspace"
        self.vault = root / "vault"
        self.workspace = HarvestWorkspace(self.workspace_root, process_probe=lambda _pid: False)
        self.clients = tuple(self._client(index) for index in range(client_count))
        self.allowed = {
            client.identity.credential_id: client.identity.user_uuid
            for client in self.clients
        }
        self.event_counter = EventCounter()
        self.sink = BoundedEventSink(self.event_counter, capacity=event_capacity)
        if events_enabled:
            self.sink.start()
        self.recorder = QueryEventRecorder(
            service="second_brain",
            actor_getter=_actor,
            sink=self.sink,
            enabled=events_enabled,
            revision="0000000",
            retrieval_revision="0000000",
        )
        self.mcp = ObservedFastMCP("synthetic-load", stateless_http=True, recorder=self.recorder)
        self.mcp.query_dispatcher = QueryDispatcher(capacity=4, per_actor=2, timeout=2.0)
        self.queue = DurableQueue(
            self.workspace,
            actor_provider=self._queue_actor,
            verifier=self._verify_actor,
            capacity=50,
            per_owner=5,
        )
        self._register_query()

    @staticmethod
    def _client(index: int) -> Client:
        user_uuid = str(uuid5(NAMESPACE_URL, f"second-brain-load-client-{index}"))
        raw_key = f"synthetic-raw-credential-{index}-{user_uuid}"
        return Client(
            index,
            Identity(
                f"load-client-{index}",
                "member",
                user_uuid,
                credential_id=hash_key(raw_key),
            ),
            raw_key,
        )

    def _queue_actor(self) -> QueueActor:
        identity = get_current_identity()
        if identity is None:
            raise QueueDenied("missing synthetic identity")
        return QueueActor(
            identity.user_uuid or "",
            identity.credential_id or "",
            admin=identity.is_admin(),
        )

    def _verify_actor(self, actor: QueueActor) -> bool:
        return self.allowed.get(actor.credential_id) == actor.user_id

    def _register_query(self) -> None:
        @self.mcp.tool()
        def search_notes(query: str) -> str:
            identity = get_current_identity()
            expected, _, sequence = query.partition(":")
            if identity is None or identity.user_uuid != expected or not sequence.isdigit():
                raise PermissionError("synthetic actor mismatch")
            mark_outcome(Outcome(result_count=1))
            return f"synthetic-result:{identity.user_uuid}:{sequence}"

    async def query(self, client: Client, sequence: int) -> tuple[str, bool]:
        token = set_identity(client.identity)
        try:
            result = await self.mcp.call_tool(
                "search_notes", {"query": f"{client.identity.user_uuid}:{sequence}"}
            )
        finally:
            _current.reset(token)
        text = str(result)
        isolated = bool(client.identity.user_uuid and client.identity.user_uuid in text)
        return text, isolated

    def ingest(self, client: Client, sequence: int) -> tuple[bool, dict[str, float]]:
        """Run one local synthetic worker under identity copied into its thread."""
        if get_current_identity() != client.identity:
            raise QueueDenied("synthetic worker identity was not propagated")
        submit_started = time.monotonic()
        job = self.queue.enqueue(
            "ingest",
            {"synthetic": True, "sequence": sequence},
            ["synthetic-controlled-worker"],
            str(self.workspace_root),
        )
        submit_ms = (time.monotonic() - submit_started) * 1000
        job_started = time.monotonic()
        commit = commit_shared_article(
            self.vault,
            "30-resources/synthetic-controlled.md",
            SYNTHETIC_ARTICLE,
            client.identity,
            source=SYNTHETIC_SOURCE,
            index=lambda _path: True,
            verified_metadata=controlled_resolver(SYNTHETIC_SOURCE),
        )
        self.workspace.write_job_result(job["id"], {"status": "succeeded"})
        job_run_ms = (time.monotonic() - job_started) * 1000
        return not commit.created, {"submit": submit_ms, "job_run": job_run_ms}

    async def close(self) -> dict[str, Any]:
        drained = await self.mcp.close_queries(timeout=3.0)
        self.recorder.disable()
        sink_closed = self.sink.close(timeout=3.0)
        return {
            "queries_drained": drained,
            "event_sink_closed": sink_closed,
            "event_sink": asdict(self.sink.stats),
            "events_written_by_fake_sink": self.event_counter.count,
            "event_actor_count": len(self.event_counter.actors),
            "recorder_callback_failures": self.recorder.failure_count,
        }


def _rss_bytes(usage: resource.struct_rusage) -> int:
    # Darwin reports bytes; Linux reports KiB.
    return int(usage.ru_maxrss * (1024 if sys.platform.startswith("linux") else 1))


def _resources() -> dict[str, float | int]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "rss_max_bytes": _rss_bytes(usage),
        "cpu_user_seconds": usage.ru_utime,
        "cpu_system_seconds": usage.ru_stime,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
    return round(ordered[rank], 3)


def _metric_summary(
    *,
    elapsed: float,
    latencies: list[float],
    errors: int,
    timeouts: int,
    busy: int,
    actor_isolation_failures: int,
    ingest_count: int,
    duplicate_count: int,
    resources_before: dict[str, float | int],
    resources_after: dict[str, float | int],
    ingest_stage_samples: dict[str, list[float]],
) -> dict[str, Any]:
    count = len(latencies)
    return {
        "count": count,
        "elapsed_seconds": round(elapsed, 6),
        "throughput_per_second": round(count / elapsed, 3) if elapsed else None,
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        "errors": errors,
        "timeouts": timeouts,
        "busy": busy,
        "actor_isolation_failures": actor_isolation_failures,
        "ingest_count": ingest_count,
        "duplicate_count": duplicate_count,
        "ingest_stage_ms": {
            name: {
                "count": len(values),
                "total": round(sum(values), 3),
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "p99": _percentile(values, 0.99),
            }
            for name, values in ingest_stage_samples.items()
        },
        "resources": {
            "before": resources_before,
            "after": resources_after,
            "cpu_user_seconds_delta": round(
                float(resources_after["cpu_user_seconds"])
                - float(resources_before["cpu_user_seconds"]),
                6,
            ),
            "cpu_system_seconds_delta": round(
                float(resources_after["cpu_system_seconds"])
                - float(resources_before["cpu_system_seconds"]),
                6,
            ),
            "rss_max_bytes_delta": max(
                0,
                int(resources_after["rss_max_bytes"])
                - int(resources_before["rss_max_bytes"]),
            ),
        },
    }


async def _warmup(environment: SyntheticEnvironment, requests: int) -> None:
    for sequence in range(requests):
        client = environment.clients[sequence % len(environment.clients)]
        await environment.query(client, sequence)


async def run_phase(
    root: Path,
    *,
    clients: int,
    mixture: str,
    duration_seconds: float,
    rate_per_second: float,
    warmup_requests: int = MIN_WARMUP,
    events_enabled: bool = True,
) -> dict[str, Any]:
    if mixture not in MIXTURES:
        raise ValueError(f"mixture must be one of {MIXTURES}")
    if clients < 1 or duration_seconds <= 0 or rate_per_second <= 0:
        raise ValueError("clients, duration, and rate must be positive")
    if warmup_requests < MIN_WARMUP:
        raise ValueError(f"warmup_requests must be at least {MIN_WARMUP}")
    environment = SyntheticEnvironment(
        root, client_count=clients, events_enabled=events_enabled
    )
    old_multiuser = os.environ.get("SB_MULTIUSER")
    os.environ["SB_MULTIUSER"] = "1"
    latencies: list[float] = []
    errors = timeouts = busy = isolation_failures = 0
    ingest_count = duplicate_count = 0
    lock = asyncio.Lock()
    ingest_lock = asyncio.Lock()
    ingest_stage_samples: dict[str, list[float]] = {
        "submit": [],
        "queue_wait": [],
        "job_run": [],
    }
    try:
        await _warmup(environment, warmup_requests)
        resources_before = _resources()
        started = time.monotonic()
        deadline = started + duration_seconds
        per_client_interval = clients / rate_per_second

        async def worker(client: Client) -> None:
            nonlocal errors, timeouts, busy, isolation_failures
            nonlocal ingest_count, duplicate_count
            sequence = client.index
            target = started + (client.index / clients) * per_client_interval
            while target < deadline:
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                if time.monotonic() >= deadline:
                    break
                isolated = True
                duplicate = False
                did_ingest = mixture == "query+ingest" and sequence % INGEST_EVERY == 0
                query_started = time.monotonic()
                try:
                    response, isolated = await environment.query(client, sequence)
                    lowered = response.lower()
                    if "deadline exceeded" in lowered:
                        timeouts += 1
                    elif "service busy" in lowered:
                        busy += 1
                except TimeoutError:
                    timeouts += 1
                    errors += 1
                except Exception:
                    errors += 1
                latency = (time.monotonic() - query_started) * 1000
                async with lock:
                    latencies.append(latency)
                    isolation_failures += int(not isolated)
                if did_ingest:
                    wait_started = time.monotonic()
                    async with ingest_lock:
                        queue_wait_ms = (time.monotonic() - wait_started) * 1000
                        token = set_identity(client.identity)
                        try:
                            duplicate, ingest_timings = await asyncio.to_thread(
                                environment.ingest, client, sequence
                            )
                        except Exception:
                            errors += 1
                            ingest_timings = None
                        finally:
                            _current.reset(token)
                    async with lock:
                        ingest_count += 1
                        duplicate_count += int(duplicate)
                        ingest_stage_samples["queue_wait"].append(queue_wait_ms)
                        if ingest_timings is not None:
                            ingest_stage_samples["submit"].append(
                                ingest_timings["submit"]
                            )
                            ingest_stage_samples["job_run"].append(
                                ingest_timings["job_run"]
                            )
                sequence += clients
                target += per_client_interval
                # Do not replay missed slots as a saturation burst.  A slow
                # operation advances to the next future deadline instead.
                now = time.monotonic()
                if target < now:
                    missed = int((now - target) // per_client_interval) + 1
                    sequence += clients * missed
                    target += per_client_interval * missed

        await asyncio.gather(*(worker(client) for client in environment.clients))
        elapsed = time.monotonic() - started
        resources_after = _resources()
    finally:
        lifecycle = await environment.close()
        if old_multiuser is None:
            os.environ.pop("SB_MULTIUSER", None)
        else:
            os.environ["SB_MULTIUSER"] = old_multiuser
    metrics = _metric_summary(
        elapsed=elapsed,
        latencies=latencies,
        errors=errors,
        timeouts=timeouts,
        busy=busy,
        actor_isolation_failures=isolation_failures,
        ingest_count=ingest_count,
        duplicate_count=duplicate_count,
        resources_before=resources_before,
        resources_after=resources_after,
        ingest_stage_samples=ingest_stage_samples,
    )
    sink = lifecycle["event_sink"]
    metrics.update(
        {
            "clients": clients,
            "mixture": mixture,
            "target_rate_per_second": rate_per_second,
            "pacing": "fixed_rate_monotonic_deadline",
            "warmup_requests": warmup_requests,
            "ingest_every_queries": INGEST_EVERY,
            "queue_limits": {"capacity": 50, "per_owner": 5},
            "telemetry_enabled": events_enabled,
            "event_loss": sink["failed"]
            + sink["overflow"]
            + sink["rejected"]
            + sink["dropped"],
            "lifecycle": lifecycle,
        }
    )
    return metrics


async def run_event_comparison(
    root: Path,
    *,
    samples: int = MIN_EVENT_SAMPLES,
    warmup_requests: int = MIN_WARMUP,
) -> dict[str, Any]:
    if samples < MIN_EVENT_SAMPLES:
        raise ValueError(f"event samples must be at least {MIN_EVENT_SAMPLES}")
    if warmup_requests < MIN_WARMUP:
        raise ValueError(f"warmup_requests must be at least {MIN_WARMUP}")
    old_multiuser = os.environ.get("SB_MULTIUSER")
    os.environ["SB_MULTIUSER"] = "1"
    results = {}
    try:
        for enabled in (False, True):
            environment = SyntheticEnvironment(
                root / ("on" if enabled else "off"),
                client_count=1,
                events_enabled=enabled,
            )
            try:
                await _warmup(environment, warmup_requests)
                latencies = []
                started = time.monotonic()
                for sequence in range(samples):
                    call_started = time.monotonic()
                    await environment.query(environment.clients[0], sequence)
                    latencies.append((time.monotonic() - call_started) * 1000)
                elapsed = time.monotonic() - started
            finally:
                lifecycle = await environment.close()
            sink = lifecycle["event_sink"]
            results["on" if enabled else "off"] = {
                "samples": samples,
                "warmup_requests": warmup_requests,
                "throughput_per_second": round(samples / elapsed, 3),
                "latency_ms": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "p99": _percentile(latencies, 0.99),
                },
                "event_loss": sink["failed"]
                + sink["overflow"]
                + sink["rejected"]
                + sink["dropped"],
                "lifecycle": lifecycle,
            }
    finally:
        if old_multiuser is None:
            os.environ.pop("SB_MULTIUSER", None)
        else:
            os.environ["SB_MULTIUSER"] = old_multiuser
    on = results["on"]["latency_ms"]["p50"]
    off = results["off"]["latency_ms"]["p50"]
    results["p50_overhead_ms"] = round(on - off, 3)
    on_p95 = results["on"]["latency_ms"]["p95"]
    off_p95 = results["off"]["latency_ms"]["p95"]
    results["p95_overhead_ms"] = round(on_p95 - off_p95, 3)
    results["p95_overhead_target_ms"] = 20
    results["p95_overhead_target_met"] = results["p95_overhead_ms"] < 20
    results["order"] = ["off", "on"]
    return results


async def run_acceptance(
    *,
    duration_seconds: float = DEFAULT_DURATION_SECONDS,
    clients: tuple[int, ...] = DEFAULT_CLIENTS,
    mixtures: tuple[str, ...] = MIXTURES,
    rate_per_second: float = 20.0,
    warmup_requests: int = MIN_WARMUP,
    event_samples: int = MIN_EVENT_SAMPLES,
    mode: str = "matrix",
    mixed_duration_seconds: float = DEFAULT_MIXED_DURATION_SECONDS,
    base_root: Path | None = None,
) -> dict[str, Any]:
    owned_tmp = tempfile.TemporaryDirectory(prefix="sb-multiuser-load-") if base_root is None else None
    root = Path(owned_tmp.name if owned_tmp else base_root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        comparison = await run_event_comparison(
            root / "event-comparison",
            samples=event_samples,
            warmup_requests=warmup_requests,
        )
        phases = []
        specifications = (
            [(20, "query+ingest", mixed_duration_seconds)]
            if mode == "mixed30"
            else [
                (client_count, mixture, duration_seconds)
                for client_count in clients
                for mixture in mixtures
            ]
        )
        for index, (client_count, mixture, duration) in enumerate(specifications):
            phases.append(
                await run_phase(
                    root / f"phase-{index:02d}-{client_count}-{mixture.replace('+', '-')}",
                    clients=client_count,
                    mixture=mixture,
                    duration_seconds=duration,
                    rate_per_second=rate_per_second,
                    warmup_requests=warmup_requests,
                )
            )
        return {
            "schema": "second-brain.synthetic-multiuser-load.v1",
            "generated_at_epoch_seconds": time.time(),
            "mode": mode,
            "execution": "sequential phases with independent temporary roots",
            "synthetic_dependencies": {
                "retrieval": "deterministic local identity echo",
                "resolver": "fixed metadata stub only; no source resolution",
                "article_index": "local callback returning true",
                "real_components": [
                    "ObservedFastMCP registered call_tool wrapper",
                    "QueryDispatcher",
                    "BoundedEventSink",
                    "lcdda DurableQueue and HarvestWorkspace",
                    "commit_shared_article",
                ],
            },
            "not_measured": [
                "PostgreSQL latency, locks, RLS, connections, or storage metrics",
                "production ASGI/auth middleware and HTTP transport",
                "network retrieval, redirects, document conversion, or model calls",
                "production filesystem, vault, scheduler, worker subprocess, or NAS behavior",
            ],
            "telemetry_comparison": comparison,
            "phases": phases,
        }
    finally:
        if owned_tmp is not None:
            owned_tmp.cleanup()


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=DEFAULT_DURATION_SECONDS)
    parser.add_argument("--clients", type=int, nargs="+", default=list(DEFAULT_CLIENTS))
    parser.add_argument("--mixtures", nargs="+", choices=MIXTURES, default=list(MIXTURES))
    parser.add_argument("--rate-per-second", type=float, default=20.0)
    parser.add_argument("--warmup-requests", type=int, default=MIN_WARMUP)
    parser.add_argument("--event-samples", type=int, default=MIN_EVENT_SAMPLES)
    parser.add_argument("--mode", choices=("matrix", "mixed30"), default="matrix")
    parser.add_argument(
        "--mixed-duration-seconds", type=float, default=DEFAULT_MIXED_DURATION_SECONDS
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "docs/plans/load-results.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report = asyncio.run(
        run_acceptance(
            duration_seconds=arguments.duration_seconds,
            clients=tuple(arguments.clients),
            mixtures=tuple(arguments.mixtures),
            rate_per_second=arguments.rate_per_second,
            warmup_requests=arguments.warmup_requests,
            event_samples=arguments.event_samples,
            mode=arguments.mode,
            mixed_duration_seconds=arguments.mixed_duration_seconds,
        )
    )
    write_report(report, arguments.output)
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
