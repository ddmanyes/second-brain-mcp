"""manage_api_key's new user_uuid/expires_days parameters (lab-open plan,
2026-09-12) — register-path validation and store wiring, admin-gated.

Uses an in-memory fake store (no Postgres needed) so these run in every
environment; the real Postgres round-trip for user_uuid/expires_at is
covered by test_multiuser_rls.py's TestKeyRevocationAndExpiry.
"""

from __future__ import annotations

import contextlib

import pytest

from mcp_second_brain import server
from mcp_second_brain.identity import Identity, _current, set_identity


class _FakeStore:
    def __init__(self):
        self.calls: list[dict] = []

    def register_api_key(self, key_hash, user_id, role, *, user_uuid=None, expires_days=None):
        self.calls.append(
            {"key_hash": key_hash, "user_id": user_id, "role": role,
             "user_uuid": user_uuid, "expires_days": expires_days}
        )


@contextlib.contextmanager
def _as_admin():
    token = set_identity(Identity(user_id="admin", role="admin"))
    try:
        yield
    finally:
        _current.reset(token)


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(server, "_store", store)
    return store


VALID_UUID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


class TestRegisterMemberRole:
    def test_member_requires_user_uuid(self, fake_store):
        with _as_admin():
            result = server.manage_api_key(
                action="register", raw_key="k", user_id="alice", role="member"
            )
        assert "user_uuid" in result
        assert fake_store.calls == []

    def test_member_with_user_uuid_succeeds_and_is_passed_through(self, fake_store):
        with _as_admin():
            result = server.manage_api_key(
                action="register", raw_key="k", user_id="alice", role="member",
                user_uuid=VALID_UUID,
            )
        assert result.startswith("Registered")
        assert fake_store.calls[0]["role"] == "member"
        assert fake_store.calls[0]["user_uuid"] == VALID_UUID
        assert fake_store.calls[0]["expires_days"] is None

    def test_malformed_user_uuid_rejected(self, fake_store):
        with _as_admin():
            result = server.manage_api_key(
                action="register", raw_key="k", user_id="alice", role="member",
                user_uuid="not-a-uuid",
            )
        assert "uuid" in result.lower()
        assert fake_store.calls == []

    def test_non_canonical_uuid_form_rejected(self, fake_store):
        with _as_admin():
            result = server.manage_api_key(
                action="register", raw_key="k", user_id="alice", role="member",
                user_uuid=VALID_UUID.upper(),
            )
        assert "canonical" in result.lower()
        assert fake_store.calls == []

    def test_negative_expires_days_rejected(self, fake_store):
        with _as_admin():
            result = server.manage_api_key(
                action="register", raw_key="k", user_id="alice", role="reader",
                expires_days=-1,
            )
        assert "expires_days" in result
        assert fake_store.calls == []

    def test_expires_days_passed_through(self, fake_store):
        with _as_admin():
            server.manage_api_key(
                action="register", raw_key="k", user_id="alice", role="writer",
                expires_days=30,
            )
        assert fake_store.calls[0]["expires_days"] == 30

    def test_non_member_role_does_not_require_user_uuid(self, fake_store):
        with _as_admin():
            result = server.manage_api_key(
                action="register", raw_key="k", user_id="alice", role="reader"
            )
        assert result.startswith("Registered")
        assert fake_store.calls[0]["user_uuid"] is None

    def test_zero_expires_days_means_no_expiry(self, fake_store):
        with _as_admin():
            server.manage_api_key(
                action="register", raw_key="k", user_id="alice", role="reader",
                expires_days=0,
            )
        assert fake_store.calls[0]["expires_days"] is None
