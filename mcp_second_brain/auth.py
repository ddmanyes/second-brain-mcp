"""API-key authentication for the central HTTP MCP server.

Defense-in-depth on top of Tailscale membership: even a device on the tailnet must
present a valid key. Auth is **opt-in** — if no key env is set the middleware is not
installed, so local stdio / dev setups are unaffected.

Enable by setting SB_API_KEY (single key) and/or SB_API_KEYS (comma-separated, for
per-device keys you can revoke individually). A request is accepted if it presents any
configured key via either header:

    X-API-Key: <key>
    Authorization: Bearer <key>

Pure-ASGI middleware (not BaseHTTPMiddleware) so it never buffers the streaming
MCP responses — it only inspects request headers, then hands off untouched.

Identity (MULTIUSER_PLAN P1):
  After accepting a key, the middleware resolves an Identity (user_id + role) and
  stores it in a per-request contextvar (identity.set_identity).  FastMCP tool
  handlers call identity.get_current_identity() to read it — works because FastMCP
  calls sync tools directly in the same async task (no run_in_executor).

  Resolution order:
  1. lookup_fn(key) — DB-backed per-key identity (injected by server.py when a
     PostgresStore is available).
  2. Env-key fallback — keys from SB_API_KEY / SB_API_KEYS get role 'admin' for
     back-compat with existing single-user deployments.
"""
from __future__ import annotations

import hmac
import os
from typing import Callable, Optional

from .identity import Identity, set_identity

SINGLE_KEY_ENV = "SB_API_KEY"
MULTI_KEY_ENV = "SB_API_KEYS"
HEADER = b"x-api-key"
_UNAUTH_BODY = b'{"jsonrpc":"2.0","error":{"code":-32001,"message":"unauthorized: missing or invalid API key"},"id":null}'

# Type for the optional DB-backed key→identity lookup injected at startup.
KeyLookupFn = Callable[[str], Optional[Identity]]


def configured_keys() -> set[str]:
    """Collect valid keys from SB_API_KEY and SB_API_KEYS (comma-separated)."""
    keys: set[str] = set()
    single = os.environ.get(SINGLE_KEY_ENV, "").strip()
    if single:
        keys.add(single)
    multi = os.environ.get(MULTI_KEY_ENV, "")
    keys.update(k.strip() for k in multi.split(",") if k.strip())
    return keys


def _provided_key(scope) -> str | None:
    headers = dict(scope.get("headers") or [])
    raw = headers.get(HEADER)
    if raw is not None:
        return raw.decode("latin-1")
    auth = headers.get(b"authorization")
    if auth:
        text = auth.decode("latin-1")
        if text.lower().startswith("bearer "):
            return text[7:].strip()
    return None


def _key_accepted(provided: str | None, valid: set[str]) -> bool:
    if not provided:
        return False
    # constant-time compare against each valid key
    return any(hmac.compare_digest(provided, k) for k in valid)


def _resolve_identity(key: str, lookup_fn: KeyLookupFn | None) -> Identity:
    """Return the Identity for an accepted key.

    Tries the DB lookup first; falls back to admin for env keys (back-compat).
    """
    if lookup_fn is not None:
        identity = lookup_fn(key)
        if identity is not None:
            return identity
    # Env-key fallback: synthesise an admin identity so existing single-user
    # setups keep working without a DB api_keys row.
    prefix = key[:8] if len(key) >= 8 else key
    return Identity(user_id=f"env:{prefix}", role="admin")


class APIKeyMiddleware:
    """ASGI middleware that rejects HTTP requests lacking a valid API key.

    After accepting a key it calls set_identity() so downstream FastMCP tools
    can read the caller's identity via get_current_identity().
    """

    def __init__(
        self,
        app,
        keys: set[str],
        lookup_fn: KeyLookupFn | None = None,
    ):
        self.app = app
        self.keys = keys
        self.lookup_fn = lookup_fn

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key = _provided_key(scope)
        if not _key_accepted(key, self.keys):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_UNAUTH_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _UNAUTH_BODY})
            return
        # key is valid — resolve and bind identity for this request
        assert key is not None  # narrowing: _key_accepted guarantees non-None
        identity = _resolve_identity(key, self.lookup_fn)
        token = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            from .identity import _current
            _current.reset(token)


def maybe_add_api_key_auth(
    app,
    lookup_fn: KeyLookupFn | None = None,
) -> int:
    """Install API-key auth on the Starlette app if any key env is set.

    lookup_fn: optional DB-backed key→Identity callable (see MULTIUSER_PLAN P1).
    Returns the number of configured keys (0 = auth disabled, middleware not added).
    """
    keys = configured_keys()
    if not keys:
        return 0
    app.add_middleware(APIKeyMiddleware, keys=keys, lookup_fn=lookup_fn)
    return len(keys)
