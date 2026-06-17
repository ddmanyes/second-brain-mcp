"""Per-request identity propagated via contextvar.

ASGI middleware (auth.py) sets the current identity after key validation.
FastMCP tool handlers read it with get_current_identity().

FastMCP calls sync tools directly in the same async task (no run_in_executor),
so contextvars propagate correctly.  See MULTIUSER_PLAN.md §P0 spike S1.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import sys
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


# ---------------------------------------------------------------------------
# Write-permission guard (MULTIUSER_PLAN P2)
# ---------------------------------------------------------------------------

_RBAC_ENFORCE_ENV = "SB_RBAC_ENFORCE"
_DENIED_MSG = "[RBAC] read-only access denied: '{}' requires writer or admin role"


def check_write_permission(tool_name: str) -> str | None:
    """Return None if write is allowed, or an error string to return to the caller.

    Enforcement is controlled by SB_RBAC_ENFORCE:
      - Not set / "audit" (default): log the attempt to stderr but allow through.
      - "1" / "true" / "yes" / "enforce": block and return error string.

    None identity (unauthenticated stdio/dev) always passes — auth is opt-in.
    """
    identity = get_current_identity()
    if identity is None or identity.can_write():
        return None

    # reader tried a write tool
    user = identity.user_id
    enforce = os.environ.get(_RBAC_ENFORCE_ENV, "").lower() in (
        "1",
        "true",
        "yes",
        "enforce",
    )
    if enforce:
        print(
            f"[RBAC DENY] {user} blocked from write tool '{tool_name}'",
            file=sys.stderr,
        )
        return _DENIED_MSG.format(tool_name)

    print(
        f"[RBAC AUDIT] {user} (reader) used write tool '{tool_name}' — "
        "set SB_RBAC_ENFORCE=1 to block",
        file=sys.stderr,
    )
    return None
