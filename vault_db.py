"""
vault_db.py — DuckDB L2 index for second-brain vault.

DB location: ~/.second-brain/vault.db  (local only, NOT synced to Google Drive)
Rebuild anytime with sync_all(vault_path).
"""

import hashlib
import json
import re
import subprocess
import sys
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
del _os  # keep namespace clean

# Note-type exclusion lists (centralised to avoid scattered magic strings)
NEWS_TYPES: list[str] = ["cnyes_archive"]
FINANCE_DAILY_TYPES: list[str] = ["stock_analysis", "daily_briefing", "market_calendar", "dashboard"]
KNOWLEDGE_EXCLUDE: list[str] = NEWS_TYPES + FINANCE_DAILY_TYPES

DB_PATH = Path.home() / ".second-brain" / "vault.db"

_DEFAULT_VAULT_PATH = Path(
    __import__("os").environ.get(
        "SECOND_BRAIN_PATH",
        "~/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/second-brain",
    )
).expanduser()

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
CREATE INDEX IF NOT EXISTS idx_note_type     ON notes(note_type);
CREATE INDEX IF NOT EXISTS idx_status        ON notes(status);
CREATE INDEX IF NOT EXISTS idx_figures_note  ON figures(note_path);
"""

# Migration: add columns incrementally (safe to re-run)
_MIGRATIONS = [
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_path TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_tier TEXT",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS snapshot_token_est INTEGER",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS embedding BLOB",           # Phase 6
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS rules_extracted_at TIMESTAMP",  # Phase 7
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS violations TEXT",               # Phase 10 schema
]

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
                except Exception as e:
                    print(f"[vault_db] migration skipped: {migration!r} → {e}", file=sys.stderr)
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
# Schema validation (Phase 10)
# ---------------------------------------------------------------------------

def _load_vault_schema() -> dict:
    """Load vault-schema.json from vault root. Returns empty dict if missing."""
    schema_path = _DEFAULT_VAULT_PATH / "vault-schema.json"
    if schema_path.exists():
        try:
            return json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[vault_db] vault-schema.json parse error: {e}", file=sys.stderr)
    return {}


_VAULT_SCHEMA: dict = {}
_VAULT_SCHEMA_LOADED = False


def _get_vault_schema() -> dict:
    global _VAULT_SCHEMA, _VAULT_SCHEMA_LOADED
    if not _VAULT_SCHEMA_LOADED:
        _VAULT_SCHEMA = _load_vault_schema()
        _VAULT_SCHEMA_LOADED = True
    return _VAULT_SCHEMA


def validate_note(fm: dict, path: str) -> list[str]:
    """Return list of violation strings. Empty list = passes schema.

    Non-blocking: violations are recorded but never prevent writes.
    """
    schema = _get_vault_schema()
    if not schema:
        return []

    violations: list[str] = []

    # Required fields
    for field in schema.get("frontmatter_required", []):
        if not fm.get(field):
            violations.append(f"missing: {field}")

    # type value check
    type_val = fm.get("type", "")
    valid_types = schema.get("type_values", [])
    if type_val and valid_types and type_val not in valid_types:
        violations.append(f"invalid type: {type_val!r}")

    # status value check
    status_val = fm.get("status", "")
    valid_statuses = schema.get("status_values", [])
    if status_val and valid_statuses and status_val not in valid_statuses:
        violations.append(f"invalid status: {status_val!r}")

    # folder vs type consistency — longest matching prefix wins
    folder_map = schema.get("folder_type_map", {})
    matched_prefix = ""
    matched_types: list[str] = []
    for folder_prefix, allowed_types in folder_map.items():
        if (path.startswith(folder_prefix + "/") or path == folder_prefix) and \
                len(folder_prefix) > len(matched_prefix):
            matched_prefix = folder_prefix
            matched_types = allowed_types
    if matched_prefix and type_val and type_val not in matched_types:
        violations.append(
            f"folder/type mismatch: {matched_prefix!r} expects {matched_types}, got {type_val!r}"
        )

    return violations


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
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


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
    # For cnyes_archive, prepend tickers so FTS can find ticker mentions
    # (body starts with US snapshot table; stock codes appear much later in the doc)
    if fm.get("type") == "cnyes_archive":
        tickers_raw = fm.get("tickers", "[]")
        try:
            tickers_str = " ".join(json.loads(tickers_raw))
        except Exception:
            tickers_str = tickers_raw
        snippet = (tickers_str + " " + _body_snippet(text, max_chars=400))[:500]
    else:
        snippet = _body_snippet(text)          # 500 chars for DB / FTS storage
    prose = _embed_text_for(text)          # 1600 chars, code stripped
    tags_for_embed = fm.get("tags", "")
    embed_input = f"{fm.get('title', md_file.stem)} {tags_for_embed} {prose}".strip()
    vec = embed_text(embed_input)
    blob = _vec_to_blob(vec) if vec else None

    violations = validate_note(fm, rel)
    violations_json = json.dumps(violations) if violations else None

    con.execute(
        """
        INSERT INTO notes (path, title, note_type, status, tags, note_date,
                           content_hash, body_snippet, embedding, violations)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (path) DO UPDATE SET
            title              = excluded.title,
            note_type          = excluded.note_type,
            status             = excluded.status,
            tags               = excluded.tags,
            note_date          = excluded.note_date,
            content_hash       = excluded.content_hash,
            body_snippet       = excluded.body_snippet,
            embedding          = COALESCE(excluded.embedding, notes.embedding),
            violations         = excluded.violations
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
            violations_json,
        ],  # last_accessed intentionally omitted; set only by record_access()
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
    updates: list[tuple[bytes, str]] = []

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
            updates.append((_vec_to_blob(vec), path))
            updated += 1
        else:
            failed += 1

    if updates:
        with _connect() as con:
            con.executemany("UPDATE notes SET embedding = ? WHERE path = ?", updates)

    return {"updated": updated, "failed": failed, "skipped": len(rows) - updated - failed}


