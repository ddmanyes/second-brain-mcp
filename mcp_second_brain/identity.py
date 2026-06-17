"""Per-request identity propagated via contextvar.

ASGI middleware (auth.py) sets the current identity after key validation.
FastMCP tool handlers read it with get_current_identity().

FastMCP calls sync tools directly in the same async task (no run_in_executor),
so contextvars propagate correctly.  See MULTIUSER_PLAN.md §P0 spike S1.
"""
from __future__ import annotations

import contextvars
import hashlib
from dataclasses import dataclass
from typing import Optional

VALID_ROLES = frozenset(("reader", "writer", "admin"))

_current: contextvars.ContextVar[Optional["Identity"]] = contextvars.ContextVar(
    "sb_identity", default=None
)


@dataclass(frozen=True)
class Identity:
    user_id: str
    role: str  # 'reader' | 'writer' | 'admin'

    def __post_init__(self) -> None:
        if self.role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}, got {self.role!r}")

    def can_write(self) -> bool:
        return self.role in ("writer", "admin")

    def is_admin(self) -> bool:
        return self.role == "admin"


def set_identity(identity: Identity) -> contextvars.Token:
    """Set identity for the current request. Call _current.reset(token) when done."""
    return _current.set(identity)


def get_current_identity() -> Optional[Identity]:
    """Return the identity for the current request, or None if not authenticated."""
    return _current.get()


def hash_key(raw_key: str) -> str:
    """SHA-256 hex digest of a raw API key — safe for DB storage."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
