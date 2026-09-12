import asyncio
from unittest.mock import Mock

from mcp_second_brain.auth import APIKeyMiddleware, maybe_add_api_key_auth


def test_lookup_outage_does_not_call_downstream_or_expose_secrets():
    downstream = Mock()
    lookup = Mock(side_effect=RuntimeError("dsn-password-canary"))
    app = APIKeyMiddleware(downstream, {"owner-key"}, lookup_fn=lookup)
    messages = []

    async def send(message):
        messages.append(message)

    asyncio.run(app({"type": "http", "path": "/mcp", "headers": [(b"x-api-key", b"owner-key")]}, None, send))
    assert messages[0]["status"] == 503
    assert "dsn-password-canary" not in str(messages)
    downstream.assert_not_called()


def test_required_auth_remains_installed_with_zero_keys(monkeypatch):
    monkeypatch.delenv("SB_API_KEY", raising=False)
    monkeypatch.delenv("SB_API_KEYS", raising=False)
    app = Mock()
    maybe_add_api_key_auth(app, required=True)
    app.add_middleware.assert_called_once()