def sync_all(vault: Path) -> int:
    """Scan all .md files in vault and upsert into DB. Returns count synced.

    Also removes DB rows for notes/figures whose markdown files no longer exist.
    """
    seen: set[str] = set()
    with _connect() as con:
        count = 0
        for md_file in vault.rglob("*.md"):
            if any(p in md_file.parts for p in (".obsidian", ".claude", "templates")):
                continue
            upsert_note(con, vault, md_file)
            seen.add(str(md_file.relative_to(vault)))
            count += 1
        # Reconcile: remove stale rows that no longer have a backing file
        if seen:
            placeholders = ",".join("?" * len(seen))
            con.execute(
                f"DELETE FROM figures WHERE note_path NOT IN ({placeholders})", list(seen)
            )
            con.execute(
                f"DELETE FROM notes WHERE path NOT IN ({placeholders})", list(seen)
            )
        _ensure_fts(con)
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


def hybrid_search(
    query: str,
    limit: int = 20,
    alpha: float = 0.5,
    exclude_types: list[str] | None = None,
) -> list[dict]:
    """Hybrid BM25 + cosine search. alpha=0.5 weights both equally.

    Falls back to fts_search if embedding server unavailable.
    exclude_types: list of note_type values to exclude (e.g. ['cnyes_archive']).
    """
    bm25 = fts_search(query, limit=limit * 2)
    sem = semantic_search(query, limit=limit * 2)

    if exclude_types and (bm25 or sem):
        excluded = set(exclude_types)
        with _connect() as con:
            rows = con.execute(
                "SELECT path, note_type FROM notes WHERE path IN ({})".format(
                    ",".join("?" * (len(bm25) + len(sem)))
                ),
                [r["path"] for r in bm25 + sem],
            ).fetchall()
        excluded_paths = {path for path, ntype in rows if ntype in excluded}
        bm25 = [r for r in bm25 if r["path"] not in excluded_paths]
        sem = [r for r in sem if r["path"] not in excluded_paths]

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


def search_news(query: str, days: int = 7, limit: int = 20) -> list[dict]:
    """Search only cnyes_archive notes within the last N days.

    Uses LIKE on body_snippet first (reliable for numeric ticker codes which DuckDB
    FTS tokenizer does not index), then falls back to BM25 for Chinese text queries.
    Returns results sorted by note_date DESC.
    """
    with _connect() as con:
        # Primary: LIKE match on body_snippet (works for numeric tickers like "2317")
        q_like = f"% {query} %" if query.isdigit() else f"%{query.lower()}%"
        rows = con.execute(
            """
            SELECT path, title, 1.0 AS score, note_date
            FROM notes
            WHERE note_type = 'cnyes_archive'
              AND note_date IS NOT NULL
              AND date_diff('day', note_date, current_date) <= ?
              AND (body_snippet LIKE ? OR lower(body_snippet) LIKE ?)
            ORDER BY note_date DESC
            LIMIT ?
            """,
            [days, q_like, q_like.lower(), limit],
        ).fetchall()
        if rows:
            return [{"path": r[0], "title": r[1], "score": r[2], "date": str(r[3])} for r in rows]

        # Fallback: BM25 FTS (better for multi-word Chinese queries)
        try:
            rows = con.execute(
                """
                SELECT path, title, score, note_date FROM (
                    SELECT *, fts_main_notes.match_bm25(path, ?) AS score
                    FROM notes
                ) t
                WHERE score IS NOT NULL
                  AND note_type = 'cnyes_archive'
                  AND note_date IS NOT NULL
                  AND date_diff('day', note_date, current_date) <= ?
                ORDER BY note_date DESC, score DESC
                LIMIT ?
                """,
                [query, days, limit],
            ).fetchall()
            return [{"path": r[0], "title": r[1], "score": r[2], "date": str(r[3])} for r in rows]
        except Exception:
            q = f"%{query.lower()}%"
            rows = con.execute(
                """
                SELECT path, title, 1.0 AS score, note_date
                FROM notes
                WHERE note_type = 'cnyes_archive'
                  AND note_date IS NOT NULL
                  AND date_diff('day', note_date, current_date) <= ?
                  AND (lower(title) LIKE ? OR lower(body_snippet) LIKE ?)
                ORDER BY note_date DESC
                LIMIT ?
                """,
                [days, q, q, limit],
            ).fetchall()
            return [{"path": r[0], "title": r[1], "score": r[2], "date": str(r[3])} for r in rows]


