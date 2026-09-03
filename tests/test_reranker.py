"""Tests for reranker.py — decision 2's production reranking client."""
from __future__ import annotations

import json

from mcp_second_brain import reranker as rr


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRerank:
    def test_empty_documents_returns_empty_without_a_call(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("should not be called for empty documents")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert rr.rerank("query", []) == []

    def test_parses_scores_in_document_order(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _FakeResponse(
                {
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.1},
                    ]
                }
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        scores = rr.rerank("query", ["doc a", "doc b"])
        assert scores == [0.1, 0.9]

    def test_server_unreachable_returns_none_not_raise(self, monkeypatch):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert rr.rerank("query", ["doc a"]) is None

    def test_malformed_response_returns_none_not_raise(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _FakeResponse({"unexpected": "shape"})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert rr.rerank("query", ["doc a"]) is None


class TestRerankCandidates:
    def test_reorders_by_best_chunk_score(self, monkeypatch):
        candidates = [{"path": "a.md"}, {"path": "b.md"}]
        chunks_by_path = {"a.md": ["a chunk"], "b.md": ["b chunk"]}

        def fake_rerank(query, docs, timeout=30.0):
            # b.md's chunk scores higher -> should end up first
            return [0.1, 0.9]

        monkeypatch.setattr(rr, "rerank", fake_rerank)
        out = rr.rerank_candidates("q", candidates, chunks_by_path)
        assert [c["path"] for c in out] == ["b.md", "a.md"]

    def test_score_field_is_overwritten_with_reranker_score_when_present(self, monkeypatch):
        candidates = [{"path": "a.md", "score": 0.031}, {"path": "b.md", "score": 0.029}]
        chunks_by_path = {"a.md": ["a chunk"], "b.md": ["b chunk"]}

        def fake_rerank(query, docs, timeout=30.0):
            return [0.1, 0.9]  # a.md=0.1, b.md=0.9 -> b.md should now lead with 0.9

        monkeypatch.setattr(rr, "rerank", fake_rerank)
        out = rr.rerank_candidates("q", candidates, chunks_by_path)
        assert out[0] == {"path": "b.md", "score": 0.9}
        assert out[1] == {"path": "a.md", "score": 0.1}
        # original input list must not be mutated in place
        assert candidates[0]["score"] == 0.031

    def test_takes_max_score_across_a_candidates_multiple_chunks(self, monkeypatch):
        candidates = [{"path": "a.md"}, {"path": "b.md"}]
        chunks_by_path = {"a.md": ["a1", "a2"], "b.md": ["b1"]}

        def fake_rerank(query, docs, timeout=30.0):
            # docs order: a1, a2, b1 -> a2 scores highest overall
            assert docs == ["a1", "a2", "b1"]
            return [0.2, 0.95, 0.5]

        monkeypatch.setattr(rr, "rerank", fake_rerank)
        out = rr.rerank_candidates("q", candidates, chunks_by_path)
        assert [c["path"] for c in out] == ["a.md", "b.md"]

    def test_candidate_with_no_chunks_stays_at_the_tail_not_dropped(self, monkeypatch):
        candidates = [{"path": "a.md"}, {"path": "no-chunks.md"}, {"path": "b.md"}]
        chunks_by_path = {"a.md": ["a chunk"], "b.md": ["b chunk"]}

        def fake_rerank(query, docs, timeout=30.0):
            return [0.9, 0.1]  # for a.md, b.md respectively (in candidate order)

        monkeypatch.setattr(rr, "rerank", fake_rerank)
        out = rr.rerank_candidates("q", candidates, chunks_by_path)
        paths = [c["path"] for c in out]
        assert paths[-1] == "no-chunks.md"
        assert set(paths) == {"a.md", "no-chunks.md", "b.md"}

    def test_no_candidates_have_chunks_returns_input_unchanged(self, monkeypatch):
        candidates = [{"path": "a.md"}, {"path": "b.md"}]

        def boom(*a, **k):
            raise AssertionError("rerank should not be called with nothing to send")

        monkeypatch.setattr(rr, "rerank", boom)
        assert rr.rerank_candidates("q", candidates, {}) == candidates

    def test_reranker_failure_falls_back_to_original_order(self, monkeypatch):
        candidates = [{"path": "a.md"}, {"path": "b.md"}]
        chunks_by_path = {"a.md": ["a chunk"], "b.md": ["b chunk"]}

        monkeypatch.setattr(rr, "rerank", lambda *a, **k: None)
        out = rr.rerank_candidates("q", candidates, chunks_by_path)
        assert out == candidates
