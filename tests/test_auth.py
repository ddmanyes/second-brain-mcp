"""Tests for the API-key auth middleware (mcp_second_brain.auth) + identity propagation."""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_second_brain import auth
from mcp_second_brain.identity import (
    Identity,
    KeyState,
    get_current_identity,
    hash_key,
)


def _build_app(capture: list | None = None) -> Starlette:
    async def ok(request):
        if capture is not None:
            capture.append(get_current_identity())
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/mcp", ok), Route("/", ok)])


def _client(
    monkeypatch,
    *,
    single="",
    multi="",
    lookup_fn=None,
    capture: list | None = None,
    db_key_count: int = 0,
) -> TestClient:
    monkeypatch.setenv(auth.SINGLE_KEY_ENV, single)
    monkeypatch.setenv(auth.MULTI_KEY_ENV, multi)
    app = _build_app(capture=capture)
    n = auth.maybe_add_api_key_auth(app, lookup_fn=lookup_fn, db_key_count=db_key_count)
    app.state.n_keys = n
    return TestClient(app)


# ---------------------------------------------------------------------------
# existing tests (unchanged behaviour)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# P1 — identity propagation
# ---------------------------------------------------------------------------

class TestIdentityEnvKeyFallback:
    """Env keys (no lookup_fn) → admin identity for back-compat."""

    def test_env_key_resolves_admin_identity(self, monkeypatch):
        captured: list = []
        client = _client(monkeypatch, single="myenvkey", capture=captured)
        client.get("/mcp", headers={"X-API-Key": "myenvkey"})
        assert len(captured) == 1
        identity = captured[0]
        assert identity is not None
        assert identity.role == "admin"
        assert identity.user_id.startswith("env:")

    def test_identity_not_set_on_rejected_request(self, monkeypatch):
        captured: list = []
        client = _client(monkeypatch, single="secret", capture=captured)
        client.get("/mcp", headers={"X-API-Key": "wrong"})
        # handler never ran → captured is empty
        assert captured == []

    def test_identity_not_set_when_auth_disabled(self, monkeypatch):
        captured: list = []
        client = _client(monkeypatch, single="", multi="", capture=captured)
        client.get("/mcp")
        assert len(captured) == 1
        assert captured[0] is None  # no middleware → identity never set


class TestIdentityLookupFn:
    """DB-backed lookup_fn takes precedence over env fallback."""

    def _make_lookup(self, mapping: dict[str, Identity]):
        """Return a lookup_fn backed by a dict: key → Identity (or None)."""
        def lookup(key: str) -> Identity | None:
            return mapping.get(key)
        return lookup

    def test_lookup_fn_provides_identity(self, monkeypatch):
        alice = Identity(user_id="alice", role="writer")
        lookup = self._make_lookup({"alice-key": alice})
        captured: list = []
        client = _client(monkeypatch, single="alice-key", lookup_fn=lookup, capture=captured)
        client.get("/mcp", headers={"X-API-Key": "alice-key"})
        assert captured[0] == alice

    def test_lookup_fn_unknown_key_falls_back_to_admin(self, monkeypatch):
        """lookup_fn returns None → back-compat admin identity from env."""
        lookup = self._make_lookup({})  # no DB entry
        captured: list = []
        client = _client(monkeypatch, single="orphan-key", lookup_fn=lookup, capture=captured)
        client.get("/mcp", headers={"X-API-Key": "orphan-key"})
        assert captured[0] is not None
        assert captured[0].role == "admin"

    def test_unknown_key_not_in_env_is_rejected(self, monkeypatch):
        lookup = self._make_lookup({})
        captured: list = []
        client = _client(monkeypatch, single="other-key", lookup_fn=lookup, capture=captured)
        r = client.get("/mcp", headers={"X-API-Key": "nobodys-key"})
        assert r.status_code == 401
        assert captured == []

    def test_reader_identity_propagated(self, monkeypatch):
        bob = Identity(user_id="bob", role="reader")
        lookup = self._make_lookup({"bob-key": bob})
        captured: list = []
        client = _client(monkeypatch, single="bob-key", lookup_fn=lookup, capture=captured)
        client.get("/mcp", headers={"X-API-Key": "bob-key"})
        assert captured[0].role == "reader"
        assert not captured[0].can_write()


