"""Tests for late_chunking.py — mean-pooled, full-context chunk embeddings.

No real llama-server: every test monkeypatches ``_post_json`` (the one function
that makes an HTTP call) with a fake that mimics the two llama.cpp-native
endpoints this module uses (/tokenize, /embedding).
"""
from __future__ import annotations

import pytest

from mcp_second_brain import late_chunking as lc


@pytest.fixture(autouse=True)
def _reset_special_ids_cache():
    lc._special_ids_cache = None
    yield
    lc._special_ids_cache = None


class _FakeServer:
    """Word-level fake tokenizer: each whitespace-separated word is one token,
    id = a stable hash-free counter keyed by the word's identity within a call
    (good enough — tests only need per-token vectors to reveal which word each
    slot covers). Mimics the real tokenizer's behavior of dropping newlines
    entirely (this is the exact bug the paragraph-based redesign works around)."""

    BOS = 1000
    EOS = 1001

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 0
        self._word_ids: dict[str, int] = {}

    def _id_for(self, word: str) -> int:
        if word not in self._word_ids:
            self._word_ids[word] = self._next_id
            self._next_id += 1
        return self._word_ids[word]

    def post_json(self, path: str, payload: dict, *, timeout: float = 60.0):
        self.calls.append((path, payload))
        if path == "/tokenize":
            if payload.get("add_special"):
                return {"tokens": [self.BOS, self.EOS]}
            words = payload["content"].split()
            return {"tokens": [self._id_for(w) for w in words]}
        if path == "/embedding":
            ids = payload["content"]
            return [{"index": 0, "embedding": [[tid] for tid in ids]}]
        raise AssertionError(f"unexpected path {path}")


@pytest.fixture()
def fake_server(monkeypatch):
    fs = _FakeServer()
    monkeypatch.setattr(lc, "_post_json", fs.post_json)
    return fs


class TestBosEosIds:
    def test_derives_and_caches_special_ids(self, fake_server):
        assert lc._bos_eos_ids() == (_FakeServer.BOS, _FakeServer.EOS)
        assert lc._bos_eos_ids() == (_FakeServer.BOS, _FakeServer.EOS)
        tokenize_calls = [c for p, c in fake_server.calls if p == "/tokenize"]
        assert len(tokenize_calls) == 1  # second call served from cache

    def test_rejects_unexpected_special_token_count(self, monkeypatch):
        monkeypatch.setattr(lc, "_post_json", lambda *a, **k: {"tokens": [1, 2, 3]})
        with pytest.raises(lc.LateChunkingUnavailable):
            lc._bos_eos_ids()


class TestMeanPool:
    def test_averages_elementwise(self):
        assert lc._mean_pool([[1.0, 2.0], [3.0, 4.0]]) == [2.0, 3.0]

    def test_empty_input_returns_empty(self):
        assert lc._mean_pool([]) == []


class TestEncodeBodyVectorsSinglePass:
    def test_short_sequence_drops_synthetic_bos_eos(self, fake_server):
        token_ids = [10, 20, 30]
        vecs = lc._encode_body_vectors(token_ids, n_ctx=100)
        assert vecs == [[10], [20], [30]]
        embed_calls = [p for path, p in fake_server.calls if path == "/embedding"]
        assert embed_calls[0]["content"] == [_FakeServer.BOS, 10, 20, 30, _FakeServer.EOS]


class TestEncodeBodyVectorsSlidingWindow:
    def test_stitched_sequence_exactly_tiles_body_with_no_gap_or_overlap(self, fake_server):
        n = 1000
        token_ids = list(range(n))
        vecs = lc._encode_body_vectors(
            token_ids, n_ctx=50, window_body_tokens=400, window_overlap_tokens=50
        )
        assert vecs == [[i] for i in range(n)]

    @pytest.mark.parametrize(
        "n,window_body,overlap",
        [(1000, 400, 50), (401, 400, 50), (400, 400, 50), (37, 10, 3), (999, 100, 99)],
    )
    def test_tiling_holds_across_boundary_sizes(self, fake_server, n, window_body, overlap):
        token_ids = list(range(n))
        vecs = lc._encode_body_vectors(
            token_ids, n_ctx=window_body, window_body_tokens=window_body,
            window_overlap_tokens=overlap,
        )
        assert vecs == [[i] for i in range(n)]

    def test_uses_more_than_one_embedding_call_for_a_long_document(self, fake_server):
        token_ids = list(range(1000))
        lc._encode_body_vectors(token_ids, n_ctx=50, window_body_tokens=400, window_overlap_tokens=50)
        embed_calls = [p for p, _ in fake_server.calls if p == "/embedding"]
        assert len(embed_calls) > 1


class TestApproxCharSpan:
    def test_whole_paragraph_is_exact(self):
        # tok_start=0, tok_end=total -> should return the full text unchanged
        text = "the quick brown fox jumps"
        assert lc._approx_char_span(text, 5, 0, 5) == text

    def test_partial_span_snaps_to_word_boundaries(self):
        text = "alpha beta gamma delta epsilon"
        # roughly the first 2 of 5 "tokens" -> should land at/near a word boundary,
        # not mid-word
        out = lc._approx_char_span(text, 5, 0, 2)
        assert out == out.strip()
        assert not out.endswith((" ",))
        # must be a prefix of the original text (word-boundary snapped, not garbled)
        assert text.startswith(out)


class TestChunkAndEmbedEndToEnd:
    def test_empty_text_returns_no_chunks(self, fake_server):
        assert lc.chunk_and_embed("") == []

    def test_single_paragraph_one_chunk(self, fake_server):
        out = lc.chunk_and_embed("hello there world", target_tokens=512)
        assert len(out) == 1
        text, emb = out[0]
        assert text == "hello there world"
        assert len(emb) == 1  # fake vectors are 1-dim

    def test_newlines_do_not_break_chunking_even_though_tokenizer_drops_them(self, fake_server):
        """Regression: an earlier version tried to find \\n\\n in piece-
        reconstructed text and silently produced one giant hard-cut chunk on
        every multi-paragraph document, because the fake (and real) tokenizer
        never represents newlines as tokens at all."""
        text = "first paragraph here\n\nsecond paragraph here too"
        out = lc.chunk_and_embed(text, target_tokens=2, min_tail_tokens=0)
        assert len(out) == 2
        assert out[0][0] == "first paragraph here"
        assert out[1][0] == "second paragraph here too"

    def test_chunk_embeddings_are_mean_of_their_token_span(self, fake_server):
        text = "one two\n\nthree four"
        out = lc.chunk_and_embed(text, target_tokens=1, min_tail_tokens=0)
        assert len(out) == 2
        # fake tokenizer assigns ids in first-seen order: one=0, two=1, three=2, four=3
        (_t0, e0), (_t1, e1) = out
        assert e0[0] == pytest.approx((0 + 1) / 2)
        assert e1[0] == pytest.approx((2 + 3) / 2)
