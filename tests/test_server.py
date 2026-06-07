"""Tests for server.py — Phase 4: read_note_as_image + save_article URL normalisation."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import vault_db


def _load_server_functions(vault_path: Path):
    """Load server module with patched VAULT path."""
    import server
    original_vault = server.VAULT
    server.VAULT = vault_path
    return server, original_vault


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_db, "DB_PATH", tmp_path / "vault.db")
    monkeypatch.setattr(vault_db, "_schema_applied", False)


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


# ---------------------------------------------------------------------------
# Phase 4 — read_note_as_image
# ---------------------------------------------------------------------------

class TestReadNoteAsImage:
    def test_note_not_found_returns_error(self, vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.read_note_as_image("nonexistent/note.md")
        assert "not found" in result.lower()

    def test_text_fallback_when_no_snapshot(self, vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.read_note_as_image("10-projects/test-note.md")
        assert isinstance(result, str)
        assert "TEXT MODE" in result
        assert "snapshot_note_tool" in result

    def test_text_fallback_contains_note_content(self, vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.read_note_as_image("10-projects/test-note.md")
        assert "Content here" in result

    def test_returns_image_when_snapshot_exists(self, vault, monkeypatch, tmp_path):
        import server
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
        import server
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
        import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.read_note_as_image("../../etc/passwd")
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# Phase 3 — vault_sleep MCP tool
# ---------------------------------------------------------------------------

class TestVaultSleepTool:
    def test_sleep_status_returns_string(self, vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.sleep_status()
        assert isinstance(result, str)

    def test_vault_sleep_dry_run(self, vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", vault)
        result = server.vault_sleep(dry_run=True)
        assert isinstance(result, str)
        assert "dry" in result.lower() or "candidate" in result.lower() or "processed" in result.lower()


# ---------------------------------------------------------------------------
# save_article — arxiv URL normalisation
# ---------------------------------------------------------------------------

class TestNormaliseSourceUrl:
    def test_arxiv_abs_converted_to_html(self):
        import server
        result = server._normalise_source_url("https://arxiv.org/abs/2601.07190")
        assert result == "https://arxiv.org/html/2601.07190v1"

    def test_arxiv_abs_with_version_preserved(self):
        import server
        result = server._normalise_source_url("https://arxiv.org/abs/2604.15877v2")
        assert result == "https://arxiv.org/html/2604.15877v2"

    def test_non_arxiv_url_unchanged(self):
        import server
        url = "https://github.com/mem0ai/mem0"
        assert server._normalise_source_url(url) == url

    def test_nature_url_unchanged(self):
        import server
        url = "https://www.nature.com/articles/s41592-019-0619-0"
        assert server._normalise_source_url(url) == url

    def test_arxiv_html_url_unchanged(self):
        import server
        url = "https://arxiv.org/html/2601.07190v1"
        assert server._normalise_source_url(url) == url


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
        import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        reg = server._load_project_registry()
        assert "my-project" in reg
        assert reg["my-project"] == "10-projects/my-project"

    def test_excludes_flat_projects(self, registry_vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        reg = server._load_project_registry()
        assert "flat-proj" not in reg

    def test_empty_when_no_registry(self, tmp_path, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", tmp_path)
        assert server._load_project_registry() == {}


class TestDetectProjectSlug:
    def test_detects_slug_in_title(self):
        import server
        reg = {"my-project": "10-projects/my-project"}
        assert server._detect_project_slug("my-project 架構圖", "", reg) == "my-project"

    def test_detects_slug_in_tags(self):
        import server
        reg = {"my-project": "10-projects/my-project"}
        assert server._detect_project_slug("架構圖", "my-project,docs", reg) == "my-project"

    def test_no_match_returns_none(self):
        import server
        reg = {"my-project": "10-projects/my-project"}
        assert server._detect_project_slug("random note", "coding", reg) is None

    def test_longer_slug_wins_over_shorter(self):
        import server
        reg = {"my": "10-projects/my", "my-project": "10-projects/my-project"}
        result = server._detect_project_slug("my-project 設計", "", reg)
        assert result == "my-project"


class TestNewNoteProjectRouting:
    def test_resource_routes_to_project_docs(self, registry_vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("vault_db._connect"):
            result = server.new_note("resource", "my-project 架構圖", tags="my-project")
        assert "10-projects/my-project/docs" in result

    def test_coding_routes_to_project_phases(self, registry_vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("vault_db._connect"):
            result = server.new_note("coding", "my-project phase-1", tags="my-project")
        assert "10-projects/my-project/phases" in result

    def test_research_routes_to_project_research(self, registry_vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("vault_db._connect"):
            result = server.new_note("research", "my-project 競品分析", tags="my-project")
        assert "10-projects/my-project/research" in result

    def test_decision_ignores_project_routing(self, registry_vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        (registry_vault / "decisions").mkdir()
        tmpl = registry_vault / "templates" / "decision-template.md"
        tmpl.write_text(TEMPLATE_CONTENT, encoding="utf-8")
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("vault_db._connect"):
            result = server.new_note("decision", "my-project 技術選型")
        assert result.startswith("Created: decisions/")

    def test_no_slug_match_uses_default_folder(self, registry_vault, monkeypatch):
        import server
        monkeypatch.setattr(server, "VAULT", registry_vault)
        with patch.object(server, "_extract_semantic_keywords_via_gemini", return_value=[]), \
             patch.object(server, "_inject_related_links", return_value=0), \
             patch("vault_db._connect"):
            result = server.new_note("resource", "一般參考資料")
        assert result.startswith("Created: 30-resources/") or "30-resources" in result
