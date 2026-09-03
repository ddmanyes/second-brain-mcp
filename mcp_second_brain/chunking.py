"""chunking.py — paragraph-aligned, token-sized chunk boundary math for note_chunks.

Phase B-2 of second-brain's chunking/embedding plan
(10-projects/second-brain/phases/second-brain-分塊-embedding-與-late-chunking-實施計畫.md).

Pure integer math, no tokenizer server involved: given each paragraph's already-
tokenized length, decide where to cut. Paragraph splitting itself is also pure
(plain text, no tokenizer). Turning a decided span into actual chunk text and an
embedding is late_chunking.py's job — it owns the one real subtlety here:

    bge-m3's tokenizer (XLM-RoBERTa-style) does not represent newlines as
    tokens at all — tokenizing "a\\n\\nb" produces exactly the same two tokens
    as "a b". Concatenating a document's own token *pieces* back together
    therefore cannot recover where its paragraph breaks were; that information
    is destroyed at tokenization time. (Found empirically while building this —
    an earlier version of this module tried to find "\\n\\n" in piece-
    reconstructed text and silently produced hard mid-word cuts on every
    document, because no boundary was ever found.)

    The fix: split paragraphs on the *original* text (always correct — plain
    string ops, no tokenizer round-trip involved), tokenize each paragraph
    independently, and do all boundary math in units of "paragraphs" rather
    than trying to locate character positions inside a token stream.

Overlap is deliberately 0 (decided in the plan, cross-checked against two
independent sources: no measurable retrieval benefit, only extra index cost).
Don't reuse ``extract_statements.py``'s ``overlap=200`` here — that was sized for
LLM extraction context, not embedding chunks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ChunkSpan", "split_paragraphs", "plan_chunk_spans", "TARGET_TOKENS", "MIN_TAIL_TOKENS"]

TARGET_TOKENS = 512
# A trailing chunk shorter than this merges into the previous one instead of
# standing alone — a 20-token orphan chunk is index noise, not a retrievable unit.
MIN_TAIL_TOKENS = 64

_PARAGRAPH_BREAK_RE = re.compile(r"\n{2,}")


def split_paragraphs(text: str) -> list[str]:
    """Split on runs of 2+ newlines. Drops paragraphs that are empty/whitespace-only.

    This is the one place paragraph boundaries are ever determined — always on
    the original text, never reconstructed from tokens (see module docstring).
    """
    return [p for p in _PARAGRAPH_BREAK_RE.split(text) if p.strip()]


@dataclass(frozen=True)
class ChunkSpan:
    """One chunk's span in the concatenated full-document token sequence.

    start/end are token indices (end exclusive) into the sequence formed by
    concatenating each paragraph's own token-id list in order — i.e. exactly
    what late_chunking.py sends to the embedding server. Not character offsets,
    and not offsets into any single paragraph.
    """

    start: int
    end: int


def plan_chunk_spans(
    paragraph_token_counts: list[int],
    *,
    target_tokens: int = TARGET_TOKENS,
    min_tail_tokens: int = MIN_TAIL_TOKENS,
) -> list[ChunkSpan]:
    """Decide chunk spans from each paragraph's token count, in the concatenated
    token sequence's index space.

    Walks forward, cutting at the first paragraph boundary at or past
    ``target_tokens`` from the current start. A paragraph disproportionately
    larger than ``target_tokens`` on its own (a giant table/code block with no
    internal break) is hard-cut at ``target_tokens`` — better than one huge
    chunk. A short trailing remainder merges into the previous chunk.
    """
    boundaries: list[int] = []
    acc = 0
    for count in paragraph_token_counts:
        acc += count
        boundaries.append(acc)
    n = acc
    if n == 0:
        return []

    spans: list[ChunkSpan] = []
    start = 0
    bi = 0
    while start < n:
        while bi < len(boundaries) and boundaries[bi] <= start:
            bi += 1

        cut = None
        target = start + target_tokens
        # A boundary is only a good stopping point if it's not too far past
        # target — otherwise "the first boundary >= target" degenerates to
        # "the end of a single giant paragraph", never hard-cutting it at all.
        cap = start + 2 * target_tokens
        j = bi
        while j < len(boundaries):
            if boundaries[j] >= target:
                if boundaries[j] <= cap:
                    cut = boundaries[j]
                break  # nearest candidate found (accepted or not) — stop scanning
            j += 1

        if cut is None:
            # No qualifying boundary ahead: either this is the last paragraph
            # (take it whole, even a bit past target_tokens — beats splitting
            # mid-paragraph), or one paragraph is disproportionately larger
            # than target_tokens (needs a hard cut or it becomes one huge
            # chunk). 2x is a documented, somewhat arbitrary threshold between
            # "slightly over" and "way over".
            remaining = n - start
            cut = target if remaining > 2 * target_tokens else n

        spans.append(ChunkSpan(start, cut))
        start = cut

    if len(spans) >= 2 and (spans[-1].end - spans[-1].start) < min_tail_tokens:
        prev, last = spans[-2], spans[-1]
        spans[-2:] = [ChunkSpan(prev.start, last.end)]

    return spans
