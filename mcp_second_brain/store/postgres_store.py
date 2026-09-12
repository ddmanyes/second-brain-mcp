"""PostgresStore — VaultStore implementation backed by Postgres + pgvector.

Connection pool: psycopg[binary,pool] (psycopg3).
Vector similarity: pgvector HNSW index, cosine ops.
Keyword FTS: pg_trgm similarity (language-neutral, CJK-safe) + tsvector for English.

Environment variables:
  SB_PG_DSN   — PostgreSQL DSN, e.g. postgresql://postgres:pw@localhost:5432/sb_personal
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..identity import Identity, KeyState

import psycopg
from psycopg_pool import ConnectionPool

# The markdown → index projection is shared with DuckDBStore; the seam between the
# two backends is "how it is stored", not "what a note is".
from dataclasses import dataclass

from ..note_row import project_note, FRONTMATTER_RE, NoteRow
from ..article_metadata import (
    article_result,
    author_candidate_terms,
    normalise_author_name,
    normalise_doi,
)

# vault_db still owns the embedding client and the vault schema validator, plus the
# pure vector helpers (_cosine, _path_penalty). Those don't touch DuckDB.
from .. import vault_db as _vdb

# Phase B — chunk-level embeddings (late chunking, see chunking.py/late_chunking.py).
from ..late_chunking import chunk_and_embed, LateChunkingUnavailable
from ..snippets import strip_references

# Decision 2 — reranker (see reranker.py's docstring for the "top-1 chunk gets
# fooled by boilerplate" lesson this module's NUM_CHUNKS_PER_CANDIDATE encodes).
from ..request_budget import request_embedding
from .. import reranker as _reranker

# Lab-open plan (2026-09-12) — see visibility.py's module docstring for the
# full picture. Imported at module scope (not lazily like ..identity below)
# because _conn() needs multiuser_enabled() on every checkout, not just on
# the API-key paths.
from .. import visibility as _visibility

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


# NOTE: an earlier version of this module had a _merge_by_path_max_score()
# helper here that pre-merged the notes-level and chunk-level result lists
# for each modality (by keeping the higher raw score per path) before RRF
# fusion. Removed 2026-09-04 — see hybrid_search()'s docstring for why
# score-merging two differently-scaled embedding sources silently defeated
# Phase B's purpose (the "back_half_1" retrieval gap). hybrid_search() now
# feeds all four ranked lists into RRF directly instead.


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
        # Lab-open plan (2026-09-12): SB_MULTIUSER — see visibility.py. Read
        # once at construction (matches how SB_DB_BACKEND/SB_PG_DSN are also
        # only read once, in factory.py) rather than on every _conn() call.
        self._multiuser = _visibility.multiuser_enabled()
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            kwargs={"autocommit": False},
        )
        try:
            self._apply_schema()
        except Exception:
            self._pool.close()
            raise

    def _apply_schema(self) -> None:
        if self._multiuser:
            self._verify_multiuser_schema()
            return
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

    def _verify_multiuser_schema(self) -> None:
        """SB_MULTIUSER=1 connects as the non-superuser sb_app role (see the
        plan's Phase 2), which lacks CREATE EXTENSION / ALTER TABLE / CREATE
        POLICY privileges — running postgres_schema.sql or
        postgres_rls_schema.sql here, as _apply_schema does for single-user
        deployments, would fail outright (and if it didn't fail, doing DDL on
        every process start from a fleet of servers is its own hazard).
        Migration is a separate, explicit step run once as the owning role
        (see store/migrate_multiuser.py); this only verifies it already
        happened, so a forgotten migration fails loudly at startup — serving
        traffic with no RLS enabled must never happen silently.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                WITH protected AS (
                    SELECT c.oid, c.relowner, c.relrowsecurity, c.relforcerowsecurity
                    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relname IN ('notes', 'note_chunks', 'figures')
                      AND c.relkind = 'r'
                )
                SELECT
                    (SELECT count(*) = 3 FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND table_name IN ('notes', 'note_chunks', 'figures')
                       AND column_name = 'owner_id'),
                    (SELECT count(*) = 2 FROM information_schema.columns
                     WHERE table_schema = 'public' AND table_name = 'api_keys'
                       AND column_name IN ('user_uuid', 'expires_at')),
                    (SELECT count(*) = 3 FROM protected),
                    COALESCE((SELECT bool_and(relrowsecurity AND relforcerowsecurity)
                              FROM protected), false),
                    EXISTS(SELECT 1 FROM pg_roles WHERE rolname = current_user
                           AND NOT rolsuper AND NOT rolbypassrls),
                    NOT EXISTS(SELECT 1 FROM protected
                               WHERE pg_has_role(current_user, relowner, 'MEMBER')),
                    to_regclass('notes') = to_regclass('public.notes')
                        AND to_regclass('note_chunks') = to_regclass('public.note_chunks')
                        AND to_regclass('figures') = to_regclass('public.figures'),
                    to_regclass('api_keys') = to_regclass('public.api_keys')
                """
            ).fetchone()
        if not row or not all(row):
            raise RuntimeError(
                "SB_MULTIUSER=1 requires migrated public tables, ENABLE/FORCE RLS "
                "on notes/note_chunks/figures, and a NOSUPERUSER NOBYPASSRLS role "
                "without membership in table-owning roles. The search path must "
                "resolve the public tables. Apply migrations separately; startup "
                "will not repair an unsafe database."
            )

    @contextmanager
    def _conn(self, *, timeout=None):
        """Checkout a pooled connection and, in multiuser mode, bind the
        caller's identity to the transaction-local Postgres GUCs the RLS
        policies (postgres_rls_schema.sql) read via current_setting(). This
        replaces plain self._pool.connection() everywhere below so RLS
        applies to every query without each of them repeating the SET.

        A no-op wrapper around self._pool.connection() when SB_MULTIUSER is
        unset — :9100/:9106 get byte-identical behaviour to before this
        existed. Missing identity never grants an administrative bypass; it
        gets shared-only visibility (also needed for pre-auth key lookup).
        Background maintenance needing private rows must bind an explicit,
        authorized Identity rather than relying on an absent request context.
        """
        from contextlib import ExitStack
        from ..request_budget import remaining_timeout, stage

        bound = remaining_timeout(timeout if timeout is not None else (0.5 if self._multiuser else 30))
        with ExitStack() as stack:
            with stage("pool_wait"):
                checkout = self._pool.connection() if timeout is None and not self._multiuser else self._pool.connection(timeout=bound)
                conn = stack.enter_context(checkout)
            if self._multiuser:
                from ..identity import get_current_identity

                identity = get_current_identity()
                actor_id = (identity.user_uuid or "") if identity is not None else ""
                is_admin = identity is not None and identity.is_admin()
                milliseconds = str(max(1, int(remaining_timeout(2.0) * 1000)))
                conn.execute(
                    "SELECT set_config('sb.actor_id', %s, true), "
                    "set_config('sb.actor_is_admin', %s, true), "
                    "set_config('statement_timeout', %s, true), "
                    "set_config('lock_timeout', '500', true)",
                    [actor_id, "on" if is_admin else "off", milliseconds],
                )
            with stage("db"):
                yield conn

    def index_shared_article_metadata(self, vault: Path, md_file: Path) -> bool:
        """Bounded text/metadata index update without model calls or enrichment.

        Only the managed shared committer should call this after publication.
        Figures, chunks and vectors retain separate readiness; this returns True
        for the notes row transaction only.
        """
        from ..article_attribution import validate_shared_destination
        from ..identity import get_current_identity
        from ..vault_paths import resolve_in_vault

        identity = get_current_identity()
        if identity is None or not identity.user_uuid or not identity.can_write():
            raise PermissionError("trusted article identity required")
        root = Path(vault).resolve()
        rel = Path(md_file).resolve().relative_to(root).as_posix()
        validate_shared_destination(rel)
        target = resolve_in_vault(root, rel, must_exist=True)
        if target.suffix != ".md":
            raise ValueError("article index requires Markdown")
        note = project_note(root, target)
        with self._conn(timeout=0.5) as conn:
            conn.execute("SELECT set_config('statement_timeout', '1000', true), "
                         "set_config('lock_timeout', '500', true)")
            with conn.cursor() as cur:
                self._write_note_plan(cur, self._NotePlan(note=note, chunks=None))
        return True

    def close(self) -> None:
        self._pool.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Plan / write split (architecture debt fix, 2026-09-04)
    # ------------------------------------------------------------------
    #
    # _plan_note_upsert() / _plan_chunks_for_note() do all the slow work — file
    # reads, embed_text() and chunk_and_embed()'s external HTTP calls — with no
    # Postgres transaction open. _write_note_plan() / _write_chunks() are pure
    # SQL, safe to run inside a short-lived transaction. Before this split,
    # _sync_chunks_for_note() ran chunk_and_embed() (which can take minutes on
    # a long document's sliding windows) while holding the very cursor/
    # transaction its caller (index_file/sync_all/sync_incremental) had opened
    # — an idle-in-transaction connection for the whole HTTP round trip. Under
    # load this was observed to make unrelated read_note/new_note calls time
    # out (see the plan note's Phase B execution record). Splitting compute
    # from write removes that window entirely: every transaction opened below
    # now contains INSERT/UPDATE/DELETE only.

    @dataclass
    class _NotePlan:
        note: NoteRow
        chunks: list[tuple[str, list[float]]] | None  # None = leave existing chunks untouched

    def _plan_note_upsert(self, vault: Path, md_file: Path) -> "PostgresStore._NotePlan | None":
        """Compute (no DB writes) everything needed to upsert one note + its
        chunks. Returns None if the note's content_hash is unchanged — mirrors
        the pre-refactor early-return, including that chunks are *not*
        revisited for an unchanged note (that's sync_chunks()'s job, per
        decision 1: an unchanged note is never revisited by sync_all/
        sync_incremental's hash short-circuit).
        """
        rel = str(md_file.relative_to(vault))
        chash = _vdb._content_hash_of_file(md_file)

        with self._conn() as conn:
            row = conn.execute(
                "SELECT content_hash FROM notes WHERE path = %s", [rel]
            ).fetchone()
        if row and row[0] == chash:
            return None

        note = project_note(
            vault, md_file, embed=_vdb.embed_text, validate=_vdb.validate_note,
            log_prefix="pg_store",
        )
        chunks = self._plan_chunks_for_note(note.path, note.content_hash, md_file)
        return PostgresStore._NotePlan(note=note, chunks=chunks)

    def _plan_chunks_for_note(
        self, note_path: str, content_hash: str, md_file: Path
    ) -> list[tuple[str, list[float]]] | None:
        """Compute (no DB writes) the new chunk set for one note (decision 1
        of the chunking/embedding plan).

        Returns None when: the stored chunk hash already matches (skip, avoids
        recomputing embeddings for a multi-MB paper on every sync pass), the
        file can't be read, or the late-chunking server is unavailable — in
        all three cases the caller must leave existing chunks untouched rather
        than delete-with-nothing-to-replace-them (a transient outage should
        never leave a note with zero chunks). Returns [] to mean "delete only,
        nothing to insert" (empty body after stripping references).

        Reads the file's full text independently of project_note() — that
        projection truncates files over note_row.LARGE_FILE_THRESHOLD
        (Phase B-0), which would defeat the point of chunking (built
        specifically to reach the parts of long documents the single-vector
        embedding can't).
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT content_hash FROM note_chunks WHERE note_path = %s LIMIT 1",
                [note_path],
            ).fetchone()
        if row and row[0] == content_hash:
            return None

        try:
            full_text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            print("[pg_store] chunk read unavailable" if _visibility.multiuser_enabled() else f"[pg_store] chunk sync: read failed for {note_path}: {e}", file=sys.stderr)
            return None

        # References are the cited papers' claims, not this note's own (same
        # rationale as embed_text_for's Phase B-0 fix) — no reason to spend
        # chunks, an HNSW index, and a trgm index on someone else's bibliography.
        body = strip_references(FRONTMATTER_RE.sub("", full_text).strip())
        if not body.strip():
            return []

        try:
            return chunk_and_embed(body)
        except LateChunkingUnavailable as e:
            print(
                "[pg_store] chunk embedding unavailable; existing chunks preserved" if _visibility.multiuser_enabled() else
                f"[pg_store] chunk sync: embedding unavailable for {note_path}, keeping existing chunks: {e}",
                file=sys.stderr,
            )
            return None

    def _write_chunks(
        self,
        cur: psycopg.Cursor,
        note_path: str,
        content_hash: str,
        chunks: list[tuple[str, list[float]]],
        owner_id: str | None = None,
    ) -> None:
        """Pure SQL: full replace (DELETE + re-INSERT) of one note's chunks.

        Never an in-place update — the caller (_write_note_plan / sync_chunks)
        only calls this once the new chunk set is already computed in hand,
        so the DELETE is never left with nothing to replace it.

        owner_id (lab-open plan, 2026-09-12): denormalised from the parent
        note (visibility.owner_of_path(note_path)) so postgres_rls_schema.sql
        can filter note_chunks directly, without an EXISTS subquery against
        notes on every row — see that file's header comment.
        """
        cur.execute("DELETE FROM note_chunks WHERE note_path = %s", [note_path])
        for idx, (chunk_text, emb) in enumerate(chunks):
            cur.execute(
                """
                INSERT INTO note_chunks
                    (note_path, chunk_idx, chunk_text, content_hash, embedding, owner_id)
                VALUES (%s, %s, %s, %s, %s::vector, %s)
                """,
                [note_path, idx, chunk_text, content_hash, str(emb) if emb else None, owner_id],
            )

    def _write_note_plan(self, cur: psycopg.Cursor, plan: "PostgresStore._NotePlan") -> None:
        """Pure SQL: write a precomputed _NotePlan. No HTTP, safe inside a
        short-lived transaction."""
        note = plan.note
        # Lab-open plan (2026-09-12): owner_id is derived purely from the
        # note's path (visibility.owner_of_path), so it is always recomputed
        # on every write rather than COALESCEd like the optional enrichment
        # fields above it — a note that moved out of/into 90-personal/ must
        # have its owner_id change to match, not keep stale ownership.
        owner_id = _visibility.owner_of_path(note.path)
        cur.execute(
            """
            INSERT INTO notes (
                path, title, note_type, status, tags, note_date,
                content_hash, body_snippet, embedding, violations,
                semantic_keywords, neighbor_keywords, cluster_topic,
                authors, author_ids, author_search, doi, pmid, pmcid,
                journal, publication_year, canonical_url, owner_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s::vector, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s
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
                cluster_topic      = COALESCE(EXCLUDED.cluster_topic, notes.cluster_topic),
                authors            = EXCLUDED.authors,
                author_ids         = EXCLUDED.author_ids,
                author_search      = EXCLUDED.author_search,
                doi                = EXCLUDED.doi,
                pmid               = EXCLUDED.pmid,
                pmcid              = EXCLUDED.pmcid,
                journal            = EXCLUDED.journal,
                publication_year   = EXCLUDED.publication_year,
                canonical_url      = EXCLUDED.canonical_url,
                owner_id           = EXCLUDED.owner_id
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
                note.authors_json,
                note.author_ids_json,
                note.author_search,
                note.doi,
                note.pmid,
                note.pmcid,
                note.journal,
                note.publication_year,
                note.canonical_url,
                owner_id,
            ],
        )

        if plan.chunks is not None:
            self._write_chunks(cur, note.path, note.content_hash, plan.chunks, owner_id)

    # ------------------------------------------------------------------
    # Core indexing
    # ------------------------------------------------------------------

    def index_file(self, vault: Path, md_file: Path) -> None:
        plan = self._plan_note_upsert(vault, md_file)  # HTTP calls happen here, no transaction open
        if plan is None:
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                self._write_note_plan(cur, plan)
            conn.commit()

    def index_metadata_only(
        self,
        vault: Path,
        md_file: Path,
        *,
        previous_content_hash: str,
        expected_body_sha256: str,
    ) -> None:
        """Refresh a frontmatter-only projection without recomputing vectors.

        This is intentionally narrower than :meth:`index_file`: callers must
        prove the body is unchanged, and the stored row must still represent
        the exact pre-edit file.  Title and tags are checked separately because
        they are part of the note-level embedding input even though they live
        in frontmatter.  Only then may the existing note/chunk embeddings be
        retained while their content hashes advance to the post-edit file.

        The method is used by deterministic bibliographic backfills.  Any
        concurrent edit, stale chunk set, or embedding-input change fails
        closed so the caller can roll the canonical file back.
        """
        raw = md_file.read_text(encoding="utf-8", errors="strict")
        body = FRONTMATTER_RE.sub("", raw, count=1)
        body_sha256 = hashlib.sha256(body.encode()).hexdigest()
        if body_sha256 != expected_body_sha256:
            raise ValueError("metadata-only index body hash mismatch")

        note = project_note(
            vault,
            md_file,
            embed=None,
            validate=_vdb.validate_note,
            log_prefix="pg_store_metadata_only",
        )
        with self._conn() as conn:
            with conn.cursor() as cur:
                stored = cur.execute(
                    "SELECT content_hash, title, tags FROM notes WHERE path = %s FOR UPDATE",
                    [note.path],
                ).fetchone()
                if stored is None:
                    raise ValueError("metadata-only index requires an existing note")
                if stored[0] != previous_content_hash:
                    raise ValueError("metadata-only index stored content hash changed")
                if stored[1] != note.title or stored[2] != note.tags_json:
                    raise ValueError("metadata-only index embedding inputs changed")

                chunk_hashes = {
                    row[0]
                    for row in cur.execute(
                        "SELECT DISTINCT content_hash FROM note_chunks WHERE note_path = %s",
                        [note.path],
                    ).fetchall()
                }
                if chunk_hashes and chunk_hashes != {previous_content_hash}:
                    raise ValueError("metadata-only index stored chunk hash changed")

                self._write_note_plan(
                    cur, PostgresStore._NotePlan(note=note, chunks=None)
                )
                cur.execute(
                    "UPDATE note_chunks SET content_hash = %s WHERE note_path = %s",
                    [note.content_hash, note.path],
                )
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
                print("[pg-sync] scan unavailable" if _visibility.multiuser_enabled() else f"[pg-sync] rglob attempt {attempt + 1} failed (Drive deadlock?): {e}", file=sys.stderr)
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
                # Compute phase first (HTTP calls, no transaction open), then one
                # short write transaction for the whole batch — see the "Plan /
                # write split" note above _plan_note_upsert.
                plans = []
                for f in batch:
                    plans.append(self._plan_note_upsert(vault, f))
                    seen.add(str(f.relative_to(vault)))
                    count += 1
                with self._conn() as conn:
                    with conn.cursor() as cur:
                        for plan in plans:
                            if plan is not None:
                                self._write_note_plan(cur, plan)
                    conn.commit()
                batch = []

        # Reconcile: remove stale rows
        with self._conn() as conn:
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
            print("[pg-sync] scan unavailable" if _visibility.multiuser_enabled() else f"[pg-sync] rglob failed (Drive deadlock?): {e}", file=sys.stderr)
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
        plans = []
        for f in changed:
            try:
                plans.append(self._plan_note_upsert(vault, f))  # HTTP here, no transaction open
                updated += 1
            except OSError as e:
                print("[pg-sync] note sync skipped" if _visibility.multiuser_enabled() else f"[pg-sync] skip {f.name}: {e}", file=sys.stderr)
                skipped += 1
        with self._conn() as conn:
            with conn.cursor() as cur:
                for plan in plans:
                    if plan is not None:
                        self._write_note_plan(cur, plan)
            conn.commit()
        return {"updated": updated, "skipped": skipped}

    def sync_if_stale(self, vault: Path) -> None:
        # No-op: the central Postgres index is kept fresh by the scheduled
        # `second-brain-pg-sync` job (30-min incremental), so a live query is
        # always current. Startup does not trigger its own re-scan.
        return

    def sync_embeddings(self, vault: Path | None = None) -> dict:
        with self._conn() as conn:
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
                            print("[pg_store] text read retry" if _visibility.multiuser_enabled() else f"[pg_store] read_text retry for {path}: {e}", file=sys.stderr)
                            time.sleep(delay)
                    else:
                        try:
                            full_text = md_file.read_text(encoding="utf-8", errors="ignore")
                        except OSError as e:
                            print("[pg_store] embedding skipped" if _visibility.multiuser_enabled() else f"[pg_store] skip embedding {path} (Drive deadlock?): {e}", file=sys.stderr)
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
                print("[pg_store] invalid embedding dimensions" if _visibility.multiuser_enabled() else f"[pg_store] embedding dim error: {path} — {e}", file=sys.stderr)
                vec = None
            if vec:
                updates.append((str(vec), path))
                updated += 1
            else:
                failed += 1

        if updates:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    for vec_str, path in updates:
                        cur.execute(
                            "UPDATE notes SET embedding = %s::vector WHERE path = %s",
                            [vec_str, path],
                        )
                conn.commit()

        return {"updated": updated, "failed": failed, "skipped": len(rows) - updated - failed}

    def sync_chunks(self, vault: Path, limit: int | None = None) -> dict:
        """Backfill note_chunks for notes that don't have a matching hash yet.

        Mirrors sync_embeddings()'s "backfill what's missing" semantics but for
        the chunks table. Necessary as a *separate* pass because
        _plan_note_upsert's content_hash short-circuit means an unchanged note
        is never revisited by sync_all/sync_incremental — a brand-new
        note_chunks table (or any note that predates this feature) needs this
        explicit backfill once. After that, ordinary edits keep chunks current
        automatically (decision 1).

        limit: cap how many candidate notes this call processes (ordered by
        path, for determinism across repeated calls). None means "no cap,
        process everything" — used by the dedicated backfill tool. A bounded
        default is what sync_index() passes on every call, so that tool's
        runtime stays predictable instead of unbounded when a large backlog
        is outstanding (architecture debt fixed 2026-09-04, see the plan
        note's Phase B execution record: sync_index() was observed to run
        90+ minutes reprocessing the same 4215 notes a concurrent backfill
        script was also working through). The returned "remaining" count lets
        a caller decide whether to run it again.
        """
        with self._conn() as conn:
            total_row = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT n.path
                    FROM notes n
                    LEFT JOIN note_chunks c
                        ON c.note_path = n.path AND c.content_hash = n.content_hash
                    WHERE c.note_path IS NULL
                    GROUP BY n.path
                ) t
                """
            ).fetchone()
            total_candidates = total_row[0] if total_row else 0

            sql = """
                SELECT n.path, n.content_hash
                FROM notes n
                LEFT JOIN note_chunks c
                    ON c.note_path = n.path AND c.content_hash = n.content_hash
                WHERE c.note_path IS NULL
                GROUP BY n.path, n.content_hash
                ORDER BY n.path
            """
            params: list = []
            if limit is not None:
                sql += " LIMIT %s"
                params = [limit]
            rows = conn.execute(sql, params).fetchall()

        updated, failed = 0, 0
        for path, content_hash in rows:
            md_file = vault / path
            if not md_file.exists():
                continue
            try:
                chunks = self._plan_chunks_for_note(path, content_hash, md_file)  # HTTP, no txn
                if chunks is not None:
                    with self._conn() as conn:
                        with conn.cursor() as cur:
                            self._write_chunks(cur, path, content_hash, chunks)
                        conn.commit()
                updated += 1
            except Exception as e:
                print("[pg_store] chunk sync unavailable" if _visibility.multiuser_enabled() else f"[pg_store] sync_chunks failed for {path}: {e}", file=sys.stderr)
                failed += 1

        remaining = max(total_candidates - updated, 0)
        return {
            "updated": updated,
            "failed": failed,
            "candidates": len(rows),
            "remaining": remaining,
        }

    def compute_neighbor_keywords(
        self, threshold: float = 0.75, top_n: int = 5
    ) -> dict[str, dict]:
        cache = self.load_embedding_cache()
        if not cache or len(cache) > 2000:
            return {}

        paths = list(cache.keys())
        with self._conn() as conn:
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
            with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
            conn.execute(
                "UPDATE notes SET status = %s WHERE path = %s", [status, path]
            )
            conn.commit()

    def update_snapshot(
        self, path: str, snapshot_path: str, tier: str, token_est: int
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE notes SET snapshot_path=%s, snapshot_tier=%s, snapshot_token_est=%s WHERE path=%s",
                [snapshot_path, tier, token_est, path],
            )
            conn.commit()

    def mark_rules_extracted(self, path: str) -> None:
        with self._conn() as conn:
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
        # Lab-open plan (2026-09-12): owner_id denormalised from note_path,
        # same reasoning as _write_chunks — see postgres_rls_schema.sql.
        owner_id = _visibility.owner_of_path(note_path)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO figures
                   (note_path, fig_index, image_url, local_path, ocr_text, description,
                    token_est, caption, owner_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (note_path, fig_index) DO UPDATE SET
                       image_url = EXCLUDED.image_url,
                       local_path = EXCLUDED.local_path,
                       ocr_text = EXCLUDED.ocr_text,
                       description = EXCLUDED.description,
                       token_est = EXCLUDED.token_est,
                       caption = EXCLUDED.caption,
                       owner_id = EXCLUDED.owner_id""",
                [
                    note_path, fig_index, image_url, local_path, ocr_text, description,
                    token_est, caption, owner_id,
                ],
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _trgm_search(self, query: str, limit: int) -> list[dict]:
        """Trigram-based keyword search (language-neutral, CJK-safe)."""
        q = f"%{query}%"
        with self._conn() as conn:
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
        q_vec = request_embedding(query, _vdb.embed_text)
        if not q_vec:
            return []
        vec_str = str(q_vec)
        with self._conn() as conn:
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
    # Both paths query note_chunks *in addition to* notes — hybrid_search()
    # feeds all four resulting lists into RRF directly (see its docstring for
    # why not score-merging notes+chunks first) — not a full swap. A note
    # that hasn't been chunked yet (mid-backfill, or a transient
    # late-chunking-server outage at write time — see _plan_chunks_for_note)
    # would otherwise vanish from search entirely until its chunks catch up;
    # keeping the notes-level query as a floor means coverage only ever gets
    # wider, never narrower, exactly the "silent regression" failure mode
    # decision 1 calls out.

    def _trgm_search_chunks(self, query: str, limit: int) -> list[dict]:
        """Chunk-level trigram search, aggregated to note level (max score).

        Reaches text the notes-level search cannot: body_snippet only covers
        a note's first 500 chars; chunk_text covers the whole document
        (minus references) via note_chunks (see B-1's index-choice note for
        why this needs its own trgm/GIN index rather than reusing notes').
        """
        q = f"%{query}%"
        with self._conn() as conn:
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
        q_vec = request_embedding(query, _vdb.embed_text)
        if not q_vec:
            return []
        vec_str = str(q_vec)
        funnel = max(limit * 20, 200)
        with self._conn() as conn:
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
        with self._conn() as conn:
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

        Fusion strategy (fixed 2026-09-04, the "back_half_1" retrieval-gap
        candidate from the plan note): all four ranked lists — notes-BM25,
        chunks-BM25, notes-semantic, chunks-semantic — feed RRF *directly*,
        each contributing its own 1/(rank+k) term. An earlier version first
        score-merged notes+chunks within each modality (keeping the higher
        raw cosine score per path) before RRF. That silently defeated Phase
        B's own purpose: a whole-note embedding averages over up to ~32K
        chars (Phase B-0) and so scores *systematically* higher on a broad,
        thematically-clustered corpus than any single paragraph's embedding
        does for a narrow, back-half-only query — even when that paragraph is
        the one genuinely relevant chunk. Score-merging by raw value then
        buried the chunk-only match under dozens of note-level matches with
        merely-thematic similarity (confirmed live: a target whose best chunk
        ranked #10 of 18 in an isolated chunk-only semantic search fell to
        rank 46 of 52 once merged with the notes-level list by score, pushing
        it out of the candidate pool entirely). RRF is specifically designed
        to fuse heterogeneous ranked lists *without* needing their scores to
        be on a comparable scale — feeding all four lists into it directly,
        instead of pre-merging two of them by score first, is what actually
        uses that property instead of working around it.
        """
        bm25_notes = self._trgm_search(query, limit=limit * 2)
        bm25_chunks = self._trgm_search_chunks(query, limit=limit * 2)
        sem_notes = self._semantic_search(query, limit=limit * 2)
        sem_chunks = self._semantic_search_chunks(query, limit=limit * 2)
        all_lists = (bm25_notes, bm25_chunks, sem_notes, sem_chunks)

        if exclude_types and any(all_lists):
            excluded = set(exclude_types)
            candidate_paths = list({r["path"] for lst in all_lists for r in lst})
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT path, note_type FROM notes WHERE path = ANY(%s)",
                    [candidate_paths],
                ).fetchall()
            excluded_paths = {path for path, ntype in rows if ntype in excluded}
            bm25_notes = [r for r in bm25_notes if r["path"] not in excluded_paths]
            bm25_chunks = [r for r in bm25_chunks if r["path"] not in excluded_paths]
            sem_notes = [r for r in sem_notes if r["path"] not in excluded_paths]
            sem_chunks = [r for r in sem_chunks if r["path"] not in excluded_paths]
            all_lists = (bm25_notes, bm25_chunks, sem_notes, sem_chunks)

        rrf_scores: dict[str, float] = {}
        penalty_map: dict[str, float] = {}

        def _rrf(rank: int, k: int = 60) -> float:
            return 1.0 / (rank + k)

        for lst in all_lists:
            for rank, r in enumerate(lst):
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
        with self._conn() as conn:
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
            query_vec = request_embedding(query, _vdb.embed_text)
            if query_vec:
                chunks_by_path = self._top_chunks_for_paths(
                    [r["path"] for r in results], query_vec, _reranker.NUM_CHUNKS_PER_CANDIDATE
                )
                results = _reranker.rerank_candidates(query, results, chunks_by_path)

        return results[:limit]

    def hybrid_search_grouped(self, query: str, limit: int = 10) -> dict[str, list[dict]]:
        """See base.py's docstring for why this must exist as a VaultStore
        method (RLS inheritance for search_grouped) rather than server.py
        calling vault_db.hybrid_search_grouped() directly. Mirrors that
        function's knowledge/news split exactly, but through self.hybrid_search
        / self.search_news — both already route through self._conn(), which
        is what makes Postgres RLS apply here for free.
        """
        knowledge = self.hybrid_search(query, limit=limit, exclude_types=_vdb.KNOWLEDGE_EXCLUDE)
        news = self.search_news(query, days=7, limit=limit)
        return {"knowledge": knowledge, "news": news}

    def search_news(self, query: str, days: int = 7, limit: int = 20) -> list[dict]:
        q_like = f"% {query} %" if query.isdigit() else f"%{query.lower()}%"
        with self._conn() as conn:
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

    def search_articles(
        self,
        *,
        author: str = "",
        title: str = "",
        doi: str = "",
        pmid: str = "",
        pmcid: str = "",
        year: int = 0,
        limit: int = 20,
    ) -> list[dict]:
        """Search structured article metadata without body/reference false positives."""
        if not any((author, title, doi, pmid, pmcid, year)):
            return []
        clauses = [
            "(note_type = 'research' OR authors IS NOT NULL OR doi IS NOT NULL "
            "OR pmid IS NOT NULL OR pmcid IS NOT NULL)"
        ]
        params: list[object] = []
        if author:
            try:
                terms = author_candidate_terms(author)
            except ValueError:
                return []
            clauses.append(
                "(" + " OR ".join(
                    "COALESCE(author_search, '') ILIKE %s" for _ in terms
                ) + ")"
            )
            params.extend(f"%{normalise_author_name(term)}%" for term in terms)
        if title:
            clauses.append("COALESCE(title, '') ILIKE %s")
            params.append(f"%{title}%")
        if doi:
            clauses.append("lower(COALESCE(doi, '')) = %s")
            params.append(normalise_doi(doi))
        if pmid:
            clauses.append("COALESCE(pmid, '') = %s")
            params.append("".join(char for char in pmid if char.isdigit()))
        if pmcid:
            canonical_pmcid = pmcid.strip().upper()
            if canonical_pmcid and not canonical_pmcid.startswith("PMC"):
                canonical_pmcid = f"PMC{canonical_pmcid}"
            clauses.append("upper(COALESCE(pmcid, '')) = %s")
            params.append(canonical_pmcid)
        if year:
            clauses.append("publication_year = %s")
            params.append(year)

        params.append(max(limit * 10, 100))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT path, title, authors, author_ids, doi, pmid, pmcid,
                       journal, publication_year, canonical_url
                FROM notes
                WHERE {' AND '.join(clauses)}
                ORDER BY publication_year DESC NULLS LAST, title
                LIMIT %s
                """,
                params,
            ).fetchall()

        keys = (
            "path", "title", "authors_json", "author_ids_json", "doi", "pmid",
            "pmcid", "journal", "publication_year", "canonical_url",
        )
        results = [
            result
            for row in rows
            if (result := article_result(dict(zip(keys, row, strict=True)), author=author))
            is not None
        ]
        rank = {"orcid": 4, "full_name": 3, "alias": 2, "surname": 1, "identifier": 0}
        results.sort(
            key=lambda item: (
                -rank.get(str(item["match_type"]), 0),
                -(int(item["publication_year"] or 0)),
                str(item["title"]),
            )
        )
        return results[:limit]

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
        with self._conn() as conn:
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
        with self._conn() as conn:
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

    def get_figures_for_note(self, note_path: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT note_path, fig_index, image_url, local_path, ocr_text, "
                "description, coalesce(caption,''), coalesce(token_est,0) "
                "FROM figures WHERE note_path = %s ORDER BY fig_index",
                [note_path],
            ).fetchall()
        return [
            {
                "note_path": row[0], "fig_index": row[1],
                "image_url": row[2], "local_path": row[3],
                "ocr_text": row[4], "description": row[5],
                "caption": row[6], "token_est": row[7],
            }
            for row in rows
        ]

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
            with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT path, embedding FROM notes WHERE embedding IS NOT NULL"
            ).fetchall()
        return {r[0]: _parse_vec(r[1]) or [] for r in rows}

    def get_notes_with_snapshots(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT path FROM notes WHERE snapshot_path IS NOT NULL AND snapshot_path != ''"
            ).fetchall()
        return {Path(r[0]).name for r in rows}

    def get_rules_candidates(
        self, min_access: int = 5, stale_days: int = 90
    ) -> list[str]:
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        return bool(row and row[0] > 0)

    def get_snapshot_path(self, path: str) -> str | None:
        with self._conn() as conn:
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
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        return [r[0] for r in rows]

    def get_paths_for_neighbor_keywords(self, force: bool = False) -> list[str]:
        sql = (
            "SELECT path FROM notes WHERE embedding IS NOT NULL"
            if force
            else "SELECT path FROM notes WHERE embedding IS NOT NULL AND neighbor_keywords IS NULL"
        )
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        return [r[0] for r in rows]

    def get_paths_with_embeddings(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT path FROM notes WHERE embedding IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def db_stats(self) -> dict:
        long_running = 0
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        with self._conn() as conn:
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
        """Return Identity for an active, unexpired key, KeyState.REVOKED if
        revoked OR expired, else None.

        Revoked must stay distinguishable from unknown: auth.py falls back to an
        env-key admin identity on None, so answering None for a revoked key would
        promote it to admin instead of denying it. Lab-open plan (2026-09-12):
        an expired key (expires_at in the past) gets exactly the same
        treatment as a revoked one — Phase 4's acceptance test is "expires_at
        過期同樣 401", not "200 as some other role".
        """
        from ..identity import Identity, KeyState  # local import: circular dependency

        with self._conn(timeout=0.5) as conn:
            conn.execute("SELECT set_config('statement_timeout', '1000', true), "
                         "set_config('lock_timeout', '500', true)")
            row = conn.execute(
                "SELECT user_id, role, revoked_at, user_uuid, "
                "(expires_at IS NOT NULL AND expires_at <= now()) AS expired "
                "FROM api_keys WHERE key_hash = %s",
                [key_hash],
            ).fetchone()
        if row is None:
            return None
        if row[2] is not None or row[4]:
            return KeyState.REVOKED
        user_uuid = str(row[3]) if row[3] is not None else None
        return Identity(user_id=row[0], role=row[1], user_uuid=user_uuid, credential_id=key_hash)

    def count_active_api_keys(self) -> int:
        """Number of un-revoked keys — lets auth stay enabled with no env key set."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL"
            ).fetchone()
        return int(row[0]) if row else 0

    def register_api_key(
        self,
        key_hash: str,
        user_id: str,
        role: str,
        *,
        user_uuid: str | None = None,
        expires_days: int | None = None,
    ) -> None:
        """Insert a new API key. Raises psycopg.errors.UniqueViolation if duplicate.

        user_uuid: canonical UUID from EP lab-access (lab_identity_invitations /
            lab_person_profiles) — None for legacy keys, matching api_keys.user_uuid
            being nullable. Required in practice for role='member' (see
            visibility.slugify_user), but not validated here — manage_lab_access.py's
            person activate is what enforces that pairing before calling this.
        expires_days: if given, expires_at is set to now() + this many days;
            None (default) means no expiry, same as every key before this plan.
        """
        from ..identity import VALID_ROLES

        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(VALID_ROLES)}, got {role!r}")
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=expires_days)
            if expires_days is not None
            else None
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO api_keys (key_hash, user_id, role, user_uuid, expires_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                [key_hash, user_id, role, user_uuid, expires_at],
            )
            conn.commit()

    def revoke_api_key(self, key_hash: str) -> bool:
        """Set revoked_at = NOW(). Returns True if a row was updated."""
        with self._conn() as conn:
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
        with self._conn() as conn:
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
