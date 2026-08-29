"""Tests for server.py public helpers and MCP tool contracts."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError



from mcp_second_brain import vault_db


def _load_server_functions(vault_path: Path):
    """Load server module with patched VAULT path."""
    from mcp_second_brain import server
    original_vault = server.VAULT
    server.VAULT = vault_path
    return server, original_vault


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_db, "DB_PATH", tmp_path / "vault.db")
    monkeypatch.setattr(vault_db, "_schema_applied", False)
    monkeypatch.setattr(vault_db, "EMBED_AUTO_START", False)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    (tmp_path / "10-projects").mkdir()
    note = tmp_path / "10-projects" / "test-note.md"
    note.write_text(
        "---\ntitle: Test Note\ndate: 2026-05-29\ntype: project\nstatus: active\ntags: []\n---\n\n# Test\n\nContent here.",
        encoding="utf-8",
    )
    vault_db.sync_all(tmp_path)
    return tmp_path


def _mcp_tool(server, name: str):
    """Return one tool through FastMCP's public listing seam."""
    tools = asyncio.run(server.mcp.list_tools())
    matches = [tool for tool in tools if tool.name == name]
    assert len(matches) == 1, f"expected exactly one registered MCP tool named {name!r}"
    return matches[0]


def _call_mcp(server, name: str, arguments: dict):
    """Invoke a tool through FastMCP's public call seam."""
    return asyncio.run(server.mcp.call_tool(name, arguments))


# ---------------------------------------------------------------------------
# Article housekeeping — audit_article_records MCP contract
# ---------------------------------------------------------------------------


