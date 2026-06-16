"""Tests for PostgresStore backend.

Requires a running Postgres instance. Set SB_PG_TEST_DSN to override.
Default: postgresql://postgres:sbpassword@localhost:5432/sb_personal

Run:
    SB_PG_TEST_DSN=... pytest tests/test_postgres_store.py -v
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

TEST_DSN = os.environ.get(
    "SB_PG_TEST_DSN",
    "postgresql://postgres:sbpassword@localhost:5432/sb_personal",
)


@pytest.fixture(scope="module")
def store():
    """Module-scoped PostgresStore connected to a test schema."""
    try:
        from mcp_second_brain.store.postgres_store import PostgresStore
    except ImportError:
        pytest.skip("psycopg not installed")

    try:
        s = PostgresStore(TEST_DSN)
    except Exception as e:
        pytest.skip(f"Postgres unavailable: {e}")

    # Clean slate for tests
    with s._pool.connection() as conn:
        conn.execute("DELETE FROM figures")
        conn.execute("DELETE FROM notes")
        conn.commit()

    yield s
    s.close()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Minimal vault directory with a few markdown files."""
    v = tmp_path / "vault"
    v.mkdir()
    (v / "note1.md").write_text(
        "---\ntitle: Alpha Note\ntype: note\nstatus: active\ntags: [test]\n---\n\nContent of alpha.",
        encoding="utf-8",
    )
    (v / "note2.md").write_text(
        "---\ntitle: Beta Note\ntype: note\nstatus: active\ntags: [test]\n---\n\nContent of beta.",
        encoding="utf-8",
    )
    return v


# ---------------------------------------------------------------------------
# Basic upsert / db_stats
# ---------------------------------------------------------------------------

class TestBasicOps:
    def test_index_file_and_stats(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        stats = store.db_stats()
        assert stats["total_notes"] >= 1

    def test_index_file_idempotent(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.index_file(vault, vault / "note1.md")  # second call is no-op
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE path = %s", ["note1.md"]
            ).fetchone()
        assert row[0] == 1

    def test_set_note_status(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.set_note_status("note1.md", "archived")
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT status FROM notes WHERE path = %s", ["note1.md"]
            ).fetchone()
        assert row[0] == "archived"
        # restore
        store.set_note_status("note1.md", "active")

    def test_record_access(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.record_access("note1.md")
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT access_count FROM notes WHERE path = %s", ["note1.md"]
            ).fetchone()
        assert row[0] >= 1

    def test_mark_rules_extracted(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.mark_rules_extracted("note1.md")
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT rules_extracted_at FROM notes WHERE path = %s", ["note1.md"]
            ).fetchone()
        assert row[0] is not None

    def test_update_snapshot(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.update_snapshot("note1.md", "/snapshots/note1.png", "compact", 500)
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT snapshot_path, snapshot_tier, snapshot_token_est FROM notes WHERE path = %s",
                ["note1.md"],
            ).fetchone()
        assert row[0] == "/snapshots/note1.png"
        assert row[1] == "compact"
        assert row[2] == 500

    def test_get_notes_with_snapshots(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.update_snapshot("note1.md", "/snap/note1.png", "t1", 100)
        snaps = store.get_notes_with_snapshots()
        assert "note1.md" in snaps


# ---------------------------------------------------------------------------
# sync_all
# ---------------------------------------------------------------------------

class TestSyncAll:
    def test_sync_all_indexes_all_files(self, store, vault):
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM notes")
            conn.commit()
        result = store.sync_all(vault)
        assert result["synced"] == 2
        stats = store.db_stats()
        assert stats["total_notes"] == 2

    def test_sync_all_prunes_deleted_files(self, store, vault):
        store.sync_all(vault)
        # Remove one file from vault and re-sync
        (vault / "note2.md").unlink()
        result = store.sync_all(vault)
        assert result["synced"] == 1
        with store._pool.connection() as conn:
            rows = conn.execute("SELECT path FROM notes").fetchall()
        paths = [r[0] for r in rows]
        assert "note2.md" not in paths
        # Restore
        (vault / "note2.md").write_text(
            "---\ntitle: Beta Note\ntype: note\nstatus: active\ntags: [test]\n---\n\nContent of beta.",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

class TestFigures:
    def test_upsert_and_search_figure(self, store):
        store.upsert_figure(
            "note1.md", 1, "http://img/1.png", "/local/1.png", "diagram text", "A chart", 100
        )
        results = store.search_figures("diagram")
        assert any(r["note_path"] == "note1.md" for r in results)

    def test_upsert_figure_updates_existing(self, store):
        store.upsert_figure("note1.md", 1, "http://img/1.png", "/local/1.png", "old ocr", "old desc", 50)
        store.upsert_figure("note1.md", 1, "http://img/1.png", "/local/1.png", "new ocr", "new desc", 60)
        results = store.search_figures("new ocr")
        assert any(r["note_path"] == "note1.md" for r in results)


# ---------------------------------------------------------------------------
# Search (keyword / ranking — does not require embedding server)
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_figures_empty_query(self, store):
        assert store.search_figures("") == []

    def test_top_by_recency_returns_list(self, store, vault):
        store.sync_all(vault)
        store.record_access("note1.md")
        results = store.top_by_recency(limit=10)
        assert isinstance(results, list)
        if results:
            assert "path" in results[0]

    def test_top_by_score_returns_list(self, store, vault):
        store.sync_all(vault)
        results = store.top_by_score(limit=10)
        assert isinstance(results, list)

    def test_sleep_candidates_returns_list(self, store, vault):
        store.sync_all(vault)
        results = store.sleep_candidates(min_age_days=0, max_score=9999)
        assert isinstance(results, list)

    def test_get_rules_candidates(self, store, vault):
        store.sync_all(vault)
        # bump access count so the note qualifies
        with store._pool.connection() as conn:
            conn.execute("UPDATE notes SET access_count = 10 WHERE path = 'note1.md'")
            conn.commit()
        candidates = store.get_rules_candidates(min_access=5, stale_days=0)
        assert "note1.md" in candidates


# ---------------------------------------------------------------------------
# Concurrent writes — no lock errors, no data corruption
# ---------------------------------------------------------------------------

class TestConcurrentWrites:
    def test_concurrent_upsert_no_errors(self, store, vault):
        errors: list[Exception] = []
        notes = [vault / "note1.md", vault / "note2.md"]

        def worker(md_file: Path) -> None:
            try:
                for _ in range(5):
                    store.index_file(vault, md_file)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in notes * 4]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent upsert raised: {errors}"
        with store._pool.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
        assert row[0] == 2  # exactly 2 distinct notes
