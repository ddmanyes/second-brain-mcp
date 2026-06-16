"""Tests for the API-key auth middleware (mcp_second_brain.auth)."""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_second_brain import auth


def _build_app() -> Starlette:
    async def ok(_request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/mcp", ok), Route("/", ok)])


def _client(monkeypatch, *, single="", multi="") -> TestClient:
    monkeypatch.setenv(auth.SINGLE_KEY_ENV, single)
    monkeypatch.setenv(auth.MULTI_KEY_ENV, multi)
    app = _build_app()
    n = auth.maybe_add_api_key_auth(app)
    app.state.n_keys = n
    return TestClient(app)


class TestConfiguredKeys:
    def test_no_env_means_no_keys(self, monkeypatch):
        monkeypatch.delenv(auth.SINGLE_KEY_ENV, raising=False)
        monkeypatch.delenv(auth.MULTI_KEY_ENV, raising=False)
        assert auth.configured_keys() == set()

    def test_single_and_multi_merge(self, monkeypatch):
        monkeypatch.setenv(auth.SINGLE_KEY_ENV, "alpha")
        monkeypatch.setenv(auth.MULTI_KEY_ENV, "beta, gamma ,")
        assert auth.configured_keys() == {"alpha", "beta", "gamma"}


class TestMiddleware:
    def test_disabled_when_no_key(self, monkeypatch):
        client = _client(monkeypatch, single="", multi="")
        assert client.app.state.n_keys == 0
        # no middleware installed → request passes without any key
        assert client.get("/mcp").status_code == 200

    def test_rejects_without_key(self, monkeypatch):
        client = _client(monkeypatch, single="secret")
        assert client.app.state.n_keys == 1
        r = client.get("/mcp")
        assert r.status_code == 401
        assert "unauthorized" in r.text

    def test_rejects_wrong_key(self, monkeypatch):
        client = _client(monkeypatch, single="secret")
        assert client.get("/mcp", headers={"X-API-Key": "nope"}).status_code == 401

    def test_accepts_x_api_key(self, monkeypatch):
        client = _client(monkeypatch, single="secret")
        assert client.get("/mcp", headers={"X-API-Key": "secret"}).status_code == 200

    def test_accepts_bearer(self, monkeypatch):
        client = _client(monkeypatch, single="secret")
        assert client.get("/mcp", headers={"Authorization": "Bearer secret"}).status_code == 200

    def test_accepts_any_of_multiple_keys(self, monkeypatch):
        client = _client(monkeypatch, multi="k1,k2,k3")
        assert client.app.state.n_keys == 3
        assert client.get("/mcp", headers={"X-API-Key": "k2"}).status_code == 200
        assert client.get("/mcp", headers={"X-API-Key": "k9"}).status_code == 401
