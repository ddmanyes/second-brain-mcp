"""VaultStore — backend-agnostic protocol for second-brain vault index operations.

All public operations that callers (server.py, vault_sleep.py, figures.py) need
from the vault index are declared here.  Concrete implementations:
  - DuckDBStore  (store/duckdb_store.py)  — current default
  - PostgresStore (store/postgres_store.py) — target (SB_DB_BACKEND=postgres)

Pure utilities that don't touch the DB (blob ↔ vec conversion, cosine) stay in
vault_db.py and can be imported directly by callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..identity import Identity, KeyState


@runtime_checkable
class VaultStore(Protocol):
    # ------------------------------------------------------------------
    # Core indexing
    # ------------------------------------------------------------------

    def index_file(self, vault: Path, md_file: Path) -> None:
        """Index (upsert) a single markdown file and refresh FTS."""
        ...

    def sync_all(self, vault: Path) -> dict:
        """Full vault scan: upsert all .md files, prune stale rows, rebuild FTS.

        Returns {"synced": N, "embed_failed": M}.
        """
        ...

    def sync_incremental(self, vault: Path) -> dict:
        """Upsert only files newer than the index. Returns {"updated": N}.

        Does not prune orphan rows (nightly sync_all handles that).
        """
        ...

    def sync_if_stale(self, vault: Path) -> None:
        """Sync the index only if this backend considers it stale.

        Called on startup once ``has_index()`` is already True. Each backend owns
        its own staleness policy — the server must not know how staleness is decided
        (e.g. DuckDB throttles on the DB-file mtime; Postgres relies on a scheduled
        incremental sync and treats a live query as always fresh).
        """
        ...

    def sync_embeddings(self, vault: Path | None = None) -> dict:
        """Backfill embeddings for notes that are missing them.

        Returns {"updated": N, "failed": M, "skipped": K}.
        """
        ...

    def sync_chunks(self, vault: Path) -> dict:
        """Backfill note_chunks for notes whose chunks are missing or stale
        (Phase B of the chunking/embedding plan — late-chunked, paragraph-
        aligned chunk embeddings, separate from the single-vector notes.embedding).

        Ordinary sync (sync_all/sync_incremental) keeps chunks current for any
        note whose content actually changes; this backfills notes that predate
        the feature (or whose chunks failed on a previous pass). DuckDBStore
        does not implement Phase B chunking (Postgres is the only backend
        deployed live for sb/lcdda) and returns a no-op result.

        Returns {"updated": N, "failed": M, "candidates": K}.
        """
        ...

    def compute_neighbor_keywords(
        self, threshold: float = 0.75, top_n: int = 5
    ) -> dict[str, dict]:
        """Compute neighbor_keywords and cluster_topic for all embedded notes."""
        ...

    # ------------------------------------------------------------------
    # Note mutations
    # ------------------------------------------------------------------

    def record_access(self, path: str) -> None:
        """Increment access_count and update last_accessed for a note."""
        ...

    def set_note_status(self, path: str, status: str) -> None:
        """Update the status field of a note (e.g. 'archived', 'archive_backup')."""
        ...

    def update_snapshot(
        self, path: str, snapshot_path: str, tier: str, token_est: int
    ) -> None:
        """Update snapshot fields for an existing note."""
        ...

    def mark_rules_extracted(self, path: str) -> None:
        """Set rules_extracted_at = now for a note."""
        ...

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
        """Insert or update a figure record."""
        ...

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
        """Hybrid keyword + semantic search (BM25/trgm + cosine, RRF fusion)."""
        ...

    def search_news(
        self, query: str, days: int = 7, limit: int = 20
    ) -> list[dict]:
        """Search cnyes_archive notes within the last N days."""
        ...

    def search_figures(self, query: str, limit: int = 10) -> list[dict]:
        """Search figures by OCR text or description."""
        ...

    def get_figure(self, note_path: str, fig_index: int) -> dict | None:
        """Return a single figure row (for read_figure), or None if absent."""
        ...

    def find_related(
        self,
        path: str,
        limit: int = 5,
        threshold: float = 0.7,
        _embedding_cache: dict[str, list[float]] | None = None,
    ) -> list[str]:
        """Find semantically related notes by cosine similarity."""
        ...

    # ------------------------------------------------------------------
    # Ranking / retrieval
    # ------------------------------------------------------------------

    def top_by_recency(
        self, limit: int = 20, exclude_types: list[str] | None = None
    ) -> list[dict]:
        """Return top notes by last_accessed timestamp."""
        ...

    def top_by_score(
        self, limit: int = 20, exclude_types: list[str] | None = None
    ) -> list[dict]:
        """Return top notes by composite score."""
        ...

    def sleep_candidates(
        self, min_age_days: int = 90, max_score: float = 0.5
    ) -> list[dict]:
        """Return notes eligible for Vault Sleep consolidation."""
        ...

    # ------------------------------------------------------------------
    # Bulk / batch operations
    # ------------------------------------------------------------------

    def load_embedding_cache(self) -> dict[str, list[float]]:
        """Load all embeddings from index as {path: vec}. Used for batch ops."""
        ...

    def get_notes_with_snapshots(self) -> set[str]:
        """Return the set of note filenames (basename) that have a snapshot.

        Used by prune_archive to decide which archived notes are safe to delete.
        """
        ...

    def get_rules_candidates(
        self, min_access: int = 5, stale_days: int = 90
    ) -> list[str]:
        """Return paths of notes eligible for rule extraction.

        Criteria: access_count >= min_access AND
                  (rules_extracted_at IS NULL OR stale > stale_days ago).
        """
        ...

    def load_notes_with_embeddings(
        self, note_type_filter: str | None = None
    ) -> list[tuple[str, str, list[float]]]:
        """Return [(path, note_type, vec)] for notes with embeddings.

        Used by find_consolidation_candidates in vault_sleep.py.
        Filter by note_type when note_type_filter is given.
        """
        ...

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def db_stats(self) -> dict:
        """Return summary statistics about the vault index."""
        ...

    # ------------------------------------------------------------------
    # Server-side query helpers (used by server.py tool implementations)
    # ------------------------------------------------------------------

    def has_index(self) -> bool:
        """Return True if the index has been populated (at least one note)."""
        ...

    def get_snapshot_path(self, path: str) -> str | None:
        """Return the snapshot_path for a note, or None if absent."""
        ...

    def get_paths_for_semantic_keywords(self, force: bool = False) -> list[str]:
        """Return paths of notes that need semantic keyword extraction.

        force=False → only notes where semantic_keywords IS NULL.
        force=True  → all notes.
        """
        ...

    def get_paths_for_neighbor_keywords(self, force: bool = False) -> list[str]:
        """Return paths of notes with embeddings that need neighbor_keywords.

        force=False → only notes where neighbor_keywords IS NULL.
        force=True  → all notes with embeddings.
        """
        ...

    def get_paths_with_embeddings(self) -> list[str]:
        """Return paths of all notes that have an embedding vector."""
        ...

    # ------------------------------------------------------------------
    # Audit log (MULTIUSER_PLAN P3)
    # ------------------------------------------------------------------

    def append_audit_log(self, user_id: str, tool: str, target: str = "") -> None:
        """Append an immutable write-action record.

        user_id: identity.user_id or 'unknown' for unauthenticated writes.
        tool:    write tool name (e.g. 'new_note').
        target:  note_path / URL / title, or '' for batch operations.
        """
        ...

    def query_audit_log(
        self,
        user_id: str | None = None,
        tool: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return recent audit records, optionally filtered by user_id or tool.

        Returns list of {"ts", "user_id", "tool", "target"} dicts.
        """
        ...

    # ------------------------------------------------------------------
    # API key lifecycle (MULTIUSER_PLAN P4)
    # ------------------------------------------------------------------

    def get_identity_for_key(self, key_hash: str) -> Identity | KeyState | None:
        """Resolve a key hash to an Identity, KeyState.REVOKED, or None (unknown).

        The three outcomes must stay distinct: auth.py falls back to an env-key
        admin identity on None, so a revoked key reported as None would be
        promoted to admin rather than denied.
        DuckDB (single-user) always returns None → auth.py falls back to env-key admin.
        """
        ...

    def count_active_api_keys(self) -> int:
        """Number of un-revoked keys, so auth can stay on with no env key set."""
        ...

    def register_api_key(self, key_hash: str, user_id: str, role: str) -> None:
        """Insert a new API key row. Raises if key_hash already exists."""
        ...

    def revoke_api_key(self, key_hash: str) -> bool:
        """Set revoked_at = NOW() for the given key.

        Returns True if the key existed and was revoked, False if not found.
        """
        ...

    def list_api_keys(self, user_id: str | None = None, limit: int = 1000) -> list[dict]:
        """Return API key records.

        Returns list of {"key_hash_prefix", "user_id", "role", "created_at", "revoked_at"} dicts.
        key_hash is truncated to first 8 chars to avoid exposing the full hash.
        limit: max rows (default 1000).
        """
        ...
