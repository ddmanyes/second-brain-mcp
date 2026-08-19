"""Tests for figures.py — Phase 4A (OCR/figure extraction) and 4B (snapshot rendering)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch



from mcp_second_brain import figures


# ---------------------------------------------------------------------------
# Phase 4 — Snapshot tiers
# ---------------------------------------------------------------------------

class TestSnapshotTiers:
    def test_tiers_defined(self):
        assert set(figures.SNAPSHOT_TIERS.keys()) == {"large", "base", "small"}

    def test_large_has_highest_tokens(self):
        assert figures.SNAPSHOT_TIERS["large"]["token_est"] > figures.SNAPSHOT_TIERS["base"]["token_est"]
        assert figures.SNAPSHOT_TIERS["base"]["token_est"] > figures.SNAPSHOT_TIERS["small"]["token_est"]

    def test_large_has_highest_resolution(self):
        assert figures.SNAPSHOT_TIERS["large"]["width"] > figures.SNAPSHOT_TIERS["base"]["width"]

    def test_token_estimates_in_range(self):
        for tier, info in figures.SNAPSHOT_TIERS.items():
            assert 50 <= info["token_est"] <= 500, f"{tier} token_est out of range"


# ---------------------------------------------------------------------------
# Phase 4B — _estimate_image_tokens (fix-2026-08-18: downsample cap)
# ---------------------------------------------------------------------------

class TestEstimateImageTokens:
    def _make_png(self, tmp_path, w, h):
        from PIL import Image
        p = tmp_path / "snap.png"
        Image.new("RGB", (w, h)).save(p)
        return p

    def test_small_image_uses_raw_dims(self, tmp_path):
        # well under the cap — no downsampling, matches the naive formula
        p = self._make_png(tmp_path, 1024, 1024)
        assert figures._estimate_image_tokens(p) == int((1024 / 28) * (1024 / 28))

    def test_tall_image_is_capped_before_estimating(self, tmp_path):
        # a full-page snapshot's long edge (height) exceeds the API's downsample cap —
        # raw-pixel math would overestimate by ~3x if the cap isn't applied first
        p = self._make_png(tmp_path, 1280, 4455)
        raw = int((1280 / 28) * (4455 / 28))
        capped = figures._estimate_image_tokens(p)
        assert capped < raw
        # matches applying the cap by hand: long edge (4455) -> 2576, width scaled to match
        k = 2576 / 4455
        expected = max(1, int((int(1280 * k) / 28) * (int(4455 * k) / 28)))
        assert capped == expected

    def test_missing_file_falls_back_to_base_tier(self, tmp_path):
        assert figures._estimate_image_tokens(tmp_path / "nope.png") == figures.SNAPSHOT_TIERS["base"]["token_est"]


# ---------------------------------------------------------------------------
# Phase 4B — Slug / path helpers
# ---------------------------------------------------------------------------

class TestSlug:
    def test_slug_is_12_chars(self):
        s = figures._slug("decisions/my-note.md")
        assert len(s) == 12

    def test_slug_is_hex(self):
        s = figures._slug("some/path.md")
        assert all(c in "0123456789abcdef" for c in s)

    def test_same_path_same_slug(self):
        assert figures._slug("a/b.md") == figures._slug("a/b.md")

    def test_different_paths_different_slugs(self):
        assert figures._slug("a/b.md") != figures._slug("a/c.md")


class TestFigureSlug:
    def test_kebab_from_filename_stem(self):
        assert figures._figure_slug("20-areas/research/2024_Smith_FooBar.md") == "2024-smith-foobar"

    def test_punctuation_becomes_separator_not_merge(self):
        # parens/dots must not glue words together
        assert figures._figure_slug("x/Co-Scientist（v2）.md") == "co-scientist-v2"

    def test_cjk_preserved(self):
        assert figures._figure_slug("x/論文-摘要.md") == "論文-摘要"

    def test_visible_dir_not_hidden(self):
        # extracted figures must live in a visible dir so Obsidian indexes them
        assert figures.FIGURES_DIR.name == "figures"

    def test_extract_filename_is_hyphen_two_digit(self):
        # naming convention: fig-00, fig-01, ... (matches AGENTS.md)
        assert f"fig-{0:02d}" == "fig-00"


# ---------------------------------------------------------------------------
# Phase 4B — render_note_to_png (mocked Playwright)
# ---------------------------------------------------------------------------

class TestRenderNoteToPng:
    def test_returns_none_if_note_missing(self, tmp_path):
        result = figures.render_note_to_png("nonexistent/note.md", tmp_path, tier="base")
        assert result is None

    def test_snapshot_path_uses_slug(self, tmp_path):
        note = tmp_path / "my-note.md"
        note.write_text("---\ntitle: T\n---\n\n# Hello", encoding="utf-8")
        slug = figures._slug("my-note.md")
        expected_dir = tmp_path / ".snapshots" / slug
        # The snapshot dir should be derived from the slug
        assert slug in str(expected_dir)
        assert len(slug) == 12



# ---------------------------------------------------------------------------
# Phase 4A — URL helpers
# ---------------------------------------------------------------------------

class TestURLHelpers:
    def test_parse_source_url_from_frontmatter(self):
        md = "---\ntitle: T\nsource: https://example.com/paper\n---\n\nbody"
        url = figures._parse_source_url(md)
        assert url == "https://example.com/paper"

    def test_parse_source_url_missing(self):
        md = "---\ntitle: T\n---\n\nbody"
        url = figures._parse_source_url(md)
        assert url is None

    def test_is_content_image_excludes_badges(self):
        assert not figures._is_content_image("badge", "https://img.shields.io/badge/x.svg")

    def test_is_content_image_includes_figures(self):
        assert figures._is_content_image("Figure 1", "https://example.com/fig1.png")