class TestRegisteredKeyRegressions:
    """The two defects that made per-key identity unusable (MULTIUSER_PLAN R1/R2)."""

    def test_db_key_authenticates_without_being_in_env(self, monkeypatch):
        """R1: env keys must not act as an admission list for DB-registered keys.

        Previously _key_accepted() ran first against the env set, so a key issued
        via manage_api_key 401'd before lookup_fn was ever consulted.
        """
        alice = Identity(user_id="alice", role="reader")
        lookup = lambda k: alice if k == "alice-key" else None  # noqa: E731
        captured: list = []
        client = _client(
            monkeypatch, single="owner-env-key", lookup_fn=lookup,
            capture=captured, db_key_count=1,
        )
        r = client.get("/mcp", headers={"X-API-Key": "alice-key"})
        assert r.status_code == 200
        assert captured[0] == alice

    def test_revoked_key_is_denied_not_promoted_to_admin(self, monkeypatch):
        """R2: revoking a key must deny it, not hand it role='admin'.

        With the key also present in SB_API_KEYS (the only way R1 could be worked
        around), revocation used to fall through to the env-key admin fallback —
        turning 'remove this person's access' into 'make them an administrator'.
        """
        lookup = lambda k: KeyState.REVOKED if k == "alice-key" else None  # noqa: E731
        captured: list = []
        client = _client(
            monkeypatch, single="owner-env-key", multi="alice-key",
            lookup_fn=lookup, capture=captured,
        )
        r = client.get("/mcp", headers={"X-API-Key": "alice-key"})
        assert r.status_code == 401
        assert captured == []
        # the owner's own env key still works — revocation is per-key
        assert client.get("/mcp", headers={"X-API-Key": "owner-env-key"}).status_code == 200

    def test_auth_stays_on_when_only_db_keys_exist(self, monkeypatch):
        """Dropping the shared env key must not silently disable auth for everyone."""
        alice = Identity(user_id="alice", role="writer")
        lookup = lambda k: alice if k == "alice-key" else None  # noqa: E731
        client = _client(monkeypatch, single="", lookup_fn=lookup, db_key_count=1)
        assert client.app.state.n_keys == 1
        assert client.get("/mcp").status_code == 401
        assert client.get("/mcp", headers={"X-API-Key": "alice-key"}).status_code == 200


# ---------------------------------------------------------------------------
# P1 — Identity dataclass + hash_key
# ---------------------------------------------------------------------------

class TestIdentityHelpers:
    def test_valid_roles_accepted(self):
        for role in ("reader", "writer", "admin"):
            i = Identity(user_id="u", role=role)
            assert i.role == role

    def test_invalid_role_raises(self):
        with pytest.raises(ValueError, match="role must be"):
            Identity(user_id="u", role="superuser")

    def test_can_write(self):
        assert Identity("u", "writer").can_write()
        assert Identity("u", "admin").can_write()
        assert not Identity("u", "reader").can_write()

    def test_is_admin(self):
        assert Identity("u", "admin").is_admin()
        assert not Identity("u", "writer").is_admin()

    def test_hash_key_is_deterministic(self):
        assert hash_key("abc") == hash_key("abc")

    def test_hash_key_not_plaintext(self):
        raw = "supersecret"
        assert raw not in hash_key(raw)
        assert len(hash_key(raw)) == 64  # SHA-256 hex = 64 chars

class TestNonAsciiKey:
    """hmac.compare_digest 對非 ASCII str 拋 TypeError → 未擋會變 500 而非 401。

    實測（2026-07-30）：X-API-Key: 鑰匙 對 9100 回 HTTP 500，並在 log 留下
    traceback（每次嘗試一份，可被用來灌爆磁碟）。
    """

    def test_non_ascii_key_returns_401_not_500(self, monkeypatch):
        client = _client(monkeypatch, single="alpha")
        # 以 bytes 傳入：httpx 不接受非 ASCII str header，而真實 client（curl）
        # 送的就是 raw bytes，server 端再以 latin-1 解碼 —— 這才是 500 的重現路徑。
        resp = client.get("/mcp", headers={"X-API-Key": "鑰匙".encode()})
        assert resp.status_code == 401

    def test_non_ascii_bearer_returns_401(self, monkeypatch):
        client = _client(monkeypatch, single="alpha")
        resp = client.get("/mcp", headers={"Authorization": "Bearer 鑰匙".encode()})
        assert resp.status_code == 401

    def test_ascii_prefix_with_non_ascii_tail_rejected(self, monkeypatch):
        client = _client(monkeypatch, single="alpha")
        assert client.get("/mcp", headers={"X-API-Key": "alpha鑰".encode()}).status_code == 401

    def test_unit_level_no_raise(self):
        assert auth._key_accepted("鑰匙", {"alpha"}) is False

    def test_non_ascii_configured_key_does_not_raise(self):
        assert auth._key_accepted("alpha", {"金鑰"}) is False

    def test_valid_key_still_accepted(self, monkeypatch):
        client = _client(monkeypatch, single="alpha")
        assert client.get("/mcp", headers={"X-API-Key": "alpha"}).status_code == 200
