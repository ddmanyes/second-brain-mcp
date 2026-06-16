"""Storage backend abstraction for second-brain vault index.

Usage:
    from mcp_second_brain.store import get_store
    store = get_store()  # returns DuckDBStore or PostgresStore based on SB_DB_BACKEND
"""

from .base import VaultStore
from .factory import get_store, reset_store

__all__ = ["VaultStore", "get_store", "reset_store"]
