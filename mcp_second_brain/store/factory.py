"""Factory for VaultStore backends.

Backend is selected by the SB_DB_BACKEND environment variable:
  - "duckdb"   (default) → DuckDBStore
  - "postgres"           → PostgresStore

PostgresStore also requires SB_PG_DSN, e.g.:
  SB_PG_DSN=postgresql://postgres:password@localhost:5432/sb_personal
"""

from __future__ import annotations

import os

from .duckdb_store import DuckDBStore

_store_instance: DuckDBStore | None = None  # singleton per process


def get_store() -> DuckDBStore:
    """Return the process-level VaultStore instance (DuckDBStore or PostgresStore).

    Cached after first call — environment variables are read once at startup.
    """
    global _store_instance
    if _store_instance is not None:
        return _store_instance

    backend = os.environ.get("SB_DB_BACKEND", "duckdb").lower()

    if backend == "postgres":
        from .postgres_store import PostgresStore  # imported lazily (psycopg optional)
        dsn = os.environ.get("SB_PG_DSN", "")
        if not dsn:
            raise RuntimeError(
                "SB_DB_BACKEND=postgres requires SB_PG_DSN to be set, e.g. "
                "postgresql://postgres:password@localhost:5432/sb_personal"
            )
        _store_instance = PostgresStore(dsn)  # type: ignore[assignment]
    else:
        _store_instance = DuckDBStore()

    return _store_instance


def reset_store() -> None:
    """Clear the cached store instance (for testing only)."""
    global _store_instance
    _store_instance = None
