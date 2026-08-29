"""Read-only article record auditing for a Second Brain vault.

The module owns filesystem inspection only. It never updates markdown, state files,
or an index, which keeps it safe to reuse from MCP tools and maintenance CLIs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .note_row import parse_frontmatter

__all__ = ["audit_article_records"]

AuditScope = Literal["articles", "social", "all"]

_ISSUE_KEYS = (
    "missing_frontmatter",
    "broken_wikilinks",
    "exact_duplicate_groups",
    "overdue_inbox",
    "stale_sources",
)
_REQUIRED_FRONTMATTER = ("date", "status", "title", "type")


def _markdown_files(vault: Path) -> tuple[list[Path], list[str]]:
    """Return safe markdown files and warnings for paths that were skipped."""
    root = vault.resolve()
    files: list[Path] = []
    warnings: list[str] = []

    for candidate in root.rglob("*.md"):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            relative = candidate.relative_to(root).as_posix()
            warnings.append(f"Skipped unreadable markdown path {relative}: {exc}")
            continue

        if not resolved.is_relative_to(root):
            relative = candidate.relative_to(root).as_posix()
            warnings.append(f"Skipped markdown symlink outside vault: {relative}")
            continue
        if resolved.is_file():
            files.append(candidate)

    return sorted(files, key=lambda path: path.relative_to(root).as_posix()), warnings


def _is_research_note(relative_path: str, frontmatter: dict[str, str]) -> bool:
    note_type = frontmatter.get("type", "").strip().lower()
    return relative_path.startswith("20-areas/research/") or note_type in {
        "paper",
        "research",
    }


def _is_social_note(relative_path: str, frontmatter: dict[str, str]) -> bool:
    note_type = frontmatter.get("type", "").strip().lower()
    tags = frontmatter.get("tags", "").lower()
    name = Path(relative_path).name.lower()
    return (
        note_type in {"social", "bookmark"}
        or any(source in tags for source in ("github-stars", "threads", "x-bookmark"))
        or name.startswith(("github-star-", "threads-", "x-bookmark-"))
    )


def _is_article_note(relative_path: str, frontmatter: dict[str, str]) -> bool:
    return (
        relative_path.startswith("30-resources/")
        or _is_research_note(relative_path, frontmatter)
        or bool(frontmatter.get("source", "").strip())
    )


def _empty_issues() -> dict[str, list[dict]]:
    return {key: [] for key in _ISSUE_KEYS}


def audit_article_records(
    vault: Path | str,
    *,
    scope: AuditScope = "all",
    limit: int = 100,
    stale_after_days: int = 8,
    indexed_notes: int | None = None,
    vault_backend: str = "filesystem",
    vault_id: str | None = None,
) -> dict:
    """Inspect article records under the vault and return a bounded result.

    indexed_notes is injected by the MCP wrapper when a live index is
    available. Standalone callers default it to the number of safe markdown
    files so a filesystem-only audit does not report a fabricated index gap.
    """
    if scope not in {"articles", "social", "all"}:
        raise ValueError("scope must be one of: articles, social, all")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if not 1 <= stale_after_days <= 90:
        raise ValueError("stale_after_days must be between 1 and 90")

    root = Path(vault).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(
            f"Article audit could not read vault {root}. "
            "Verify SECOND_BRAIN_PATH points to an existing directory, then retry."
        )

    markdown_files, warnings = _markdown_files(root)
    missing_frontmatter: list[dict] = []
    article_notes = 0
    research_notes = 0
    social_notes = 0

    for markdown_file in markdown_files:
        relative_path = markdown_file.relative_to(root).as_posix()
        text = markdown_file.read_text(encoding="utf-8", errors="ignore")
        frontmatter = parse_frontmatter(text)

        is_research = _is_research_note(relative_path, frontmatter)
        is_social = _is_social_note(relative_path, frontmatter)
        is_article = _is_article_note(relative_path, frontmatter)

        article_notes += int(is_article)
        research_notes += int(is_research)
        social_notes += int(is_social)

        if is_article and scope in {"articles", "all"}:
            missing = sorted(
                field for field in _REQUIRED_FRONTMATTER if not frontmatter.get(field)
            )
            if missing:
                missing_frontmatter.append(
                    {"path": relative_path, "missing_fields": missing}
                )

    safe_indexed_notes = (
        len(markdown_files) if indexed_notes is None else max(0, int(indexed_notes))
    )
    issues = _empty_issues()
    issues["missing_frontmatter"] = missing_frontmatter[:limit]
    totals = {key: 0 for key in _ISSUE_KEYS}
    totals["missing_frontmatter"] = len(missing_frontmatter)
    truncated = len(missing_frontmatter) > limit

    recommended_actions: list[str] = []
    if missing_frontmatter:
        recommended_actions.append(
            "Review the reported notes and add the required frontmatter fields."
        )

    return {
        "run_id": str(uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "vault": {
            "backend": vault_backend,
            "vault_id": vault_id or root.name,
        },
        "counts": {
            "vault_markdown_files": len(markdown_files),
            "indexed_notes": safe_indexed_notes,
            "article_notes": article_notes,
            "research_notes": research_notes,
            "social_notes": social_notes,
        },
        "index_gap": len(markdown_files) - safe_indexed_notes,
        "issues": issues,
        "totals": totals,
        "truncated": truncated,
        "recommended_actions": recommended_actions,
        "warnings": warnings,
    }
