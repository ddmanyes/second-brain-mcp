"""The embedding dimension must agree across schema, code, and running model.

History: `postgres_schema.sql` declared `vector(768)` with the comment
"nomic-embed-text / bge-m3, both return 768d on this host". bge-m3 returns 1024d,
so that comment was false — and it is the likely reason a documented 2026-06-27
"migration to bge-m3" was recorded as done while nothing in the codebase ever
referenced bge (`git log -S"bge"` finds no commit). The architecture note claimed
the new stack for over two months while the system ran nomic at 768d.

A dimension mismatch is not silent here — `_embed_text` raises ValueError — but a
schema that disagrees with EMBED_DIM means every insert fails at runtime instead
of at build time. These tests move that failure forward.
"""
from __future__ import annotations

import re
from pathlib import Path

from mcp_second_brain import vault_db

SCHEMA_SQL = Path(vault_db.__file__).resolve().parent / "store" / "postgres_schema.sql"


def test_schema_vector_dim_matches_embed_dim():
    """`vector(N)` in the Postgres schema must equal vault_db.EMBED_DIM."""
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    match = re.search(r"embedding\s+vector\((\d+)\)", sql)
    assert match, "postgres_schema.sql no longer declares embedding as vector(N)"
    assert int(match.group(1)) == vault_db.EMBED_DIM, (
        f"postgres_schema.sql declares vector({match.group(1)}) but EMBED_DIM is "
        f"{vault_db.EMBED_DIM}. Changing the embedding model means changing both, "
        f"plus an ALTER on every existing database."
    )


def test_embed_dim_default_is_bge_m3():
    """Guard the pairing: the default model and the default dimension must match."""
    assert vault_db.EMBED_MODEL == "bge-m3"
    assert vault_db.EMBED_DIM == 1024


def test_autostart_port_matches_default_embed_port():
    """The auto-start fallback is gated on a port literal; a stale one makes it
    dead code, or worse, starts a different model on the port in use."""
    source = Path(vault_db.__file__).read_text(encoding="utf-8")
    gate = re.search(r"if EMBED_PORT != (\d+):", source)
    assert gate, "the embedding auto-start port gate is gone or reshaped"
    assert int(gate.group(1)) == vault_db.EMBED_PORT, (
        f"auto-start is gated on port {gate.group(1)} but EMBED_PORT defaults to "
        f"{vault_db.EMBED_PORT} — the fallback would never fire."
    )
