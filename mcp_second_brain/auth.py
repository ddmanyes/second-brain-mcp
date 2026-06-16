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
"""
from __future__ import annotations

import hmac
import os

SINGLE_KEY_ENV = "SB_API_KEY"
MULTI_KEY_ENV = "SB_API_KEYS"
HEADER = b"x-api-key"
_UNAUTH_BODY = b'{"jsonrpc":"2.0","error":{"code":-32001,"message":"unauthorized: missing or invalid API key"},"id":null}'


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


class APIKeyMiddleware:
    """ASGI middleware that rejects HTTP requests lacking a valid API key."""

    def __init__(self, app, keys: set[str]):
        self.app = app
        self.keys = keys

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not _key_accepted(_provided_key(scope), self.keys):
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
        await self.app(scope, receive, send)


def maybe_add_api_key_auth(app) -> int:
    """Install API-key auth on the Starlette app if any key env is set.

    Returns the number of configured keys (0 = auth disabled, middleware not added).
    """
    keys = configured_keys()
    if not keys:
        return 0
    app.add_middleware(APIKeyMiddleware, keys=keys)
    return len(keys)
