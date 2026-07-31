"""Tests for the RBAC guard (MULTIUSER_PLAN P2) and the tool boundary that applies it.

Two layers:

  * ``check_write_permission`` in isolation — audit-only (default) vs enforce mode.
  * The real ``@write_tool`` / ``@admin_tool`` seam — that every registered tool
    actually goes through the guard, and that the registry is complete.

The second layer is the point. The previous version of this file kept a
hand-copied ``WRITE_TOOLS`` list and only ever fed strings to the guard, so it
stayed green while ``extract_rules_tool`` wrote memory/rules.md with no guard and
no audit trail at all. Tool names here come from the decorator registry, and the
guard tests drive the actual tool functions.
"""
from __future__ import annotations

import inspect

import pytest

from mcp_second_brain import server
from mcp_second_brain.identity import (
    Identity,
    _current,
    check_write_permission,
    set_identity,
)

# Tool names are derived from the decorator registry — never hand-copied.
WRITE_TOOLS = sorted(server.WRITE_TOOLS)
ADMIN_TOOLS = sorted(server.ADMIN_TOOLS)

# Change detector: adding or removing a write tool must be a deliberate edit here.
EXPECTED_WRITE_TOOLS = sorted([
    "annotate_figure",
    "append_to_note",
    "consolidate_tool",
    "enrich_neighbor_keywords_tool",
    "expand_semantic_keywords_tool",
    "extract_figures_for",
    "extract_rules_tool",
    "init_vault",
    "mark_note_status",
    "new_note",
    "prune_archive_tool",
    "save_article",
    "snapshot_note_tool",
    "update_goals",
    "update_links_tool",
    "update_note",
    "vault_sleep",
])

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

_DUMMY = {str: "x", int: 0, float: 0.0, bool: False}


def _dummy_args(fn) -> dict:
    """Minimal kwargs to call a tool. The guard returns before the body runs."""
    args = {}
    for name, p in inspect.signature(fn).parameters.items():
        if p.default is inspect.Parameter.empty:
            args[name] = _DUMMY.get(p.annotation, "x")
    return args


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
# The registry itself
# ---------------------------------------------------------------------------

class TestWriteToolRegistry:
    def test_registry_matches_expected_set(self):
        assert WRITE_TOOLS == EXPECTED_WRITE_TOOLS

    def test_every_registered_tool_is_a_module_level_tool(self):
        for name in WRITE_TOOLS + ADMIN_TOOLS:
            fn = getattr(server, name, None)
            assert callable(fn), f"{name} is registered but not exposed"
            assert fn.__name__ == name, "audited name drifted from the function"

    def test_audit_target_is_a_real_parameter_or_a_constant(self):
        for name, target in server.WRITE_TOOLS.items():
            if not target or "/" in target:  # unset, or a target_const like memory/goals.md
                continue
            params = inspect.signature(getattr(server, name)).parameters
            assert target in params, f"{name}: audit target {target!r} is not a parameter"


# ---------------------------------------------------------------------------
# The real tools go through the guard
# ---------------------------------------------------------------------------

class TestRealToolsAreGuarded:
    @pytest.fixture(autouse=True)
    def enforce(self, monkeypatch):
        monkeypatch.setenv("SB_RBAC_ENFORCE", "1")

    @pytest.mark.parametrize("name", WRITE_TOOLS)
    def test_reader_is_blocked_from_every_write_tool(self, name):
        """Drives the actual tool: proves the guard is wired, not just importable."""
        fn = getattr(server, name)
        with _with_identity(Identity(user_id="carol", role="reader")):
            result = fn(**_dummy_args(fn))
        assert isinstance(result, str), f"{name} did not return the guard's error"
        assert "read-only access denied" in result
        assert name in result

    @pytest.mark.parametrize("name", ADMIN_TOOLS)
    def test_writer_is_blocked_from_every_admin_tool(self, name):
        fn = getattr(server, name)
        with _with_identity(Identity(user_id="bob", role="writer")):
            result = fn(**_dummy_args(fn))
        assert "admin access required" in result
        assert name in result

    @pytest.mark.parametrize("name", ADMIN_TOOLS)
    def test_admin_guard_ignores_audit_mode(self, name, monkeypatch):
        """Unlike writes, admin is enforced even when SB_RBAC_ENFORCE is off."""
        monkeypatch.delenv("SB_RBAC_ENFORCE", raising=False)
        fn = getattr(server, name)
        with _with_identity(Identity(user_id="bob", role="writer")):
            assert "admin access required" in fn(**_dummy_args(fn))


