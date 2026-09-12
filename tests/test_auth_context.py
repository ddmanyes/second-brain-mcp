"""Focused contract tests for the authenticated caller-context MCP tool."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp.exceptions import ToolError

from mcp_second_brain import auth, server
from mcp_second_brain.identity import Identity, KeyState, _current, set_identity

USER_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _call_auth_context():
    return asyncio.run(server.mcp.call_tool("auth_context", {}))


def test_auth_context_is_registered_read_only_with_no_caller_arguments():
    tools = asyncio.run(server.mcp.list_tools())
    tool = next(tool for tool in tools if tool.name == "auth_context")

    assert tool.inputSchema["properties"] == {}
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.outputSchema is not None

    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("auth_context", {"user_id": USER_UUID}))


@pytest.mark.parametrize("role", ["reader", "member", "writer", "admin"])
def test_auth_context_returns_canonical_user_uuid_and_current_policy(role, monkeypatch):
    monkeypatch.setenv("SB_RBAC_ENFORCE", "yes")
    token = set_identity(
        Identity(user_id="member-label", role=role, user_uuid=USER_UUID)
    )
    try:
        content, result = _call_auth_context()
    finally:
        _current.reset(token)

    expected = {"user_id": USER_UUID, "role": role, "rbac_enforced": True}
    assert result == expected
    assert USER_UUID in content[0].text


@pytest.mark.parametrize(
    ("identity", "message"),
    [
        (None, "AUTH_IDENTITY_REQUIRED"),
        (
            Identity(user_id="env:deadbeef", role="admin", user_uuid=USER_UUID),
            "AUTH_REGISTERED_IDENTITY_REQUIRED",
        ),
        (Identity(user_id="member-label", role="member"), "AUTH_USER_UUID_REQUIRED"),
        (
            Identity(user_id="member-label", role="member", user_uuid="not-a-uuid"),
            "AUTH_USER_UUID_INVALID",
        ),
        (
            Identity(
                user_id="member-label", role="member", user_uuid=USER_UUID.upper()
            ),
            "AUTH_USER_UUID_NONCANONICAL",
        ),
    ],
)
def test_auth_context_rejects_untrusted_or_noncanonical_identity(identity, message):
    token = _current.set(identity)
    try:
        with pytest.raises(ToolError, match=message):
            _call_auth_context()
    finally:
        _current.reset(token)


def test_auth_context_does_not_read_store(monkeypatch):
    class FailOnAccess:
        def __getattr__(self, name):
            raise AssertionError(f"store access is forbidden: {name}")

    monkeypatch.setattr(server, "_store", FailOnAccess())
    token = set_identity(
        Identity(user_id="member-label", role="member", user_uuid=USER_UUID)
    )
    try:
        _, result = _call_auth_context()
    finally:
        _current.reset(token)

    assert result["user_id"] == USER_UUID


def test_auth_context_over_streamable_http_uses_registry_identity(monkeypatch):
    monkeypatch.setenv("SB_RBAC_ENFORCE", "enforce")
    identity = Identity(user_id="member-label", role="member", user_uuid=USER_UUID)

    async def exercise():
        inner = server.mcp.streamable_http_app()
        app = auth.APIKeyMiddleware(
            inner,
            keys=set(),
            lookup_fn=lambda key: (
                identity if key == "registered-key" else KeyState.REVOKED
            ),
        )
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8000",
                headers={"X-API-Key": "registered-key"},
            ) as http_client, streamable_http_client(
                "http://127.0.0.1:8000/mcp", http_client=http_client
            ) as (read, write, _), ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("auth_context", {})
                return result.structuredContent

    assert asyncio.run(exercise()) == {
        "user_id": USER_UUID,
        "role": "member",
        "rbac_enforced": True,
    }
