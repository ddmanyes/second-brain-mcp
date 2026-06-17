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
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

# Import shared parsing/embedding utilities from vault_db (they don't touch the DB).
from .. import vault_db as _vdb

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
        sql = schema_path.read_text(encoding="utf-8")
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
        """Parse md_file and upsert into Postgres notes table."""
        if md_file.stat().st_size > _vdb._LARGE_FILE_THRESHOLD:
            raw = md_file.read_bytes()
            chash = _vdb._content_hash(raw.decode("utf-8", errors="ignore"))
            text = raw[: _vdb._LARGE_FILE_READ_LIMIT].decode("utf-8", errors="ignore")
        else:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
            chash = _vdb._content_hash(text)

        fm = _vdb._parse_frontmatter(text)
        rel = str(md_file.relative_to(vault))

        row = cur.execute(
            "SELECT content_hash FROM notes WHERE path = %s", [rel]
        ).fetchone()
        if row and row[0] == chash:
            return

        tags_raw = fm.get("tags", "[]")
        tags_json = tags_raw if tags_raw.startswith("[") else json.dumps([tags_raw])

        if fm.get("type") == "cnyes_archive":
            tickers_raw = fm.get("tickers", "[]")
            try:
                tickers_str = " ".join(json.loads(tickers_raw))
            except Exception:
                tickers_str = tickers_raw
            snippet = (tickers_str + " " + _vdb._body_snippet(text, max_chars=400))[:500]
        else:
            snippet = _vdb._body_snippet(text)

        prose = _vdb._embed_text_for(text)
        tags_for_embed = fm.get("tags", "")
        embed_input = f"{fm.get('title', md_file.stem)} {tags_for_embed} {prose}".strip()
        try:
            vec = _vdb.embed_text(embed_input)
            if vec is None:
                print(f"[pg_store] embedding failed: {rel}", file=sys.stderr)
        except ValueError as e:
            print(f"[pg_store] embedding dim error: {rel} — {e}", file=sys.stderr)
            vec = None

        violations = _vdb.validate_note(fm, rel)
        violations_json = json.dumps(violations) if violations else None

        sk_raw = fm.get("semantic_keywords", "").strip()
        if not sk_raw:
            sk_json = None
        elif sk_raw.startswith("["):
            try:
                sk_list = json.loads(sk_raw)
                sk_json = json.dumps(sk_list, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                inner = sk_raw.strip("[]")
                sk_list = [s.strip().strip('"').strip("'") for s in inner.split(",") if s.strip()]
                sk_json = json.dumps(sk_list, ensure_ascii=False) if sk_list else None
        else:
            sk_list = [s.strip() for s in sk_raw.split(",") if s.strip()]
            sk_json = json.dumps(sk_list, ensure_ascii=False) if sk_list else None

        nk_raw = fm.get("neighbor_keywords", "")
        if isinstance(nk_raw, list):
            nk_json = json.dumps(nk_raw, ensure_ascii=False) if nk_raw else None
        else:
            nk_raw = str(nk_raw).strip()
            if nk_raw.startswith("["):
                try:
                    nk_json = json.dumps(json.loads(nk_raw), ensure_ascii=False)
                except (json.JSONDecodeError, ValueError):
                    nk_json = None
            else:
                nk_list = [s.strip() for s in nk_raw.split(",") if s.strip()]
                nk_json = json.dumps(nk_list, ensure_ascii=False) if nk_list else None

        cluster_topic = fm.get("cluster_topic", None) or None

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
                rel,
                fm.get("title", md_file.stem),
                fm.get("type", "note"),
                fm.get("status", "active"),
                tags_json,
                _vdb._parse_date(fm.get("date", "")),
                chash,
                snippet,
                str(vec) if vec else None,   # cast to vector via SQL ::vector
                violations_json,
                sk_json,
                nk_json,
                cluster_topic,
            ],
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

        all_files = [
            f
            for f in vault.rglob("*.md")
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
        changed = [
            f
            for f in vault.rglob("*.md")
            if not any(p in f.parts for p in (".obsidian", ".claude", "templates"))
            and f.stat().st_mtime > db_mtime
        ]
        if not changed:
            return {"updated": 0, "skipped": "all fresh"}
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                for f in changed:
                    self._upsert_note_row(cur, vault, f)
            conn.commit()
        return {"updated": len(changed)}

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
                if md_file.exists():
                    full_text = md_file.read_text(encoding="utf-8", errors="ignore")
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

    def hybrid_search(
        self,
        query: str,
        limit: int = 20,
        alpha: float = 0.5,  # noqa: ARG002 — kept for API compat with DuckDBStore
        exclude_types: list[str] | None = None,
        fusion: str = "rrf",  # noqa: ARG002 — always RRF for Postgres; kept for compat
        apply_path_penalty: bool = True,
    ) -> list[dict]:
        bm25 = self._trgm_search(query, limit=limit * 2)
        sem = self._semantic_search(query, limit=limit * 2)

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

        scored = sorted(
            rrf_scores.items(),
            key=lambda x: x[1] * penalty_map[x[0]],
            reverse=True,
        )[:limit]

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

        return [
            {
                "path": p,
                "title": meta.get(p, ("", ""))[0],
                "note_type": meta.get(p, ("", ""))[1],
                "score": round(score_map[p], 6),
            }
            for p, _ in scored
            if p in meta
        ]

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
