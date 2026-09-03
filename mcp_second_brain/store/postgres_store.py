"""PostgresStore — VaultStore implementation backed by Postgres + pgvector.

Connection pool: psycopg[binary,pool] (psycopg3).
Vector similarity: pgvector HNSW index, cosine ops.
Keyword FTS: pg_trgm similarity (language-neutral, CJK-safe) + tsvector for English.

Environment variables:
  SB_PG_DSN   — PostgreSQL DSN, e.g. postgresql://postgres:pw@localhost:5432/sb_personal
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..identity import Identity, KeyState

import psycopg
from psycopg_pool import ConnectionPool

# The markdown → index projection is shared with DuckDBStore; the seam between the
# two backends is "how it is stored", not "what a note is".
from ..note_row import project_note, FRONTMATTER_RE

# vault_db still owns the embedding client and the vault schema validator, plus the
# pure vector helpers (_cosine, _path_penalty). Those don't touch DuckDB.
from .. import vault_db as _vdb

# Phase B — chunk-level embeddings (late chunking, see chunking.py/late_chunking.py).
from ..late_chunking import chunk_and_embed, LateChunkingUnavailable
from ..snippets import strip_references

# Decision 2 — reranker (see reranker.py's docstring for the "top-1 chunk gets
# fooled by boilerplate" lesson this module's NUM_CHUNKS_PER_CANDIDATE encodes).
from .. import reranker as _reranker

_SCORE_SQL = """
(access_count + 1.0) / (1.0 + ln(GREATEST(
    (CURRENT_DATE - COALESCE(last_accessed::date, note_date, CURRENT_DATE))::float,
    1
) + 1))
""".strip()

_SYNC_BATCH_SIZE = 50


def _vec_to_pg(vec: list[float]) -> list[float]:
    """Pass-through — psycopg3 + pgvector stores Python lists directly as vector."""
    return vec


def _parse_vec(v: object) -> list[float] | None:
    """Normalise a pgvector value to list[float].

    psycopg3 without a registered pgvector adapter returns the vector column as
    a string "[0.1, 0.2, ...]". Convert to list[float] so cosine arithmetic works.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return json.loads(v)
    return [float(x) for x in v]  # type: ignore[union-attr]


def _merge_by_path_max_score(*result_lists: list[dict]) -> list[dict]:
    """Merge several {"path","title","score"} lists, keeping the best score per
    path, re-sorted descending by score.

    Used to combine chunk-level and notes-level search results (Phase B-4):
    the caller's downstream RRF fusion ranks by *position* in this list, so
    re-sorting after the merge (not just deduplicating) is what actually
    matters — an unsorted merge would silently corrupt every rank-based score.
    """
    best: dict[str, dict] = {}
    for results in result_lists:
        for r in results:
            p = r["path"]
            if p not in best or r["score"] > best[p]["score"]:
                best[p] = r
    return sorted(best.values(), key=lambda r: r["score"], reverse=True)


def _redact_dsn(dsn: str) -> str:
    """Hide the password in a postgresql:// DSN before returning it in stats/logs."""
    import re

    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", dsn)


