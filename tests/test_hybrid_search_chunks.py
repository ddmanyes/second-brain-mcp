"""Tests for Phase B-4: chunk-level search merged into hybrid_search.

hybrid_search() feeds notes-BM25/chunks-BM25/notes-semantic/chunks-semantic
directly into RRF (see its docstring — a pre-RRF score-merge step used to sit
here, removed 2026-09-04 after it was found to silently bury genuine
chunk-only matches under thematically-similar notes-level noise; see
TestBackHalfRetrievalGap below for the regression test). The integration
tests need a real Postgres (sb_test convention, see test_postgres_store.py)
and monkeypatch chunk_and_embed with a deterministic fake — same reasoning
as test_note_chunks_sync.py.
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


# ---------------------------------------------------------------------------
# Integration: chunk text findable via hybrid_search even when it never
# appears in notes.body_snippet (the whole point of Phase B).
# ---------------------------------------------------------------------------

def _fake_chunk_and_embed(text, **kwargs):
    """One chunk per paragraph. Embedding: a near-unit vector whose direction
    encodes which distinctive keyword the paragraph contains, so cosine
    similarity behaves meaningfully in these tests instead of being arbitrary."""
    paras = [p for p in text.split("\n\n") if p.strip()]
    out = []
    for p in paras:
        vec = [0.0] * 1024
        # Deterministic "embedding": hash each word into one of 1024 slots.
        for i, word in enumerate(p.lower().split()):
            vec[hash(word) % 1024] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        vec = [v / norm for v in vec]
        out.append((p, vec))
    return out


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


class TestFusionDoesNotBuryChunkOnlyMatches:
    """Regression test for the 2026-09-04 fix (the "back_half_1" retrieval
    gap — see hybrid_search()'s docstring for the full root-cause writeup):
    a target findable only through the chunk-semantic list, at a modest rank
    within that list, must not be pushed out of the candidate pool by a flood
    of notes-level matches that merely score higher in raw (uncalibrated)
    terms — the exact failure mode of the removed score-merge-then-RRF design.
    """

    def test_target_found_only_via_chunks_survives_a_flood_of_notes_level_matches(
        self, store, monkeypatch
    ):
        # Every one of these 30 "decoys" only appears in the notes-level
        # semantic list, all scored higher in raw terms than target's chunk
        # hit — mimicking a thematically-clustered corpus where whole-note
        # embeddings systematically outscore any single paragraph's. 30 decoys
        # + 6 chunk-only entries = 36 total candidates, comfortably more than
        # the funnel (20): under the old score-merge design this guarantees
        # the funnel truncates before ever reaching target (dead last by raw
        # score); under RRF-direct fusion target lands with a comfortable
        # margin inside the top 20 (worked out by hand: ~12th) rather than
        # sitting right at the boundary.
        decoys = [
            {"path": f"decoy{i}.md", "title": f"D{i}", "score": 0.9 - i * 0.01}
            for i in range(30)
        ]
        # target.md is findable *only* via chunks, and not even at the top of
        # that list — 5 weaker chunk-only hits rank ahead of it there.
        chunk_list = [
            {"path": f"chunkonly{i}.md", "title": f"C{i}", "score": 0.5 - i * 0.01}
            for i in range(5)
        ] + [{"path": "target.md", "title": "Target", "score": 0.35}]

        monkeypatch.setattr(store, "_trgm_search", lambda q, limit: [])
        monkeypatch.setattr(store, "_trgm_search_chunks", lambda q, limit: [])
        monkeypatch.setattr(store, "_semantic_search", lambda q, limit: decoys)
        monkeypatch.setattr(store, "_semantic_search_chunks", lambda q, limit: chunk_list)

        all_paths = [r["path"] for r in decoys] + [r["path"] for r in chunk_list]
        with store._pool.connection() as conn:
            with conn.cursor() as cur:
                for p in all_paths:
                    cur.execute(
                        "INSERT INTO notes (path, title, note_type, status, tags, content_hash) "
                        "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (path) DO NOTHING",
                        [p, p, "note", "active", "[]", f"hash-{p}"],
                    )
            conn.commit()

        results = store.hybrid_search(
            "irrelevant query text", limit=20, apply_path_penalty=False, rerank=False
        )
        paths = [r["path"] for r in results]
        assert "target.md" in paths, (
            "target.md was buried out of the candidate pool by notes-level "
            "matches that only scored higher in raw (uncalibrated) terms — "
            "this is exactly the back_half_1 dilution bug"
        )


class TestHybridSearchReachesChunkText:
    def test_keyword_only_in_a_late_paragraph_is_findable(self, store, vault, monkeypatch):
        monkeypatch.setattr(
            "mcp_second_brain.store.postgres_store.chunk_and_embed", _fake_chunk_and_embed
        )
        # body_snippet only covers the first 500 chars — pad the front so the
        # distinctive term genuinely cannot appear there.
        padding = "filler sentence about nothing in particular. " * 20
        f = vault / "deepterm.md"
        f.write_text(
            FM + padding + "\n\n" + "zzyzxquux appears only in this trailing paragraph.",
            encoding="utf-8",
        )
        store.index_file(vault, f)

        with store._pool.connection() as conn:
            snippet = conn.execute(
                "SELECT body_snippet FROM notes WHERE path = %s", ["deepterm.md"]
            ).fetchone()[0]
        assert "zzyzxquux" not in snippet  # confirms the notes-level path can't see it

        results = store.hybrid_search("zzyzxquux", limit=10)
        assert "deepterm.md" in [r["path"] for r in results]

    def test_note_without_any_chunks_still_findable_via_notes_level_fallback(
        self, store, vault, monkeypatch
    ):
        """A note whose chunk sync never ran (e.g. late-chunking server was
        down at write time) must not vanish from search — B-4's merge, not
        replace, design."""
        monkeypatch.setattr(
            "mcp_second_brain.store.postgres_store.chunk_and_embed", _fake_chunk_and_embed
        )
        f = vault / "nochunkyet.md"
        f.write_text(FM + "unique marker fistfulofdollars right up front.", encoding="utf-8")
        store.index_file(vault, f)

        # Simulate "chunk sync never succeeded" by deleting its chunks after
        # the fact, without touching notes.embedding/body_snippet.
        with store._pool.connection() as conn:
            conn.execute("DELETE FROM note_chunks WHERE note_path = %s", ["nochunkyet.md"])
            conn.commit()

        results = store.hybrid_search("fistfulofdollars", limit=10)
        assert "nochunkyet.md" in [r["path"] for r in results]
