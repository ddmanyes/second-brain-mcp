"""Request-scoped deadline, bounded stage timings, and embedding memoization.

The state is carried by a ContextVar, so callers may explicitly propagate it to
worker threads with ``contextvars.copy_context()``.  It contains no reporting or
logging path for query text; snapshots expose only fixed stage labels and times.
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

REQUEST_TIMEOUT_MESSAGE = "request deadline exceeded"
ALLOWED_STAGES = frozenset(
    {
        "pool_wait",
        "db",
        "query_embedding",
        "rerank",
        "file_read",
        "queue_wait",
        "job_run",
    }
)
_QUERY = "query"
_VALUE = "value"


@dataclass(slots=True)
class _RequestState:
    deadline: float
    embedding_memo: dict[str, Any]
    timings: dict[str, float]
    lock: threading.RLock


_CURRENT: ContextVar[_RequestState | None] = ContextVar(
    "second_brain_request_budget", default=None
)


def _duration(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite and nonnegative")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return result


@contextmanager
def request_budget(seconds: float):
    """Install a request deadline; nested scopes may only shorten it.

    Nested scopes share the request's memo and timings, then restore the outer
    deadline on exit.  The outermost exit discards all request-local state.
    """

    duration = _duration(seconds, "request budget")
    current = _CURRENT.get()
    deadline = time.monotonic() + duration
    if current is None:
        state = _RequestState(deadline, {}, {}, threading.RLock())
    else:
        state = _RequestState(
            min(current.deadline, deadline),
            current.embedding_memo,
            current.timings,
            current.lock,
        )
    token = _CURRENT.set(state)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def remaining_timeout(default: float) -> float:
    """Return a timeout capped by the current request's remaining budget."""

    fallback = _duration(default, "default timeout")
    state = _CURRENT.get()
    if state is None:
        return fallback
    remaining = state.deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(REQUEST_TIMEOUT_MESSAGE)
    return min(fallback, remaining)


@contextmanager
def stage(name: str):
    """Accumulate elapsed milliseconds under one fixed, non-sensitive label."""

    if not isinstance(name, str) or name not in ALLOWED_STAGES:
        raise ValueError("invalid request timing stage")
    state = _CURRENT.get()
    if state is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000)
        with state.lock:
            state.timings[name] = state.timings.get(name, 0.0) + elapsed_ms


def timing_snapshot() -> dict[str, float]:
    """Copy request stage timings; query text and memo values are never exposed."""

    state = _CURRENT.get()
    if state is None:
        return {}
    with state.lock:
        return dict(state.timings)


def request_embedding(query: str, provider):
    """Compute one query embedding per request and memoize even a ``None`` result.

    A distinct query still calls the provider but is not cached, preserving
    multi-query helper behavior while keeping the memo at one entry.  Outside a
    request budget there is no memoization because no request lifetime exists.
    """

    state = _CURRENT.get()
    if state is None:
        return provider(query)
    with state.lock:
        memo = state.embedding_memo
        if _QUERY in memo:
            if memo[_QUERY] != query:
                with stage("query_embedding"):
                    return provider(query)
            return memo[_VALUE]
        with stage("query_embedding"):
            value = provider(query)
        memo[_QUERY] = query
        memo[_VALUE] = value
        return value