class PostgresStore:
    """VaultStore backed by Postgres 16 + pgvector.

    Usage:
        store = PostgresStore("postgresql://postgres:pw@localhost:5432/sb_personal")
    """

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            kwargs={"autocommit": False},
        )
        self._apply_schema()

    def _apply_schema(self) -> None:
        schema_path = Path(__file__).parent / "postgres_schema.sql"
        local_cache = Path.home() / ".local/share/second-brain/postgres_schema.sql"
        try:
            sql = schema_path.read_text(encoding="utf-8")
            local_cache.parent.mkdir(parents=True, exist_ok=True)
            local_cache.write_text(sql, encoding="utf-8")
        except OSError:
            if local_cache.exists():
                sql = local_cache.read_text(encoding="utf-8")
            else:
                raise
        with self._pool.connection() as conn:
            conn.execute(sql)
            conn.commit()

    def close(self) -> None:
        self._pool.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_note_row(
        self,
        cur: psycopg.Cursor,
        vault: Path,
        md_file: Path,
    ) -> None:
        """Project md_file into a NoteRow and upsert it into the Postgres notes table.

        What a note looks like in the index is owned by note_row.project_note();
        this method only binds that projection into Postgres SQL.
        """
        rel = str(md_file.relative_to(vault))

        # Skip if unchanged — hash first so an untouched file costs no embedding call.
        chash = _vdb._content_hash_of_file(md_file)
        row = cur.execute(
            "SELECT content_hash FROM notes WHERE path = %s", [rel]
        ).fetchone()
        if row and row[0] == chash:
            return

        note = project_note(
            vault, md_file, embed=_vdb.embed_text, validate=_vdb.validate_note,
            log_prefix="pg_store",
        )

        cur.execute(
            """
            INSERT INTO notes (
                path, title, note_type, status, tags, note_date,
                content_hash, body_snippet, embedding, violations,
                semantic_keywords, neighbor_keywords, cluster_topic
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s::vector, %s,
                %s, %s, %s
            )
            ON CONFLICT (path) DO UPDATE SET
                title              = EXCLUDED.title,
                note_type          = EXCLUDED.note_type,
                status             = EXCLUDED.status,
                tags               = EXCLUDED.tags,
                note_date          = EXCLUDED.note_date,
                content_hash       = EXCLUDED.content_hash,
                body_snippet       = EXCLUDED.body_snippet,
                embedding          = COALESCE(EXCLUDED.embedding, notes.embedding),
                violations         = EXCLUDED.violations,
                semantic_keywords  = COALESCE(EXCLUDED.semantic_keywords, notes.semantic_keywords),
                neighbor_keywords  = COALESCE(EXCLUDED.neighbor_keywords, notes.neighbor_keywords),
                cluster_topic      = COALESCE(EXCLUDED.cluster_topic, notes.cluster_topic)
            """,
            [
                note.path,
                note.title,
                note.note_type,
                note.status,
                note.tags_json,
                note.note_date,
                note.content_hash,
                note.body_snippet,
                str(note.embedding) if note.embedding else None,  # SQL ::vector cast
                note.violations_json,
                note.semantic_keywords,
                note.neighbor_keywords,
                note.cluster_topic,
            ],
        )

        self._sync_chunks_for_note(cur, note.path, note.content_hash, md_file)

    def _sync_chunks_for_note(
        self,
        cur: psycopg.Cursor,
        note_path: str,
        content_hash: str,
        md_file: Path,
    ) -> None:
        """Rebuild note_chunks for one note when its content changed (decision 1
        of the chunking/embedding plan).

        Skips entirely when the stored chunk hash already matches the note's
        current content_hash — avoids recomputing embeddings for a multi-MB
        paper on every sync pass. Full replace (DELETE + re-INSERT), never an
        in-place update — computes the new chunk set *before* deleting the old
        one, so a transient late-chunking-server outage leaves the previous
        (stale but present) chunks in place instead of leaving the note with
        zero chunks until the next successful sync.

        Reads the file's full text independently of project_note() — that
        projection truncates files over note_row.LARGE_FILE_THRESHOLD
        (Phase B-0), which would defeat the point of chunking (built
        specifically to reach the parts of long documents the single-vector
        embedding can't).
        """
        row = cur.execute(
            "SELECT content_hash FROM note_chunks WHERE note_path = %s LIMIT 1",
            [note_path],
        ).fetchone()
        if row and row[0] == content_hash:
            return

        try:
            full_text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            print(f"[pg_store] chunk sync: read failed for {note_path}: {e}", file=sys.stderr)
            return

        # References are the cited papers' claims, not this note's own (same
        # rationale as embed_text_for's Phase B-0 fix) — no reason to spend
        # chunks, an HNSW index, and a trgm index on someone else's bibliography.
        body = strip_references(FRONTMATTER_RE.sub("", full_text).strip())
        if not body.strip():
            cur.execute("DELETE FROM note_chunks WHERE note_path = %s", [note_path])
            return

        try:
            chunks = chunk_and_embed(body)
        except LateChunkingUnavailable as e:
            print(
                f"[pg_store] chunk sync: embedding unavailable for {note_path}, "
                f"keeping existing chunks: {e}",
                file=sys.stderr,
            )
            return

        cur.execute("DELETE FROM note_chunks WHERE note_path = %s", [note_path])
        for idx, (chunk_text, emb) in enumerate(chunks):
            cur.execute(
                """
                INSERT INTO note_chunks (note_path, chunk_idx, chunk_text, content_hash, embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
                """,
                [note_path, idx, chunk_text, content_hash, str(emb) if emb else None],
            )

    # ------------------------------------------------------------------
    # Core indexing
    # ------------------------------------------------------------------

    def index_file(self, vault: Path, md_file: Path) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                self._upsert_note_row(cur, vault, md_file)
            conn.commit()

    def sync_all(self, vault: Path) -> dict:
        seen: set[str] = set()
        count = 0
        batch: list[Path] = []

        # Google Drive's virtual filesystem occasionally raises EDEADLK on a full
        # recursive walk (same quirk sync_incremental guards against below); retry
        # a few times with backoff before giving up, since this is a manually
        # triggered full rebuild and silently returning 0 would be misleading.
        all_md: list[Path] | None = None
        last_err: OSError | None = None
        for attempt, delay in enumerate((0.5, 1.0, 2.0, 4.0, 8.0)):
            try:
                all_md = list(vault.rglob("*.md"))
                break
            except OSError as e:
                last_err = e
                print(f"[pg-sync] rglob attempt {attempt + 1} failed (Drive deadlock?): {e}", file=sys.stderr)
                time.sleep(delay)
        if all_md is None:
            assert last_err is not None
            raise last_err

        all_files = [
            f
            for f in all_md
            if not any(p in f.parts for p in (".obsidian", ".claude", "templates"))
        ]

        for i, md_file in enumerate(all_files):
            batch.append(md_file)
            if len(batch) >= _SYNC_BATCH_SIZE or i == len(all_files) - 1:
                with self._pool.connection() as conn:
                    with conn.cursor() as cur:
                        for f in batch:
                            self._upsert_note_row(cur, vault, f)
                            seen.add(str(f.relative_to(vault)))
                            count += 1
                    conn.commit()
                batch = []

        # Reconcile: remove stale rows
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                if seen:
                    cur.execute("CREATE TEMP TABLE IF NOT EXISTS _seen_paths (path TEXT)")
                    cur.execute("DELETE FROM _seen_paths")
                    cur.executemany(
                        "INSERT INTO _seen_paths VALUES (%s)", [[p] for p in seen]
                    )
                    cur.execute(
                        "DELETE FROM figures WHERE note_path NOT IN (SELECT path FROM _seen_paths)"
                    )
                    cur.execute(
                        "DELETE FROM notes WHERE path NOT IN (SELECT path FROM _seen_paths)"
                    )
                    cur.execute("DROP TABLE IF EXISTS _seen_paths")
                row = cur.execute(
                    "SELECT COUNT(*) FROM notes WHERE embedding IS NULL"
                ).fetchone()
                embed_failed: int = row[0] if row else 0
            conn.commit()

        return {"synced": count, "embed_failed": embed_failed}

    def sync_incremental(self, vault: Path) -> dict:
        # Use vault.db mtime as reference if DuckDB file exists; else compare to a fixed old time
        db_path = _vdb.DB_PATH
        db_mtime = db_path.stat().st_mtime if db_path.exists() else 0
        try:
            candidates = list(vault.rglob("*.md"))
        except OSError as e:
            # Google Drive FUSE deadlock — non-fatal, backfill handled next run
            print(f"[pg-sync] rglob failed (Drive deadlock?): {e}", file=sys.stderr)
            return {"updated": 0, "skipped": "drive_unavailable"}
        changed = []
        for f in candidates:
            if any(p in f.parts for p in (".obsidian", ".claude", "templates")):
                continue
            try:
                if f.stat().st_mtime > db_mtime:
                    changed.append(f)
            except OSError:
                pass
        if not changed:
            return {"updated": 0, "skipped": "all fresh"}
        updated, skipped = 0, 0
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                for f in changed:
                    try:
                        self._upsert_note_row(cur, vault, f)
                        updated += 1
                    except OSError as e:
                        print(f"[pg-sync] skip {f.name}: {e}", file=sys.stderr)
                        skipped += 1
            conn.commit()
        return {"updated": updated, "skipped": skipped}

    def sync_if_stale(self, vault: Path) -> None:
        # No-op: the central Postgres index is kept fresh by the scheduled
        # `second-brain-pg-sync` job (30-min incremental), so a live query is
        # always current. Startup does not trigger its own re-scan.
        return

    def sync_embeddings(self, vault: Path | None = None) -> dict:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT path, title, body_snippet, tags FROM notes WHERE embedding IS NULL"
            ).fetchall()

        updated, failed = 0, 0
        updates: list[tuple[str, str]] = []

        for path, title, snippet, tags in rows:
            if vault:
                md_file = vault / path
                full_text = None
                if md_file.exists():
                    # Google Drive's virtual filesystem occasionally raises EDEADLK on
                    # an individual read_text (same quirk as the rglob walk in sync_all).
                    # Retry briefly; on persistent failure, skip this note for now — it
                    # stays embedding=NULL and gets picked up on the next sync round.
                    for delay in (0.3, 1.0):
                        try:
                            full_text = md_file.read_text(encoding="utf-8", errors="ignore")
                            break
                        except OSError as e:
                            print(f"[pg_store] read_text retry for {path}: {e}", file=sys.stderr)
                            time.sleep(delay)
                    else:
                        try:
                            full_text = md_file.read_text(encoding="utf-8", errors="ignore")
                        except OSError as e:
                            print(f"[pg_store] skip embedding {path} (Drive deadlock?): {e}", file=sys.stderr)
                            failed += 1
                            continue
                if full_text is not None:
                    prose = _vdb._embed_text_for(full_text)
                    text = f"{title or ''} {tags or ''} {prose}".strip()
                else:
                    text = f"{title or ''} {snippet or ''}".strip()
            else:
                text = f"{title or ''} {snippet or ''}".strip()
            try:
                vec = _vdb.embed_text(text)
            except ValueError as e:
                print(f"[pg_store] embedding dim error: {path} — {e}", file=sys.stderr)
                vec = None
            if vec:
                updates.append((str(vec), path))
                updated += 1
            else:
                failed += 1

        if updates:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    for vec_str, path in updates:
                        cur.execute(
                            "UPDATE notes SET embedding = %s::vector WHERE path = %s",
                            [vec_str, path],
                        )
                conn.commit()

        return {"updated": updated, "failed": failed, "skipped": len(rows) - updated - failed}

    def sync_chunks(self, vault: Path) -> dict:
        """Backfill note_chunks for notes that don't have a matching hash yet.

        Mirrors sync_embeddings()'s "backfill what's missing" semantics but for
        the chunks table. Necessary as a *separate* pass because
        _upsert_note_row's content_hash short-circuit means an unchanged note
        is never revisited by sync_all/sync_incremental — a brand-new
        note_chunks table (or any note that predates this feature) needs this
        explicit backfill once. After that, ordinary edits keep chunks current
        via _upsert_note_row automatically (decision 1).
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT n.path, n.content_hash
                FROM notes n
                LEFT JOIN note_chunks c
                    ON c.note_path = n.path AND c.content_hash = n.content_hash
                WHERE c.note_path IS NULL
                GROUP BY n.path, n.content_hash
                """
            ).fetchall()

        updated, failed = 0, 0
        for path, content_hash in rows:
            md_file = vault / path
            if not md_file.exists():
                continue
            try:
                with self._pool.connection() as conn:
                    with conn.cursor() as cur:
                        self._sync_chunks_for_note(cur, path, content_hash, md_file)
                    conn.commit()
                updated += 1
            except Exception as e:
                print(f"[pg_store] sync_chunks failed for {path}: {e}", file=sys.stderr)
                failed += 1

        return {"updated": updated, "failed": failed, "candidates": len(rows)}

    def compute_neighbor_keywords(
        self, threshold: float = 0.75, top_n: int = 5
    ) -> dict[str, dict]:
        cache = self.load_embedding_cache()
        if not cache or len(cache) > 2000:
            return {}

        paths = list(cache.keys())
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT path, title, tags, semantic_keywords FROM notes WHERE path = ANY(%s)",
                [paths],
            ).fetchall()
        meta = {r[0]: {"title": r[1], "tags": r[2] or "", "sk": r[3] or ""} for r in rows}

        result: dict[str, dict] = {}
        for path, q_vec in cache.items():
            scored = [
                (other, _vdb._cosine(q_vec, vec))
                for other, vec in cache.items()
                if other != path
            ]
            scored = [(p, s) for p, s in scored if s >= threshold]
            scored.sort(key=lambda x: x[1], reverse=True)
            neighbors = [p for p, _ in scored[:top_n]]
            if not neighbors:
                continue
            words: list[str] = []
            for nb in neighbors:
                m = meta.get(nb, {})
                words += (m.get("title") or "").split()
                words += (m.get("sk") or "").split(",")
            freq: dict[str, int] = {}
            for w in words:
                w = w.strip().lower()
                if w:
                    freq[w] = freq.get(w, 0) + 1
            top = sorted(freq, key=lambda x: -freq[x])[:10]
            topic = top[0] if top else ""
            result[path] = {"neighbor_keywords": top, "cluster_topic": topic}

        if result:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    for path, data in result.items():
                        cur.execute(
                            "UPDATE notes SET neighbor_keywords = %s, cluster_topic = %s WHERE path = %s",
                            [
                                json.dumps(data["neighbor_keywords"], ensure_ascii=False),
                                data["cluster_topic"],
                                path,
                            ],
                        )
                conn.commit()
        return result

    # ------------------------------------------------------------------
    # Note mutations
    # ------------------------------------------------------------------

    def record_access(self, path: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE notes
                SET access_count  = access_count + 1,
                    last_accessed = CURRENT_TIMESTAMP
                WHERE path = %s
                """,
                [path],
            )
            conn.commit()

    def set_note_status(self, path: str, status: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE notes SET status = %s WHERE path = %s", [status, path]
            )
            conn.commit()

    def update_snapshot(
        self, path: str, snapshot_path: str, tier: str, token_est: int
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE notes SET snapshot_path=%s, snapshot_tier=%s, snapshot_token_est=%s WHERE path=%s",
                [snapshot_path, tier, token_est, path],
            )
            conn.commit()

    def mark_rules_extracted(self, path: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE notes SET rules_extracted_at = CURRENT_TIMESTAMP WHERE path = %s",
                [path],
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Figures
    # ------------------------------------------------------------------

    def upsert_figure(
        self,
        note_path: str,
        fig_index: int,
        image_url: str,
        local_path: str,
        ocr_text: str,
        description: str,
        token_est: int = 0,
        caption: str = "",
    ) -> None:
        with self._pool.connection() as conn:
            existing = conn.execute(
                "SELECT id FROM figures WHERE note_path = %s AND fig_index = %s",
                [note_path, fig_index],
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE figures SET image_url=%s, local_path=%s, ocr_text=%s,
                       description=%s, token_est=%s, caption=%s WHERE id=%s""",
                    [image_url, local_path, ocr_text, description, token_est, caption, existing[0]],
                )
            else:
                conn.execute(
                    """INSERT INTO figures
                       (note_path, fig_index, image_url, local_path, ocr_text, description, token_est, caption)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [note_path, fig_index, image_url, local_path, ocr_text, description, token_est, caption],
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _trgm_search(self, query: str, limit: int) -> list[dict]:
        """Trigram-based keyword search (language-neutral, CJK-safe)."""
        q = f"%{query}%"
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT path, title,
                    greatest(
                        similarity(%s, COALESCE(title, '')),
                        similarity(%s, COALESCE(body_snippet, '')),
                        similarity(%s, COALESCE(tags, '')),
                        similarity(%s, COALESCE(semantic_keywords, ''))
                    ) AS score
                FROM notes
                WHERE
                    COALESCE(title, '') ILIKE %s OR
                    COALESCE(body_snippet, '') ILIKE %s OR
                    COALESCE(tags, '') ILIKE %s OR
                    COALESCE(semantic_keywords, '') ILIKE %s OR
                    COALESCE(neighbor_keywords, '') ILIKE %s OR
                    COALESCE(cluster_topic, '') ILIKE %s
                ORDER BY score DESC
                LIMIT %s
                """,
                [query, query, query, query, q, q, q, q, q, q, limit],
            ).fetchall()
        return [{"path": r[0], "title": r[1], "score": float(r[2])} for r in rows]

    def _semantic_search(self, query: str, limit: int) -> list[dict]:
        """Vector cosine search via pgvector HNSW."""
        q_vec = _vdb.embed_text(query)
        if not q_vec:
            return []
        vec_str = str(q_vec)
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT path, title, 1 - (embedding <=> %s::vector) AS score
                FROM notes
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                [vec_str, vec_str, limit],
            ).fetchall()
        return [{"path": r[0], "title": r[1], "score": float(r[2])} for r in rows]

    # ------------------------------------------------------------------
    # Chunk-level search — Phase B-4 of the chunking/embedding plan.
    # ------------------------------------------------------------------
    #
    # Both paths query note_chunks *in addition to* notes, merged by taking
    # the max score per path (see _merge_by_path_max_score below) — not a
    # full swap. A note that hasn't been chunked yet (mid-backfill, or a
    # transient late-chunking-server outage at write time — see
    # _sync_chunks_for_note) would otherwise vanish from search entirely
    # until its chunks catch up; keeping the notes-level query as a floor
    # means coverage only ever gets wider, never narrower, exactly the
    # "silent regression" failure mode decision 1 calls out.

    def _trgm_search_chunks(self, query: str, limit: int) -> list[dict]:
        """Chunk-level trigram search, aggregated to note level (max score).

        Reaches text the notes-level search cannot: body_snippet only covers
        a note's first 500 chars; chunk_text covers the whole document
        (minus references) via note_chunks (see B-1's index-choice note for
        why this needs its own trgm/GIN index rather than reusing notes').
        """
        q = f"%{query}%"
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.note_path, n.title, max(similarity(%s, c.chunk_text)) AS score
                FROM note_chunks c
                JOIN notes n ON n.path = c.note_path
                WHERE c.chunk_text ILIKE %s
                GROUP BY c.note_path, n.title
                ORDER BY score DESC
                LIMIT %s
                """,
                [query, q, limit],
            ).fetchall()
        return [{"path": r[0], "title": r[1], "score": float(r[2])} for r in rows]

    def _semantic_search_chunks(self, query: str, limit: int) -> list[dict]:
        """Chunk-level cosine search, aggregated to note level (max score, i.e.
        each note's single best-matching chunk — "B-4: chunk 命中聚合回筆記用
        max-score，不是平均" in the plan).

        The inner CTE is an ANN funnel: fetch a wide multiple of `limit`
        nearest *chunks* (HNSW-accelerated) before grouping by note_path, so
        Postgres still uses the index for the expensive part instead of
        scoring every chunk. A note can own many chunks, so the funnel has to
        be wider than a plain top-K to leave room for enough distinct notes
        to surface — 20x is a documented, tunable-later heuristic, not a
        precise bound.
        """
        q_vec = _vdb.embed_text(query)
        if not q_vec:
            return []
        vec_str = str(q_vec)
        funnel = max(limit * 20, 200)
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                WITH top_chunks AS (
                    SELECT c.note_path, (c.embedding <=> %s::vector) AS distance
                    FROM note_chunks c
                    WHERE c.embedding IS NOT NULL
                    ORDER BY c.embedding <=> %s::vector
                    LIMIT %s
                )
                SELECT tc.note_path, n.title, 1 - min(tc.distance) AS score
                FROM top_chunks tc
                JOIN notes n ON n.path = tc.note_path
                GROUP BY tc.note_path, n.title
                ORDER BY score DESC
                LIMIT %s
                """,
                [vec_str, vec_str, funnel, limit],
            ).fetchall()
        return [{"path": r[0], "title": r[1], "score": float(r[2])} for r in rows]

    def _top_chunks_for_paths(
        self, paths: list[str], query_vec: list[float], n_per_path: int
    ) -> dict[str, list[str]]:
        """Each path's top-``n_per_path`` chunks by cosine distance to
        ``query_vec``, as ``{path: [chunk_text, ...]}`` (nearest first).

        One query for every candidate (window function, not N round trips) —
        used to feed the reranker (decision 2): see reranker.py's docstring
        for why more than one chunk per candidate matters.
        """
        if not paths:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT note_path, chunk_text FROM (
                    SELECT note_path, chunk_text,
                           row_number() OVER (
                               PARTITION BY note_path
                               ORDER BY embedding <=> %s::vector
                           ) AS rn
                    FROM note_chunks
                    WHERE note_path = ANY(%s) AND embedding IS NOT NULL
                ) ranked
                WHERE rn <= %s
                ORDER BY note_path, rn
                """,
                [str(query_vec), paths, n_per_path],
            ).fetchall()
        out: dict[str, list[str]] = {}
        for path, chunk_text in rows:
            out.setdefault(path, []).append(chunk_text)
        return out

    def hybrid_search(
        self,
        query: str,
        limit: int = 20,
        alpha: float = 0.5,  # noqa: ARG002 — kept for API compat with DuckDBStore
        exclude_types: list[str] | None = None,
        fusion: str = "rrf",  # noqa: ARG002 — always RRF for Postgres; kept for compat
        apply_path_penalty: bool = True,
        rerank: bool = True,
    ) -> list[dict]:
        """RRF-fused keyword + semantic search, notes and chunks merged (B-4),
        then reranked (decision 2).

        rerank: pass the fused candidates through the reranker before
        truncating to `limit`. Default on — the A/B experiment
        (decisions/second-brain-reranker-ab對照實驗結果-決策2.md) found a
        consistent, substantial ranking improvement. Fails soft: an
        unreachable reranker leaves RRF order unchanged rather than erroring
        (see reranker.rerank()). Callers that want the pre-rerank baseline
        (e.g. tests, or a future A/B comparison) pass rerank=False.
        """
        bm25 = _merge_by_path_max_score(
            self._trgm_search(query, limit=limit * 2),
            self._trgm_search_chunks(query, limit=limit * 2),
        )
        sem = _merge_by_path_max_score(
            self._semantic_search(query, limit=limit * 2),
            self._semantic_search_chunks(query, limit=limit * 2),
        )

        if exclude_types and (bm25 or sem):
            excluded = set(exclude_types)
            candidate_paths = list({r["path"] for r in bm25 + sem})
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT path, note_type FROM notes WHERE path = ANY(%s)",
                    [candidate_paths],
                ).fetchall()
            excluded_paths = {path for path, ntype in rows if ntype in excluded}
            bm25 = [r for r in bm25 if r["path"] not in excluded_paths]
            sem = [r for r in sem if r["path"] not in excluded_paths]

        rrf_scores: dict[str, float] = {}
        penalty_map: dict[str, float] = {}

        def _rrf(rank: int, k: int = 60) -> float:
            return 1.0 / (rank + k)

        for rank, r in enumerate(bm25):
            p = r["path"]
            rrf_scores[p] = rrf_scores.get(p, 0) + _rrf(rank)
            penalty_map[p] = _vdb._path_penalty(p) if apply_path_penalty else 1.0
        for rank, r in enumerate(sem):
            p = r["path"]
            rrf_scores[p] = rrf_scores.get(p, 0) + _rrf(rank)
            if p not in penalty_map:
                penalty_map[p] = _vdb._path_penalty(p) if apply_path_penalty else 1.0

        # Reranking needs a wider funnel than the final `limit` — it can only
        # promote candidates that are already in the pool, not find new ones.
        funnel_limit = max(limit * 2, 20) if rerank else limit
        scored = sorted(
            rrf_scores.items(),
            key=lambda x: x[1] * penalty_map[x[0]],
            reverse=True,
        )[:funnel_limit]

        if not scored:
            return []

        paths = [p for p, _ in scored]
        score_map = {p: s * penalty_map[p] for p, s in scored}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT path, title, note_type FROM notes WHERE path = ANY(%s)",
                [paths],
            ).fetchall()
        meta = {r[0]: (r[1], r[2]) for r in rows}

        results = [
            {
                "path": p,
                "title": meta.get(p, ("", ""))[0],
                "note_type": meta.get(p, ("", ""))[1],
                "score": round(score_map[p], 6),
            }
            for p, _ in scored
            if p in meta
        ]

        if rerank and results:
            query_vec = _vdb.embed_text(query)
            if query_vec:
                chunks_by_path = self._top_chunks_for_paths(
                    [r["path"] for r in results], query_vec, _reranker.NUM_CHUNKS_PER_CANDIDATE
                )
                results = _reranker.rerank_candidates(query, results, chunks_by_path)

        return results[:limit]

    def search_news(self, query: str, days: int = 7, limit: int = 20) -> list[dict]:
        q_like = f"% {query} %" if query.isdigit() else f"%{query.lower()}%"
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT path, title, 1.0 AS score, note_date
                FROM notes
                WHERE note_type = 'cnyes_archive'
                  AND note_date IS NOT NULL
                  AND (CURRENT_DATE - note_date) <= %s
                  AND (body_snippet ILIKE %s OR lower(body_snippet) LIKE %s)
                ORDER BY note_date DESC
                LIMIT %s
                """,
                [days, q_like, q_like.lower(), limit],
            ).fetchall()
            if rows:
                return [
                    {"path": r[0], "title": r[1], "score": float(r[2]), "date": str(r[3])}
                    for r in rows
                ]
            # Fallback: pg_trgm similarity
            rows = conn.execute(
                """
                SELECT path, title,
                    similarity(%s, COALESCE(title,'') || ' ' || COALESCE(body_snippet,'')) AS score,
                    note_date
                FROM notes
                WHERE note_type = 'cnyes_archive'
                  AND note_date IS NOT NULL
                  AND (CURRENT_DATE - note_date) <= %s
                  AND (
                    COALESCE(title,'') ILIKE %s OR
                    COALESCE(body_snippet,'') ILIKE %s
                  )
                ORDER BY note_date DESC, score DESC
                LIMIT %s
                """,
                [query, days, f"%{query}%", f"%{query}%", limit],
            ).fetchall()
            return [
                {"path": r[0], "title": r[1], "score": float(r[2]), "date": str(r[3])}
                for r in rows
            ]

    def search_figures(self, query: str, limit: int = 10) -> list[dict]:
        words = query.lower().split()
        if not words:
            return []
        clauses = " OR ".join(
            "(lower(coalesce(ocr_text,'')) LIKE %s OR lower(coalesce(description,'')) LIKE %s "
            "OR lower(coalesce(caption,'')) LIKE %s)"
            for _ in words
        )
        params: list = [p for w in words for p in (f"%{w}%", f"%{w}%", f"%{w}%")]
        params.append(limit)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT note_path, fig_index, image_url, ocr_text, description, "
                f"coalesce(caption,''), coalesce(token_est,0) "
                f"FROM figures WHERE {clauses} ORDER BY note_path LIMIT %s",
                params,
            ).fetchall()
        return [
            {
                "note_path": r[0],
                "fig_index": r[1],
                "image_url": r[2],
                "ocr_text": r[3],
                "description": r[4],
                "caption": r[5],
                "token_est": r[6],
            }
            for r in rows
        ]

    def get_figure(self, note_path: str, fig_index: int) -> dict | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT note_path, fig_index, image_url, local_path, ocr_text, "
                "description, coalesce(caption,''), coalesce(token_est,0) "
                "FROM figures WHERE note_path = %s AND fig_index = %s",
                [note_path, fig_index],
            ).fetchone()
        if not row:
            return None
        return {
            "note_path": row[0], "fig_index": row[1], "image_url": row[2],
            "local_path": row[3], "ocr_text": row[4], "description": row[5],
            "caption": row[6], "token_est": row[7],
        }

    def find_related(
        self,
        path: str,
        limit: int = 5,
        threshold: float = 0.7,
        _embedding_cache: dict[str, list[float]] | None = None,
    ) -> list[str]:
        if _embedding_cache is not None:
            q_vec = _embedding_cache.get(path)
            if not q_vec:
                return []
            scored = [
                (other_path, _vdb._cosine(q_vec, vec))
                for other_path, vec in _embedding_cache.items()
                if other_path != path
            ]
        else:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT embedding FROM notes WHERE path = %s", [path]
                ).fetchone()
                if not row or not row[0]:
                    return []
                q_vec = _parse_vec(row[0])
                if not q_vec:
                    return []
                rows = conn.execute(
                    "SELECT path, embedding FROM notes WHERE embedding IS NOT NULL AND path != %s",
                    [path],
                ).fetchall()
            parsed_rows = [(r[0], _parse_vec(r[1])) for r in rows]
            scored = [(p, _vdb._cosine(q_vec, v)) for p, v in parsed_rows if v]

        scored = [(p, s) for p, s in scored if s >= threshold]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [p for p, _ in scored[:limit]]

    # ------------------------------------------------------------------
    # Ranking / retrieval
    # ------------------------------------------------------------------

    def top_by_recency(
        self, limit: int = 20, exclude_types: list[str] | None = None
    ) -> list[dict]:
        extra = ""
        params: list = []
        if exclude_types:
            placeholders = ",".join(["%s"] * len(exclude_types))
            extra = f"AND note_type NOT IN ({placeholders})"
            params = list(exclude_types)
        params.append(limit)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT path, title, note_type, last_accessed
                FROM notes
                WHERE last_accessed IS NOT NULL
                  {extra}
                ORDER BY last_accessed DESC
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [
            {"path": r[0], "title": r[1], "type": r[2], "last_accessed": str(r[3])}
            for r in rows
        ]

    def top_by_score(
        self, limit: int = 20, exclude_types: list[str] | None = None
    ) -> list[dict]:
        extra = ""
        params: list = []
        if exclude_types:
            placeholders = ",".join(["%s"] * len(exclude_types))
            extra = f"AND note_type NOT IN ({placeholders})"
            params = list(exclude_types)
        params.append(limit)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT path, title, note_type,
                       {_SCORE_SQL} AS score
                FROM notes
                WHERE status != 'archived'
                  {extra}
                ORDER BY score DESC
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [
            {"path": r[0], "title": r[1], "type": r[2], "score": round(float(r[3]), 4)}
            for r in rows
        ]

    def sleep_candidates(
        self, min_age_days: int = 90, max_score: float = 0.5
    ) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT path, title, age_days, score FROM (
                    SELECT path, title,
                           (CURRENT_DATE - COALESCE(note_date, CURRENT_DATE))::float AS age_days,
                           {_SCORE_SQL} AS score
                    FROM notes
                    WHERE status NOT IN ('archived', 'deprecated')
                      AND (CURRENT_DATE - COALESCE(note_date, CURRENT_DATE))::float >= %s
                ) t
                WHERE score <= %s
                ORDER BY score ASC
                """,
                [min_age_days, max_score],
            ).fetchall()
        return [
            {"path": r[0], "title": r[1], "age_days": int(r[2]), "score": round(float(r[3]), 4)}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Bulk / batch operations
    # ------------------------------------------------------------------

    def load_embedding_cache(self) -> dict[str, list[float]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT path, embedding FROM notes WHERE embedding IS NOT NULL"
            ).fetchall()
        return {r[0]: _parse_vec(r[1]) or [] for r in rows}

    def get_notes_with_snapshots(self) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT path FROM notes WHERE snapshot_path IS NOT NULL AND snapshot_path != ''"
            ).fetchall()
        return {Path(r[0]).name for r in rows}

    def get_rules_candidates(
        self, min_access: int = 5, stale_days: int = 90
    ) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT path FROM notes
                WHERE access_count >= %s
                  AND (rules_extracted_at IS NULL
                       OR rules_extracted_at < NOW() - INTERVAL '1 day' * %s)
                ORDER BY access_count DESC
                """,
                [min_access, stale_days],
            ).fetchall()
        return [r[0] for r in rows]

    def load_notes_with_embeddings(
        self, note_type_filter: str | None = None
    ) -> list[tuple[str, str, list[float]]]:
        sql = (
            "SELECT path, note_type, embedding FROM notes "
            "WHERE embedding IS NOT NULL AND (status IS NULL OR status != 'consolidated')"
        )
        with self._pool.connection() as conn:
            rows = (
                conn.execute(sql + " AND note_type = %s", [note_type_filter]).fetchall()
                if note_type_filter
                else conn.execute(sql).fetchall()
            )
        return [(r[0], r[1], list(r[2])) for r in rows]

    # ------------------------------------------------------------------
    # Server-side query helpers
    # ------------------------------------------------------------------

    def has_index(self) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        return bool(row and row[0] > 0)

    def get_snapshot_path(self, path: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT snapshot_path FROM notes WHERE path = %s", [path]
            ).fetchone()
        return row[0] if row else None

    def get_paths_for_semantic_keywords(self, force: bool = False) -> list[str]:
        sql = (
            "SELECT path FROM notes"
            if force
            else "SELECT path FROM notes WHERE semantic_keywords IS NULL"
        )
        with self._pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [r[0] for r in rows]

    def get_paths_for_neighbor_keywords(self, force: bool = False) -> list[str]:
        sql = (
            "SELECT path FROM notes WHERE embedding IS NOT NULL"
            if force
            else "SELECT path FROM notes WHERE embedding IS NOT NULL AND neighbor_keywords IS NULL"
        )
        with self._pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [r[0] for r in rows]

    def get_paths_with_embeddings(self) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT path FROM notes WHERE embedding IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def db_stats(self) -> dict:
        long_running = 0
        with self._pool.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
            total = row[0] if row else 0
            by_type = conn.execute(
                "SELECT note_type, COUNT(*) FROM notes GROUP BY note_type ORDER BY 2 DESC"
            ).fetchall()
            try:
                fig_row = conn.execute("SELECT COUNT(*) FROM figures").fetchone()
                figures = fig_row[0] if fig_row else 0
            except Exception:
                figures = None
            # observability: queries running >5s on this database (excludes idle)
            try:
                lr = conn.execute(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'active' "
                    "AND now() - query_start > interval '5 seconds'"
                ).fetchone()
                long_running = lr[0] if lr else 0
            except Exception:
                pass
        # pool usage (psycopg_pool): in-use, available, waiters
        try:
            pool = self._pool.get_stats()
        except Exception:
            pool = {}
        return {
            "backend": "postgres",
            "total_notes": total,
            "by_type": {r[0]: r[1] for r in by_type},
            "db_path": _redact_dsn(self._dsn),
            "figures": figures,
            "pool": {
                "size": pool.get("pool_size"),
                "available": pool.get("pool_available"),
                "requests_waiting": pool.get("requests_waiting"),
            },
            "long_running_queries": long_running,
        }

    # ------------------------------------------------------------------
    # Audit log (MULTIUSER_PLAN P3)
    # ------------------------------------------------------------------

    def append_audit_log(self, user_id: str, tool: str, target: str = "") -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, tool, target) VALUES (%s, %s, %s)",
                [user_id, tool, target],
            )
            conn.commit()

    def query_audit_log(
        self,
        user_id: str | None = None,
        tool: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        conditions = []
        params: list = []
        if user_id is not None:
            conditions.append("user_id = %s")
            params.append(user_id)
        if tool is not None:
            conditions.append("tool = %s")
            params.append(tool)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT ts, user_id, tool, target FROM audit_log {where} "
                f"ORDER BY ts DESC LIMIT %s",
                params,
            ).fetchall()
        return [
            {"ts": str(r[0]), "user_id": r[1], "tool": r[2], "target": r[3]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # API key lifecycle (MULTIUSER_PLAN P4)
    # ------------------------------------------------------------------

    def get_identity_for_key(self, key_hash: str) -> "Identity | KeyState | None":
        """Return Identity for an active key, KeyState.REVOKED if revoked, else None.

        Revoked must stay distinguishable from unknown: auth.py falls back to an
        env-key admin identity on None, so answering None for a revoked key would
        promote it to admin instead of denying it.
        """
        from ..identity import Identity, KeyState  # local import: circular dependency

        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT user_id, role, revoked_at FROM api_keys WHERE key_hash = %s",
                [key_hash],
            ).fetchone()
        if row is None:
            return None
        if row[2] is not None:
            return KeyState.REVOKED
        return Identity(user_id=row[0], role=row[1])

    def count_active_api_keys(self) -> int:
        """Number of un-revoked keys — lets auth stay enabled with no env key set."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL"
            ).fetchone()
        return int(row[0]) if row else 0

    def register_api_key(self, key_hash: str, user_id: str, role: str) -> None:
        """Insert a new API key. Raises psycopg.errors.UniqueViolation if duplicate."""
        from ..identity import VALID_ROLES

        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}, got {role!r}")
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO api_keys (key_hash, user_id, role) VALUES (%s, %s, %s)",
                [key_hash, user_id, role],
            )
            conn.commit()

    def revoke_api_key(self, key_hash: str) -> bool:
        """Set revoked_at = NOW(). Returns True if a row was updated."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = NOW() "
                "WHERE key_hash = %s AND revoked_at IS NULL",
                [key_hash],
            )
            conn.commit()
            return (cur.rowcount or 0) > 0

    def list_api_keys(self, user_id: str | None = None, limit: int = 1000) -> list[dict]:
        """Return key records with truncated hash prefix (first 8 chars)."""
        conditions = []
        params: list = []
        if user_id is not None:
            conditions.append("user_id = %s")
            params.append(user_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT key_hash, user_id, role, created_at, revoked_at "
                f"FROM api_keys {where} ORDER BY created_at DESC LIMIT %s",
                params,
            ).fetchall()
        return [
            {
                "key_hash_prefix": r[0][:8],
                "user_id": r[1],
                "role": r[2],
                "created_at": str(r[3]),
                "revoked_at": str(r[4]) if r[4] else None,
            }
            for r in rows
        ]
