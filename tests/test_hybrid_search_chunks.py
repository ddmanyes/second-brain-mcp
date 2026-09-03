"""Tests for Phase B-4: chunk-level search merged into hybrid_search.

_merge_by_path_max_score is pure (no DB). The integration tests need a real
Postgres (sb_test convention, see test_postgres_store.py) and monkeypatch
chunk_and_embed with a deterministic fake — same reasoning as
test_note_chunks_sync.py.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_second_brain.store.postgres_store import _merge_by_path_max_score

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


class TestMergeByPathMaxScore:
    def test_keeps_the_higher_score_for_a_path_in_both_lists(self):
        a = [{"path": "x.md", "title": "X", "score": 0.5}]
        b = [{"path": "x.md", "title": "X", "score": 0.9}]
        merged = _merge_by_path_max_score(a, b)
        assert merged == [{"path": "x.md", "title": "X", "score": 0.9}]

    def test_unions_paths_present_in_only_one_list(self):
        a = [{"path": "a.md", "title": "A", "score": 0.3}]
        b = [{"path": "b.md", "title": "B", "score": 0.7}]
        merged = _merge_by_path_max_score(a, b)
        assert {r["path"] for r in merged} == {"a.md", "b.md"}

    def test_result_is_sorted_descending_by_score(self):
        a = [{"path": "low.md", "title": "L", "score": 0.1}]
        b = [{"path": "high.md", "title": "H", "score": 0.9}, {"path": "mid.md", "title": "M", "score": 0.5}]
        merged = _merge_by_path_max_score(a, b)
        assert [r["path"] for r in merged] == ["high.md", "mid.md", "low.md"]

    def test_empty_lists_give_empty_result(self):
        assert _merge_by_path_max_score([], []) == []

    def test_merges_more_than_two_lists(self):
        a = [{"path": "x.md", "title": "X", "score": 0.1}]
        b = [{"path": "x.md", "title": "X", "score": 0.4}]
        c = [{"path": "x.md", "title": "X", "score": 0.2}]
        merged = _merge_by_path_max_score(a, b, c)
        assert merged[0]["score"] == 0.4


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