class TestAuditArticleRecordsMCPContract:
    TOOL_NAME = "audit_article_records"

    def test_tool_registration_and_bounded_input_schema(self):
        from mcp_second_brain import server

        tool = _mcp_tool(server, self.TOOL_NAME)
        properties = tool.inputSchema["properties"]

        assert tool.inputSchema["required"] == []
        assert properties["scope"]["default"] == "all"
        assert properties["scope"]["enum"] == ["articles", "social", "all"]
        assert properties["limit"] == {
            "default": 100,
            "maximum": 500,
            "minimum": 1,
            "title": "Limit",
            "type": "integer",
        }
        assert properties["stale_after_days"] == {
            "default": 8,
            "maximum": 90,
            "minimum": 1,
            "title": "Stale After Days",
            "type": "integer",
        }

    def test_tool_is_explicitly_read_only_and_has_structured_output(self):
        from mcp_second_brain import server

        tool = _mcp_tool(server, self.TOOL_NAME)

        assert tool.annotations is not None
        assert tool.annotations.model_dump(by_alias=True, exclude_none=True) == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        assert tool.outputSchema is not None
        assert tool.outputSchema["type"] == "object"

    def test_call_returns_structured_content_and_equivalent_text_fallback(
        self, vault, monkeypatch
    ):
        from mcp_second_brain import server

        monkeypatch.setattr(server, "VAULT", vault)
        content, structured = _call_mcp(server, self.TOOL_NAME, {})

        assert structured["scope"] == "all"
        assert structured["counts"]["vault_markdown_files"] == 1
        assert set(structured["issues"]) == {
            "missing_frontmatter",
            "broken_wikilinks",
            "exact_duplicate_groups",
            "overdue_inbox",
            "stale_sources",
        }
        assert len(content) == 1
        assert content[0].type == "text"
        assert json.loads(content[0].text) == structured

    def test_unreadable_vault_returns_actionable_tool_error(self, tmp_path, monkeypatch):
        from mcp_second_brain import server

        missing_vault = tmp_path / "missing-vault"
        monkeypatch.setattr(server, "VAULT", missing_vault)

        with pytest.raises(ToolError) as exc_info:
            _call_mcp(server, self.TOOL_NAME, {})

        message = str(exc_info.value)
        assert "could not read vault" in message.lower()
        assert "SECOND_BRAIN_PATH" in message
        assert "retry" in message.lower()

    def test_corrupt_social_state_is_reported_without_failing_audit(
        self, vault, monkeypatch
    ):
        from mcp_second_brain import server

        state_dir = vault / "memory" / "sync-state"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "x-bookmarks.md"
        state_path.write_text(
            "---\n"
            "title: X bookmark sync state\n"
            "date: 2026-08-29\n"
            "type: sync_state\n"
            "status: active\n"
            "source: x-bookmarks\n"
            "last_synced_at: definitely-not-a-date\n"
            "pending: many\n"
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(server, "VAULT", vault)

        _, structured = _call_mcp(
            server,
            self.TOOL_NAME,
            {"scope": "social", "stale_after_days": 8},
        )

        invalid = next(
            issue
            for issue in structured["issues"]["stale_sources"]
            if issue["source"] == "x-bookmarks"
        )
        assert invalid["status"] == "invalid"
        assert invalid["path"] == "memory/sync-state/x-bookmarks.md"
        assert any(
            "Invalid sync state fields" in warning
            and "memory/sync-state/x-bookmarks.md" in warning
            for warning in structured["warnings"]
        )
        assert any(
            "social source state" in action
            for action in structured["recommended_actions"]
        )

    def test_limit_above_contract_returns_actionable_tool_error(self):
        from mcp_second_brain import server

        with pytest.raises(ToolError) as exc_info:
            _call_mcp(server, self.TOOL_NAME, {"limit": 501})

        message = str(exc_info.value).lower()
        assert "limit" in message
        assert "500" in message


# ---------------------------------------------------------------------------
# Phase 4 — read_note_as_image
# ---------------------------------------------------------------------------

class TestReadNoteAsImage:
    def test_note_not_found_returns_error(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.read_note_as_image("nonexistent/note.md")
        assert "not found" in result.lower()

    def test_text_fallback_when_no_snapshot(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.read_note_as_image("10-projects/test-note.md")
        assert isinstance(result, str)
        assert "TEXT MODE" in result
        assert "snapshot_note_tool" in result

    def test_text_fallback_contains_note_content(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.read_note_as_image("10-projects/test-note.md")
        assert "Content here" in result

    def test_returns_image_when_snapshot_exists(self, vault, monkeypatch, tmp_path):
        from mcp_second_brain import server
        from mcp.server.fastmcp import Image

        monkeypatch.setattr(server, "VAULT", vault)

        # Create a fake snapshot PNG
        snap_dir = tmp_path / ".snapshots" / "abc123def456"
        snap_dir.mkdir(parents=True)
        snap_file = snap_dir / "snapshot_base.png"
        snap_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

        # Register snapshot in DB
        vault_db.update_snapshot(
            "10-projects/test-note.md",
            str(snap_file),
            "base",
            256,
        )

        result = server.read_note_as_image("10-projects/test-note.md")
        assert isinstance(result, Image)

    def test_text_fallback_when_snapshot_path_missing(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)

        # Register snapshot in DB but don't create the file
        vault_db.update_snapshot(
            "10-projects/test-note.md",
            "/nonexistent/snap.png",
            "base",
            256,
        )

        result = server.read_note_as_image("10-projects/test-note.md")
        assert isinstance(result, str)
        assert "TEXT MODE" in result

    def test_path_traversal_blocked(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.read_note_as_image("../../etc/passwd")
        # Escape and "missing note" are distinct answers since resolve_in_vault
        # owns the check — an escape must not be reported as a missing note.
        assert "within the vault" in result.lower()


# ---------------------------------------------------------------------------
# Phase 3 — vault_sleep MCP tool
# ---------------------------------------------------------------------------

class TestVaultSleepTool:
    def test_sleep_status_returns_string(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.sleep_status()
        assert isinstance(result, str)

    def test_vault_sleep_dry_run(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.vault_sleep(dry_run=True)
        assert isinstance(result, str)
        assert "dry" in result.lower() or "candidate" in result.lower() or "processed" in result.lower()


# ---------------------------------------------------------------------------
# save_article — arxiv URL normalisation
# ---------------------------------------------------------------------------

class TestNormaliseSourceUrl:
    def test_arxiv_abs_converted_to_html(self):
        from mcp_second_brain import server
        result = server._normalise_source_url("https://arxiv.org/abs/2601.07190")
        assert result == "https://arxiv.org/html/2601.07190v1"

    def test_arxiv_abs_with_version_preserved(self):
        from mcp_second_brain import server
        result = server._normalise_source_url("https://arxiv.org/abs/2604.15877v2")
        assert result == "https://arxiv.org/html/2604.15877v2"

    def test_non_arxiv_url_unchanged(self):
        from mcp_second_brain import server
        url = "https://github.com/mem0ai/mem0"
        assert server._normalise_source_url(url) == url

    def test_nature_url_unchanged(self):
        from mcp_second_brain import server
        url = "https://www.nature.com/articles/s41592-019-0619-0"
        assert server._normalise_source_url(url) == url

    def test_arxiv_html_url_unchanged(self):
        from mcp_second_brain import server
        url = "https://arxiv.org/html/2601.07190v1"
        assert server._normalise_source_url(url) == url


class TestSlugify:
    def test_punctuation_becomes_separator_not_merge(self):
        from mcp_second_brain import server
        # Parens/tilde/slash/dot must not glue adjacent words together.
        assert server._slugify("SOP（~/.claude 設定）") == "sop-claude-設定"

    def test_basic_kebab(self):
        from mcp_second_brain import server
        assert server._slugify("Finance Kit Overview") == "finance-kit-overview"

    def test_cjk_preserved(self):
        from mcp_second_brain import server
        assert server._slugify("台股日報補強") == "台股日報補強"

    def test_no_leading_or_trailing_dash(self):
        from mcp_second_brain import server
        assert server._slugify("（前綴符號）標題") == "前綴符號-標題"

    def test_underscores_and_spaces_collapse(self):
        from mcp_second_brain import server
        assert server._slugify("a__b  c") == "a-b-c"


# ---------------------------------------------------------------------------
# Project-aware routing — _load_project_registry / _detect_project_slug / new_note
# ---------------------------------------------------------------------------

REGISTRY_MD = """\
---
title: Project Registry
---

| Slug | 正式名稱 | Overview 位置 |
|------|---------|--------------|
| my-project | My Project | 10-projects/my-project/overview.md |
| flat-proj | Flat Proj | 10-projects/flat-proj-overview.md |
"""

TEMPLATE_CONTENT = """\
---
title: "{{title}}"
date: {{date}}
type: note
tags: []
---

# {{title}}
"""


@pytest.fixture()
def registry_vault(tmp_path: Path) -> Path:
    (tmp_path / "10-projects").mkdir()
    (tmp_path / "10-projects" / "PROJECT_REGISTRY.md").write_text(REGISTRY_MD, encoding="utf-8")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "note-template.md").write_text(TEMPLATE_CONTENT, encoding="utf-8")
    (tmp_path / "templates" / "research-note-template.md").write_text(TEMPLATE_CONTENT, encoding="utf-8")
    (tmp_path / "00-inbox").mkdir()
    (tmp_path / "memory").mkdir()
    return tmp_path


class TestLoadProjectRegistry:
    def test_returns_only_subfoldered_projects(self, registry_vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        reg = server._load_project_registry()
        assert "my-project" in reg
        assert reg["my-project"] == "10-projects/my-project"

    def test_excludes_flat_projects(self, registry_vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        reg = server._load_project_registry()
        assert "flat-proj" not in reg

    def test_empty_when_no_registry(self, tmp_path, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", tmp_path)
        assert server._load_project_registry() == {}


class TestDetectProjectSlug:
    def test_detects_slug_in_title(self):
        from mcp_second_brain import server
        reg = {"my-project": "10-projects/my-project"}
        assert server._detect_project_slug("my-project 架構圖", "", reg) == "my-project"

    def test_detects_slug_in_tags(self):
        from mcp_second_brain import server
        reg = {"my-project": "10-projects/my-project"}
        assert server._detect_project_slug("架構圖", "my-project,docs", reg) == "my-project"

    def test_no_match_returns_none(self):
        from mcp_second_brain import server
        reg = {"my-project": "10-projects/my-project"}
        assert server._detect_project_slug("random note", "coding", reg) is None

    def test_longer_slug_wins_over_shorter(self):
        from mcp_second_brain import server
        reg = {"my": "10-projects/my", "my-project": "10-projects/my-project"}
        result = server._detect_project_slug("my-project 設計", "", reg)
        assert result == "my-project"


class TestNewNoteProjectRouting:
    def test_resource_routes_to_project_docs(self, registry_vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("mcp_second_brain.vault_db._connect"):
            result = server.new_note("resource", "my-project 架構圖", tags="my-project")
        assert "10-projects/my-project/docs" in result

    def test_coding_routes_to_project_phases(self, registry_vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("mcp_second_brain.vault_db._connect"):
            result = server.new_note("coding", "my-project phase-1", tags="my-project")
        assert "10-projects/my-project/phases" in result

    def test_coding_fix_prefixed_routes_to_project_fixes(self, registry_vault, monkeypatch):
        """E5 (fix-2026-08-18): a fix-* postmortem shouldn't land in phases/ and need a
        manual move — it routes straight to fixes/."""
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("mcp_second_brain.vault_db._connect"):
            result = server.new_note("coding", "Fix update_note frontmatter bug", tags="my-project")
        assert "10-projects/my-project/fixes" in result
        assert "phases" not in result

    def test_coding_non_fix_still_routes_to_project_phases(self, registry_vault, monkeypatch):
        """Guard against over-matching: only a fix-* slug should divert from phases/."""
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("mcp_second_brain.vault_db._connect"):
            result = server.new_note("coding", "prefix-unrelated-note", tags="my-project")
        assert "10-projects/my-project/phases" in result

    def test_research_routes_to_project_research(self, registry_vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("mcp_second_brain.vault_db._connect"):
            result = server.new_note("research", "my-project 競品分析", tags="my-project")
        assert "10-projects/my-project/research" in result

    def test_decision_ignores_project_routing(self, registry_vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        (registry_vault / "decisions").mkdir()
        tmpl = registry_vault / "templates" / "decision-template.md"
        tmpl.write_text(TEMPLATE_CONTENT, encoding="utf-8")
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("mcp_second_brain.vault_db._connect"):
            result = server.new_note("decision", "my-project 技術選型")
        assert result.startswith("Created: decisions/")

    def test_no_slug_match_uses_default_folder(self, registry_vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("mcp_second_brain.vault_db._connect"):
            result = server.new_note("resource", "一般參考資料")
        assert result.startswith("Created: 30-resources/") or "30-resources" in result


# ---------------------------------------------------------------------------
# after_write — the shared post-write index+enrichment tail
# ---------------------------------------------------------------------------


class TestAfterWrite:
    """Contract of the tail every note write path funnels through."""

    def _patched(self, server, monkeypatch, *, index_raises=False):
        store = MagicMock()
        if index_raises:
            store.index_file.side_effect = RuntimeError("boom")
        monkeypatch.setattr(server, "_store", store)
        append = MagicMock()
        relink = MagicMock(return_value=3)
        enrich = MagicMock()
        figures = MagicMock()
        monkeypatch.setattr(server, "_append_to_index", append)
        monkeypatch.setattr(server, "_inject_related_links", relink)
        monkeypatch.setattr(server, "_run_keyword_enrichment_async", enrich)
        monkeypatch.setattr(server, "_spawn_figure_extract", figures)
        return store, append, relink, enrich, figures

    def test_invariant_always_indexes(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        store, *_ = self._patched(server, monkeypatch)
        server.after_write(vault / "10-projects" / "test-note.md", "10-projects/test-note.md")
        assert store.index_file.called

    def test_register_label_none_skips_registration(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        _, append, *_ = self._patched(server, monkeypatch)
        server.after_write(vault / "10-projects" / "test-note.md", "10-projects/test-note.md")
        assert not append.called

    def test_register_label_triggers_registration(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        _, append, *_ = self._patched(server, monkeypatch)
        server.after_write(vault / "n.md", "n.md", register_label="My Label")
        assert append.called
        assert append.call_args[0][1] == "My Label"

    def test_relink_false_skips_and_returns_zero(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        _, _, relink, *_ = self._patched(server, monkeypatch)
        n = server.after_write(vault / "n.md", "n.md", relink=False)
        assert n == 0
        assert not relink.called

    def test_relink_returns_count(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        self._patched(server, monkeypatch)
        n = server.after_write(vault / "n.md", "n.md")
        assert n == 3

    def test_enrich_and_figures_are_opt_in(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        _, _, _, enrich, figures = self._patched(server, monkeypatch)
        server.after_write(vault / "n.md", "n.md")
        assert not enrich.called and not figures.called
        server.after_write(vault / "n.md", "n.md", enrich="body text", extract_figures=True)
        assert enrich.called and figures.called

    def test_index_failure_never_raises_and_returns_zero(self, vault, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", vault)
        self._patched(server, monkeypatch, index_raises=True)
        n = server.after_write(vault / "n.md", "n.md")  # must not raise
        assert n == 0
