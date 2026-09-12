"""Last-resort process exit for an explicitly owned server after failed drain.

This is opt-in at service composition, never installed by reusable middleware.
It runs only after shutdown has started and a bounded drain has failed. It does
not kill other processes or claim that an unfinished operation rolled back.
"""
from __future__ import annotations

import os
import threading


def arm_shutdown_exit(*, seconds=5.0):
    if not 0 < seconds <= 30:
        raise ValueError('shutdown exit deadline must be at most 30 seconds')
    timer = threading.Timer(seconds, os._exit, args=(1,))
    timer.name = 'sb-shutdown-deadline'
    timer.daemon = True
    timer.start()
