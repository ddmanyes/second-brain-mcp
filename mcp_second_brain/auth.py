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

  Resolution order (see _authenticate):
  1. lookup_fn(key) — DB-backed per-key identity (injected by server.py when a
     PostgresStore is available). A DB-registered key is accepted on its own; it
     does not also have to appear in the env vars.
  2. KeyState.REVOKED — rejected outright, never falls through to step 3.
  3. Env-key fallback — keys from SB_API_KEY / SB_API_KEYS get role 'admin' for
     back-compat with existing single-user deployments.

  Steps 1 and 2 exist because the original flow checked env keys *first* and only
  consulted the DB afterwards, which made per-key identities unusable (a registered
  key 401'd) and turned revocation into a promotion to admin.
"""
from __future__ import annotations

import hmac
import os
from typing import Callable, Optional, Union

from .identity import Identity, KeyState, set_identity

SINGLE_KEY_ENV = "SB_API_KEY"
MULTI_KEY_ENV = "SB_API_KEYS"
HEADER = b"x-api-key"
_UNAUTH_BODY = b'{"jsonrpc":"2.0","error":{"code":-32001,"message":"unauthorized: missing or invalid API key"},"id":null}'

# Type for the optional DB-backed key→identity lookup injected at startup.
# Returns an Identity, KeyState.REVOKED, or None when the key is unknown.
KeyLookupFn = Callable[[str], Union[Identity, KeyState, None]]


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
    # hmac.compare_digest raises TypeError on non-ASCII str, which would surface as a
    # 500 (plus a traceback per attempt) instead of a clean 401. No valid key contains
    # non-ASCII, so reject such input up front.
    if not provided.isascii():
        return False
    # constant-time compare against each valid key
    return any(hmac.compare_digest(provided, k) for k in valid if k.isascii())


def _env_admin_identity(key: str) -> Identity:
    """Synthesised admin identity for an env key with no DB row (back-compat)."""
    prefix = key[:8] if len(key) >= 8 else key
    return Identity(user_id=f"env:{prefix}", role="admin")


def _authenticate(
    provided: str | None, valid: set[str], lookup_fn: KeyLookupFn | None
) -> Identity | None:
    """Return the caller's Identity, or None to reject the request with 401.

    Authentication and identity resolution are one decision, not two. Splitting
    them meant the env-key set acted as an admission list that DB-registered keys
    could never pass, and that a revoked key — indistinguishable from an unknown
    one — landed on the env-key admin fallback.
    """
    if not provided:
        return None
    # hmac.compare_digest raises TypeError on non-ASCII str, which would surface as a
    # 500 (plus a traceback per attempt) instead of a clean 401. No valid key contains
    # non-ASCII, so reject such input up front.
    if not provided.isascii():
        return None

    if lookup_fn is not None:
        result = lookup_fn(provided)
        if isinstance(result, Identity):
            return result  # DB-registered key: authenticates on its own
        if result is KeyState.REVOKED:
            return None  # deny; must never reach the admin fallback below

    if _key_accepted(provided, valid):
        return _env_admin_identity(provided)
    return None


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
        exempt_paths: set[str] | None = None,
    ):
        self.app = app
        self.keys = keys
        self.lookup_fn = lookup_fn
        # Read-only HTTP routes that a browser opens directly (can't send X-API-Key).
        # Exempting them relies on the Tailscale boundary alone — the same fallback as
        # when no key is configured. Only use for non-mutating, non-sensitive routes.
        self.exempt_paths = exempt_paths or set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)  # Tailscale-only route; no identity bound
            return
        key = _provided_key(scope)
        identity = _authenticate(key, self.keys, self.lookup_fn)
        if identity is None:
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
        # key is valid — bind the resolved identity for this request
        token = set_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            from .identity import _current
            _current.reset(token)


def maybe_add_api_key_auth(
    app,
    lookup_fn: KeyLookupFn | None = None,
    exempt_paths: set[str] | None = None,
    db_key_count: int = 0,
) -> int:
    """Install API-key auth on the Starlette app if any key is configured.

    lookup_fn: optional DB-backed key→Identity callable (see MULTIUSER_PLAN P1).
    exempt_paths: HTTP paths that skip the key check (Tailscale-only; read-only routes
      a browser opens directly, e.g. '/graph').
    db_key_count: number of active DB-registered keys. Counted so that dropping the
      shared env key — the intended end state once everyone has their own key — turns
      auth *on* for those keys rather than silently disabling it for everyone.
    Returns the number of usable keys (0 = auth disabled, middleware not added).
    """
    keys = configured_keys()
    if not keys and db_key_count <= 0:
        return 0
    app.add_middleware(APIKeyMiddleware, keys=keys, lookup_fn=lookup_fn,
                       exempt_paths=exempt_paths)
    return len(keys) + db_key_count
