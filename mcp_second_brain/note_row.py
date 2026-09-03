"""NoteRow — what one markdown note looks like inside the index.

This is the projection ``markdown file → indexable row``, and it is pure domain
knowledge: which frontmatter fields matter, how the cnyes_archive ticker special
case works, what text feeds the embedding, how the three legal spellings of
``semantic_keywords`` are tolerated.

It used to live twice — once in ``vault_db.upsert_note`` (DuckDB) and once in
``postgres_store._upsert_note_row`` — as ~70 lines of line-for-line twins whose
only real difference was the SQL placeholder style. That put a piece of domain
knowledge *across* the store seam: adding a frontmatter field meant editing both
backends, and missing one meant the two indexes silently disagreed.

The seam belongs at "how it is stored". Each store now binds a ``NoteRow`` into
its own SQL and owns nothing else about what a note is.

Everything here is I/O-free except ``project_note``, which reads the file and
calls the embedding server; the parsing helpers below are pure and testable
without a database.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .snippets import strip_references

__all__ = [
    "NoteRow",
    "project_note",
    "parse_frontmatter",
    "body_snippet",
    "embed_text_for",
    "content_hash",
    "parse_date",
    "normalise_keyword_list",
    "FRONTMATTER_RE",
]

# Read only the first 40 KB of files over 32 KB — Drive I/O optimisation (still
# skips the 4-5 MB proceedings outliers) while leaving headroom above
# embed_text_for's own max_chars cap (see Phase B-0 note below). The hash
# still covers the whole file, so an edit past the cut-off still triggers a reindex.
#
# 2026-09-03: this used to be 16 KB, well under embed_text_for's old 900-char
# cap. Raising max_chars to ~32,000 without raising this made the new cap a
# no-op for any note over 32 KB on disk — which is most research notes (median
# note is 81,074 chars). Both numbers have to move together or this silently
# regresses back to the old ceiling on the next edit-triggered resync.
LARGE_FILE_READ_LIMIT = 40 * 1024
LARGE_FILE_THRESHOLD = 32 * 1024

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]{1,80}`")
_URL_RE = re.compile(r"https?://\S+")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


# ---------------------------------------------------------------------------
# Pure parsing helpers
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> dict:
    """Flat key → string map from the leading ``---`` block. No block, no keys."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def body_snippet(text: str, max_chars: int = 500) -> str:
    body = FRONTMATTER_RE.sub("", text).strip()
    return body[:max_chars]


def embed_text_for(text: str, max_chars: int = 32_000) -> str:
    """Prepare text for embedding: drop references, strip code/URLs/fullwidth chars.

    max_chars defaults to ~8,192 tokens (bge-m3's context, ~4 chars/token) —
    see 10-projects/second-brain/phases/second-brain-分塊-embedding-與-late-chunking-實施計畫.md
    Phase B-0. References are stripped first (they are the cited papers' claims,
    not this note's own — see snippets.strip_references) so the char budget goes
    to the note's own prose instead of its bibliography.

    Three known llama-server crash triggers:
    1. URLs with query strings (?param=val)
    2. Fullwidth Unicode punctuation (U+FF00-FFEF, e.g. （）：)
    3. Very long code blocks with shell special chars
    """
    body = FRONTMATTER_RE.sub("", text).strip()
    body = strip_references(body)
    body = _CODE_BLOCK_RE.sub(" ", body)
    body = _INLINE_CODE_RE.sub(" ", body)
    body = _URL_RE.sub(" ", body)
    body = _MD_LINK_RE.sub(r"\1", body)
    # Keep only: ASCII printable (0x20-0x7E) + CJK Unified (U+4E00–U+9FFF) + newlines
    body = "".join(
        c if (0x20 <= ord(c) <= 0x7E) or (0x4E00 <= ord(c) <= 0x9FFF) or c in "\n\t"
        else " "
        for c in body
    )
    body = re.sub(r"\s{3,}", "\n\n", body)
    return body[:max_chars]


