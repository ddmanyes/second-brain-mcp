"""Opt-in, sanitized invocation telemetry, independent of storage and MCP.

Construct one recorder with a trusted identity getter and a *nonblocking*, bounded
sink (for example put_nowait on a bounded queue). Decorate public tool entrypoints;
nested decorated helpers produce no extra event. Never use client inputs to name
service/tool/operation, or parse exception messages to classify failures. A sink
must not perform synchronous network/DB I/O on the request path.

This module deliberately has no query text, paths, headers, results, or exception
text fields. Raw-query collection and persistence are separate deployment gates.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import wraps
from uuid import UUID, uuid4

_LOG = logging.getLogger(__name__)
_ACTIVE: ContextVar[bool] = ContextVar("query_event_active", default=False)
_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_REVISION = re.compile(r"[0-9a-f]{7,64}\Z")


class ActorRole(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"
    READER = "reader"
    WRITER = "writer"
    VIEWER = "viewer"
    ANALYST = "analyst"
    UNKNOWN = "unknown"


class EventStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ErrorCode(StrEnum):
    NONE = "none"
    INTERNAL = "internal"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"
    CLASSIFICATION_FAILED = "classification_failed"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class Actor:
    """Identity supplied exclusively from authenticated server context.

    Legacy env-key identities that are not UUIDs must map to a null UUID; do not
    copy their key prefix or arbitrary user_id into telemetry.
    """

    user_id: UUID | None = None
    role: ActorRole = ActorRole.UNKNOWN

    def __post_init__(self):
        if self.user_id is not None and type(self.user_id) is not UUID:
            raise TypeError("actor user_id must be UUID or None")
        if type(self.role) is not ActorRole:
            raise TypeError("actor role must be ActorRole")


@dataclass(frozen=True, slots=True)
class Outcome:
    """Explicit result classification, including tools returning denied/error values."""

    status: EventStatus = EventStatus.SUCCESS
    error_code: ErrorCode = ErrorCode.NONE
    result_count: int | None = None

    def __post_init__(self):
        if (
            type(self.status) is not EventStatus
            or type(self.error_code) is not ErrorCode
        ):
            raise TypeError("outcome requires enum values")
        if self.result_count is not None and (
            type(self.result_count) is not int
            or not 0 <= self.result_count <= 2**63 - 1
        ):
            raise ValueError("result_count must be a nonnegative bounded integer")
        required = {
            EventStatus.SUCCESS: ErrorCode.NONE,
            EventStatus.DENIED: ErrorCode.DENIED,
            EventStatus.CANCELLED: ErrorCode.CANCELLED,
        }
        if self.status in required and self.error_code != required[self.status]:
            raise ValueError("inconsistent outcome")
        if self.status == EventStatus.UNKNOWN and self.error_code not in {
            ErrorCode.CLASSIFICATION_FAILED,
            ErrorCode.UNCLASSIFIED,
        }:
            raise ValueError("unknown outcome requires a classification code")
        if self.status == EventStatus.ERROR and self.error_code not in {
            ErrorCode.INTERNAL,
            ErrorCode.TIMEOUT,
            ErrorCode.UNAVAILABLE,
            ErrorCode.INVALID_REQUEST,
        }:
            raise ValueError("error outcome requires an error code")


_MARKED_OUTCOME: ContextVar[Outcome | None] = ContextVar(
    "query_marked_outcome", default=None
)
_MARK_ALLOWED: ContextVar[bool] = ContextVar("query_mark_allowed", default=False)


def mark_outcome(outcome: Outcome) -> bool:
    """Mark the current outer tool's result at its source, without parsing strings.

    Returns False outside enabled instrumentation and inside nested decorated
    helpers. Exceptions always override marks. Marks are scoped to this context
    and cleared on invocation exit; child tasks cannot mutate their parent's mark.
    """
    if not _ACTIVE.get() or not _MARK_ALLOWED.get():
        return False
    if type(outcome) is not Outcome:
        raise TypeError("mark_outcome requires Outcome")
    _MARKED_OUTCOME.set(outcome)
    return True


@contextmanager
def _nested_invocation():
    token = _MARK_ALLOWED.set(False)
    try:
        yield
    finally:
        _MARK_ALLOWED.reset(token)


@dataclass(frozen=True, slots=True)
class QueryEvent:
    event_id: UUID
    request_id: UUID
    actor_id: UUID | None
    actor_role: ActorRole
    service: str
    tool: str
    operation: str
    started_at: datetime
    duration_ms: float
    status: EventStatus
    error_code: ErrorCode
    result_count: int | None
    revision: str | None
    retrieval_revision: str | None

    def to_payload(self) -> dict:
        """Fixed allowlist; no arbitrary extension attributes or raw data."""
        payload = asdict(self)
        for key in ("event_id", "request_id", "actor_id"):
            payload[key] = str(payload[key]) if payload[key] is not None else None
        payload["started_at"] = self.started_at.isoformat()
        for key in ("actor_role", "status", "error_code"):
            payload[key] = payload[key].value
        return payload


class QueryEventRecorder:
    """Disabled by default; injected callbacks are never called when disabled.

    Both synchronous and async tools use the same fast synchronous sink contract.
    Sink exceptions and classifier/getter failures never change the tool result;
    only a saturating counter and at most 16 fixed-message warnings are produced.
    """

    def __init__(
        self,
        *,
        service: str,
        actor_getter: Callable[[], Actor] | None = None,
        sink: Callable[[QueryEvent], None] | None = None,
        enabled: bool = False,
        revision: str | None = None,
        retrieval_revision: str | None = None,
    ):
        if not _LABEL.fullmatch(service):
            raise ValueError("invalid service label")
        if revision is not None and not _REVISION.fullmatch(revision):
            raise ValueError("revision must be a commit hash")
        if retrieval_revision is not None and not _REVISION.fullmatch(
            retrieval_revision
        ):
            raise ValueError("retrieval_revision must be a commit hash")
        if enabled and (actor_getter is None or sink is None):
            raise ValueError("enabled telemetry requires actor_getter and sink")
        if inspect.iscoroutinefunction(sink) or inspect.iscoroutinefunction(
            getattr(sink, "__call__", None)
        ):
            raise TypeError("sink must be synchronous and nonblocking")
        self.service = service
        self.revision = revision
        self.retrieval_revision = retrieval_revision
        self.enabled = enabled
        self._actor_getter = actor_getter
        self._sink = sink
        self._failures = 0
        self._failure_lock = threading.Lock()

    def configure_sink(self, sink, *, enabled: bool = True) -> None:
        """Attach a validated sink at startup before requests or worker activation.

        Does not start the sink. Runtime must start it after schema verification
        and call disable() before stopping admission during shutdown.
        """
        if self.enabled:
            raise RuntimeError("disable recorder before configuring sink")
        if (
            not callable(sink)
            or inspect.iscoroutinefunction(sink)
            or inspect.iscoroutinefunction(getattr(sink, "__call__", None))
        ):
            raise TypeError("sink must be synchronous and callable")
        if enabled and self._actor_getter is None:
            raise ValueError("enabled telemetry requires actor_getter")
        self._sink = sink
        self.enabled = enabled

    def disable(self) -> None:
        """Stop new event admission; in-flight invocations may still finish."""
        self.enabled = False

    @property
    def failure_count(self) -> int:
        with self._failure_lock:
            return self._failures

    def _failure(self):
        with self._failure_lock:
            if self._failures == 65535:
                return
            self._failures += 1
            report = self._failures & (self._failures - 1) == 0
        if report:
            try:
                _LOG.warning("Query telemetry callback failed; details suppressed")
            except Exception:  # noqa: BLE001, S110 -- logging must not break the tool
                pass

    def _actor(self) -> Actor:
        try:
            actor = self._actor_getter()
            if type(actor) is not Actor:
                raise TypeError("invalid actor")
            return actor
        except Exception:  # noqa: BLE001 -- isolated telemetry callback
            self._failure()
            return Actor()

    def _outcome(self, result, classifier) -> Outcome:
        if classifier is None:
            return Outcome(EventStatus.UNKNOWN, ErrorCode.UNCLASSIFIED)
        try:
            outcome = classifier(result)
            if type(outcome) is not Outcome:
                raise TypeError("invalid outcome")
            return outcome
        except Exception:  # noqa: BLE001 -- isolated telemetry callback
            self._failure()
            # A classifier failure proves neither success nor tool failure.
            return Outcome(EventStatus.UNKNOWN, ErrorCode.CLASSIFICATION_FAILED)

    def instrument(self, *, tool: str, operation: str, classify_result=None):
        """Decorate one logical invocation; labels are static server configuration."""
        if not _LABEL.fullmatch(tool) or not _LABEL.fullmatch(operation):
            raise ValueError("invalid tool or operation label")

        def decorate(function):
            def begin():
                token = _ACTIVE.set(True)
                mark_token = _MARKED_OUTCOME.set(None)
                allow_token = _MARK_ALLOWED.set(True)
                return (
                    token,
                    mark_token,
                    allow_token,
                    self._actor(),
                    uuid4(),
                    datetime.now(UTC),
                    time.monotonic(),
                )

            def finish(state, outcome):
                token, mark_token, allow_token, actor, request_id, started_at, start = (
                    state
                )
                try:
                    event = QueryEvent(
                        uuid4(),
                        request_id,
                        actor.user_id,
                        actor.role,
                        self.service,
                        tool,
                        operation,
                        started_at,
                        max(0.0, (time.monotonic() - start) * 1000),
                        outcome.status,
                        outcome.error_code,
                        outcome.result_count,
                        self.revision,
                        self.retrieval_revision,
                    )
                    result = self._sink(event)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise TypeError("sink returned an awaitable")
                except Exception:  # noqa: BLE001 -- isolated telemetry sink
                    self._failure()
                finally:
                    _MARK_ALLOWED.reset(allow_token)
                    _MARKED_OUTCOME.reset(mark_token)
                    _ACTIVE.reset(token)

            def failure_outcome(exc):
                if isinstance(
                    exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
                ):
                    return Outcome(EventStatus.CANCELLED, ErrorCode.CANCELLED)
                if isinstance(exc, PermissionError):
                    return Outcome(EventStatus.DENIED, ErrorCode.DENIED)
                if isinstance(exc, TimeoutError):
                    return Outcome(EventStatus.ERROR, ErrorCode.TIMEOUT)
                return Outcome(EventStatus.ERROR, ErrorCode.INTERNAL)

            if inspect.iscoroutinefunction(function):

                @wraps(function)
                async def async_wrapper(*args, **kwargs):
                    if _ACTIVE.get():
                        with _nested_invocation():
                            return await function(*args, **kwargs)
                    if not self.enabled:
                        return await function(*args, **kwargs)
                    state = begin()
                    outcome = Outcome(EventStatus.ERROR, ErrorCode.INTERNAL)
                    try:
                        result = await function(*args, **kwargs)
                        outcome = _MARKED_OUTCOME.get() or self._outcome(
                            result, classify_result
                        )
                        return result
                    except BaseException as exc:
                        outcome = failure_outcome(exc)
                        raise
                    finally:
                        finish(state, outcome)

                return async_wrapper

            @wraps(function)
            def sync_wrapper(*args, **kwargs):
                if _ACTIVE.get():
                    with _nested_invocation():
                        return function(*args, **kwargs)
                if not self.enabled:
                    return function(*args, **kwargs)
                state = begin()
                outcome = Outcome(EventStatus.ERROR, ErrorCode.INTERNAL)
                try:
                    result = function(*args, **kwargs)
                    outcome = _MARKED_OUTCOME.get() or self._outcome(
                        result, classify_result
                    )
                    return result
                except BaseException as exc:
                    outcome = failure_outcome(exc)
                    raise
                finally:
                    finish(state, outcome)

            return sync_wrapper

        return decorate
