"""Read-only article record auditing for a Second Brain vault.

The module owns filesystem inspection only. It never updates markdown, state files,
or an index, which keeps it safe to reuse from MCP tools and maintenance CLIs.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from .note_row import parse_frontmatter

__all__ = [
    "audit_article_records",
    "find_naming_violations",
    "find_overdue_inbox",
    "missing_required_frontmatter",
]

AuditScope = Literal["articles", "social", "all"]

_ISSUE_KEYS = (
    "missing_frontmatter",
    "broken_wikilinks",
    "exact_duplicate_groups",
    "overdue_inbox",
    "stale_sources",
)
_REQUIRED_FRONTMATTER = ("date", "status", "title", "type")
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
_EXPECTED_SOCIAL_SOURCES = (
    "github-stars",
    "threads-bookmarks",
    "x-bookmarks",
)
_SOURCE_ALIASES = {
    "github": "github-stars",
    "github-star": "github-stars",
    "github-stars": "github-stars",
    "threads": "threads-bookmarks",
    "threads-bookmark": "threads-bookmarks",
    "threads-bookmarks": "threads-bookmarks",
    "twitter": "x-bookmarks",
    "x": "x-bookmarks",
    "x-bookmark": "x-bookmarks",
    "x-bookmarks": "x-bookmarks",
}
_INBOX_OVERDUE_DAYS = 7


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
    if frontmatter.get("type", "").strip().lower() == "sync_state":
        return False
    return (
        relative_path.startswith("30-resources/")
        or _is_research_note(relative_path, frontmatter)
        or bool(frontmatter.get("source", "").strip())
    )


def _empty_issues() -> dict[str, list[dict]]:
    return {key: [] for key in _ISSUE_KEYS}

def missing_required_frontmatter(
    frontmatter: dict[str, str],
    required: Iterable[str] = _REQUIRED_FRONTMATTER,
) -> list[str]:
    return sorted(field for field in required if not frontmatter.get(field))


def find_overdue_inbox(
    vault: Path | str,
    *,
    now: datetime | None = None,
    overdue_days: int = _INBOX_OVERDUE_DAYS,
    prefer_frontmatter_date: bool = True,
    article_only: bool = False,
) -> list[dict]:
    root = Path(vault).expanduser().resolve()
    inbox = root / "00-inbox"
    if not inbox.is_dir():
        return []

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)

    overdue: list[dict] = []
    for candidate in sorted(inbox.glob("*.md")):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(root) or not resolved.is_file():
            continue

        relative_path = candidate.relative_to(root).as_posix()
        frontmatter: dict[str, str] = {}
        if prefer_frontmatter_date or article_only:
            text = resolved.read_text(encoding="utf-8", errors="ignore")
            frontmatter = parse_frontmatter(text)
        if article_only and not _is_article_note(relative_path, frontmatter):
            continue

        note_time = (
            _parse_datetime(frontmatter.get("date", ""))
            if prefer_frontmatter_date
            else None
        )
        if note_time is None:
            note_time = datetime.fromtimestamp(
                resolved.stat().st_mtime,
                tz=timezone.utc,
            )
        age_days = max(0, (current_time - note_time).days)
        if age_days > overdue_days:
            overdue.append({"path": relative_path, "age_days": age_days})
    return overdue


def find_naming_violations(
    directory: Path | str,
    patterns: Iterable[str],
) -> list[dict]:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        return []

    violations: list[dict] = []
    for candidate in sorted(root.glob("*.md")):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(root) or not resolved.is_file():
            continue
        for pattern in patterns:
            if re.match(pattern, candidate.name):
                violations.append({"path": candidate.name, "pattern": pattern})
    return violations


def _normalize_doi(value: str) -> str | None:
    match = _DOI_RE.search(value.strip())
    if not match:
        return None
    return match.group(0).rstrip(".,;").casefold()


def _canonical_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None

    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    query_items = []
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, item_value))
    query = urlencode(sorted(query_items))

    return urlunsplit((scheme, host, path, query, ""))


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("_", " ")
    return " ".join(re.sub(r"[^\w\s]", " ", normalized).split())


def _duplicate_match_key(frontmatter: dict[str, str]) -> str | None:
    source = frontmatter.get("source", "").strip()
    doi = _normalize_doi(frontmatter.get("doi", "")) or _normalize_doi(source)
    if doi:
        return f"doi:{doi}"

    canonical_url = _canonical_url(source)
    if canonical_url:
        return f"url:{canonical_url}"

    title = _normalize_text(frontmatter.get("title", ""))
    normalized_source = _normalize_text(source)
    if title and normalized_source:
        return f"title_source:{title}|{normalized_source}"
    return None


def _normalize_wikilink_target(raw_target: str) -> str | None:
    target = raw_target.split("|", 1)[0].strip()
    if not target or target.startswith("#"):
        return None
    target = target.split("#", 1)[0].strip().replace("\\", "/")
    if not target or "://" in target:
        return None
    if target.casefold().endswith(".md"):
        target = target[:-3]
    return target.strip("/")


def _find_broken_wikilinks(
    text: str,
    *,
    source_path: str,
    known_paths: set[str],
    known_stems: set[str],
) -> list[dict]:
    broken: list[dict] = []
    seen: set[str] = set()
    for match in _WIKILINK_RE.finditer(text):
        target = _normalize_wikilink_target(match.group(1))
        if not target or target in seen:
            continue
        seen.add(target)
        exists = target in known_paths
        if "/" not in target:
            exists = exists or target in known_stems
        if not exists:
            broken.append({"path": source_path, "target": target})
    return broken


def _parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_source_id(value: str) -> str | None:
    return _SOURCE_ALIASES.get(value.strip().casefold())


def _pending_count(value: str) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def audit_article_records(
    vault: Path | str,
    *,
    scope: AuditScope = "all",
    limit: int = 100,
    stale_after_days: int = 8,
    indexed_notes: int | None = None,
    vault_backend: str = "filesystem",
    vault_id: str | None = None,
    now: datetime | None = None,
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

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)

    markdown_files, warnings = _markdown_files(root)
    known_paths = {
        path.relative_to(root).as_posix()[:-3] for path in markdown_files
    }
    known_stems = {path.stem for path in markdown_files}
    missing_frontmatter: list[dict] = []
    broken_wikilinks: list[dict] = []
    overdue_inbox: list[dict] = []
    source_states: dict[str, dict] = {}
    article_notes = 0
    research_notes = 0
    social_notes = 0
    duplicate_paths: dict[str, list[str]] = defaultdict(list)

    for markdown_file in markdown_files:
        relative_path = markdown_file.relative_to(root).as_posix()
        text = markdown_file.read_text(encoding="utf-8", errors="ignore")
        frontmatter = parse_frontmatter(text)

        is_research = _is_research_note(relative_path, frontmatter)
        is_social = _is_social_note(relative_path, frontmatter)
        is_article = _is_article_note(relative_path, frontmatter)

        if frontmatter.get("type", "").strip().lower() == "sync_state":
            source_id = _canonical_source_id(frontmatter.get("source", ""))
            if source_id:
                source_states[source_id] = {
                    "path": relative_path,
                    "last_synced_at": frontmatter.get("last_synced_at", ""),
                    "pending": frontmatter.get("pending", ""),
                }

        article_notes += int(is_article)
        research_notes += int(is_research)
        social_notes += int(is_social)

        if is_article and scope in {"articles", "all"}:
            missing = missing_required_frontmatter(frontmatter)
            if missing:
                missing_frontmatter.append(
                    {"path": relative_path, "missing_fields": missing}
                )
            match_key = _duplicate_match_key(frontmatter)
            if match_key:
                duplicate_paths[match_key].append(relative_path)
            broken_wikilinks.extend(
                _find_broken_wikilinks(
                    text,
                    source_path=relative_path,
                    known_paths=known_paths,
                    known_stems=known_stems,
                )
            )

    if scope in {"articles", "all"}:
        overdue_inbox = find_overdue_inbox(
            root,
            now=current_time,
            overdue_days=_INBOX_OVERDUE_DAYS,
            prefer_frontmatter_date=True,
            article_only=True,
        )
    exact_duplicate_groups = [
        {
            "match_key": match_key,
            "paths": sorted(paths),
            "confidence": "exact",
        }
        for match_key, paths in sorted(duplicate_paths.items())
        if len(paths) > 1
    ]

    stale_sources: list[dict] = []
    should_check_missing_sources = scope == "social" or (
        scope == "all" and bool(source_states)
    )
    if should_check_missing_sources:
        for source_id in _EXPECTED_SOCIAL_SOURCES:
            state = source_states.get(source_id)
            if state is None:
                stale_sources.append(
                    {
                        "source": source_id,
                        "path": None,
                        "status": "missing",
                        "last_synced_at": None,
                        "age_days": None,
                        "pending": None,
                    }
                )
                continue

            last_synced_raw = state["last_synced_at"]
            last_synced = _parse_datetime(last_synced_raw)
            pending = _pending_count(state["pending"])
            if last_synced is None or pending is None:
                warnings.append(
                    f"Invalid sync state fields in {state['path']} for {source_id}."
                )
                stale_sources.append(
                    {
                        "source": source_id,
                        "path": state["path"],
                        "status": "invalid",
                        "last_synced_at": last_synced_raw or None,
                        "age_days": None,
                        "pending": pending,
                    }
                )
                continue

            age_days = max(0, (current_time - last_synced).days)
            status = (
                "stale"
                if age_days >= stale_after_days
                else "pending"
                if pending > 0
                else "fresh"
            )
            if status != "fresh":
                stale_sources.append(
                    {
                        "source": source_id,
                        "path": state["path"],
                        "status": status,
                        "last_synced_at": last_synced_raw,
                        "age_days": age_days,
                        "pending": pending,
                    }
                )

    safe_indexed_notes = (
        len(markdown_files) if indexed_notes is None else max(0, int(indexed_notes))
    )
    issues = _empty_issues()
    issues["missing_frontmatter"] = missing_frontmatter[:limit]
    issues["broken_wikilinks"] = broken_wikilinks[:limit]
    issues["exact_duplicate_groups"] = exact_duplicate_groups[:limit]
    issues["overdue_inbox"] = overdue_inbox[:limit]
    issues["stale_sources"] = stale_sources[:limit]
    totals = {key: 0 for key in _ISSUE_KEYS}
    totals["missing_frontmatter"] = len(missing_frontmatter)
    totals["broken_wikilinks"] = len(broken_wikilinks)
    totals["exact_duplicate_groups"] = len(exact_duplicate_groups)
    totals["overdue_inbox"] = len(overdue_inbox)
    totals["stale_sources"] = len(stale_sources)
    truncated = any(
        total > limit for total in totals.values()
    )

    recommended_actions: list[str] = []
    if missing_frontmatter:
        recommended_actions.append(
            "Review the reported notes and add the required frontmatter fields."
        )
    if exact_duplicate_groups:
        recommended_actions.append(
            "Review exact duplicate candidates manually before any merge or deletion."
        )
    if broken_wikilinks:
        recommended_actions.append(
            "Review broken wikilinks and correct their vault-relative targets."
        )
    if overdue_inbox:
        recommended_actions.append(
            "Review overdue inbox articles and route them without automatic deletion."
        )
    if stale_sources:
        recommended_actions.append(
            "Review stale, pending, missing, or invalid social source state."
        )

    return {
        "run_id": str(uuid4()),
        "generated_at": current_time.isoformat(),
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