def content_hash(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def parse_date(val: str) -> date | None:
    try:
        return date.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def normalise_keyword_list(raw: object) -> str | None:
    """Frontmatter keyword field → JSON array string, or None when absent.

    Tolerates the three spellings that exist in the vault, in order of how they
    got there: a real JSON array, a bracketed-but-unquoted list (hand-edited), and
    a bare comma-separated string. A malformed bracketed value degrades to the
    comma split rather than being dropped.
    """
    if isinstance(raw, list):
        return json.dumps(raw, ensure_ascii=False) if raw else None

    text = str(raw or "").strip()
    if not text:
        return None

    if text.startswith("["):
        try:
            return json.dumps(json.loads(text), ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            inner = text.strip("[]")
            items = [s.strip().strip('"').strip("'") for s in inner.split(",") if s.strip()]
            return json.dumps(items, ensure_ascii=False) if items else None

    items = [s.strip() for s in text.split(",") if s.strip()]
    return json.dumps(items, ensure_ascii=False) if items else None


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NoteRow:
    """One note as the index sees it. Stores bind these fields into their own SQL."""

    path: str                       # vault-relative, the primary key
    title: str
    note_type: str
    status: str
    tags_json: str
    note_date: date | None
    content_hash: str
    body_snippet: str
    embedding: list[float] | None   # None when the embedding server was unavailable
    violations_json: str | None
    semantic_keywords: str | None
    neighbor_keywords: str | None
    cluster_topic: str | None


def project_note(
    vault: Path,
    md_file: Path,
    *,
    embed=None,
    validate=None,
    log_prefix: str = "note_row",
) -> NoteRow:
    """Read one markdown file and project it into a NoteRow.

    Args:
        vault: vault root, used to derive the relative path.
        md_file: the file to project.
        embed: ``text -> list[float] | None``; the embedding call is injected so
            the projection can be tested without an embedding server.
        validate: ``(frontmatter, rel) -> list[str]`` schema check.
        log_prefix: tag for the stderr warnings, so the caller stays identifiable.

    An embedding failure is recorded as ``embedding=None`` and warned about — it
    never aborts the projection, because a note missing its vector is still worth
    indexing for keyword search.
    """
    if md_file.stat().st_size > LARGE_FILE_THRESHOLD:
        raw = md_file.read_bytes()
        chash = content_hash(raw.decode("utf-8", errors="ignore"))
        text = raw[:LARGE_FILE_READ_LIMIT].decode("utf-8", errors="ignore")
    else:
        text = md_file.read_text(encoding="utf-8", errors="ignore")
        chash = content_hash(text)

    fm = parse_frontmatter(text)
    rel = str(md_file.relative_to(vault))

    tags_raw = fm.get("tags", "[]")
    tags_json = tags_raw if tags_raw.startswith("[") else json.dumps([tags_raw])

    # cnyes_archive bodies open with a US market table; the stock codes appear far
    # below the snippet cut-off, so prepend the tickers or FTS can never find them.
    if fm.get("type") == "cnyes_archive":
        tickers_raw = fm.get("tickers", "[]")
        try:
            tickers_str = " ".join(json.loads(tickers_raw))
        except Exception:
            tickers_str = tickers_raw
        snippet = (tickers_str + " " + body_snippet(text, max_chars=400))[:500]
    else:
        snippet = body_snippet(text)

    vec: list[float] | None = None
    if embed is not None:
        embed_input = f"{fm.get('title', md_file.stem)} {fm.get('tags', '')} {embed_text_for(text)}".strip()
        try:
            vec = embed(embed_input)
            if vec is None:
                print(f"[{log_prefix}] embedding failed: {rel}", file=sys.stderr)
        except ValueError as e:
            print(f"[{log_prefix}] embedding dim error: {rel} — {e}", file=sys.stderr)
            vec = None

    violations = validate(fm, rel) if validate is not None else []

    return NoteRow(
        path=rel,
        title=fm.get("title", md_file.stem),
        note_type=fm.get("type", "note"),
        status=fm.get("status", "active"),
        tags_json=tags_json,
        note_date=parse_date(fm.get("date", "")),
        content_hash=chash,
        body_snippet=snippet,
        embedding=vec,
        violations_json=json.dumps(violations) if violations else None,
        semantic_keywords=normalise_keyword_list(fm.get("semantic_keywords", "")),
        neighbor_keywords=normalise_keyword_list(fm.get("neighbor_keywords", "")),
        cluster_topic=fm.get("cluster_topic", None) or None,
    )
