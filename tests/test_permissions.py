"""Tests for RBAC write-permission guard (MULTIUSER_PLAN P2).

Tests check_write_permission() in both audit-only (default) and enforce modes,
and verify the identity contextvar integration with the middleware.
"""
from __future__ import annotations

import os

import pytest

from mcp_second_brain.identity import (
    Identity,
    _current,
    check_write_permission,
    set_identity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WRITE_TOOLS = [
    "new_note",
    "update_goals",
    "update_note",
    "append_to_note",
    "mark_note_status",
    "vault_sleep",
    "expand_semantic_keywords_tool",
    "enrich_neighbor_keywords_tool",
    "save_article",
    "update_links_tool",
    "extract_figures_for",
    "snapshot_note_tool",
    "consolidate_tool",
    "prune_archive_tool",
    "annotate_figure",
    "init_vault",
]

READ_TOOLS = [
    "search_notes",
    "read_note",
    "get_context",
    "find_related_notes",
    "search_figures",
    "read_figure",
    "index_stats",
    "get_agent_instructions",
]


def _with_identity(identity: Identity | None):
    """Context manager: set identity for the duration of the with-block."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        if identity is None:
            yield
        else:
            tok = set_identity(identity)
            try:
                yield
            finally:
                _current.reset(tok)

    return _ctx()


# ---------------------------------------------------------------------------
# No identity (stdio / dev — auth disabled)
# ---------------------------------------------------------------------------

class TestNoIdentity:
    def test_write_allowed_when_no_identity(self):
        """Unauthenticated (stdio dev setup) always passes — auth is opt-in."""
        for tool in WRITE_TOOLS:
            assert check_write_permission(tool) is None, f"should allow {tool}"


# ---------------------------------------------------------------------------
# Audit-only mode (default — SB_RBAC_ENFORCE not set)
# ---------------------------------------------------------------------------

class TestAuditMode:
    @pytest.fixture(autouse=True)
    def clear_enforce_env(self, monkeypatch):
        monkeypatch.delenv("SB_RBAC_ENFORCE", raising=False)

    def test_admin_can_write(self):
        admin = Identity(user_id="alice", role="admin")
        with _with_identity(admin):
            for tool in WRITE_TOOLS:
                assert check_write_permission(tool) is None

    def test_writer_can_write(self):
        writer = Identity(user_id="bob", role="writer")
        with _with_identity(writer):
            for tool in WRITE_TOOLS:
                assert check_write_permission(tool) is None

    def test_reader_passes_in_audit_mode(self, capsys):
        """In audit mode reader is NOT blocked — just logged."""
        reader = Identity(user_id="carol", role="reader")
        with _with_identity(reader):
            result = check_write_permission("new_note")
        assert result is None, "audit mode must not block"
        captured = capsys.readouterr()
        assert "RBAC AUDIT" in captured.err
        assert "carol" in captured.err
        assert "new_note" in captured.err

    def test_audit_log_mentions_tool(self, capsys):
        reader = Identity(user_id="dave", role="reader")
        with _with_identity(reader):
            check_write_permission("save_article")
        assert "save_article" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Enforce mode (SB_RBAC_ENFORCE=1)
# ---------------------------------------------------------------------------

class TestEnforceMode:
    @pytest.fixture(autouse=True)
    def set_enforce(self, monkeypatch):
        monkeypatch.setenv("SB_RBAC_ENFORCE", "1")

    def test_admin_still_allowed(self):
        admin = Identity(user_id="alice", role="admin")
        with _with_identity(admin):
            for tool in WRITE_TOOLS:
                assert check_write_permission(tool) is None

    def test_writer_still_allowed(self):
        writer = Identity(user_id="bob", role="writer")
        with _with_identity(writer):
            for tool in WRITE_TOOLS:
                assert check_write_permission(tool) is None

    def test_reader_blocked_from_all_write_tools(self):
        reader = Identity(user_id="carol", role="reader")
        with _with_identity(reader):
            for tool in WRITE_TOOLS:
                result = check_write_permission(tool)
                assert result is not None, f"reader should be blocked from {tool}"
                assert "read-only" in result.lower() or "denied" in result.lower()

    def test_deny_message_includes_tool_name(self):
        reader = Identity(user_id="eve", role="reader")
        with _with_identity(reader):
            result = check_write_permission("update_note")
        assert "update_note" in result

    def test_deny_logs_to_stderr(self, capsys):
        reader = Identity(user_id="frank", role="reader")
        with _with_identity(reader):
            check_write_permission("new_note")
        assert "RBAC DENY" in capsys.readouterr().err

    @pytest.mark.parametrize("val", ["1", "true", "yes", "enforce", "True", "YES"])
    def test_all_truthy_env_values_enforce(self, monkeypatch, val):
        monkeypatch.setenv("SB_RBAC_ENFORCE", val)
        reader = Identity(user_id="g", role="reader")
        with _with_identity(reader):
            assert check_write_permission("new_note") is not None

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "audit"])
    def test_falsy_env_values_stay_audit(self, monkeypatch, val):
        monkeypatch.setenv("SB_RBAC_ENFORCE", val)
        reader = Identity(user_id="h", role="reader")
        with _with_identity(reader):
            assert check_write_permission("new_note") is None


# ---------------------------------------------------------------------------
# read tools are never gated (sanity)
# ---------------------------------------------------------------------------

class TestReadToolsNeverGated:
    """check_write_permission is only called on write tools — read tools don't call it.
    This confirms the helper itself doesn't accidentally block on any identity."""

    def test_no_tool_name_gated_for_reader_in_audit(self, monkeypatch):
        monkeypatch.delenv("SB_RBAC_ENFORCE", raising=False)
        reader = Identity(user_id="x", role="reader")
        with _with_identity(reader):
            # read tools never invoke check_write_permission — simulate by calling
            # with a fictional "read_note" to confirm audit mode always returns None
            assert check_write_permission("read_note") is None
