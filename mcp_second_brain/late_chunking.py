"""late_chunking.py — Phase B-3: chunk embeddings computed with full-document context.

Late chunking (Jina AI, "Late Chunking in Long-Context Embedding Models", 2024):
encode the *whole* document through the transformer first, mean-pool each chunk's
token vectors from that single pass. Every chunk's vector carries the document's
full context instead of only its own ~512 tokens — the point of this phase.

Requires a llama-server instance launched with ``--pooling none`` (per-request
pooling isn't supported by llama.cpp; see the plan's Phase B-3 note). This is a
*separate* instance from the shared embedding server (``EMBED_URL``, :8081) —
that one's pooling mode is load-bearing for every vector already stored in
``notes``/``analysis_artifacts``, and must not change.

Talks to two llama.cpp-native endpoints (not the OpenAI-compatible ``/v1/embeddings``
``EMBED_URL`` uses elsewhere in this codebase):
  - POST /tokenize   {"content": str}          -> plain token-id list
  - POST /embedding  {"content": [token_ids]}  -> per-token vectors

Chunk boundaries are decided per-paragraph (chunking.py), each paragraph
tokenized independently and concatenated — not by tokenizing the whole document
and trying to locate "\\n\\n" in the reconstructed text, which doesn't work (see
chunking.py's module docstring for why). The one thing that approach loses —
byte-exact chunk text when a single paragraph is so large it must be hard-cut
mid-paragraph — is recovered approximately: proportional token->char
interpolation over that paragraph's own text, snapped to the nearest word
boundary. That only ever affects the *stored* chunk text for the rare
giant-unbroken-paragraph case (a huge table or code block); the embedding
itself is always computed from the exact token span, never approximated.

Long Late Chunking (Jina paper, Algorithm 2): documents whose token count
exceeds the server's context window are encoded in overlapping windows; each
window keeps only the token vectors it has full left-context for (the
non-overlapping tail), so the stitched sequence tiles the document exactly once
with no gap or overlap.

Administrative sections (Author Contributions, Acknowledgments, Conflict of
Interest, Data Availability, ...) are dropped before chunking at all —
chunking.py's filter_administrative_sections(). Found during decision 2's
reranker A/B experiment: a short, generic "Author Contributions" chunk beat
every real content chunk on raw cosine distance for a cross-lingual query,
which is exactly the kind of false hit that shouldn't exist in the index.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .chunking import ChunkSpan, filter_administrative_sections, plan_chunk_spans, split_paragraphs

__all__ = [
    "LATE_CHUNK_URL",
    "N_CTX",
    "WINDOW_BODY_TOKENS",
    "WINDOW_OVERLAP_TOKENS",
    "chunk_and_embed",
    "LateChunkingUnavailable",
]

LATE_CHUNK_PORT = int(os.environ.get("LATE_CHUNK_PORT", "8082"))
LATE_CHUNK_URL = os.environ.get("LATE_CHUNK_URL", f"http://localhost:{LATE_CHUNK_PORT}")

# bge-m3's trained context. Leave headroom below it for BOS/EOS and for the
# server's own accounting — see test_late_chunking.py's window-math tests for
# why WINDOW_BODY_TOKENS must stay comfortably under N_CTX - 2.
N_CTX = 8192
WINDOW_BODY_TOKENS = 8000
# Overlap size for the sliding window: large enough that a token near a window
# boundary still gets meaningful left-context in whichever window keeps it.
# 256 is the same order of magnitude as extract_statements.py's chunk overlap
# (200) elsewhere in this stack, picked for the same reason (enough context
# without materially inflating compute) — not reused directly, this is a
# window-encoding detail internal to late chunking, unrelated to that overlap.
WINDOW_OVERLAP_TOKENS = 256

assert WINDOW_OVERLAP_TOKENS < WINDOW_BODY_TOKENS
assert WINDOW_BODY_TOKENS + 2 <= N_CTX


class LateChunkingUnavailable(Exception):
    """The --pooling none server is unreachable or returned something unusable."""


def _post_json(path: str, payload: dict, *, timeout: float = 60.0) -> dict | list:
    req = urllib.request.Request(
        f"{LATE_CHUNK_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        raise LateChunkingUnavailable(f"{path} failed: {e}") from e


def _tokenize_ids(text: str) -> list[int]:
    """Plain token-id list for one piece of text (no BOS/EOS, no pieces)."""
    data = _post_json("/tokenize", {"content": text})
    return data["tokens"]  # type: ignore[index]


_special_ids_cache: tuple[int, int] | None = None


def _bos_eos_ids() -> tuple[int, int]:
    """(bos_id, eos_id) for the model this server is running, cached per-process.

    Tokenizing an empty string with add_special=true returns exactly
    [BOS, EOS] for bge-m3's XLM-RoBERTa-style tokenizer (empirically verified,
    see the plan's Phase B execution notes).
    """
    global _special_ids_cache
    if _special_ids_cache is None:
        data = _post_json("/tokenize", {"content": "", "add_special": True})
        ids = data["tokens"]  # type: ignore[index]
        if len(ids) != 2:
            raise LateChunkingUnavailable(
                f"expected exactly [BOS, EOS] from empty-string tokenize, got {ids!r}"
            )
        _special_ids_cache = (ids[0], ids[1])
    return _special_ids_cache


def _embed_token_ids(token_ids: list[int]) -> list[list[float]]:
    """Per-token embeddings for an already-tokenized sequence (BOS/EOS included).

    Sending token IDs directly (not text) means llama.cpp does no re-tokenization
    — the response's token count always matches ``len(token_ids)`` exactly, so
    window-stitching math never has to guess at a boundary.
    """
    data = _post_json("/embedding", {"content": token_ids}, timeout=120.0)
    if not isinstance(data, list) or not data or "embedding" not in data[0]:
        raise LateChunkingUnavailable(f"unexpected /embedding response shape: {data!r}")
    vecs = data[0]["embedding"]
    if len(vecs) != len(token_ids):
        raise LateChunkingUnavailable(
            f"/embedding returned {len(vecs)} vectors for {len(token_ids)} tokens"
        )
    return vecs


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    sums = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            sums[i] += x
    n = len(vectors)
    return [s / n for s in sums]


def _encode_body_vectors(
    token_ids: list[int],
    *,
    n_ctx: int = N_CTX,
    window_body_tokens: int = WINDOW_BODY_TOKENS,
    window_overlap_tokens: int = WINDOW_OVERLAP_TOKENS,
) -> list[list[float]]:
    """Per-token vectors for the body sequence, length == len(token_ids) exactly.

    Single pass when the body fits in one context window; otherwise Long Late
    Chunking (sliding windows, overlap discarded from each window's head except
    the first) so the stitched sequence still tiles the body with no gap/overlap.

    The three window-shape parameters default to the module constants; tests
    override them to exercise the sliding-window path without a multi-thousand-
    token fixture.
    """
    bos, eos = _bos_eos_ids()
    n = len(token_ids)

    if n + 2 <= n_ctx:
        vecs = _embed_token_ids([bos, *token_ids, eos])
        return vecs[1:-1]

    stitched: list[list[float]] = []
    pos = 0
    while True:
        w_end = min(pos + window_body_tokens, n)
        window_ids = [bos, *token_ids[pos:w_end], eos]
        vecs = _embed_token_ids(window_ids)
        body_vecs = vecs[1:-1]  # drop this window's own synthetic BOS/EOS
        keep_from = 0 if pos == 0 else window_overlap_tokens
        stitched.extend(body_vecs[keep_from:])
        if w_end >= n:
            break
        pos = w_end - window_overlap_tokens

    if len(stitched) != n:  # pragma: no cover — invariant, see test_late_chunking.py
        raise LateChunkingUnavailable(
            f"window stitching produced {len(stitched)} vectors for {n} body tokens"
        )
    return stitched


def _nearest_word_boundary(text: str, pos: int, *, window: int = 50) -> int:
    """Snap `pos` to the nearest preceding whitespace within `window` chars.

    Used only for the approximate char slice of a hard-cut inside one giant
    paragraph (see module docstring) — cosmetic (avoids visibly cutting a word
    in half in the stored chunk text), not load-bearing for correctness.
    """
    lo = max(0, pos - window)
    idx = text.rfind(" ", lo, pos + 1)
    return idx + 1 if idx != -1 else pos


def _approx_char_span(paragraph: str, total_tokens: int, tok_start: int, tok_end: int) -> str:
    """Proportional token->char interpolation over one paragraph's own text.

    Approximate by construction (token density varies across a paragraph); see
    the module docstring for why this is an acceptable tradeoff here.
    """
    n = len(paragraph)
    if total_tokens == 0 or n == 0:
        return ""
    char_start = round(n * tok_start / total_tokens)
    char_end = round(n * tok_end / total_tokens)
    if tok_start > 0:
        char_start = _nearest_word_boundary(paragraph, char_start)
    if tok_end < total_tokens:
        char_end = _nearest_word_boundary(paragraph, char_end)
    return paragraph[char_start:char_end].strip()


def _span_text(
    span: ChunkSpan,
    paragraphs: list[str],
    para_bounds: list[tuple[int, int]],
    para_lengths: list[int],
) -> str:
    """Reconstruct a chunk's text from whichever paragraphs its span overlaps.

    Whole-paragraph coverage joins the original paragraph text exactly (no
    approximation). Partial coverage (span starts or ends inside a paragraph —
    only possible for the paragraph that triggered a hard cut) uses
    ``_approx_char_span`` for just that paragraph's contribution.
    """
    parts: list[str] = []
    for i, (p_start, p_end) in enumerate(para_bounds):
        overlap_start = max(span.start, p_start)
        overlap_end = min(span.end, p_end)
        if overlap_start >= overlap_end:
            continue
        if overlap_start == p_start and overlap_end == p_end:
            parts.append(paragraphs[i])
        else:
            parts.append(
                _approx_char_span(
                    paragraphs[i], para_lengths[i],
                    overlap_start - p_start, overlap_end - p_start,
                )
            )
    return "\n\n".join(parts)


def chunk_and_embed(
    text: str,
    *,
    target_tokens: int | None = None,
    min_tail_tokens: int | None = None,
) -> list[tuple[str, list[float]]]:
    """Split `text` into chunks and late-chunk-embed each one.

    Returns ``[(chunk_text, embedding), ...]`` in document order. Empty/
    all-whitespace input returns ``[]``. ``target_tokens``/``min_tail_tokens``
    default to chunking.py's module constants when omitted.
    """
    paragraphs = split_paragraphs(text)
    paragraphs = filter_administrative_sections(paragraphs)
    if not paragraphs:
        return []

    kwargs = {}
    if target_tokens is not None:
        kwargs["target_tokens"] = target_tokens
    if min_tail_tokens is not None:
        kwargs["min_tail_tokens"] = min_tail_tokens

    para_token_ids = [_tokenize_ids(p) for p in paragraphs]
    para_lengths = [len(ids) for ids in para_token_ids]

    para_bounds: list[tuple[int, int]] = []
    acc = 0
    for length in para_lengths:
        para_bounds.append((acc, acc + length))
        acc += length

    spans = plan_chunk_spans(para_lengths, **kwargs)
    if not spans:
        return []

    full_token_ids = [tok for ids in para_token_ids for tok in ids]
    body_vectors = _encode_body_vectors(full_token_ids)

    results: list[tuple[str, list[float]]] = []
    for span in spans:
        chunk_text = _span_text(span, paragraphs, para_bounds, para_lengths)
        vec = _mean_pool(body_vectors[span.start:span.end])
        results.append((chunk_text, vec))
    return results
