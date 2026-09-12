"""PostgresStore integration tests against an owned disposable container.

Run with ``pytest --run-postgres tests/test_postgres_store.py``. Without that
flag tests are skipped. External SB_PG_TEST_DSN / PG* overrides are rejected;
no localhost/default or externally managed database is ever used.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def store(multiuser_postgres):
    """Module-scoped PostgresStore connected to a test schema."""
    try:
        from mcp_second_brain.store.postgres_store import PostgresStore
    except ImportError:
        pytest.skip("psycopg not installed")

    # No external DSN, fallback connection or skip-on-failure: this endpoint
    # belongs to the explicitly opted-in, freshly verified tmpfs container.
    multiuser_postgres.reset()
    s = PostgresStore(multiuser_postgres.dsn())

    try:
        yield s
    finally:
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

    def test_structured_article_author_search(self, store, vault):
        paper = vault / "paper.md"
        paper.write_text(
            '---\ntitle: Hair Biology\ntype: research\nstatus: active\ntags: [research]\n'
            'authors: ["Sung-Jan Lin"]\n'
            'author_ids: ["0000-0002-1825-0097"]\n'
            'doi: "10.1000/lin"\npublication_year: 2024\n---\n\nbody',
            encoding="utf-8",
        )
        store.index_file(vault, paper)

        hits = store.search_articles(author="Lin SJ")

        assert hits[0]["path"] == "paper.md"
        assert hits[0]["matched_author"] == "Sung-Jan Lin"


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
    def test_upsert_and_search_figure(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.upsert_figure(
            "note1.md", 1, "http://img/1.png", "/local/1.png", "diagram text", "A chart", 100
        )
        results = store.search_figures("diagram")
        assert any(r["note_path"] == "note1.md" for r in results)

    def test_upsert_figure_updates_existing(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.upsert_figure("note1.md", 1, "http://img/1.png", "/local/1.png", "old ocr", "old desc", 50)
        store.upsert_figure("note1.md", 1, "http://img/1.png", "/local/1.png", "new ocr", "new desc", 60)
        results = store.search_figures("new ocr")
        assert any(r["note_path"] == "note1.md" for r in results)

    def test_get_figures_for_note_is_complete_and_ordered(self, store, vault):
        store.index_file(vault, vault / "note1.md")
        store.upsert_figure("note1.md", 2, "", "/local/2.png", "", "", 2)
        store.upsert_figure("note1.md", 0, "", "/local/0.png", "", "", 0)

        rows = store.get_figures_for_note("note1.md")

        indices = [row["fig_index"] for row in rows]
        assert indices == sorted(indices)
        assert {0, 2}.issubset(indices)
        assert {row["fig_index"]: row for row in rows}[0]["local_path"] == "/local/0.png"

    def test_reconcile_local_file_end_to_end_is_idempotent(self, store, vault):
        from PIL import Image

        from mcp_second_brain.figure_reconciliation import reconcile_figures

        note = vault / "repair-note.md"
        note.write_text("---\ntitle: Repair\n---\n\nbody", encoding="utf-8")
        store.index_file(vault, note)
        image = vault / "figures/repair-note/fig-00.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (2, 2), "white").save(image)

        first = reconcile_figures(
            ["repair-note.md"], vault, store, dry_run=False, limit=20,
        )
        second = reconcile_figures(
            ["repair-note.md"], vault, store, dry_run=False, limit=20,
        )

        assert first["summary"]["applied"] == 1
        assert second["summary"]["applied"] == 0
        assert store.get_figure("repair-note.md", 0)["local_path"] == str(image.resolve())

    def test_figure_identity_is_unique(self, store):
        from psycopg import errors

        note_path = "__task4_schema_unique__.md"
        with store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO notes(path) VALUES (%s) ON CONFLICT DO NOTHING",
                [note_path],
            )
            conn.execute(
                "INSERT INTO figures(note_path, fig_index) VALUES (%s, %s)",
                [note_path, 0],
            )
            conn.commit()

        with (
            pytest.raises(errors.UniqueViolation),
            store._pool.connection() as conn,
            conn.transaction(force_rollback=True),
        ):
            conn.execute(
                "INSERT INTO figures(note_path, fig_index) VALUES (%s, %s)",
                [note_path, 0],
            )

    def test_figure_index_is_required(self, store):
        from psycopg import errors

        note_path = "__task4_schema_not_null__.md"
        with store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO notes(path) VALUES (%s) ON CONFLICT DO NOTHING",
                [note_path],
            )
            conn.commit()

        with (
            pytest.raises(errors.NotNullViolation),
            store._pool.connection() as conn,
            conn.transaction(force_rollback=True),
        ):
            conn.execute(
                "INSERT INTO figures(note_path, fig_index) VALUES (%s, NULL)",
                [note_path],
            )

    def test_figure_requires_parent_note(self, store):
        from psycopg import errors

        note_path = "__task4_missing_parent__.md"
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM figures WHERE note_path = %s", [note_path])
            conn.execute("DELETE FROM notes WHERE path = %s", [note_path])
            conn.commit()

        with (
            pytest.raises(errors.ForeignKeyViolation),
            store._pool.connection() as conn,
            conn.transaction(force_rollback=True),
        ):
            conn.execute(
                "INSERT INTO figures(note_path, fig_index) VALUES (%s, %s)",
                [note_path, 0],
            )

    def test_deleting_parent_note_cascades_to_figure(self, store):
        note_path = "__task4_schema_cascade__.md"
        with store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO notes(path) VALUES (%s) ON CONFLICT DO NOTHING",
                [note_path],
            )
            conn.commit()
        store.upsert_figure(note_path, 0, "", "", "", "")

        with store._pool.connection() as conn:
            conn.execute("DELETE FROM notes WHERE path = %s", [note_path])
            conn.commit()

        assert store.get_figure(note_path, 0) is None


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

    def test_concurrent_figure_upsert_keeps_one_row(self, store):
        note_path = "__task4_concurrent_figure__.md"
        with store._pool.connection() as conn:
            conn.execute(
                "INSERT INTO notes(path) VALUES (%s) ON CONFLICT DO NOTHING",
                [note_path],
            )
            conn.execute("DELETE FROM figures WHERE note_path = %s", [note_path])
            conn.commit()

        worker_count = 8
        start = threading.Barrier(worker_count)
        errors: list[Exception] = []

        def worker(worker_id: int) -> None:
            try:
                start.wait()
                store.upsert_figure(
                    note_path,
                    0,
                    f"https://example.test/{worker_id}.png",
                    f"/tmp/{worker_id}.png",
                    f"ocr-{worker_id}",
                    f"description-{worker_id}",
                    worker_id,
                    f"caption-{worker_id}",
                )
            except Exception as error:  # noqa: BLE001 - thread reports every failure to caller
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == [], f"Concurrent figure upsert raised: {errors}"
        with store._pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM figures WHERE note_path = %s AND fig_index = %s",
                [note_path, 0],
            ).fetchone()
        assert row[0] == 1
