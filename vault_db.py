"""
vault_db.py — DuckDB L2 index for second-brain vault.

DB location: ~/.second-brain/vault.db  (local only, NOT synced to Google Drive)
Rebuild anytime with sync_all(vault_path).
"""

import hashlib
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
import duckdb

# ---------------------------------------------------------------------------
# Embedding config (Phase 6)
# ---------------------------------------------------------------------------
# Override via environment variables for Ollama compatibility:
#   EMBED_URL=http://localhost:11434/v1/embeddings  (Ollama)
#   EMBED_URL=http://localhost:11435/v1/embeddings  (llama-server, default)
#   EMBED_MODEL=nomic-embed-text  (same model name works for both)

import os as _os

EMBED_PORT = int(_os.environ.get("EMBED_PORT", "11435"))
EMBED_URL = _os.environ.get("EMBED_URL", f"http://localhost:{EMBED_PORT}/v1/embeddings")
EMBED_MODEL = _os.environ.get("EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768

del _os  # keep namespace clean

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

# Migration: add columns incrementally (safe to re-run)
_MIGRATIONS = [
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_path TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_tier TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_token_est INTEGER",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS embedding BLOB",           # Phase 6
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS rules_extracted_at TIMESTAMP",  # Phase 7
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
_schema_lock = threading.Lock()


def _connect() -> duckdb.DuckDBPyConnection:
    global _schema_applied
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    with _schema_lock:
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


_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]{1,80}`")
_URL_RE = re.compile(r"https?://\S+")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _embed_text_for(text: str, max_chars: int = 900) -> str:
    """Prepare text for embedding: strip code/URLs/fullwidth chars, keep prose.

    Three known llama-server crash triggers:
    1. URLs with query strings (?param=val)
    2. Fullwidth Unicode punctuation (U+FF00-FFEF, e.g. （）：)
    3. Very long code blocks with shell special chars
    """
    body = FRONTMATTER_RE.sub("", text).strip()
    body = _CODE_BLOCK_RE.sub(" ", body)
    body = _INLINE_CODE_RE.sub(" ", body)
    body = _URL_RE.sub(" ", body)
    body = _MD_LINK_RE.sub(r"\1", body)
    # Keep only: ASCII printable (0x20-0x7E) + CJK Unified (U+4E00–U+9FFF) + newlines
    # Filters out fullwidth punctuation, math symbols, and other Unicode that crashes llama-server
    body = "".join(
        c if (0x20 <= ord(c) <= 0x7E) or (0x4E00 <= ord(c) <= 0x9FFF) or c in "\n\t"
        else " "
        for c in body
    )
    body = re.sub(r"\s{3,}", "\n\n", body)
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

    # Compute embedding: title + tags + prose body (code blocks stripped to avoid server errors)
    snippet = _body_snippet(text)          # 500 chars for DB / FTS storage
    prose = _embed_text_for(text)          # 1600 chars, code stripped
    tags_for_embed = fm.get("tags", "")
    embed_input = f"{fm.get('title', md_file.stem)} {tags_for_embed} {prose}".strip()
    vec = embed_text(embed_input)
    blob = _vec_to_blob(vec) if vec else None

    con.execute(
        """
        INSERT INTO notes (path, title, note_type, status, tags, note_date,
                           content_hash, body_snippet, last_accessed, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp, ?)
        ON CONFLICT (path) DO UPDATE SET
            title              = excluded.title,
            note_type          = excluded.note_type,
            status             = excluded.status,
            tags               = excluded.tags,
            note_date          = excluded.note_date,
            content_hash       = excluded.content_hash,
            body_snippet       = excluded.body_snippet,
            embedding          = COALESCE(excluded.embedding, notes.embedding)
            -- snapshot fields managed by update_snapshot(), not here
        """,
        [
            rel,
            fm.get("title", md_file.stem),
            fm.get("type", "note"),
            fm.get("status", "active"),
            tags_json,
            _parse_date(fm.get("date", "")),
            chash,
            snippet,
            blob,
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


def sync_embeddings(vault: Path | None = None) -> dict:
    """Backfill embeddings for notes missing them. Safe to re-run.

    Pass vault path to use richer 1800-char content for better semantic quality.
    Without vault, falls back to the stored 500-char body_snippet.
    """
    with _connect() as con:
        rows = con.execute(
            "SELECT path, title, body_snippet, tags FROM notes WHERE embedding IS NULL"
        ).fetchall()

    updated, failed = 0, 0
    for path, title, snippet, tags in rows:
        if vault:
            md_file = vault / path
            if md_file.exists():
                full_text = md_file.read_text(encoding="utf-8", errors="ignore")
                prose = _embed_text_for(full_text)
                text = f"{title or ''} {tags or ''} {prose}".strip()
            else:
                text = f"{title or ''} {snippet or ''}".strip()
        else:
            text = f"{title or ''} {snippet or ''}".strip()
        vec = embed_text(text)
        if vec:
            blob = _vec_to_blob(vec)
            with _connect() as con:
                con.execute("UPDATE notes SET embedding = ? WHERE path = ?", [blob, path])
            updated += 1
        else:
            failed += 1

    return {"updated": updated, "failed": failed, "skipped": len(rows) - updated - failed}


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
# Embedding (Phase 6) — llama.cpp nomic-embed-text via REST
# ---------------------------------------------------------------------------

def _vec_to_blob(vec: list[float]) -> bytes:
    import array as _array
    return _array.array("f", vec).tobytes()


def _blob_to_vec(blob: bytes) -> list[float]:
    import array as _array
    a = _array.array("f")
    a.frombytes(blob)
    return list(a)


_embed_proc: subprocess.Popen | None = None  # lazy-started server process
EMBED_AUTO_START: bool = True  # set False in tests to skip lazy-start


def _call_embed_api(text: str) -> list[float] | None:
    """Call embedding endpoint, retrying with shorter text on 500 (model context limit)."""
    for max_chars in (2048, 1024, 512):
        payload = json.dumps({"input": text[:max_chars], "model": EMBED_MODEL}).encode()
        req = urllib.request.Request(
            EMBED_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())["data"][0]["embedding"]
        except urllib.error.HTTPError as e:
            if e.code == 500:
                continue  # text too long for model context — retry shorter
            return None
        except Exception:
            return None
    return None


def _ensure_embed_server() -> bool:
    """Auto-start llama-server if not running. Only applies to default llama-server port.

    If EMBED_URL points to Ollama (port 11434) or a custom URL, we skip auto-start
    — the user manages their own server.
    """
    global _embed_proc
    try:
        urllib.request.urlopen(f"http://localhost:{EMBED_PORT}/health", timeout=1)
        return True
    except Exception:
        pass

    # Only auto-start for the default llama-server setup
    if EMBED_PORT != 11435:
        return False  # Ollama or custom server — user manages it

    llama = Path.home() / "llama.cpp" / "build" / "bin" / "llama-server"
    model = Path.home() / "nomic-embed-text-v1.5.Q8_0.gguf"
    if not llama.exists() or not model.exists():
        return False

    _embed_proc = subprocess.Popen(
        [str(llama), "-m", str(model),
         "--port", str(EMBED_PORT),
         "--embedding", "--pooling", "mean", "-np", "4", "-c", "2048", "--log-disable"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(15):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://localhost:{EMBED_PORT}/health", timeout=1)
            return True
        except Exception:
            continue
    return False


def embed_text(text: str) -> list[float] | None:
    """Call llama-server embedding endpoint. Auto-starts server if needed.

    Returns None only if model files are missing or server fails to start.
    Server stays running for the lifetime of this process (stopped by OS on exit).
    """
    result = _call_embed_api(text)
    if result is not None:
        return result

    # Server not running — try to auto-start (disabled in tests via EMBED_AUTO_START=False)
    if EMBED_AUTO_START and _ensure_embed_server():
        return _call_embed_api(text)
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb + 1e-9)


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


def semantic_search(query: str, limit: int = 20) -> list[dict]:
    """Vector cosine search via nomic-embed-text. Returns [] if server unavailable."""
    q_vec = embed_text(query)
    if not q_vec:
        return []

    with _connect() as con:
        rows = con.execute(
            "SELECT path, title, embedding FROM notes WHERE embedding IS NOT NULL"
        ).fetchall()

    # SQL already filters NULL; blob is always non-None here
    scored = [(path, title, _cosine(q_vec, _blob_to_vec(blob))) for path, title, blob in rows]
    scored.sort(key=lambda x: x[2], reverse=True)
    return [{"path": p, "title": t, "score": s} for p, t, s in scored[:limit]]


def hybrid_search(query: str, limit: int = 20, alpha: float = 0.5) -> list[dict]:
    """Hybrid BM25 + cosine search. alpha=0.5 weights both equally.

    Falls back to fts_search if embedding server unavailable.
    """
    bm25 = fts_search(query, limit=limit * 2)
    sem = semantic_search(query, limit=limit * 2)

    if not sem:
        return bm25[:limit]

    # Normalise scores to [0,1] then combine
    def _norm(results: list[dict]) -> dict[str, float]:
        if not results:
            return {}
        max_s = max(r["score"] for r in results) or 1.0
        return {r["path"]: r["score"] / max_s for r in results}

    bm25_scores = _norm(bm25)
    sem_scores = _norm(sem)
    all_paths = set(bm25_scores) | set(sem_scores)

    combined = []
    titles = {r["path"]: r["title"] for r in bm25 + sem}
    for path in all_paths:
        score = (1 - alpha) * bm25_scores.get(path, 0.0) + alpha * sem_scores.get(path, 0.0)
        combined.append({"path": path, "title": titles[path], "score": score})

    combined.sort(key=lambda x: x["score"], reverse=True)
    return combined[:limit]


def find_related(path: str, limit: int = 5, threshold: float = 0.7) -> list[str]:
    """Find semantically related notes for a given note path.

    Returns list of vault-relative paths (wikilink format, no extension).
    Returns [] if the note has no embedding or server is unavailable.
    """
    with _connect() as con:
        row = con.execute(
            "SELECT embedding FROM notes WHERE path = ?", [path]
        ).fetchone()

    if not row or not row[0]:
        return []

    q_vec = _blob_to_vec(row[0])

    with _connect() as con:
        rows = con.execute(
            "SELECT path, embedding FROM notes WHERE embedding IS NOT NULL AND path != ?",
            [path]
        ).fetchall()

    # SQL already filters NULL; blob is always non-None here
    scored = []
    for other_path, blob in rows:
        sim = _cosine(q_vec, _blob_to_vec(blob))
        if sim >= threshold:
            scored.append((other_path, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    # Return as wikilink stems (strip .md)
    return [p.removesuffix(".md") for p, _ in scored[:limit]]


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
                   (1.0 + ln(GREATEST(date_diff('day', COALESCE(CAST(last_accessed AS DATE), note_date, current_date), current_date), 1) + 1))
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
                       (1.0 + ln(GREATEST(date_diff('day', COALESCE(CAST(last_accessed AS DATE), note_date, current_date), current_date), 1) + 1))
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
    """Search figures by OCR text or description (all query words must match)."""
    words = query.lower().split()
    if not words:
        return []

    # Each word must appear in description OR ocr_text
    clauses = " AND ".join(
        f"(lower(coalesce(ocr_text,'')) LIKE ? OR lower(coalesce(description,'')) LIKE ?)"
        for _ in words
    )
    params: list = [p for w in words for p in (f"%{w}%", f"%{w}%")]
    params.append(limit)

    with _connect() as con:
        rows = con.execute(
            f"SELECT note_path, fig_index, image_url, ocr_text, description "
            f"FROM figures WHERE {clauses} ORDER BY note_path LIMIT ?",
            params,
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
