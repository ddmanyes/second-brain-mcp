"""Bounded local handoff to an injected event writer; no database dependency.

The application owns lifecycle: explicitly start only when telemetry is enabled,
then close within its shutdown budget. The writer runs on one daemon thread and
must configure its own connection/statement timeout. Python cannot interrupt a
blocked writer; flush/close return False at their deadlines instead of stalling
requests/shutdown. Undelivered events are best-effort and not crash-durable.
"""

from __future__ import annotations

import inspect
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from mcp_second_brain.query_events import QueryEvent

_MAX_COUNTER = 2**63 - 1


@dataclass(frozen=True, slots=True)
class SinkStats:
    accepted: int
    written: int
    failed: int
    overflow: int
    rejected: int
    dropped: int
    pending: int


class BoundedEventSink:
    """Nonblocking enqueue; capacity includes the event currently being written.

    No thread starts in __init__. The writer takes a sanitized QueryEvent and
    returns None. Do not pass an async writer. Failures are counted without any
    raw exception messages, event data, retries, or recursive logging.
    """

    def __init__(self, writer: Callable[[QueryEvent], None], *, capacity: int = 256):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if (
            not callable(writer)
            or inspect.iscoroutinefunction(writer)
            or inspect.iscoroutinefunction(getattr(writer, "__call__", None))
        ):
            raise TypeError("writer must be synchronous and callable")
        self._writer = writer
        self._capacity = capacity
        self._condition = threading.Condition()
        self._queue: deque[QueryEvent] = deque()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._inflight = 0
        self._counts = dict.fromkeys(
            ("accepted", "written", "failed", "overflow", "rejected", "dropped"), 0
        )

    def _increment(self, name: str, amount: int = 1) -> None:
        # Caller holds the condition lock; saturating counters use bounded space.
        self._counts[name] = min(_MAX_COUNTER, self._counts[name] + amount)

    def start(self) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("event sink is closed")
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._run, name="query-event-writer", daemon=True
            )
            thread.start()
            self._thread = thread

    def __call__(self, event: QueryEvent) -> None:
        if type(event) is not QueryEvent:
            raise TypeError("sink requires QueryEvent")
        with self._condition:
            if self._closed or self._thread is None or not self._thread.is_alive():
                self._increment("rejected")
            elif len(self._queue) + self._inflight >= self._capacity:
                self._increment("overflow")
            else:
                self._queue.append(event)
                self._increment("accepted")
                self._condition.notify_all()

    @property
    def stats(self) -> SinkStats:
        with self._condition:
            return SinkStats(**self._counts, pending=len(self._queue) + self._inflight)

    @staticmethod
    def _deadline(timeout: float) -> float:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        return time.monotonic() + timeout

    def flush(self, *, timeout: float = 1.0) -> bool:
        """Wait at most timeout for current work; True does not imply all writes succeeded.

        Admission stays open; producers should be quiescent for a stable flush.
        Inspect stats.failed/overflow/rejected/dropped for delivery loss.
        """
        deadline = self._deadline(timeout)
        with self._condition:
            while self._queue or self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, *, timeout: float = 1.0, flush: bool = True) -> bool:
        """Stop admission and wait at most timeout; False means writer still running.

        flush=False discards waiting events but cannot cancel an in-flight write.
        Calling close again is safe, including after a previous deadline expired.
        """
        deadline = self._deadline(timeout)
        with self._condition:
            self._closed = True
            if not flush:
                self._increment("dropped", len(self._queue))
                self._queue.clear()
            thread = self._thread
            self._condition.notify_all()
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(max(0.0, deadline - time.monotonic()))
        return not thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    if self._closed:
                        return
                    self._condition.wait()
                event = self._queue.popleft()
                self._inflight = 1
            succeeded = False
            try:
                result = self._writer(event)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError("writer returned an awaitable")
                succeeded = True
            except Exception:  # noqa: BLE001 -- writer failures cannot affect requests
                succeeded = False
            finally:
                with self._condition:
                    self._increment("written" if succeeded else "failed")
                    self._inflight = 0
                    self._condition.notify_all()
