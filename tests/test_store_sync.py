"""Tests for DuckDBStore.sync_if_stale — the backend-owned staleness throttle (candidate D).

The server no longer knows *how* staleness is decided; it just calls sync_if_stale.
These tests pin the DuckDB backend's throttle contract.
"""

import time
from unittest.mock import MagicMock

import pytest

from mcp_second_brain import vault_db
from mcp_second_brain.store.duckdb_store import DuckDBStore


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_db, "DB_PATH", tmp_path / "vault.db")
    (tmp_path / "note.md").write_text("---\ntitle: n\n---\n\nbody\n", encoding="utf-8")
    return tmp_path


def _spy_incremental(monkeypatch):
    spy = MagicMock(return_value={"updated": 0})
    monkeypatch.setattr(vault_db, "sync_incremental", spy)
    return spy


def test_no_db_file_is_noop(vault, monkeypatch):
    spy = _spy_incremental(monkeypatch)
    DuckDBStore().sync_if_stale(vault)  # DB_PATH does not exist yet
    assert not spy.called


def test_recent_db_is_throttled(vault, monkeypatch):
    spy = _spy_incremental(monkeypatch)
    vault_db.DB_PATH.write_bytes(b"")  # fresh mtime = now
    DuckDBStore().sync_if_stale(vault)
    assert not spy.called


def test_stale_db_with_newer_markdown_triggers_sync(vault, monkeypatch):
    spy = _spy_incremental(monkeypatch)
    vault_db.DB_PATH.write_bytes(b"")
    old = time.time() - 4000  # older than the 30-min throttle window
    import os
    os.utime(vault_db.DB_PATH, (old, old))
    # note.md mtime is now (newer than the stale DB) → should sync
    DuckDBStore().sync_if_stale(vault)
    assert spy.called


def test_stale_db_but_no_newer_markdown_is_noop(vault, monkeypatch):
    spy = _spy_incremental(monkeypatch)
    vault_db.DB_PATH.write_bytes(b"")
    old = time.time() - 4000
    import os
    os.utime(vault_db.DB_PATH, (old, old))
    # Make the markdown even older than the DB → nothing newer → no sync
    older = time.time() - 5000
    os.utime(vault / "note.md", (older, older))
    DuckDBStore().sync_if_stale(vault)
    assert not spy.called
