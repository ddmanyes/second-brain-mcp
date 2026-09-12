"""Process-local event delivery health, without event payloads or external alerts."""
from __future__ import annotations

import anyio


class DeliveryHealth:
    def __init__(self):
        self._last_loss = 0
        self._degraded = False
        self._clean = 0
        self._last_written = 0
        self._recovery_written = False

    def observe(self, stats):
        lost = stats.failed + stats.overflow + stats.rejected + stats.dropped
        progress = stats.written > self._last_written
        self._last_written = stats.written
        changed = lost > self._last_loss
        self._last_loss = lost
        if changed:
            self._recovery_written = False
            self._clean = 0
            if not self._degraded:
                self._degraded = True
                return 'query_event_delivery_degraded'
        elif self._degraded and stats.pending == 0:
            self._recovery_written = self._recovery_written or progress
            if not self._recovery_written:
                return None
            self._clean += 1
            if self._clean >= 3:
                self._degraded = False
                self._clean = 0
                return 'query_event_delivery_recovered'
        else:
            self._clean = 0
        return None


async def watch_delivery(sink, emit, *, interval=10.0):
    health = DeliveryHealth()
    while True:
        await anyio.sleep(interval)
        signal = health.observe(sink.stats)
        if signal is not None:
            emit(signal)
