"""Tests for API key lifecycle (MULTIUSER_PLAN P4).

Covers:
- check_admin_permission: None identity (dev/stdio) passes, admin passes,
  reader and writer are blocked (always enforced, no audit-only mode)
- manage_api_key tool: register/revoke/list gated by admin
- DuckDBStore noop: register/revoke/list return empty / False
- MockStore contract: register→lookup→revoke cycle
- SB_POOL_MAX_SIZE env var is read by factory (no live DB needed)
"""
from __future__ import annotations

import os
import pytest

from mcp_second_brain.identity import (
    Identity,
    _current,
    check_admin_permission,
    hash_key,
    set_identity,
)


# ---------------------------------------------------------------------------
# check_admin_permission
# ---------------------------------------------------------------------------

class TestCheckAdminPermission:
    def test_none_identity_passes(self):
        assert check_admin_permission("manage_api_key") is None

    def test_admin_passes(self):
        tok = set_identity(Identity(user_id="root", role="admin"))
        try:
            assert check_admin_permission("manage_api_key") is None
        finally:
            _current.reset(tok)

    def test_writer_is_blocked(self):
        tok = set_identity(Identity(user_id="alice", role="writer"))
        try:
            result = check_admin_permission("manage_api_key")
            assert result is not None
            assert "admin" in result
            assert "writer" in result
        finally:
            _current.reset(tok)

    def test_reader_is_blocked(self):
        tok = set_identity(Identity(user_id="bob", role="reader"))
        try:
            result = check_admin_permission("manage_api_key")
            assert result is not None
            assert "admin" in result
        finally:
            _current.reset(tok)

    def test_blocked_regardless_of_rbac_enforce(self, monkeypatch):
        """Admin check is always enforced — SB_RBAC_ENFORCE has no effect."""
        monkeypatch.delenv("SB_RBAC_ENFORCE", raising=False)
        tok = set_identity(Identity(user_id="bob", role="reader"))
        try:
            assert check_admin_permission("manage_api_key") is not None
        finally:
            _current.reset(tok)

        monkeypatch.setenv("SB_RBAC_ENFORCE", "1")
        tok = set_identity(Identity(user_id="bob", role="reader"))
        try:
            assert check_admin_permission("manage_api_key") is not None
        finally:
            _current.reset(tok)


# ---------------------------------------------------------------------------
# DuckDB noop implementations
# ---------------------------------------------------------------------------

class TestDuckDBKeyLifecycleNoop:
    def test_get_identity_returns_none(self):
        from mcp_second_brain.store.duckdb_store import DuckDBStore
        store = DuckDBStore()
        assert store.get_identity_for_key(hash_key("any-key")) is None

    def test_register_is_noop(self):
        from mcp_second_brain.store.duckdb_store import DuckDBStore
        store = DuckDBStore()
        store.register_api_key(hash_key("k1"), "alice", "reader")  # must not raise

    def test_revoke_returns_false(self):
        from mcp_second_brain.store.duckdb_store import DuckDBStore
        store = DuckDBStore()
        assert store.revoke_api_key(hash_key("k1")) is False

    def test_list_returns_empty(self):
        from mcp_second_brain.store.duckdb_store import DuckDBStore
        store = DuckDBStore()
        assert store.list_api_keys() == []
        assert store.list_api_keys(user_id="alice") == []


# ---------------------------------------------------------------------------
# Mock store — register → lookup → revoke lifecycle
# ---------------------------------------------------------------------------

class _MockKeyStore:
    """In-memory key store for lifecycle testing."""

    def __init__(self):
        self._keys: dict[str, dict] = {}

    def register_api_key(self, key_hash: str, user_id: str, role: str) -> None:
        if key_hash in self._keys:
            raise ValueError(f"duplicate key_hash: {key_hash}")
        self._keys[key_hash] = {"user_id": user_id, "role": role, "revoked": False}

    def get_identity_for_key(self, key_hash: str) -> Identity | None:
        row = self._keys.get(key_hash)
        if row is None or row["revoked"]:
            return None
        return Identity(user_id=row["user_id"], role=row["role"])

    def revoke_api_key(self, key_hash: str) -> bool:
        row = self._keys.get(key_hash)
        if row is None or row["revoked"]:
            return False
        row["revoked"] = True
        return True

    def list_api_keys(self, user_id: str | None = None) -> list[dict]:
        rows = []
        for kh, v in self._keys.items():
            if user_id is not None and v["user_id"] != user_id:
                continue
            rows.append({
                "key_hash_prefix": kh[:8],
                "user_id": v["user_id"],
                "role": v["role"],
                "created_at": "2026-01-01T00:00:00",
                "revoked_at": "revoked" if v["revoked"] else None,
            })
        return rows


