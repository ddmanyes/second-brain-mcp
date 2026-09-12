"""Bounded, storage-free evaluation of sanitized operational health samples."""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from .query_events import ErrorCode, QueryEvent

ALERT_KEYS = (
    "queue_oldest_seconds",
    "query_p95_ms",
    "query_timeout_rate",
    "worker_heartbeat_ok",
    "storage_ok",
    "event_loss_count",
    "backup_ok",
)
SNAPSHOT_KEYS = (
    "queue_oldest_seconds",
    "query_p95_ms",
    "query_timeout_rate",
    "worker_heartbeat_ok",
    "storage_ok",
    "event_loss_count",
    "event_written_count",
    "backup_ok",
)
_SNAPSHOT_KEY_SET = frozenset(SNAPSHOT_KEYS)
_BOOLEAN_KEYS = frozenset({"backup_ok", "storage_ok", "worker_heartbeat_ok"})
_TRIGGER_CODES = {key: f"{key}_active" for key in ALERT_KEYS}
_RECOVERY_CODES = {key: f"{key}_recovered" for key in ALERT_KEYS}
_INPUT_LIMIT = 16 * 1024
_MAX_SAMPLES = 100
_MAX_COUNTER = 2**63 - 1
_WINDOW_SECONDS = 300.0
_WINDOW_CAPACITY = 1000
_MIN_QUERY_SAMPLES = 100


@dataclass(frozen=True, slots=True)
class AlertEvaluation:
    """One evaluator step containing only fixed alert keys and states."""

    triggered: tuple[str, ...]
    recovered: tuple[str, ...]
    active: tuple[str, ...]
    unknown: tuple[str, ...]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(_TRIGGER_CODES[key] for key in self.triggered) + tuple(
            _RECOVERY_CODES[key] for key in self.recovered
        )

    def to_payload(self) -> dict[str, list[str]]:
        return {
            "active": list(self.active),
            "codes": list(self.codes),
            "unknown": list(self.unknown),
        }


@dataclass(slots=True)
class _AlertState:
    active: bool = False
    bad: int = 0
    good: int = 0


class OperationalAlertEvaluator:
    """Require three consecutive known samples to trigger or recover an alert.

    ``event_written_count`` is progress evidence for ``event_loss_count``. An
    unchanged loss counter is healthy only when the written counter advances;
    idle or reset counters are unknown and cannot recover an active alert.
    """

    def __init__(
        self,
        *,
        queue_oldest_seconds_threshold: float,
        query_p95_ms_threshold: float,
        query_timeout_rate_threshold: float,
    ):
        thresholds = (
            queue_oldest_seconds_threshold,
            query_p95_ms_threshold,
            query_timeout_rate_threshold,
        )
        if any(isinstance(value, bool) for value in thresholds):
            raise ValueError("invalid operational alert threshold")
        try:
            thresholds = tuple(float(value) for value in thresholds)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("invalid operational alert threshold") from None
        if not all(math.isfinite(value) for value in thresholds):
            raise ValueError("invalid operational alert threshold")
        (
            queue_oldest_seconds_threshold,
            query_p95_ms_threshold,
            query_timeout_rate_threshold,
        ) = thresholds
        if (
            queue_oldest_seconds_threshold <= 0
            or query_p95_ms_threshold <= 0
            or not 0 <= query_timeout_rate_threshold <= 1
        ):
            raise ValueError("invalid operational alert threshold")
        self._thresholds = {
            "queue_oldest_seconds": float(queue_oldest_seconds_threshold),
            "query_p95_ms": float(query_p95_ms_threshold),
            "query_timeout_rate": float(query_timeout_rate_threshold),
        }
        self._states = {key: _AlertState() for key in ALERT_KEYS}
        self._event_counters: tuple[int, int] | None = None

    @staticmethod
    def _number(value, *, rate: bool = False) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0 or (rate and number > 1):
            return None
        return number

    @staticmethod
    def _counter(value) -> int | None:
        if type(value) is not int or not 0 <= value <= _MAX_COUNTER:
            return None
        return value

    def _classify_event_loss(self, loss_value, written_value) -> bool | None:
        loss = self._counter(loss_value)
        written = self._counter(written_value)
        if loss is None or written is None:
            return None
        previous = self._event_counters
        self._event_counters = (loss, written)
        if previous is None or loss < previous[0] or written < previous[1]:
            return None
        if loss > previous[0]:
            return True
        if written > previous[1]:
            return False
        return None

    def _classify(self, key: str, value, written_value) -> bool | None:
        if key in _BOOLEAN_KEYS:
            return not value if type(value) is bool else None
        if key == "event_loss_count":
            return self._classify_event_loss(value, written_value)
        number = self._number(value, rate=key == "query_timeout_rate")
        if number is None:
            return None
        return number >= self._thresholds[key]

    def evaluate(self, snapshot: Mapping[str, object]) -> AlertEvaluation:
        """Evaluate only allowlisted fields; input values are never retained."""
        values = snapshot if isinstance(snapshot, Mapping) else {}
        triggered: list[str] = []
        recovered: list[str] = []
        unknown: list[str] = []
        try:
            written_value = values.get("event_written_count")
        except Exception:  # noqa: BLE001 -- untrusted mapping becomes unknown
            written_value = None
        for key in ALERT_KEYS:
            try:
                value = values.get(key)
            except Exception:  # noqa: BLE001 -- untrusted mapping becomes unknown
                value = None
            classification = self._classify(key, value, written_value)
            state = self._states[key]
            if classification is None:
                state.bad = 0
                state.good = 0
                unknown.append(key)
            elif classification:
                state.good = 0
                state.bad = min(3, state.bad + 1)
                if state.bad == 3 and not state.active:
                    state.active = True
                    triggered.append(key)
            else:
                state.bad = 0
                state.good = min(3, state.good + 1)
                if state.good == 3 and state.active:
                    state.active = False
                    recovered.append(key)
        active = tuple(key for key in ALERT_KEYS if self._states[key].active)
        return AlertEvaluation(
            tuple(triggered), tuple(recovered), active, tuple(unknown)
        )


