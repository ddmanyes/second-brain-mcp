"""Contract tests for the read-only article audit core."""

from pathlib import Path

import pytest

from mcp_second_brain.article_audit import audit_article_records


ISSUE_KEYS = {
    "missing_frontmatter",
    "broken_wikilinks",
    "exact_duplicate_groups",
    "overdue_inbox",
    "stale_sources",
}
RESULT_KEYS = {
    "run_id",
    "generated_at",
    "scope",
    "vault",
    "counts",
    "index_gap",
    "issues",
    "totals",
    "truncated",
    "recommended_actions",
    "warnings",
}


def _write_note(vault: Path, relative_path: str, content: str) -> Path:
    path = vault / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def valid_article() -> str:
    return """---
title: Example Article
date: 2026-08-29
type: resource
status: active
source: https://example.com/article
tags: [article]
---

# Example Article
"""


def test_empty_vault_returns_stable_schema(tmp_path: Path) -> None:
    result = audit_article_records(tmp_path)

    assert set(result) == RESULT_KEYS
    assert result["scope"] == "all"
    assert set(result["issues"]) == ISSUE_KEYS
    assert result["counts"] == {
        "vault_markdown_files": 0,
        "indexed_notes": 0,
        "article_notes": 0,
        "research_notes": 0,
        "social_notes": 0,
    }
    assert result["index_gap"] == 0
    assert all(result["issues"][key] == [] for key in ISSUE_KEYS)
    assert result["totals"] == {key: 0 for key in ISSUE_KEYS}
    assert result["truncated"] is False
    assert isinstance(result["run_id"], str) and result["run_id"]
    assert isinstance(result["generated_at"], str) and result["generated_at"]
    assert isinstance(result["vault"], dict)
    assert isinstance(result["recommended_actions"], list)
    assert isinstance(result["warnings"], list)


def test_valid_resource_article_is_counted(tmp_path: Path, valid_article: str) -> None:
    _write_note(tmp_path, "30-resources/example.md", valid_article)

    result = audit_article_records(tmp_path)

    assert result["counts"]["vault_markdown_files"] == 1
    assert result["counts"]["article_notes"] == 1
    assert result["counts"]["research_notes"] == 0
    assert result["issues"]["missing_frontmatter"] == []


def test_missing_frontmatter_reports_required_fields_and_relative_path(
    tmp_path: Path,
) -> None:
    _write_note(tmp_path, "30-resources/missing.md", "# Missing metadata\n")

    result = audit_article_records(tmp_path)

    issue = result["issues"]["missing_frontmatter"][0]
    assert issue == {
        "path": "30-resources/missing.md",
        "missing_fields": ["date", "status", "title", "type"],
    }
    assert not Path(issue["path"]).is_absolute()


def test_limit_caps_issue_payload_but_preserves_total(tmp_path: Path) -> None:
    for name in ("c.md", "a.md", "b.md"):
        _write_note(tmp_path, f"30-resources/{name}", "# Missing metadata\n")

    result = audit_article_records(tmp_path, limit=2)

    assert [item["path"] for item in result["issues"]["missing_frontmatter"]] == [
        "30-resources/a.md",
        "30-resources/b.md",
    ]
    assert result["totals"]["missing_frontmatter"] == 3
    assert result["truncated"] is True