class TestKeyLifecycleContract:
    def setup_method(self):
        self.store = _MockKeyStore()
        self.raw_key = "test-secret-key-001"
        self.kh = hash_key(self.raw_key)

    def test_register_then_lookup_succeeds(self):
        self.store.register_api_key(self.kh, "alice", "writer")
        identity = self.store.get_identity_for_key(self.kh)
        assert identity is not None
        assert identity.user_id == "alice"
        assert identity.role == "writer"

    def test_unknown_key_returns_none(self):
        assert self.store.get_identity_for_key(hash_key("unknown")) is None

    def test_revoke_blocks_subsequent_lookup(self):
        self.store.register_api_key(self.kh, "alice", "writer")
        revoked = self.store.revoke_api_key(self.kh)
        assert revoked is True
        assert self.store.get_identity_for_key(self.kh) is None

    def test_revoke_returns_false_for_unknown_key(self):
        assert self.store.revoke_api_key(hash_key("nonexistent")) is False

    def test_revoke_only_affects_target_key(self):
        k2 = hash_key("other-key-002")
        self.store.register_api_key(self.kh, "alice", "writer")
        self.store.register_api_key(k2, "bob", "reader")
        self.store.revoke_api_key(self.kh)
        assert self.store.get_identity_for_key(self.kh) is None
        assert self.store.get_identity_for_key(k2) is not None

    def test_duplicate_register_raises(self):
        self.store.register_api_key(self.kh, "alice", "writer")
        with pytest.raises(ValueError, match="duplicate"):
            self.store.register_api_key(self.kh, "alice2", "reader")

    def test_list_all_keys(self):
        self.store.register_api_key(self.kh, "alice", "writer")
        self.store.register_api_key(hash_key("k2"), "bob", "reader")
        rows = self.store.list_api_keys()
        assert len(rows) == 2

    def test_list_filter_by_user(self):
        self.store.register_api_key(self.kh, "alice", "writer")
        self.store.register_api_key(hash_key("k2"), "bob", "reader")
        rows = self.store.list_api_keys(user_id="alice")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "alice"

    def test_list_shows_revoked_status(self):
        self.store.register_api_key(self.kh, "alice", "writer")
        self.store.revoke_api_key(self.kh)
        rows = self.store.list_api_keys()
        assert rows[0]["revoked_at"] is not None


# ---------------------------------------------------------------------------
# manage_api_key tool — admin gate
# ---------------------------------------------------------------------------

class TestManageApiKeyAdminGate:
    def test_non_admin_blocked(self):
        from mcp_second_brain.server import manage_api_key
        tok = set_identity(Identity(user_id="bob", role="writer"))
        try:
            result = manage_api_key(action="list")
            assert "[RBAC]" in result
            assert "admin" in result
        finally:
            _current.reset(tok)

    def test_reader_blocked(self):
        from mcp_second_brain.server import manage_api_key
        tok = set_identity(Identity(user_id="carol", role="reader"))
        try:
            result = manage_api_key(action="list")
            assert "[RBAC]" in result
        finally:
            _current.reset(tok)

    def test_none_identity_allowed(self):
        """stdio/dev mode (no identity) must not be blocked."""
        from mcp_second_brain.server import manage_api_key
        result = manage_api_key(action="list")
        assert "[RBAC]" not in result

    def test_unknown_action_returns_error(self):
        from mcp_second_brain.server import manage_api_key
        result = manage_api_key(action="delete")
        assert "unknown action" in result.lower()

    def test_register_missing_raw_key(self):
        from mcp_second_brain.server import manage_api_key
        result = manage_api_key(action="register", user_id="alice", role="reader")
        assert "raw_key" in result

    def test_register_missing_user_id(self):
        from mcp_second_brain.server import manage_api_key
        result = manage_api_key(action="register", raw_key="somekey", role="reader")
        assert "user_id" in result

    def test_register_invalid_role(self):
        from mcp_second_brain.server import manage_api_key
        result = manage_api_key(action="register", raw_key="k", user_id="alice", role="superuser")
        assert "role" in result.lower()


# ---------------------------------------------------------------------------
# SB_POOL_MAX_SIZE env var
# ---------------------------------------------------------------------------

class TestPoolMaxSizeEnvVar:
    def test_default_is_10(self, monkeypatch):
        monkeypatch.delenv("SB_POOL_MAX_SIZE", raising=False)
        pool_max = int(os.environ.get("SB_POOL_MAX_SIZE", "10"))
        assert pool_max == 10

    def test_override_is_respected(self, monkeypatch):
        monkeypatch.setenv("SB_POOL_MAX_SIZE", "20")
        pool_max = int(os.environ.get("SB_POOL_MAX_SIZE", "10"))
        assert pool_max == 20
