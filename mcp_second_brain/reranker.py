"""reranker.py — decision 2 of the chunking/embedding plan.

Client for the dedicated reranking llama-server instance (com.llama-server-rerank,
:8083, ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF with --reranking --pooling rank —
NOT a random community GGUF: the plan's research flagged those as commonly
broken, returning ~4.5e-23 relevance scores across the board regardless of
actual relevance).

The A/B experiment (see decisions/second-brain-reranker-ab對照實驗結果-決策2.md)
found a consistent, substantial ranking improvement, on one condition: judge
several of a note's nearest chunks, not just the single closest one by cosine
distance. Feeding the reranker only the nearest chunk let a short, generic
"Author Contributions" chunk (which happened to win on raw cosine distance for
a cross-lingual query) tank an otherwise rank-1 document to rank 20 — the
reranker judged that one passage correctly, it was just the wrong passage.
_top_chunks_by_note() and rerank_candidates() below encode that lesson;
postgres_store.py's own boilerplate-chunk filtering (chunking.py's
filter_administrative_sections) closes the same gap from the other end.

Fails soft: an unreachable or erroring server returns the candidates in their
original order rather than raising — a search feature that occasionally
degrades to "no reranking" beats one that occasionally 500s the caller.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

__all__ = ["RERANK_URL", "NUM_CHUNKS_PER_CANDIDATE", "rerank", "rerank_candidates"]

RERANK_PORT = int(os.environ.get("RERANK_PORT", "8083"))
RERANK_URL = os.environ.get("RERANK_URL", f"http://localhost:{RERANK_PORT}/v1/rerank")

# How many of a candidate's nearest-by-cosine chunks to hand the reranker.
# 1 is what the A/B experiment's first pass used, and it failed on a real
# query (see module docstring) — 3 fixed it there and is the smallest number
# that did.
NUM_CHUNKS_PER_CANDIDATE = 3


def rerank(query: str, documents: list[str], *, timeout: float = 30.0) -> list[float] | None:
    """Relevance score per document, in ``documents`` order. ``None`` on any
    failure (server down, bad response) — callers should fall back to their
    pre-rerank order, not raise.
    """
    if not documents:
        return []
    payload = json.dumps(
        {"model": "reranker", "query": query, "documents": documents}
    ).encode()
    req = urllib.request.Request(
        RERANK_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"[reranker] unavailable, skipping rerank: {e}", file=sys.stderr)
        return None
    try:
        scores = [0.0] * len(documents)
        for r in data["results"]:
            scores[r["index"]] = r["relevance_score"]
        return scores
    except (KeyError, IndexError, TypeError) as e:
        print(f"[reranker] unexpected response shape, skipping rerank: {e}", file=sys.stderr)
        return None


def rerank_candidates(
    query: str,
    candidates: list[dict],
    chunks_by_path: dict[str, list[str]],
    *,
    timeout: float = 30.0,
) -> list[dict]:
    """Re-order ``candidates`` (each a dict with at least "path") by the best
    reranker score among each candidate's chunks in ``chunks_by_path``.

    A candidate absent from ``chunks_by_path`` (no chunks yet — mid-backfill,
    or a transient chunk-sync failure) keeps its original relative position
    at the tail, ranked below every candidate the reranker actually scored,
    rather than being dropped — same "degrade, don't disappear" principle as
    hybrid_search's own notes+chunks merge.

    Each reranked candidate's dict is copied with its "score" key (if any)
    overwritten by the reranker's own relevance score, so a displayed score
    stays monotonic with the new order instead of showing the pre-rerank RRF
    score next to a position it no longer justifies. Untouched (tail)
    candidates keep whatever "score" they arrived with.

    On reranker failure, returns ``candidates`` unchanged (see ``rerank()``).
    """
    flat_docs: list[str] = []
    doc_owner: list[str] = []
    for c in candidates:
        for text in chunks_by_path.get(c["path"], []):
            flat_docs.append(text)
            doc_owner.append(c["path"])

    if not flat_docs:
        return candidates

    scores = rerank(query, flat_docs, timeout=timeout)
    if scores is None:
        return candidates

    best_per_path: dict[str, float] = {}
    for path, score in zip(doc_owner, scores):
        if path not in best_per_path or score > best_per_path[path]:
            best_per_path[path] = score

    scored = [
        {**c, "score": best_per_path[c["path"]]} if "score" in c else c
        for c in candidates
        if c["path"] in best_per_path
    ]
    unscored = [c for c in candidates if c["path"] not in best_per_path]
    scored.sort(key=lambda c: best_per_path[c["path"]], reverse=True)
    return scored + unscored