class TestAuditRecordsTheRealCall:
    def test_update_note_audits_its_own_name_and_target(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        note = tmp_path / "n.md"
        note.write_text("old", encoding="utf-8")

        store = MagicMock()
        store.find_related.return_value = []
        monkeypatch.setattr(server, "VAULT", tmp_path)
        monkeypatch.setattr(server, "_store", store)

        with _with_identity(Identity(user_id="bob", role="writer")):
            result = server.update_note("n.md", "new")

        assert "Updated: n.md" in result
        assert note.read_text(encoding="utf-8") == "new"
        store.append_audit_log.assert_called_once_with("bob", "update_note", "n.md")

    def test_path_escape_is_reported_and_nothing_is_written(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        outside = tmp_path.parent / "escape-target.md"
        monkeypatch.setattr(server, "VAULT", tmp_path)
        monkeypatch.setattr(server, "_store", MagicMock())

        result = server.update_note(f"../{outside.name}", "pwned")

        assert "within the vault" in result
        assert not outside.exists()

    def test_enrich_neighbor_keywords_refuses_to_escape_the_vault(self, tmp_path, monkeypatch):
        """Regression: this tool used to skip the containment check entirely."""
        from unittest.mock import MagicMock

        outside = tmp_path.parent / "enrich-escape.md"
        outside.write_text("---\ntitle: x\n---\n", encoding="utf-8")
        before = outside.read_text(encoding="utf-8")

        store = MagicMock()
        store.compute_neighbor_keywords.return_value = {
            f"../{outside.name}": {"neighbor_keywords": ["a"], "cluster_topic": "t"}
        }
        monkeypatch.setattr(server, "VAULT", tmp_path)
        monkeypatch.setattr(server, "_store", store)

        result = server.enrich_neighbor_keywords_tool(note_path=f"../{outside.name}")

        assert "'enriched': 0" in result
        assert outside.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# check_write_permission in isolation
# ---------------------------------------------------------------------------

class TestNoIdentity:
    def test_write_allowed_when_no_identity(self):
        """Unauthenticated (stdio dev setup) always passes — auth is opt-in."""
        for tool in WRITE_TOOLS:
            assert check_write_permission(tool) is None, f"should allow {tool}"


class TestAuditMode:
    @pytest.fixture(autouse=True)
    def clear_enforce_env(self, monkeypatch):
        monkeypatch.delenv("SB_RBAC_ENFORCE", raising=False)

    def test_admin_can_write(self):
        with _with_identity(Identity(user_id="alice", role="admin")):
            for tool in WRITE_TOOLS:
                assert check_write_permission(tool) is None

    def test_writer_can_write(self):
        with _with_identity(Identity(user_id="bob", role="writer")):
            for tool in WRITE_TOOLS:
                assert check_write_permission(tool) is None

    def test_reader_passes_in_audit_mode(self, capsys):
        """In audit mode reader is NOT blocked — just logged."""
        with _with_identity(Identity(user_id="carol", role="reader")):
            result = check_write_permission("new_note")
        assert result is None, "audit mode must not block"
        captured = capsys.readouterr()
        assert "RBAC AUDIT" in captured.err
        assert "carol" in captured.err
        assert "new_note" in captured.err

    def test_audit_log_mentions_tool(self, capsys):
        with _with_identity(Identity(user_id="dave", role="reader")):
            check_write_permission("save_article")
        assert "save_article" in capsys.readouterr().err


class TestEnforceMode:
    @pytest.fixture(autouse=True)
    def set_enforce(self, monkeypatch):
        monkeypatch.setenv("SB_RBAC_ENFORCE", "1")

    def test_admin_still_allowed(self):
        with _with_identity(Identity(user_id="alice", role="admin")):
            for tool in WRITE_TOOLS:
                assert check_write_permission(tool) is None

    def test_writer_still_allowed(self):
        with _with_identity(Identity(user_id="bob", role="writer")):
            for tool in WRITE_TOOLS:
                assert check_write_permission(tool) is None

    def test_reader_blocked_from_all_write_tools(self):
        with _with_identity(Identity(user_id="carol", role="reader")):
            for tool in WRITE_TOOLS:
                result = check_write_permission(tool)
                assert result is not None, f"reader should be blocked from {tool}"
                assert "read-only" in result.lower() or "denied" in result.lower()

    def test_deny_message_includes_tool_name(self):
        with _with_identity(Identity(user_id="eve", role="reader")):
            result = check_write_permission("update_note")
        assert "update_note" in result

    def test_deny_logs_to_stderr(self, capsys):
        with _with_identity(Identity(user_id="frank", role="reader")):
            check_write_permission("new_note")
        assert "RBAC DENY" in capsys.readouterr().err

    @pytest.mark.parametrize("val", ["1", "true", "yes", "enforce", "True", "YES"])
    def test_all_truthy_env_values_enforce(self, monkeypatch, val):
        monkeypatch.setenv("SB_RBAC_ENFORCE", val)
        with _with_identity(Identity(user_id="g", role="reader")):
            assert check_write_permission("new_note") is not None

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "audit"])
    def test_falsy_env_values_stay_audit(self, monkeypatch, val):
        monkeypatch.setenv("SB_RBAC_ENFORCE", val)
        with _with_identity(Identity(user_id="h", role="reader")):
            assert check_write_permission("new_note") is None


class TestReadToolsNeverGated:
    """Read tools must not be registered as write tools, and the helper itself
    never blocks on a name it wasn't given a reader-restricted role for."""

    def test_read_tools_are_not_in_the_write_registry(self):
        assert not set(READ_TOOLS) & set(WRITE_TOOLS)

    def test_no_tool_name_gated_for_reader_in_audit(self, monkeypatch):
        monkeypatch.delenv("SB_RBAC_ENFORCE", raising=False)
        with _with_identity(Identity(user_id="x", role="reader")):
            assert check_write_permission("read_note") is None
