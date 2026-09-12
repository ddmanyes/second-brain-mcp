"""Bounded offload of synchronous reads with truthful, bounded shutdown.

Timeout and task cancellation stop waiting for a result, but cannot stop the
underlying Python thread. Such work retains its global and per-actor slot until
the dependency really returns. Shutdown therefore closes admission first and
reports whether accepted work drained within its deadline; it never claims to
have killed a stuck dependency.
"""

from __future__ import annotations

import asyncio
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from .request_budget import request_budget, timing_snapshot
from .query_events import _MARKED_OUTCOME, mark_outcome


class QueryBusy(RuntimeError):
    pass


class QueryDispatcher:
    def __init__(self, *, capacity=4, per_actor=2, timeout=10.0):
        if type(capacity) is not int or not 1 <= capacity <= 32:
            raise ValueError("query capacity must be 1..32")
        if type(per_actor) is not int or not 1 <= per_actor <= capacity:
            raise ValueError("per-actor capacity must be 1..global capacity")
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("query timeout must be >0 and <=60 seconds")
        self.capacity, self.per_actor, self.timeout = capacity, per_actor, timeout
        self._lock = threading.Lock()
        self._counts = {}
        self._active = 0
        self._executor = None
        self._closed = False
        self._drain_waiters = set()
        self.stage_totals_ms = {}
        self.measured_calls = 0

    @property
    def active(self):
        with self._lock:
            return self._active

    async def run(self, actor, function, *args, **kwargs):
        context = copy_context()
        with self._lock:
            if (
                self._closed
                or self._active >= self.capacity
                or self._counts.get(actor, 0) >= self.per_actor
            ):
                raise QueryBusy("query capacity reached; retry later")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.capacity, thread_name_prefix="sb-query"
                )
            self._active += 1
            self._counts[actor] = self._counts.get(actor, 0) + 1
            executor = self._executor

            # Submit while admission and executor lifecycle share the same lock.
            # Once counted as accepted, shutdown cannot race ahead and reject it.
            def invoke():
                with request_budget(self.timeout):
                    try:
                        return function(*args, **kwargs)
                    finally:
                        timings = timing_snapshot()
                        with self._lock:
                            self.measured_calls += 1
                            for name, elapsed in timings.items():
                                self.stage_totals_ms[name] = self.stage_totals_ms.get(name, 0.0) + elapsed

            try:
                future = executor.submit(context.run, invoke)
            except BaseException:
                self._active -= 1
                self._counts[actor] -= 1
                if not self._counts[actor]:
                    del self._counts[actor]
                raise

        def release(_future=None):
            waiters = ()
            with self._lock:
                self._active -= 1
                self._counts[actor] -= 1
                if not self._counts[actor]:
                    del self._counts[actor]
                if self._active == 0:
                    waiters = tuple(self._drain_waiters)
                    self._drain_waiters.clear()
            for loop, waiter in waiters:
                try:
                    loop.call_soon_threadsafe(self._finish_drain_waiter, waiter)
                except RuntimeError:
                    # The waiting loop was already closed; no query state is lost.
                    pass

        future.add_done_callback(release)
        # Shield prevents timeout/cancellation from falsely freeing a running
        # thread. A stuck dependency consumes a bounded slot until it finishes.
        wrapped = asyncio.wrap_future(future)
        try:
            result = await asyncio.wait_for(asyncio.shield(wrapped), self.timeout)
        except BaseException:
            # Consume eventual exceptions after the caller has gone away.
            wrapped.add_done_callback(
                lambda completed: (
                    completed.exception() if not completed.cancelled() else None
                )
            )
            raise
        outcome = context.get(_MARKED_OUTCOME)
        if outcome is not None:
            mark_outcome(outcome)
        return result

    @staticmethod
    def _finish_drain_waiter(waiter):
        if not waiter.done():
            waiter.set_result(None)

    @staticmethod
    def _validate_drain_timeout(timeout):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("shutdown timeout must be finite and nonnegative")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("shutdown timeout must be finite and nonnegative")
        return float(timeout)

    async def drain(self, *, timeout=1.0):
        """Stop admission and wait at most ``timeout`` for accepted work.

        Returns True only when every accepted call has actually returned. False
        means at least one worker thread is still running; Python cannot safely
        terminate a thread blocked in a dependency. Calling drain again is safe.
        """
        timeout = self._validate_drain_timeout(timeout)
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        with self._lock:
            self._closed = True
            if self._active == 0:
                return True
            entry = (loop, waiter)
            self._drain_waiters.add(entry)
        try:
            await asyncio.wait_for(waiter, timeout)
            return True
        except TimeoutError:
            # Prefer current worker state over a timer/completion callback race.
            with self._lock:
                return self._active == 0
        finally:
            with self._lock:
                self._drain_waiters.discard(entry)
            if not waiter.done():
                waiter.cancel()

    async def close(self, *, timeout=1.0):
        """Drain within a bound, close the executor, and return drain status.

        Executor shutdown is non-waiting and does not cancel accepted work.
        Threads still blocked after the deadline retain their slots and continue
        until their dependency returns.
        """
        try:
            return await self.drain(timeout=timeout)
        finally:
            with self._lock:
                executor = self._executor
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=False)

    def stop_accepting(self):
        """Synchronously close admission without waiting for accepted work."""
        with self._lock:
            self._closed = True
