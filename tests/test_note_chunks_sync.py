"""Tests for Phase B chunk-sync: PostgresStore._sync_chunks_for_note / sync_chunks.

Requires a running Postgres instance — same sb_test convention as
test_postgres_store.py (see that file's module docstring for the DSN/safety
guard). ``chunk_and_embed`` is monkeypatched with a deterministic fake so these
tests don't depend on a live late-chunking (--pooling none) server — the real
pipeline (chunking.py + late_chunking.py) has its own dedicated test suites
(test_chunking.py, test_late_chunking.py) plus live-server verification
documented in the plan's Phase B execution notes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DSN = os.environ.get(
    "SB_PG_TEST_DSN",
    "postgresql://postgres:postgres@localhost:5432/sb_test",
)
if TEST_DSN.rsplit("/", 1)[-1] in {"sb_personal", "sb_lab"}:
    raise RuntimeError(
        f"Refusing to run destructive Postgres tests against live DB in DSN: {TEST_DSN}. "
        "Point SB_PG_TEST_DSN at a throwaway database (e.g. sb_test)."
    )

FM = "---\ntitle: T\ntype: note\nstatus: active\ntags: []\n---\n\n"


@pytest.fixture(scope="module")
def store():
    try:
        from mcp_second_brain.store.postgres_store import PostgresStore
    except ImportError:
        pytest.skip("psycopg not installed")
    try:
        s = PostgresStore(TEST_DSN)
    except Exception as e:
        pytest.skip(f"Postgres unavailable: {e}")

    with s._pool.connection() as conn:
        conn.execute("DELETE FROM figures")
        conn.execute("DELETE FROM note_chunks")
        conn.execute("DELETE FROM notes")
        conn.commit()

    yield s
    s.close()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


def _fake_chunk_and_embed(text, **kwargs):
    """Deterministic fake: one chunk per paragraph. pgvector enforces the
    declared dimension exactly, so the fake vector must be 1024-wide even
    though only its first element is meaningful for these tests."""
    paras = [p for p in text.split("\n\n") if p.strip()]
    return [(p, [float(len(p))] + [0.0] * 1023) for p in paras]


def _patch_fake(monkeypatch):
    monkeypatch.setattr(
        "mcp_second_brain.store.postgres_store.chunk_and_embed", _fake_chunk_and_embed
    )


def _chunk_texts(store, note_path: str) -> list[str]:
    with store._pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_text FROM note_chunks WHERE note_path = %s ORDER BY chunk_idx",
            [note_path],
        ).fetchall()
    return [r[0] for r in rows]


class TestSyncChunksForNote:
    def test_creates_chunks_for_a_new_note(self, store, vault, monkeypatch):
        _patch_fake(monkeypatch)
        f = vault / "note1.md"
        f.write_text(FM + "para one\n\npara two", encoding="utf-8")
        store.index_file(vault, f)
        assert _chunk_texts(store, "note1.md") == ["para one", "para two"]

    def test_unchanged_content_skips_chunk_and_embed_call(self, store, vault, monkeypatch):
        calls: list[str] = []

        def spy(text, **kwargs):
            calls.append(text)
            return _fake_chunk_and_embed(text, **kwargs)

        monkeypatch.setattr("mcp_second_brain.store.postgres_store.chunk_and_embed", spy)

        f = vault / "note2.md"
        f.write_text(FM + "same content", encoding="utf-8")
        store.index_file(vault, f)
        assert len(calls) == 1

        store.index_file(vault, f)  # same bytes, content_hash unchanged
        assert len(calls) == 1  # not called again

    def test_changed_content_rebuilds_chunks(self, store, vault, monkeypatch):
        _patch_fake(monkeypatch)
        f = vault / "note3.md"
        f.write_text(FM + "original text", encoding="utf-8")
        store.index_file(vault, f)
        f.write_text(FM + "changed text now", encoding="utf-8")
        store.index_file(vault, f)
        assert _chunk_texts(store, "note3.md") == ["changed text now"]

    def test_embedding_server_unavailable_keeps_existing_chunks(self, store, vault, monkeypatch):
        _patch_fake(monkeypatch)
        f = vault / "note4.md"
        f.write_text(FM + "first version", encoding="utf-8")
        store.index_file(vault, f)

        from mcp_second_brain.late_chunking import LateChunkingUnavailable

        def boom(text, **kwargs):
            raise LateChunkingUnavailable("server down")

        monkeypatch.setattr("mcp_second_brain.store.postgres_store.chunk_and_embed", boom)

        f.write_text(FM + "second version, server is down now", encoding="utf-8")
        store.index_file(vault, f)  # content_hash changed, but the embed call fails

        # Stale chunks from "first version" survive — never deleted without a
        # successful replacement in hand.
        assert _chunk_texts(store, "note4.md") == ["first version"]

    def test_references_are_stripped_before_chunking(self, store, vault, monkeypatch):
        captured: dict[str, str] = {}

        def spy(text, **kwargs):
            captured["text"] = text
            return _fake_chunk_and_embed(text, **kwargs)

        monkeypatch.setattr("mcp_second_brain.store.postgres_store.chunk_and_embed", spy)

        f = vault / "note5.md"
        f.write_text(
            FM + "Main claim here.\n\n## References\n\n1. Some Cited Paper, 2020.",
            encoding="utf-8",
        )
        store.index_file(vault, f)
        assert "Main claim here" in captured["text"]
        assert "Cited Paper" not in captured["text"]

    def test_note_deletion_cascades_to_chunks(self, store, vault, monkeypatch):
        _patch_fake(monkeypatch)
        f = vault / "note6.md"
        f.write_text(FM + "will be deleted", encoding="utf-8")
        store.index_file(vault, f)
        assert _chunk_texts(store, "note6.md") != []

        # sync_all's reconciliation deliberately no-ops when the vault scan
        # comes back empty (a safety guard against wiping the whole index on a
        # transient Drive-mount hiccup — see sync_all's docstring) — so this
        # test needs at least one file left in the vault for the DELETE
        # ... WHERE path NOT IN (seen) path to actually run.
        survivor = vault / "note6b.md"
        survivor.write_text(FM + "survives", encoding="utf-8")
        store.index_file(vault, survivor)

        f.unlink()
        store.sync_all(vault)  # reconciliation deletes the notes row -> ON DELETE CASCADE

        assert _chunk_texts(store, "note6.md") == []


class TestChunkSyncDoesNotHoldTransactionAcrossEmbedding:
    """Regression test for the 2026-09-04 architecture-debt fix: the slow
    chunk_and_embed() HTTP call must happen with no Postgres transaction/
    connection held, so it can't starve an unrelated concurrent query. Uses a
    dedicated max_size=1 store so the old (buggy) behavior — computing chunks
    while still holding the cursor's connection — would provably starve the
    pool; the fixed code releases the connection before calling
    chunk_and_embed(), so a second query goes through immediately even with
    only one connection available.
    """

    @pytest.fixture()
    def single_conn_store(self):
        from mcp_second_brain.store.postgres_store import PostgresStore

        try:
            s = PostgresStore(TEST_DSN, min_size=1, max_size=1)
        except Exception as e:
            pytest.skip(f"Postgres unavailable: {e}")
        yield s
        s.close()

    def test_pool_connection_is_free_during_chunk_and_embed_call(
        self, single_conn_store, vault, monkeypatch
    ):
        import threading
        import time

        entered_embed = threading.Event()
        release_embed = threading.Event()

        def slow_chunk_and_embed(text, **kwargs):
            entered_embed.set()
            release_embed.wait(timeout=5)
            return _fake_chunk_and_embed(text, **kwargs)

        monkeypatch.setattr(
            "mcp_second_brain.store.postgres_store.chunk_and_embed", slow_chunk_and_embed
        )

        f = vault / "note_concurrent.md"
        f.write_text(FM + "content for the concurrency regression test", encoding="utf-8")

        outcome: dict = {}

        def do_index():
            single_conn_store.index_file(vault, f)
            outcome["done"] = True

        t = threading.Thread(target=do_index)
        t.start()
        try:
            assert entered_embed.wait(timeout=5), "chunk_and_embed was never called"

            # A second, unrelated query on the same size-1 pool must not be
            # blocked by the in-flight embedding call — it would hang until
            # release_embed.set() below if a transaction were still held.
            start = time.monotonic()
            with single_conn_store._pool.connection() as conn:
                conn.execute("SELECT 1").fetchone()
            elapsed = time.monotonic() - start
            assert elapsed < 2.0, (
                f"unrelated query took {elapsed:.1f}s on a size-1 pool while "
                "chunk_and_embed was in flight — a transaction is being held "
                "across the HTTP call again"
            )
        finally:
            release_embed.set()
            t.join(timeout=5)
        assert outcome.get("done")


class TestSyncChunksBackfill:
    def test_backfills_then_is_a_noop_on_the_next_pass(self, store, vault, monkeypatch):
        _patch_fake(monkeypatch)
        f = vault / "note7.md"
        f.write_text(FM + "some content", encoding="utf-8")

        # Simulate a note that predates note_chunks: insert the notes row
        # directly, bypassing index_file, so it has no chunks yet.
        from mcp_second_brain.note_row import content_hash

        chash = content_hash(f.read_text(encoding="utf-8"))
        with store._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO notes (path, title, note_type, status, tags, content_hash) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (path) DO NOTHING",
                    ["note7.md", "T", "note", "active", "[]", chash],
                )
            conn.commit()
        assert _chunk_texts(store, "note7.md") == []

        result = store.sync_chunks(vault)
        assert result["updated"] >= 1
        assert _chunk_texts(store, "note7.md") == ["some content"]

        calls: list[str] = []

        def spy(text, **kwargs):
            calls.append(text)
            return _fake_chunk_and_embed(text, **kwargs)

        monkeypatch.setattr("mcp_second_brain.store.postgres_store.chunk_and_embed", spy)
        store.sync_chunks(vault)
        # note7's own paragraph text must not be among the re-processed calls —
        # note4.md (from an earlier test) is a legitimate, unrelated candidate
        # here too (its chunks were deliberately left stale by the "server
        # unavailable" test), so this only asserts about note7 specifically.
        assert "some content" not in calls
        with store._pool.connection() as conn:
            still_missing = conn.execute(
                """
                SELECT n.path FROM notes n
                LEFT JOIN note_chunks c
                    ON c.note_path = n.path AND c.content_hash = n.content_hash
                WHERE c.note_path IS NULL AND n.path = %s
                """,
                ["note7.md"],
            ).fetchall()
        assert still_missing == []


class TestSyncChunksLimit:
    """Regression test for the 2026-09-04 architecture-debt fix: sync_chunks()
    accepts a `limit` so sync_index()'s own backfill pass can't turn into an
    unbounded, hours-long call when a large backlog is outstanding (see
    PostgresStore.sync_chunks's docstring)."""

    def test_limit_caps_this_calls_processing_and_remaining_tracks_backlog(
        self, store, vault, monkeypatch
    ):
        _patch_fake(monkeypatch)
        from mcp_second_brain.note_row import content_hash

        for i in range(3):
            f = vault / f"limitnote{i}.md"
            f.write_text(FM + f"content {i}", encoding="utf-8")
            chash = content_hash(f.read_text(encoding="utf-8"))
            with store._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO notes (path, title, note_type, status, tags, content_hash) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (path) DO NOTHING",
                        [f"limitnote{i}.md", "T", "note", "active", "[]", chash],
                    )
                conn.commit()

        result = store.sync_chunks(vault, limit=2)
        assert result["candidates"] == 2  # exactly the cap, even though >2 are outstanding
        assert result["updated"] == 2
        assert result["remaining"] >= 1  # at least limitnote2.md is still outstanding

        # Draining with no cap eventually reaches zero remaining.
        drained = store.sync_chunks(vault, limit=None)
        assert drained["remaining"] == 0
        for i in range(3):
            assert _chunk_texts(store, f"limitnote{i}.md") == [f"content {i}"]
