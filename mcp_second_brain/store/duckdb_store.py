"""DuckDBStore — VaultStore implementation backed by the existing DuckDB vault_db module."""

from __future__ import annotations

from pathlib import Path

from .. import vault_db


class DuckDBStore:
    """Thin wrapper around vault_db.py that implements the VaultStore protocol.

    All methods delegate to vault_db functions; no logic is duplicated here.
    This class exists so callers can be written against VaultStore and switch
    to PostgresStore via SB_DB_BACKEND=postgres without changing call sites.
    """

    # ------------------------------------------------------------------
    # Core indexing
    # ------------------------------------------------------------------

    def index_file(self, vault: Path, md_file: Path) -> None:
        with vault_db._connect() as con:
            vault_db.upsert_note(con, vault, md_file)
            vault_db._ensure_fts(con)

    def sync_all(self, vault: Path) -> dict:
        return vault_db.sync_all(vault)

    def sync_incremental(self, vault: Path) -> dict:
        return vault_db.sync_incremental(vault)

    def sync_embeddings(self, vault: Path | None = None) -> dict:
        return vault_db.sync_embeddings(vault)

    def compute_neighbor_keywords(
        self, threshold: float = 0.75, top_n: int = 5
    ) -> dict[str, dict]:
        return vault_db.compute_neighbor_keywords(threshold, top_n)

    # ------------------------------------------------------------------
    # Note mutations
    # ------------------------------------------------------------------

    def record_access(self, path: str) -> None:
        vault_db.record_access(path)

    def set_note_status(self, path: str, status: str) -> None:
        with vault_db._connect() as con:
            con.execute(
                "UPDATE notes SET status = ? WHERE path = ?",
                [status, path],
            )

    def update_snapshot(
        self, path: str, snapshot_path: str, tier: str, token_est: int
    ) -> None:
        vault_db.update_snapshot(path, snapshot_path, tier, token_est)

    def mark_rules_extracted(self, path: str) -> None:
        with vault_db._connect() as con:
            con.execute(
                "UPDATE notes SET rules_extracted_at = current_timestamp WHERE path = ?",
                [path],
            )

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
        vault_db.upsert_figure(
            note_path, fig_index, image_url, local_path, ocr_text, description,
            token_est, caption,
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def hybrid_search(
        self,
        query: str,
        limit: int = 20,
        alpha: float = 0.5,
        exclude_types: list[str] | None = None,
        fusion: str = "rrf",
        apply_path_penalty: bool = True,
    ) -> list[dict]:
        return vault_db.hybrid_search(
            query,
            limit=limit,
            alpha=alpha,
            exclude_types=exclude_types,
            fusion=fusion,
            apply_path_penalty=apply_path_penalty,
        )

    def search_news(self, query: str, days: int = 7, limit: int = 20) -> list[dict]:
        return vault_db.search_news(query, days, limit)

    def search_figures(self, query: str, limit: int = 10) -> list[dict]:
        return vault_db.search_figures(query, limit)

    def get_figure(self, note_path: str, fig_index: int) -> dict | None:
        return vault_db.get_figure(note_path, fig_index)

    def find_related(
        self,
        path: str,
        limit: int = 5,
        threshold: float = 0.7,
        _embedding_cache: dict[str, list[float]] | None = None,
    ) -> list[str]:
        return vault_db.find_related(path, limit, threshold, _embedding_cache)

    # ------------------------------------------------------------------
    # Ranking / retrieval
    # ------------------------------------------------------------------

    def top_by_recency(
        self, limit: int = 20, exclude_types: list[str] | None = None
    ) -> list[dict]:
        return vault_db.top_by_recency(limit, exclude_types)

    def top_by_score(
        self, limit: int = 20, exclude_types: list[str] | None = None
    ) -> list[dict]:
        return vault_db.top_by_score(limit, exclude_types)

    def sleep_candidates(
        self, min_age_days: int = 90, max_score: float = 0.5
    ) -> list[dict]:
        return vault_db.sleep_candidates(min_age_days, max_score)

    # ------------------------------------------------------------------
    # Bulk / batch operations
    # ------------------------------------------------------------------

    def load_embedding_cache(self) -> dict[str, list[float]]:
        return vault_db.load_embedding_cache()

    def get_notes_with_snapshots(self) -> set[str]:
        with vault_db._connect() as con:
            rows = con.execute(
                "SELECT path FROM notes WHERE snapshot_path IS NOT NULL AND snapshot_path != ''"
            ).fetchall()
        return {Path(r[0]).name for r in rows}

    def get_rules_candidates(
        self, min_access: int = 5, stale_days: int = 90
    ) -> list[str]:
        with vault_db._connect() as con:
            rows = con.execute(
                """
                SELECT path FROM notes
                WHERE access_count >= ?
                  AND (rules_extracted_at IS NULL
                       OR rules_extracted_at < current_timestamp - INTERVAL (?) DAY)
                ORDER BY access_count DESC
                """,
                [min_access, stale_days],
            ).fetchall()
        return [r[0] for r in rows]

    def load_notes_with_embeddings(
        self, note_type_filter: str | None = None
    ) -> list[tuple[str, str, list[float]]]:
        with vault_db._connect() as con:
            sql = (
                "SELECT path, note_type, embedding FROM notes "
                "WHERE embedding IS NOT NULL AND (status IS NULL OR status != 'consolidated')"
            )
            rows = (
                con.execute(sql + " AND note_type = ?", [note_type_filter]).fetchall()
                if note_type_filter
                else con.execute(sql).fetchall()
            )
        return [(r[0], r[1], vault_db._blob_to_vec(r[2])) for r in rows]

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def db_stats(self) -> dict:
        return vault_db.db_stats()

    # ------------------------------------------------------------------
    # Server-side query helpers
    # ------------------------------------------------------------------

    def has_index(self) -> bool:
        if not vault_db.DB_PATH.exists():
            return False
        with vault_db._connect() as con:
            row = con.execute("SELECT COUNT(*) FROM notes").fetchone()
        return bool(row and row[0] > 0)

    def get_snapshot_path(self, path: str) -> str | None:
        with vault_db._connect() as con:
            row = con.execute(
                "SELECT snapshot_path FROM notes WHERE path = ?", [path]
            ).fetchone()
        return row[0] if row else None

    def get_paths_for_semantic_keywords(self, force: bool = False) -> list[str]:
        sql = "SELECT path FROM notes" if force else "SELECT path FROM notes WHERE semantic_keywords IS NULL"
        with vault_db._connect() as con:
            rows = con.execute(sql).fetchall()
        return [r[0] for r in rows]

    def get_paths_for_neighbor_keywords(self, force: bool = False) -> list[str]:
        sql = (
            "SELECT path FROM notes WHERE embedding IS NOT NULL"
            if force
            else "SELECT path FROM notes WHERE embedding IS NOT NULL AND neighbor_keywords IS NULL"
        )
        with vault_db._connect() as con:
            rows = con.execute(sql).fetchall()
        return [r[0] for r in rows]

    def get_paths_with_embeddings(self) -> list[str]:
        with vault_db._connect() as con:
            rows = con.execute(
                "SELECT path FROM notes WHERE embedding IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Audit log (MULTIUSER_PLAN P3) — noop in DuckDB mode
    # ------------------------------------------------------------------

    def append_audit_log(self, user_id: str, tool: str, target: str = "") -> None:  # noqa: ARG002
        pass  # DuckDB is single-user local mode; audit log is a Postgres feature

    def query_audit_log(
        self,
        user_id: str | None = None,  # noqa: ARG002
        tool: str | None = None,  # noqa: ARG002
        limit: int = 100,  # noqa: ARG002
    ) -> list[dict]:
        return []

    # ------------------------------------------------------------------
    # API key lifecycle (MULTIUSER_PLAN P4) — noop in DuckDB single-user mode
    # ------------------------------------------------------------------

    def get_identity_for_key(self, key_hash: str) -> None:  # noqa: ARG002
        return None  # triggers env-key admin fallback in auth.py

    def register_api_key(self, key_hash: str, user_id: str, role: str) -> None:  # noqa: ARG002
        pass

    def revoke_api_key(self, key_hash: str) -> bool:  # noqa: ARG002
        return False

    def list_api_keys(self, user_id: str | None = None) -> list[dict]:  # noqa: ARG002
        return []