def get_note_snippet(path: str, query: str, max_per_line: int = 250, max_lines: int = 3) -> str:
    """Return body lines from a vault note that mention *query* (case-insensitive).

    Skips YAML frontmatter and lines that are purely metadata (tickers/tags lists).
    Returns matched lines joined by ' | ', or empty string if nothing found.
    """
    full_path = _DEFAULT_VAULT_PATH / path
    if not full_path.exists():
        return ""
    try:
        q = query.lower()
        lines = full_path.read_text(encoding="utf-8").splitlines()

        # Skip frontmatter (between opening and closing ---)
        body_start = 0
        if lines and lines[0].strip() == "---":
            for i, ln in enumerate(lines[1:], 1):
                if ln.strip() == "---":
                    body_start = i + 1
                    break

        matches = []
        for line in lines[body_start:]:
            stripped = line.strip()
            # Skip pure metadata lines (JSON arrays, headings without context)
            if not stripped or stripped.startswith("tickers:") or stripped.startswith("tags:"):
                continue
            if q in stripped.lower() and len(stripped) > 20:
                # Clean markdown bold/italic markers for cleaner display
                clean = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", stripped)
                matches.append(clean[:max_per_line])
                if len(matches) >= max_lines:
                    break
        return " | ".join(matches)
    except Exception:
        return ""


def hybrid_search_grouped(query: str, limit: int = 10) -> dict[str, list[dict]]:
    """Hybrid search returning results split into knowledge vs news groups.

    Returns {"knowledge": [...], "news": [...]} where:
    - knowledge: all note types except cnyes_archive
    - news: only cnyes_archive notes from the last 7 days
    """
    knowledge = hybrid_search(query, limit=limit, exclude_types=KNOWLEDGE_EXCLUDE)
    news = search_news(query, days=7, limit=limit)
    return {"knowledge": knowledge, "news": news}


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
        rows = con.execute(
            "SELECT path, embedding FROM notes WHERE embedding IS NOT NULL AND path != ?",
            [path],
        ).fetchall()

    scored = []
    for other_path, blob in rows:
        sim = _cosine(q_vec, _blob_to_vec(blob))
        if sim >= threshold:
            scored.append((other_path, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [p.removesuffix(".md") for p, _ in scored[:limit]]


def top_by_recency(limit: int = 20, exclude_types: list[str] | None = None) -> list[dict]:
    """Return top notes by last_accessed for get_context() (Phase 1)."""
    extra = ""
    params: list = [limit]
    if exclude_types:
        placeholders = ",".join("?" * len(exclude_types))
        extra = f"AND note_type NOT IN ({placeholders})"
        params = list(exclude_types) + params
    with _connect() as con:
        rows = con.execute(
            f"""
            SELECT path, title, note_type, last_accessed
            FROM notes
            WHERE status != 'archived'
              {extra}
            ORDER BY last_accessed DESC NULLS LAST,
                     note_date DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            {"path": r[0], "title": r[1], "type": r[2], "last_accessed": str(r[3])}
            for r in rows
        ]


_SCORE_SQL = (
    "(access_count + 1.0) / "
    "(1.0 + ln(GREATEST("
    "date_diff('day', COALESCE(CAST(last_accessed AS DATE), note_date, current_date), current_date)"
    ", 1) + 1))"
)


def top_by_score(limit: int = 20, exclude_types: list[str] | None = None) -> list[dict]:
    """Return top notes by Ebbinghaus score (Phase 2). Falls back to recency."""
    extra = ""
    params: list = [limit]
    if exclude_types:
        placeholders = ",".join("?" * len(exclude_types))
        extra = f"AND note_type NOT IN ({placeholders})"
        params = list(exclude_types) + params
    with _connect() as con:
        rows = con.execute(
            f"""
            SELECT path, title, note_type,
                   {_SCORE_SQL} AS score
            FROM notes
            WHERE status != 'archived'
              {extra}
            ORDER BY score DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            {"path": r[0], "title": r[1], "type": r[2], "score": round(r[3], 4)}
            for r in rows
        ]


def sleep_candidates(min_age_days: int = 90, max_score: float = 0.5) -> list[dict]:
    """Return notes eligible for Vault Sleep consolidation (Phase 3)."""
    with _connect() as con:
        rows = con.execute(
            f"""
            SELECT path, title, age_days, score FROM (
                SELECT path, title,
                       date_diff('day', COALESCE(note_date, current_date), current_date) AS age_days,
                       {_SCORE_SQL} AS score
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
