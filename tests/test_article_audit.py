"""Contract tests for the read-only article audit core."""

from datetime import datetime, timezone
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


def _article_note(title: str, source: str, *, doi: str = "") -> str:
    doi_line = f"doi: {doi}\n" if doi else ""
    return (
        "---\n"
        f"title: {title}\n"
        "date: 2026-08-29\n"
        "type: resource\n"
        "status: active\n"
        f"source: {source}\n"
        f"{doi_line}"
        "tags: [article]\n"
        "---\n\n"
        f"# {title}\n"
    )


def test_doi_match_is_case_insensitive_and_has_priority(tmp_path: Path) -> None:
    _write_note(
        tmp_path,
        "30-resources/a.md",
        _article_note(
            "First title",
            "https://example.com/first",
            doi="10.1234/ABC.Def",
        ),
    )
    _write_note(
        tmp_path,
        "30-resources/b.md",
        _article_note(
            "Different title",
            "https://other.example/paper",
            doi="https://doi.org/10.1234/abc.def",
        ),
    )

    result = audit_article_records(tmp_path)

    assert result["issues"]["exact_duplicate_groups"] == [
        {
            "match_key": "doi:10.1234/abc.def",
            "paths": ["30-resources/a.md", "30-resources/b.md"],
            "confidence": "exact",
        }
    ]


def test_canonical_url_strips_tracking_parameters(tmp_path: Path) -> None:
    _write_note(
        tmp_path,
        "30-resources/a.md",
        _article_note(
            "Story",
            "https://EXAMPLE.com/story/?utm_source=x&id=42",
        ),
    )
    _write_note(
        tmp_path,
        "30-resources/b.md",
        _article_note(
            "Story copy",
            "https://example.com/story?id=42&utm_medium=email",
        ),
    )

    result = audit_article_records(tmp_path)

    assert result["issues"]["exact_duplicate_groups"] == [
        {
            "match_key": "url:https://example.com/story?id=42",
            "paths": ["30-resources/a.md", "30-resources/b.md"],
            "confidence": "exact",
        }
    ]


def test_normalized_title_and_non_url_source_match(tmp_path: Path) -> None:
    _write_note(
        tmp_path,
        "30-resources/a.md",
        _article_note("A Useful Article!", "Example Weekly"),
    )
    _write_note(
        tmp_path,
        "30-resources/b.md",
        _article_note("  a useful article  ", "example weekly"),
    )

    result = audit_article_records(tmp_path)

    assert result["issues"]["exact_duplicate_groups"] == [
        {
            "match_key": "title_source:a useful article|example weekly",
            "paths": ["30-resources/a.md", "30-resources/b.md"],
            "confidence": "exact",
        }
    ]


def test_similar_titles_are_not_exact_duplicates(tmp_path: Path) -> None:
    _write_note(
        tmp_path,
        "30-resources/a.md",
        _article_note("TGF beta drives fibrosis", "Journal A"),
    )
    _write_note(
        tmp_path,
        "30-resources/b.md",
        _article_note("TGF beta reduces fibrosis", "Journal B"),
    )

    result = audit_article_records(tmp_path)

    assert result["issues"]["exact_duplicate_groups"] == []

def test_wikilink_audit_handles_missing_heading_alias_and_embeds(
    tmp_path: Path,
) -> None:
    _write_note(
        tmp_path,
        "30-resources/existing.md",
        _article_note("Existing", "https://example.com/existing"),
    )
    linker = _article_note("Linker", "https://example.com/linker") + """
## Local heading

[[30-resources/existing]]
[[30-resources/existing#Section|Readable alias]]
[[30-resources/missing]]
[[#Local heading]]
![[figures/missing-image.png]]
[External](https://example.com/outside)
"""
    _write_note(tmp_path, "30-resources/linker.md", linker)

    result = audit_article_records(tmp_path)

    assert result["issues"]["broken_wikilinks"] == [
        {
            "path": "30-resources/linker.md",
            "target": "30-resources/missing",
        }
    ]


def test_inbox_article_is_overdue_after_seven_days(tmp_path: Path) -> None:
    old_note = _article_note("Old inbox article", "https://example.com/old").replace(
        "date: 2026-08-29",
        "date: 2026-08-01",
    )
    recent_note = _article_note(
        "Recent inbox article",
        "https://example.com/recent",
    ).replace("date: 2026-08-29", "date: 2026-08-25")
    _write_note(tmp_path, "00-inbox/old.md", old_note)
    _write_note(tmp_path, "00-inbox/recent.md", recent_note)

    result = audit_article_records(
        tmp_path,
        now=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )

    assert result["issues"]["overdue_inbox"] == [
        {"path": "00-inbox/old.md", "age_days": 28}
    ]


def _sync_state_note(
    source: str,
    last_synced_at: str,
    pending: int,
) -> str:
    return (
        "---\n"
        f"title: {source} sync state\n"
        "date: 2026-08-29\n"
        "type: sync_state\n"
        "status: active\n"
        f"source: {source}\n"
        f"last_synced_at: {last_synced_at}\n"
        f"pending: {pending}\n"
        "---\n"
    )


def test_source_state_reports_pending_stale_and_missing(tmp_path: Path) -> None:
    _write_note(
        tmp_path,
        "memory/x-state.md",
        _sync_state_note("x-bookmarks", "2026-08-28T00:00:00+00:00", 2),
    )
    _write_note(
        tmp_path,
        "memory/threads-state.md",
        _sync_state_note("threads-bookmarks", "2026-08-01T00:00:00+00:00", 0),
    )

    result = audit_article_records(
        tmp_path,
        scope="social",
        stale_after_days=8,
        now=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )

    by_source = {
        issue["source"]: issue for issue in result["issues"]["stale_sources"]
    }
    assert by_source == {
        "github-stars": {
            "source": "github-stars",
            "path": None,
            "status": "missing",
            "last_synced_at": None,
            "age_days": None,
            "pending": None,
        },
        "threads-bookmarks": {
            "source": "threads-bookmarks",
            "path": "memory/threads-state.md",
            "status": "stale",
            "last_synced_at": "2026-08-01T00:00:00+00:00",
            "age_days": 28,
            "pending": 0,
        },
        "x-bookmarks": {
            "source": "x-bookmarks",
            "path": "memory/x-state.md",
            "status": "pending",
            "last_synced_at": "2026-08-28T00:00:00+00:00",
            "age_days": 1,
            "pending": 2,
        },
    }
