"""Multiuser search keeps RRF retrieval while cross-encoder use is explicit."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from mcp_second_brain.store import postgres_store as module
from mcp_second_brain.store.postgres_store import PostgresStore


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def execute(self, _query, _parameters):
        return _Rows([("shared.md", "Shared", "resource")])


def _store(monkeypatch, *, multiuser: bool, reranker_calls: list[str]) -> PostgresStore:
    store = object.__new__(PostgresStore)
    store._multiuser = multiuser

    retrieval_calls: list[str] = []

    def results(name):
        def run(_query, *, limit):
            retrieval_calls.append(name)
            assert limit == 10
            return [{"path": "shared.md", "title": "Shared", "score": 0.75}]

        return run

    monkeypatch.setattr(store, "_trgm_search", results("trgm_notes"))
    monkeypatch.setattr(store, "_trgm_search_chunks", results("trgm_chunks"))
    monkeypatch.setattr(store, "_semantic_search", results("semantic_notes"))
    monkeypatch.setattr(store, "_semantic_search_chunks", results("semantic_chunks"))

    @contextmanager
    def connection():
        yield _Connection()

    monkeypatch.setattr(store, "_conn", connection)
    monkeypatch.setattr(module, "request_embedding", lambda _query, _provider: [1.0])

    def top_chunks(paths, _query_vector, count):
        reranker_calls.append("top_chunks")
        assert paths == ["shared.md"]
        assert count == module._reranker.NUM_CHUNKS_PER_CANDIDATE
        return {"shared.md": ["synthetic chunk"]}

    def rerank(_query, candidates, chunks):
        reranker_calls.append("reranker")
        assert chunks == {"shared.md": ["synthetic chunk"]}
        return candidates

    monkeypatch.setattr(store, "_top_chunks_for_paths", top_chunks)
    monkeypatch.setattr(module._reranker, "rerank_candidates", rerank)
    store._retrieval_calls = retrieval_calls
    return store


@pytest.mark.parametrize("configured", [None, "0", "true"])
def test_multiuser_skips_cross_encoder_unless_value_is_exactly_one(
    monkeypatch, configured
):
    if configured is None:
        monkeypatch.delenv("SB_MULTIUSER_RERANK", raising=False)
    else:
        monkeypatch.setenv("SB_MULTIUSER_RERANK", configured)
    calls: list[str] = []
    store = _store(monkeypatch, multiuser=True, reranker_calls=calls)

    results = store.hybrid_search("synthetic query", limit=5)

    assert [item["path"] for item in results] == ["shared.md"]
    assert store._retrieval_calls == [
        "trgm_notes",
        "trgm_chunks",
        "semantic_notes",
        "semantic_chunks",
    ]
    assert calls == []


def test_multiuser_explicit_opt_in_preserves_cross_encoder_path(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER_RERANK", "1")
    calls: list[str] = []
    store = _store(monkeypatch, multiuser=True, reranker_calls=calls)

    store.hybrid_search("synthetic query", limit=5)

    assert calls == ["top_chunks", "reranker"]


@pytest.mark.parametrize("configured", [None, "0"])
def test_legacy_search_keeps_cross_encoder_default(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("SB_MULTIUSER_RERANK", raising=False)
    else:
        monkeypatch.setenv("SB_MULTIUSER_RERANK", configured)
    calls: list[str] = []
    store = _store(monkeypatch, multiuser=False, reranker_calls=calls)

    store.hybrid_search("synthetic query", limit=5)

    assert calls == ["top_chunks", "reranker"]


def test_explicit_rerank_false_wins_over_multiuser_opt_in(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER_RERANK", "1")
    calls: list[str] = []
    store = _store(monkeypatch, multiuser=True, reranker_calls=calls)

    store.hybrid_search("synthetic query", limit=5, rerank=False)

    assert calls == []
