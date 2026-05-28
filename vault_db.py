"""
vault_db.py — DuckDB L2 index for second-brain vault.

DB location: ~/.second-brain/vault.db  (local only, NOT synced to Google Drive)
Rebuild anytime with sync_all(vault_path).
"""

import hashlib
import json
import re
from datetime import date
from pathlib import Path

import duckdb

DB_PATH = Path.home() / ".second-brain" / "vault.db"

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS figures_id_seq START 1;

CREATE TABLE IF NOT EXISTS notes (
    path              TEXT PRIMARY KEY,
    title             TEXT,
    note_type         TEXT,
    status            TEXT,
    tags              TEXT,
    note_date         DATE,
    content_hash      TEXT,
    access_count      INTEGER DEFAULT 0,
    last_accessed     TIMESTAMP,
    created_at        TIMESTAMP DEFAULT current_timestamp,
    body_snippet      TEXT,
    snapshot_path     TEXT,
    snapshot_tier     TEXT,
    snapshot_token_est INTEGER
);

CREATE TABLE IF NOT EXISTS figures (
    id            INTEGER PRIMARY KEY DEFAULT nextval('figures_id_seq'),
    note_path     TEXT NOT NULL,
    fig_index     INTEGER,
    image_url     TEXT,
    local_path    TEXT,
    ocr_text      TEXT,
    description   TEXT,
    token_est     INTEGER,
    created_at    TIMESTAMP DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS idx_last_accessed ON notes(last_accessed DESC);
CREATE INDEX IF NOT EXISTS idx_note_date     ON notes(note_date DESC);
CREATE INDEX IF NOT EXISTS idx_figures_note  ON figures(note_path);
"""

# Migration: add snapshot columns if they don't exist yet
_MIGRATIONS = [
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_path TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_tier TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_token_est INTEGER",
]

FTS_SETUP = """
INSTALL fts;
LOAD fts;
PRAGMA create_fts_index(
    'notes', 'path',
    'title', 'tags', 'body_snippet',
    overwrite = 1
);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_schema_applied = False


def _connect() -> duckdb.DuckDBPyConnection:
    global _schema_applied
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    if not _schema_applied:
        con.execute(SCHEMA)
        for migration in _MIGRATIONS:
            try:
                con.execute(migration)
            except Exception:
                pass
        _schema_applied = True
    return con


def _ensure_fts(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("INSTALL fts; LOAD fts;")
        con.execute(
            "PRAGMA create_fts_index('notes','path','title','tags','body_snippet', overwrite=1);"
        )
    except Exception:
        pass  # FTS index already exists or extension unavailable


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict:
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


def _body_snippet(text: str, max_chars: int = 500) -> str:
    body = FRONTMATTER_RE.sub("", text).strip()
    return body[:max_chars]


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _parse_date(val: str) -> date | None:
    try:
        return date.fromisoformat(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def upsert_note(con: duckdb.DuckDBPyConnection, vault: Path, md_file: Path) -> None:
    """Insert or update a note row from its markdown file."""
    text = md_file.read_text(encoding="utf-8", errors="ignore")
    fm = _parse_frontmatter(text)
    rel = str(md_file.relative_to(vault))
    chash = _content_hash(text)

    # Skip if unchanged
    row = con.execute("SELECT content_hash FROM notes WHERE path = ?", [rel]).fetchone()
    if row and row[0] == chash:
        return

    tags_raw = fm.get("tags", "[]")
    tags_json = tags_raw if tags_raw.startswith("[") else json.dumps([tags_raw])

    con.execute(
        """
        INSERT INTO notes (path, title, note_type, status, tags, note_date,
                           content_hash, body_snippet, last_accessed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
        ON CONFLICT (path) DO UPDATE SET
            title              = excluded.title,
            note_type          = excluded.note_type,
            status             = excluded.status,
            tags               = excluded.tags,
            note_date          = excluded.note_date,
            content_hash       = excluded.content_hash,
            body_snippet       = excluded.body_snippet
            -- snapshot fields are intentionally NOT updated here;
            -- they are managed by update_snapshot() after rendering
        """,
        [
            rel,
            fm.get("title", md_file.stem),
            fm.get("type", "note"),
            fm.get("status", "active"),
            tags_json,
            _parse_date(fm.get("date", "")),
            chash,
            _body_snippet(text),
        ],
    )


def record_access(path: str) -> None:
    """Increment access_count and update last_accessed for a note."""
    with _connect() as con:
        con.execute(
            """
            UPDATE notes
            SET access_count  = access_count + 1,
                last_accessed = current_timestamp
            WHERE path = ?
            """,
            [path],
        )


def sync_all(vault: Path) -> int:
    """Scan all .md files in vault and upsert into DB. Returns count synced."""
    con = _connect()
    count = 0
    for md_file in vault.rglob("*.md"):
        if any(p in md_file.parts for p in (".obsidian", ".claude", "templates")):
            continue
        upsert_note(con, vault, md_file)
        count += 1
    _ensure_fts(con)
    con.close()
    return count


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def fts_search(query: str, limit: int = 20) -> list[dict]:
    """Full-text search using DuckDB FTS. Falls back to LIKE if FTS unavailable."""
    with _connect() as con:
        try:
            _ensure_fts(con)
            rows = con.execute(
                """
                SELECT path, title, score
                FROM (
                    SELECT *, fts_main_notes.match_bm25(path, ?) AS score
                    FROM notes
                ) t
                WHERE score IS NOT NULL
                ORDER BY score DESC
                LIMIT ?
                """,
                [query, limit],
            ).fetchall()
            return [{"path": r[0], "title": r[1], "score": r[2]} for r in rows]
        except Exception:
            # Fallback: simple LIKE search
            q = f"%{query.lower()}%"
            rows = con.execute(
                """
                SELECT path, title, 1.0 AS score
                FROM notes
                WHERE lower(title) LIKE ?
                   OR lower(tags)  LIKE ?
                   OR lower(body_snippet) LIKE ?
                ORDER BY last_accessed DESC
                LIMIT ?
                """,
                [q, q, q, limit],
            ).fetchall()
            return [{"path": r[0], "title": r[1], "score": r[2]} for r in rows]


def top_by_recency(limit: int = 20) -> list[dict]:
    """Return top notes by last_accessed for get_context() (Phase 1)."""
    with _connect() as con:
        rows = con.execute(
            """
            SELECT path, title, note_type, last_accessed
            FROM notes
            WHERE status != 'archived'
            ORDER BY last_accessed DESC NULLS LAST,
                     note_date DESC NULLS LAST
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            {"path": r[0], "title": r[1], "type": r[2], "last_accessed": str(r[3])}
            for r in rows
        ]


def top_by_score(limit: int = 20) -> list[dict]:
    """Return top notes by Ebbinghaus score (Phase 2). Falls back to recency."""
    with _connect() as con:
        rows = con.execute(
            """
            SELECT path, title, note_type,
                   (access_count + 1.0) /
                   (1.0 + ln(GREATEST(date_diff('day', COALESCE(note_date, current_date), current_date), 1) + 1))
                   AS score
            FROM notes
            WHERE status != 'archived'
            ORDER BY score DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            {"path": r[0], "title": r[1], "type": r[2], "score": round(r[3], 4)}
            for r in rows
        ]


def sleep_candidates(min_age_days: int = 90, max_score: float = 0.5) -> list[dict]:
    """Return notes eligible for Vault Sleep consolidation (Phase 3)."""
    with _connect() as con:
        rows = con.execute(
            """
            SELECT path, title, age_days, score FROM (
                SELECT path, title,
                       date_diff('day', COALESCE(note_date, current_date), current_date) AS age_days,
                       (access_count + 1.0) /
                       (1.0 + ln(GREATEST(date_diff('day', COALESCE(note_date, current_date), current_date), 1) + 1))
                       AS score
                FROM notes
                WHERE status NOT IN ('archived', 'deprecated')
                  AND date_diff('day', COALESCE(note_date, current_date), current_date) >= ?
            ) t
            WHERE score <= ?
            ORDER BY score ASC
            """,
            [min_age_days, max_score],
        ).fetchall()
        return [
            {"path": r[0], "title": r[1], "age_days": r[2], "score": round(r[3], 4)}
            for r in rows
        ]


def update_snapshot(path: str, snapshot_path: str, tier: str, token_est: int) -> None:
    """Update snapshot fields for an existing note."""
    with _connect() as con:
        con.execute(
            """UPDATE notes
               SET snapshot_path=?, snapshot_tier=?, snapshot_token_est=?
               WHERE path=?""",
            [snapshot_path, tier, token_est, path],
        )


def upsert_figure(
    note_path: str,
    fig_index: int,
    image_url: str,
    local_path: str,
    ocr_text: str,
    description: str,
    token_est: int = 0,
) -> None:
    """Insert or update a figure record."""
    with _connect() as con:
        existing = con.execute(
            "SELECT id FROM figures WHERE note_path = ? AND fig_index = ?",
            [note_path, fig_index],
        ).fetchone()
        if existing:
            con.execute(
                """UPDATE figures SET image_url=?, local_path=?, ocr_text=?,
                   description=?, token_est=? WHERE id=?""",
                [image_url, local_path, ocr_text, description, token_est, existing[0]],
            )
        else:
            con.execute(
                """INSERT INTO figures
                   (note_path, fig_index, image_url, local_path, ocr_text, description, token_est)
                   VALUES (?,?,?,?,?,?,?)""",
                [note_path, fig_index, image_url, local_path, ocr_text, description, token_est],
            )


def search_figures(query: str, limit: int = 10) -> list[dict]:
    """Search figures by OCR text or description."""
    q = f"%{query.lower()}%"
    with _connect() as con:
        rows = con.execute(
            """SELECT note_path, fig_index, image_url, ocr_text, description
               FROM figures
               WHERE lower(ocr_text) LIKE ? OR lower(description) LIKE ?
               ORDER BY note_path
               LIMIT ?""",
            [q, q, limit],
        ).fetchall()
        return [
            {"note_path": r[0], "fig_index": r[1], "image_url": r[2],
             "ocr_text": r[3], "description": r[4]}
            for r in rows
        ]


def db_stats() -> dict:
    """Return summary statistics about the vault index."""
    with _connect() as con:
        total = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        by_type = con.execute(
            "SELECT note_type, COUNT(*) FROM notes GROUP BY note_type ORDER BY 2 DESC"
        ).fetchall()
        return {
            "total_notes": total,
            "by_type": {r[0]: r[1] for r in by_type},
            "db_path": str(DB_PATH),
        }
