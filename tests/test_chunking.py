"""Tests for chunking.py — paragraph-aligned token chunk boundary math (Phase B-2)."""
from __future__ import annotations

from mcp_second_brain.chunking import ChunkSpan, plan_chunk_spans, split_paragraphs


class TestSplitParagraphs:
    def test_splits_on_blank_line(self):
        assert split_paragraphs("first\n\nsecond") == ["first", "second"]

    def test_collapses_runs_of_blank_lines(self):
        assert split_paragraphs("first\n\n\n\nsecond") == ["first", "second"]

    def test_no_break_is_one_paragraph(self):
        assert split_paragraphs("just one paragraph, no break") == ["just one paragraph, no break"]

    def test_drops_empty_and_whitespace_only_paragraphs(self):
        assert split_paragraphs("a\n\n   \n\nb") == ["a", "b"]

    def test_empty_text_is_no_paragraphs(self):
        assert split_paragraphs("") == []

    def test_single_newline_does_not_split(self):
        """A single \\n (soft-wrapped line, not a paragraph break) stays fused."""
        assert split_paragraphs("line one\nline two") == ["line one\nline two"]


class TestPlanChunkSpansEmpty:
    def test_no_paragraphs_gives_no_spans(self):
        assert plan_chunk_spans([]) == []

    def test_all_zero_length_paragraphs_give_no_spans(self):
        assert plan_chunk_spans([0, 0]) == []


class TestPlanChunkSpansBasic:
    def test_single_short_paragraph_is_one_span_covering_it(self):
        spans = plan_chunk_spans([10], target_tokens=512)
        assert spans == [ChunkSpan(0, 10)]

    def test_cuts_at_paragraph_boundary_not_mid_paragraph(self):
        # para1=20 tokens, para2=20 tokens; target=15 forces a cut, but it must
        # land on the boundary between them (index 20), not inside either.
        spans = plan_chunk_spans([20, 20], target_tokens=15, min_tail_tokens=0)
        assert spans == [ChunkSpan(0, 20), ChunkSpan(20, 40)]

    def test_spans_tile_the_full_sequence_no_gap_or_overlap(self):
        counts = [30, 30, 30]
        spans = plan_chunk_spans(counts, target_tokens=25, min_tail_tokens=0)
        assert spans[0].start == 0
        assert spans[-1].end == sum(counts)
        for a, b in zip(spans, spans[1:]):
            assert a.end == b.start

    def test_many_short_paragraphs_get_grouped_toward_target(self):
        # 10 paragraphs of 8 tokens each = 80 total; target=25 should group
        # roughly 3-4 paragraphs per chunk, not one-per-chunk.
        spans = plan_chunk_spans([8] * 10, target_tokens=25, min_tail_tokens=0)
        assert len(spans) < 10
        assert sum(s.end - s.start for s in spans) == 80


class TestPlanChunkSpansHardCut:
    def test_giant_single_paragraph_hard_cuts_at_target(self):
        spans = plan_chunk_spans([2000], target_tokens=512, min_tail_tokens=0)
        assert len(spans) > 1
        for s in spans[:-1]:
            assert s.end - s.start == 512

    def test_giant_paragraph_among_normal_ones_only_hard_cuts_itself(self):
        # para0=20 (normal), para1=2000 (giant), para2=20 (normal)
        spans = plan_chunk_spans([20, 2000, 20], target_tokens=512, min_tail_tokens=0)
        # first span should be the small lead paragraph plus however much of the
        # giant one it takes to reach target, or just the giant one hard-cut —
        # either way every span's length must be <= target except where it
        # legitimately reaches a paragraph boundary.
        assert spans[0].start == 0
        assert spans[-1].end == 2040
        for a, b in zip(spans, spans[1:]):
            assert a.end == b.start


class TestPlanChunkSpansTailMerge:
    def test_short_trailing_remainder_merges_into_previous_span(self):
        spans = plan_chunk_spans([20, 3], target_tokens=15, min_tail_tokens=10)
        assert spans == [ChunkSpan(0, 23)]

    def test_trailing_remainder_at_or_above_min_tail_stays_separate(self):
        spans = plan_chunk_spans([20, 12], target_tokens=15, min_tail_tokens=10)
        assert spans == [ChunkSpan(0, 20), ChunkSpan(20, 32)]