class QueryWindow:
    """Keep at most 1,000 sanitized query/read timings received in the last 300s."""

    def __init__(self, *, clock=time.monotonic):
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._samples: deque[tuple[float, float, bool]] = deque(
            maxlen=_WINDOW_CAPACITY
        )
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def observe(self, event: QueryEvent) -> bool:
        """Accept one sanitized query/read event without retaining the event itself."""
        if type(event) is not QueryEvent:
            raise TypeError("query window requires QueryEvent")
        if event.operation not in {"query", "read"}:
            return False
        if isinstance(event.duration_ms, bool) or not isinstance(
            event.duration_ms, (int, float)
        ):
            return False
        try:
            duration = float(event.duration_ms)
        except (OverflowError, TypeError, ValueError):
            return False
        if not math.isfinite(duration) or duration < 0:
            return False
        with self._lock:
            try:
                now = float(self._clock())
            except (OverflowError, TypeError, ValueError):
                return False
            if not math.isfinite(now):
                return False
            self._prune(now)
            self._samples.append(
                (now, duration, event.error_code is ErrorCode.TIMEOUT)
            )
        return True

    def snapshot(self) -> dict[str, float | None]:
        with self._lock:
            try:
                now = float(self._clock())
            except (OverflowError, TypeError, ValueError):
                return {"query_p95_ms": None, "query_timeout_rate": None}
            if not math.isfinite(now):
                return {"query_p95_ms": None, "query_timeout_rate": None}
            self._prune(now)
            samples = tuple(self._samples)
        if len(samples) < _MIN_QUERY_SAMPLES:
            return {"query_p95_ms": None, "query_timeout_rate": None}
        durations = sorted(sample[1] for sample in samples)
        rank = math.ceil(0.95 * len(durations)) - 1
        return {
            "query_p95_ms": durations[rank],
            "query_timeout_rate": sum(sample[2] for sample in samples)
            / len(samples),
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._samples)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        del message
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


def _finite_threshold(value: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("invalid threshold") from None
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("invalid threshold")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument(
        "--queue-oldest-seconds-threshold", required=True, type=_finite_threshold
    )
    parser.add_argument("--query-p95-ms-threshold", required=True, type=_finite_threshold)
    parser.add_argument(
        "--query-timeout-rate-threshold", required=True, type=_finite_threshold
    )
    return parser


def _invalid_payload() -> dict[str, list[str]]:
    return {"active": [], "codes": ["invalid_input"], "unknown": list(ALERT_KEYS)}


def _read_samples(stream) -> list[Mapping[str, object]]:
    raw = stream.read(_INPUT_LIMIT + 1)
    if not isinstance(raw, str) or len(raw) > _INPUT_LIMIT:
        raise ValueError("invalid input")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or set(parsed) != {"samples"}:
        raise ValueError("invalid input")
    samples = parsed["samples"]
    if not isinstance(samples, list) or len(samples) > _MAX_SAMPLES:
        raise ValueError("invalid input")
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != _SNAPSHOT_KEY_SET:
            raise ValueError("invalid input")
    return samples


def main(argv=None, *, stdin=None, stdout=None, stderr=None) -> int:
    """Evaluate bounded stdin samples without connecting to storage or services."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(argv)
        alerts = OperationalAlertEvaluator(
            queue_oldest_seconds_threshold=args.queue_oldest_seconds_threshold,
            query_p95_ms_threshold=args.query_p95_ms_threshold,
            query_timeout_rate_threshold=args.query_timeout_rate_threshold,
        )
        samples = _read_samples(stdin)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps(_invalid_payload(), sort_keys=True), file=stdout)
        return 2

    codes: list[str] = []
    result = AlertEvaluation((), (), (), ALERT_KEYS)
    for sample in samples:
        result = alerts.evaluate(sample)
        codes.extend(result.codes)
    payload = result.to_payload()
    payload["codes"] = codes
    print(json.dumps(payload, sort_keys=True), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
