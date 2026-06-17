"""Tests for write-action audit log (MULTIUSER_PLAN P3).

Tests cover:
- DuckDBStore noop (always returns empty)
- _log_write() helper: actor from identity contextvar, 'unknown' fallback
- store.append_audit_log / query_audit_log contract via an in-memory mock
"""
from __future__ import annotations

import pytest

from mcp_second_brain.identity import Identity, _current, set_identity


# ---------------------------------------------------------------------------
# DuckDB noop implementations
# ---------------------------------------------------------------------------

class TestDuckDBStoreAuditNoop:
    def test_append_audit_log_is_noop(self):
        from mcp_second_brain.store.duckdb_store import DuckDBStore
        store = DuckDBStore()
        # Should not raise, even without a DB connection
        store.append_audit_log("alice", "new_note", "Some Title")

    def test_query_audit_log_returns_empty(self):
        from mcp_second_brain.store.duckdb_store import DuckDBStore
        store = DuckDBStore()
        result = store.query_audit_log(user_id="alice")
        assert result == []


# ---------------------------------------------------------------------------
# _log_write helper — unit-tested with a mock store
# ---------------------------------------------------------------------------

class _MockStore:
    """Minimal in-memory store for audit testing."""

    def __init__(self):
        self.records: list[dict] = []

    def append_audit_log(self, user_id: str, tool: str, target: str = "") -> None:
        self.records.append({"user_id": user_id, "tool": tool, "target": target})

    def query_audit_log(
        self,
        user_id: str | None = None,
        tool: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        rows = self.records[:]
        if user_id:
            rows = [r for r in rows if r["user_id"] == user_id]
        if tool:
            rows = [r for r in rows if r["tool"] == tool]
        return rows[:limit]


def _make_log_write(mock_store):
    """Return a _log_write-equivalent function that uses mock_store."""
    from mcp_second_brain.identity import get_current_identity

    def _log_write(tool: str, target: str = "") -> None:
        identity = get_current_identity()
        actor = identity.user_id if identity else "unknown"
        try:
            mock_store.append_audit_log(actor, tool, target)
        except Exception:
            pass

    return _log_write


class TestLogWriteHelper:
    def test_logs_with_identity(self):
        mock = _MockStore()
        log_write = _make_log_write(mock)
        alice = Identity(user_id="alice", role="writer")
        tok = set_identity(alice)
        try:
            log_write("new_note", "My Title")
        finally:
            _current.reset(tok)
        assert len(mock.records) == 1
        assert mock.records[0] == {"user_id": "alice", "tool": "new_note", "target": "My Title"}

    def test_logs_unknown_when_no_identity(self):
        mock = _MockStore()
        log_write = _make_log_write(mock)
        log_write("update_note", "decisions/foo.md")
        assert mock.records[0]["user_id"] == "unknown"

    def test_logs_target_empty_string_for_batch_tools(self):
        mock = _MockStore()
        log_write = _make_log_write(mock)
        log_write("vault_sleep", "")
        assert mock.records[0]["target"] == ""

    def test_never_raises_on_store_error(self):
        class BrokenStore:
            def append_audit_log(self, *a, **kw):
                raise RuntimeError("DB unavailable")

        log_write = _make_log_write(BrokenStore())
        log_write("new_note", "title")  # must not raise


# ---------------------------------------------------------------------------
# Mock store query contract
# ---------------------------------------------------------------------------

class TestAuditLogQueryContract:
    def setup_method(self):
        self.store = _MockStore()
        self.store.append_audit_log("alice", "new_note", "Title A")
        self.store.append_audit_log("bob",   "update_note", "decisions/foo.md")
        self.store.append_audit_log("alice", "save_article", "https://example.com")

    def test_query_all(self):
        assert len(self.store.query_audit_log()) == 3

    def test_filter_by_user(self):
        rows = self.store.query_audit_log(user_id="alice")
        assert len(rows) == 2
        assert all(r["user_id"] == "alice" for r in rows)

    def test_filter_by_tool(self):
        rows = self.store.query_audit_log(tool="update_note")
        assert len(rows) == 1
        assert rows[0]["target"] == "decisions/foo.md"

    def test_limit(self):
        assert len(self.store.query_audit_log(limit=2)) == 2

    def test_record_fields(self):
        row = self.store.query_audit_log(user_id="bob")[0]
        assert set(row.keys()) == {"user_id", "tool", "target"}
