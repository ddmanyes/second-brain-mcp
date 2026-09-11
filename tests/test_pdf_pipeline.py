"""Tests for the PDF pipeline upgrade (IMPLEMENTATION_PLAN.md).

Covers:
  - Phase 1: pymupdf4llm primary text extraction
  - Phase 2: page-render + VLM crop figure extraction + page-hash negative cache
  - Phase 3a/3b: VLM token budget + caption threading
  - Phase 5: read_figure recall ladder + real token_est
"""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

fitz = pytest.importorskip("fitz")


# ---------------------------------------------------------------------------
# Fixture builders (synthetic PDFs — no network, deterministic)
# ---------------------------------------------------------------------------

def _make_text_pdf(path: Path) -> Path:
    """A simple PDF with a large-font heading + body paragraph."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Introduction", fontsize=24, fontname="helv")
    body = (
        "This is a sample paragraph of body text used to validate that the "
        "pymupdf4llm extractor produces clean markdown without large whitespace runs."
    )
    y = 110
    for chunk in (body[i:i + 70] for i in range(0, len(body), 70)):
        page.insert_text((72, y), chunk, fontsize=11, fontname="helv")
        y += 16
    doc.save(str(path))
    doc.close()
    return path


def _make_pdf_with_drawing(path: Path, n_pages: int = 1, draw_on: int = 0) -> Path:
    """Multi-page PDF; `draw_on` page gets a vector rectangle (a 'figure').

    pdfimages cannot extract vector drawings — this exercises the render path.
    Other pages are text-only (used to verify the negative page cache).
    """
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i} heading", fontsize=20, fontname="helv")
        page.insert_text((72, 110), "Body text on this page.", fontsize=11, fontname="helv")
        if i == draw_on:
            rect = fitz.Rect(72, 150, 400, 400)
            page.draw_rect(rect, color=(0, 0, 1), fill=(0.8, 0.8, 1.0))
            page.insert_text((80, 420), "Figure 1. A vector chart.", fontsize=10, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


# ---------------------------------------------------------------------------
# Phase 1 — pymupdf4llm primary text extraction
# ---------------------------------------------------------------------------

class TestTextExtractionPymupdf4llm:
    def test_text_extraction_pymupdf4llm(self, tmp_path):
        from mcp_second_brain import server

        pdf = _make_text_pdf(tmp_path / "paper.pdf")
        body = server._extract_pdf_body(str(pdf))

        # markdown heading present (level depends on pymupdf4llm font-size heuristic / version)
        assert re.search(r"^#+ ", body, re.MULTILINE)
        # clean output: no run of 3+ consecutive spaces (pdftotext -layout noise)
        assert not re.search(r"   ", body)
        assert len(body.strip()) > 100

    def test_pymupdf4llm_failure_falls_back(self, tmp_path):
        """If pymupdf4llm raises, extraction must fall through (Marker/pdftotext)."""
        from mcp_second_brain import server

        pdf = _make_text_pdf(tmp_path / "paper.pdf")
        with patch("pymupdf4llm.to_markdown", side_effect=RuntimeError("boom")):
            # Should not raise; falls back down the chain and still returns text.
            body = server._extract_pdf_body(str(pdf))
        assert isinstance(body, str)
        assert "Introduction" in body or "sample paragraph" in body


# ---------------------------------------------------------------------------
# Phase 2 — page-render figure extraction + negative page cache
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_fig_env(tmp_path, monkeypatch):
    """Isolated DuckDB + figures dir; embedding auto-start disabled."""
    from mcp_second_brain import vault_db, figures
    monkeypatch.setattr(vault_db, "DB_PATH", tmp_path / "vault.db")
    monkeypatch.setattr(vault_db, "_schema_applied", False)
    monkeypatch.setattr(vault_db, "EMBED_AUTO_START", False)
    monkeypatch.setattr(figures, "FIGURES_DIR", tmp_path / "figures")
    return tmp_path


def _make_note_with_pdf(vault: Path, pdf: Path, name: str = "paper.md") -> str:
    note = vault / name
    note.write_text(
        f'---\ntitle: Paper\ndate: 2026-06-16\ntype: research\n'
        f'status: active\ntags: []\nsource: "{pdf}"\n---\n\n# Paper\n\nBody.\n',
        encoding="utf-8",
    )
    return name


class TestFigureExtractionRender:
    def test_figure_extraction_vector(self, isolated_fig_env):
        from mcp_second_brain import figures, vault_db

        vault = isolated_fig_env
        pdf = _make_pdf_with_drawing(vault / "src.pdf", n_pages=1, draw_on=0)
        note_path = _make_note_with_pdf(vault, pdf)

        # _detect_figures_on_page 回傳 (detections, usage_dict)
        detect = lambda png, num: (  # noqa: E731
            [{"bbox": [100, 150, 900, 900], "caption": "Figure 1. A vector chart.",
              "type": "figure"}],
            {},
        )
        analyse = lambda p, caption="": {"ocr_text": "axis labels", "description": "a chart"}  # noqa: E731

        with patch.object(figures, "_detect_figures_on_page", side_effect=detect), \
             patch.object(figures, "analyse_figure", side_effect=analyse):
            results = figures.extract_figures(note_path, vault)

        assert len(results) >= 1
        fig_dir = figures.FIGURES_DIR / figures._figure_slug(note_path)
        assert (fig_dir / "fig-00.png").exists()

        # caption is persisted on the figure row
        row = vault_db.get_figure(note_path, 0)
        assert row is not None
        assert "vector chart" in row["caption"]

    def test_page_hash_cache(self, isolated_fig_env):
        """A second run must not re-detect any page, including figure-less ones."""
        from mcp_second_brain import figures
        from unittest.mock import MagicMock

        vault = isolated_fig_env
        # 2 pages: page 0 has a figure, page 1 is text-only (blank-cache check)
        pdf = _make_pdf_with_drawing(vault / "src.pdf", n_pages=2, draw_on=0)
        note_path = _make_note_with_pdf(vault, pdf)

        spy = MagicMock(side_effect=figures._detect_figures_geometric)
        analyse = lambda p, caption="": {"ocr_text": "", "description": "d"}  # noqa: E731

        with patch.object(figures, "_detect_figures_geometric", spy), \
             patch.object(figures, "analyse_figure", side_effect=analyse):
            figures.extract_figures(note_path, vault)
            calls_after_first = spy.call_count
            assert calls_after_first == 2  # both pages inspected once

            figures.extract_figures(note_path, vault)
            # negative cache: no page (incl. the text-only one) is re-inspected
            assert spy.call_count == calls_after_first

    def test_bbox_comes_from_pdf_geometry_not_the_vlm(self, isolated_fig_env):
        """The crop must wrap the drawn rect + its caption, and nothing else.

        Regression for VLM-guessed bboxes, which produced slivers through a
        panel and captions cut in half.
        """
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf = _make_pdf_with_drawing(vault / "src.pdf", n_pages=1, draw_on=0)
        doc = fitz.open(str(pdf))
        dets = figures._detect_figures_geometric(doc[0])
        doc.close()

        assert len(dets) == 1
        r = dets[0]["rect"]
        pad = figures._GEOM_PAD
        # drawn rect is (72,150)-(400,400); caption sits just below it
        assert r.x0 == pytest.approx(72, abs=pad + 2)
        assert r.y0 == pytest.approx(150, abs=pad + 2)
        assert r.x1 >= 400 - pad          # right edge not clipped
        assert r.y1 > 405                 # grew down to take in the caption
        assert r.y0 > 120                 # heading/body text left outside
        assert "vector chart" in dets[0]["caption"]

    def test_tiled_strips_fuse_into_one_figure(self, isolated_fig_env):
        """Publishers slice one figure into thin strips; they must not be dropped.

        Regression for a size gate that ran before clustering and deleted every
        strip of a 13-slice figure.
        """
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "tiled.pdf"
        doc = fitz.open()
        page = doc.new_page()
        strip = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 8))
        strip.set_rect(strip.irect, (40, 90, 200))
        for i in range(12):
            y = 150 + i * 8
            page.insert_image(fitz.Rect(90, y, 330, y + 8), pixmap=strip)
        page.insert_text((90, 270), "Figure 1. A sliced figure.", fontsize=10, fontname="helv")
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0])
        doc.close()

        assert len(dets) == 1, "the strips must fuse into a single figure"
        r = dets[0]["rect"]
        assert r.height > 90, "the fused figure must span every strip"
        assert "sliced figure" in dets[0]["caption"]

    def test_text_only_table_is_detected(self, isolated_fig_env):
        """Many journals typeset tables with no ruling lines at all.

        Such a table draws no ink, so ink clustering alone finds nothing —
        it has to be assembled from the "Table N" caption and the rows below.
        """
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "table.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((60, 100), "Table 1. Clinical characteristics of participants",
                         fontsize=8.5, fontname="helv")
        for i in range(10):
            page.insert_text((60, 120 + i * 12), f"Variable {i}    {i * 3}.1 ± 0.4    p = 0.0{i}",
                             fontsize=8.5, fontname="helv")
        para = ("Body prose set larger than the table, running on across the column "
                "and wrapping onto several continuous lines of discussion text. ") * 4
        page.insert_textbox(fitz.Rect(60, 320, 520, 600), para, fontsize=11, fontname="helv")
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        page = doc[0]
        assert figures._ink_rects(page) == [], "the page must draw no ink at all"
        dets = figures._detect_figures_geometric(page)
        doc.close()

        assert len(dets) == 1
        r = dets[0]["rect"]
        assert r.y0 < 105                      # starts at the caption
        assert r.y1 > 225                      # covers every row
        assert r.y1 < 320, "must stop before the body prose"
        assert "Clinical characteristics" in dets[0]["caption"]

    def test_figure_labels_are_inside_the_crop(self, isolated_fig_env):
        """Panel letters and axis labels draw no ink but belong to the figure.

        Regression for vector figures: without label text in the clustering,
        panels never fuse and the labels fall outside the crop.
        """
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "vector.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(100, 100, 300, 300), color=(0, 0, 1), fill=(0.8, 0.8, 1))
        page.insert_text((105, 316), "Time (min)", fontsize=7, fontname="helv")
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0])
        doc.close()

        assert len(dets) == 1
        assert dets[0]["rect"].y1 > 316, "the axis label must be inside the crop"

    def test_overlapping_components_are_merged(self, isolated_fig_env):
        """Two crops covering the same ink are always wrong — fuse them."""
        from mcp_second_brain import figures

        a = fitz.Rect(0, 0, 100, 100)
        b = fitz.Rect(50, 50, 150, 150)      # 25% of a, 25% of b — below threshold
        far = fitz.Rect(400, 400, 500, 500)

        assert len(figures._merge_overlapping([a, far])) == 2
        inner = fitz.Rect(10, 10, 60, 60)    # wholly inside a
        merged = figures._merge_overlapping([a, inner])
        assert len(merged) == 1
        assert merged[0].get_area() == a.get_area()
        assert len(figures._merge_overlapping([a, b])) == 2

    def test_front_matter_sidebar_is_not_a_figure(self, isolated_fig_env):
        """A text sidebar carrying one stray glyph must not read as a figure.

        Journal front matter ("OPEN ACCESS / EDITED BY / CORRESPONDENCE") is a
        column of short non-prose lines with an envelope or ORCID mark in it.
        Requiring merely "contains some ink" let the whole column through.
        """
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "frontmatter.pdf"
        doc = fitz.open()
        page = doc.new_page()
        for i, line in enumerate([
            "OPEN ACCESS", "EDITED BY", "Prashant Giri", "REVIEWED BY",
            "Harshida Gamit", "*CORRESPONDENCE", "Haoyong Yu",
            "RECEIVED 13 October 2025", "ACCEPTED 28 February 2026",
        ]):
            page.insert_text((60, 100 + i * 22), line, fontsize=8, fontname="helv")
        # the lone glyph that used to bless the whole column
        page.draw_rect(fitz.Rect(60, 216, 68, 224), color=(0, 0, 0), fill=(0, 0, 0))
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0])
        doc.close()
        assert dets == []

    def test_legend_goes_to_the_figure_above_it(self, isolated_fig_env):
        """A figure legend belongs to the figure above, not the one below.

        Nearest-in-either-direction handed "Fig. 1 ..." to whatever started
        right underneath it, leaving the real figure captionless.
        """
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "legend.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(60, 60, 520, 300), color=(0, 0, 1), fill=(0.8, 0.8, 1))
        page.insert_text((60, 320), "Fig. 1 The first figure of the paper.",
                         fontsize=8, fontname="helv")
        page.draw_rect(fitz.Rect(60, 420, 520, 660), color=(1, 0, 0), fill=(1, 0.8, 0.8))
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0])
        doc.close()

        assert len(dets) == 2, "a caption in the gutter keeps the figures apart"
        upper, lower = dets[0], dets[1]
        assert "first figure" in upper["caption"]
        assert lower["caption"] == ""

    def test_split_panels_rejoin_when_no_caption_divides_them(self, isolated_fig_env):
        """Halves of one figure separated by a gutter must come back together."""
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "split.pdf"
        doc = fitz.open()
        page = doc.new_page()
        # a gutter wider than the clustering dilation splits one figure in two
        page.draw_rect(fitz.Rect(60, 60, 520, 200), color=(0, 0, 1), fill=(0.8, 0.8, 1))
        page.draw_rect(fitz.Rect(60, 230, 520, 370), color=(0, 0, 1), fill=(0.8, 0.8, 1))
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0])
        doc.close()

        assert len(dets) == 1, "no caption divides them, so they are one figure"
        r = dets[0]["rect"]
        assert r.x0 < 65 and r.x1 > 515 and r.y0 < 65 and r.y1 > 365

    def test_masthead_is_not_a_figure(self, isolated_fig_env):
        """Publisher furniture is a tidy rectangle, so only its wording gives it away."""
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "masthead.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(50, 40, 550, 120), color=(0.9, 0.9, 0.9), fill=(0.9, 0.9, 0.9))
        page.insert_text((60, 70), "Contents lists available at ScienceDirect",
                         fontsize=9, fontname="helv")
        page.insert_text((60, 95), "journal homepage: www.elsevier.com/locate/ybbrc",
                         fontsize=9, fontname="helv")
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0], 0)
        doc.close()
        assert dets == []

    def test_prose_after_the_caption_is_trimmed_off(self, isolated_fig_env):
        """A figure crop must not run on into the next paragraph of the article."""
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "runon.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.draw_rect(fitz.Rect(60, 60, 520, 300), color=(0, 0, 1), fill=(0.8, 0.8, 1))
        page.insert_textbox(fitz.Rect(60, 310, 520, 360),
                            "Fig. 1 A chart of the measured response across every condition tested.",
                            fontsize=8.5, fontname="helv")
        body = ("The wound healing area decreased on days six, ten and fourteen, and either "
                "overexpression could reverse the inhibitory effect observed here. ") * 3
        page.insert_textbox(fitz.Rect(60, 372, 520, 460), body, fontsize=9, fontname="helv")
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0], 0)
        doc.close()

        assert len(dets) == 1
        assert "chart of the measured response" in dets[0]["caption"]
        assert dets[0]["rect"].y1 < 372, "body prose must be left outside the crop"

    def test_running_head_is_trimmed_off(self, isolated_fig_env):
        """A masthead line above a figure is page furniture, not part of it."""
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "head.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((60, 56), "www.advancedsciencenews.com", fontsize=9, fontname="helv")
        page.draw_rect(fitz.Rect(60, 120, 520, 400), color=(0, 0, 1), fill=(0.8, 0.8, 1))
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0], 5)
        doc.close()

        assert len(dets) == 1
        assert dets[0]["rect"].y0 > 60, "the running head must be left outside the crop"

    def test_table_rows_separated_by_wide_gaps_stay_whole(self, isolated_fig_env):
        """Real tables space rows up to ~22pt apart; the run must not stop early.

        Regression: tightening the run gap to stop at a section heading cut a
        systematic-review table off in the middle of its rows.
        """
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "gaps.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((60, 100), "Table 4  Outcomes and safety", fontsize=8, fontname="helv")
        y = 120
        for i in range(8):
            page.insert_text((60, y), f"Author {i}   up   12 wk   None",
                             fontsize=8, fontname="helv")
            y += 22 if i % 3 else 12          # mixed row spacing, up to 22pt
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0], 4)
        doc.close()

        assert len(dets) == 1
        assert dets[0]["rect"].y1 > y - 20, "every row must be inside the crop"

    def test_body_text_is_not_a_figure(self, isolated_fig_env):
        """Pages of prose must yield nothing — no crops of paragraphs."""
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "prose.pdf"
        doc = fitz.open()
        page = doc.new_page()
        para = ("This is a long paragraph of body text that runs across the "
                "column and wraps onto several lines of continuous prose. ") * 6
        page.insert_textbox(fitz.Rect(72, 72, 520, 700), para, fontsize=11, fontname="helv")
        doc.save(str(pdf_path))
        doc.close()

        doc = fitz.open(str(pdf_path))
        dets = figures._detect_figures_geometric(doc[0])
        doc.close()
        assert dets == []

    def test_stale_detector_output_is_wiped_not_appended(self, isolated_fig_env):
        """Crops from an older detector are replaced, never accumulated beside."""
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf = _make_pdf_with_drawing(vault / "src.pdf", n_pages=1, draw_on=0)
        note_path = _make_note_with_pdf(vault, pdf)

        fig_dir = figures.FIGURES_DIR / figures._figure_slug(note_path)
        fig_dir.mkdir(parents=True, exist_ok=True)
        (fig_dir / "fig-00.png").write_bytes(b"stale")
        (fig_dir / "fig-01.png").write_bytes(b"stale")

        analyse = lambda p, caption="": {"ocr_text": "", "description": "d"}  # noqa: E731
        with patch.object(figures, "analyse_figure", side_effect=analyse):
            figures.extract_figures(note_path, vault)

        crops = sorted(fig_dir.glob("fig-*.png"))
        assert [c.name for c in crops] == ["fig-00.png"]
        assert crops[0].read_bytes() != b"stale"


# ---------------------------------------------------------------------------
# Phase 3b — caption threads into search
# ---------------------------------------------------------------------------

class TestCaptionSearch:
    def test_caption_keyword_is_searchable(self, isolated_fig_env):
        from mcp_second_brain import vault_db

        vault_db.upsert_figure(
            note_path="20-areas/research/paper.md",
            fig_index=0,
            image_url="file:///x/fig-00.png",
            local_path="/x/fig-00.png",
            ocr_text="",            # OCR empty — only the caption carries the term
            description="a scatter plot",
            token_est=400,
            caption="Figure 1. UMAP embedding of single cells",
        )
        hits = vault_db.search_figures("UMAP")
        assert len(hits) == 1
        assert hits[0]["caption"].startswith("Figure 1. UMAP")


# ---------------------------------------------------------------------------
# Phase 5 — read_figure single-image recall + real token_est
# ---------------------------------------------------------------------------

class TestReadFigure:
    def _make_png(self, path: Path, size=(1000, 1000)):
        from PIL import Image as _PILImage
        path.parent.mkdir(parents=True, exist_ok=True)
        _PILImage.new("RGB", size, (200, 200, 255)).save(str(path), "PNG")

    def test_read_figure_returns_image_and_thumbnail(self, isolated_fig_env, monkeypatch):
        from mcp_second_brain import server, figures, vault_db
        from mcp.server.fastmcp import Image

        vault = isolated_fig_env
        monkeypatch.setattr(server, "VAULT", vault)
        monkeypatch.setattr(figures, "FIGURES_DIR", vault / "figures")

        note_path = "20-areas/research/paper.md"
        fig_path = vault / "figures" / "paper" / "fig-00.png"
        self._make_png(fig_path)
        vault_db.upsert_figure(
            note_path=note_path, fig_index=0,
            image_url=f"file://{fig_path}", local_path=str(fig_path),
            ocr_text="", description="d", token_est=400, caption="cap",
        )

        result = server.read_figure(note_path, 0)
        assert isinstance(result, Image)
        # a down-scaled thumbnail was created (long edge <= 768)
        from PIL import Image as _PILImage
        thumb = vault / ".figure-thumbs" / "paper" / "fig-00.png"
        assert thumb.exists()
        with _PILImage.open(thumb) as im:
            assert max(im.size) <= 768

    def test_thumbnail_is_regenerated_when_the_figure_changes(self, isolated_fig_env):
        """A re-extracted figure must not keep serving its old thumbnail.

        Regression: the cache keyed on (note, index) alone, so every remote
        reader kept seeing the previous crop after re-extraction.
        """
        import os
        from PIL import Image
        from mcp_second_brain import figures

        note_path = "paper.md"
        fig_dir = figures.FIGURES_DIR / figures._figure_slug(note_path)
        fig_dir.mkdir(parents=True, exist_ok=True)
        src = fig_dir / "fig-00.png"

        Image.new("RGB", (900, 300), (255, 0, 0)).save(src)
        first = figures.make_figure_thumbnail(src, note_path, 0)
        assert first is not None
        with Image.open(first) as im:
            assert im.size[0] > im.size[1], "wide source -> wide thumbnail"

        # re-extraction rewrites the same path with a differently shaped crop
        Image.new("RGB", (300, 900), (0, 0, 255)).save(src)
        os.utime(src, (src.stat().st_atime + 10, src.stat().st_mtime + 10))

        second = figures.make_figure_thumbnail(src, note_path, 0)
        assert second is not None
        with Image.open(second) as im:
            assert im.size[1] > im.size[0], "thumbnail must follow the new crop"

    def test_read_figure_missing_returns_text(self, isolated_fig_env, monkeypatch):
        from mcp_second_brain import server
        monkeypatch.setattr(server, "VAULT", isolated_fig_env)
        out = server.read_figure("nope/missing.md", 3)
        assert isinstance(out, str)
        assert "No figure" in out

    def test_estimate_image_tokens_scales_with_size(self, tmp_path):
        from mcp_second_brain import figures
        from PIL import Image as _PILImage
        big = tmp_path / "big.png"
        _PILImage.new("RGB", (1400, 1400)).save(str(big))
        small = tmp_path / "small.png"
        _PILImage.new("RGB", (280, 280)).save(str(small))
        assert figures._estimate_image_tokens(big) > figures._estimate_image_tokens(small)


# ---------------------------------------------------------------------------
# Phase 5.8 — figure insight write-back as atomic vault notes
# ---------------------------------------------------------------------------

class TestAnnotateFigure:
    def _setup_paper(self, vault: Path) -> str:
        note_rel = "20-areas/research/paper.md"
        note = vault / note_rel
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            "---\ntitle: Paper\ndate: 2026-06-16\ntype: research\n"
            "status: active\ntags: []\n---\n\n# Paper\n\nBody.\n",
            encoding="utf-8",
        )
        return note_rel

    def test_annotate_creates_atomic_note_and_backlink(self, isolated_fig_env, monkeypatch):
        from mcp_second_brain import server, vault_db
        vault = isolated_fig_env
        monkeypatch.setattr(server, "VAULT", vault)

        note_rel = self._setup_paper(vault)
        out = server.annotate_figure(note_rel, 0, "panel C: IC50uniqtok = 2.3 uM")
        assert "Created" in out

        insight = vault / "20-areas/research/figure-insights/paper--fig00.md"
        assert insight.exists()
        text = insight.read_text(encoding="utf-8")
        assert "type: figure-insight" in text
        assert "source_note:" in text
        assert "IC50uniqtok" in text
        assert "[[20-areas/research/paper]]" in text  # backlink to paper

        # forward link added to the paper note
        paper_text = (vault / note_rel).read_text(encoding="utf-8")
        assert "## Figure Insights" in paper_text
        assert "paper--fig00" in paper_text

        # second insight appends (does not create a new file)
        server.annotate_figure(note_rel, 0, "panel D: n=42")
        text2 = insight.read_text(encoding="utf-8")
        assert "IC50uniqtok" in text2 and "n=42" in text2

        # DuckDB figures table is NOT touched — insight lives only in the vault note
        with vault_db._connect() as con:
            n_figs = con.execute("SELECT COUNT(*) FROM figures").fetchone()[0]
        assert n_figs == 0

    def test_insight_searchable_and_survives_rebuild(self, isolated_fig_env, monkeypatch):
        from mcp_second_brain import server, vault_db
        from mcp_second_brain.store import get_store
        vault = isolated_fig_env
        monkeypatch.setattr(server, "VAULT", vault)

        note_rel = self._setup_paper(vault)
        server.annotate_figure(note_rel, 0, "panel C: IC50uniqtok = 2.3 uM")

        store = get_store()
        hits = store.hybrid_search("IC50uniqtok", limit=10)
        assert any("figure-insights" in h.get("path", "") for h in hits)

        # rebuild the whole index from the vault (source of truth) → still found
        vault_db.sync_all(vault)
        hits2 = store.hybrid_search("IC50uniqtok", limit=10)
        assert any("figure-insights" in h.get("path", "") for h in hits2)

    def test_read_figure_surfaces_insight(self, isolated_fig_env, monkeypatch):
        from mcp_second_brain import server, figures, vault_db
        from mcp.server.fastmcp import Image
        vault = isolated_fig_env
        monkeypatch.setattr(server, "VAULT", vault)
        monkeypatch.setattr(figures, "FIGURES_DIR", vault / "figures")

        note_rel = self._setup_paper(vault)
        fig_path = vault / "figures" / "paper" / "fig-00.png"
        fig_path.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image as _PILImage
        _PILImage.new("RGB", (800, 800)).save(str(fig_path))
        vault_db.upsert_figure(
            note_path=note_rel, fig_index=0, image_url=f"file://{fig_path}",
            local_path=str(fig_path), ocr_text="", description="d", token_est=400, caption="c",
        )
        server.annotate_figure(note_rel, 0, "panel C: IC50uniqtok = 2.3 uM")

        result = server.read_figure(note_rel, 0)
        assert isinstance(result, list)
        assert any(isinstance(x, Image) for x in result)
        assert any(isinstance(x, str) and "IC50uniqtok" in x for x in result)


# ---------------------------------------------------------------------------
# Phase 4 — fallback safeguards
# ---------------------------------------------------------------------------

class TestFallbacks:
    def test_pymupdf4llm_failure_invokes_marker(self, tmp_path):
        """When pymupdf4llm raises, the Marker converter must be tried next."""
        from unittest.mock import MagicMock
        from mcp_second_brain import server

        pdf = _make_text_pdf(tmp_path / "p.pdf")
        marker_out = MagicMock()
        marker_out.markdown = "# Marker output\n\ntext"
        fake_converter = MagicMock(return_value=marker_out)

        with patch("pymupdf4llm.to_markdown", side_effect=RuntimeError("x")), \
             patch.object(server, "_get_marker_converter", return_value=fake_converter):
            body = server._extract_pdf_body(str(pdf))

        fake_converter.assert_called_once()
        assert "Marker output" in body

    def test_vlm_detection_failure_falls_back_to_pdfimages(self, isolated_fig_env):
        """Scanned page + unreachable VLM = nothing readable, so pdfimages runs.

        Geometry handles ordinary PDFs on its own; this fallback is only for
        pages that are a single scanned image with no extractable text.
        """
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf_path = vault / "scan.pdf"
        doc = fitz.open()
        page = doc.new_page()
        scan = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 600, 800))
        scan.set_rect(scan.irect, (210, 210, 200))
        page.insert_image(page.rect, pixmap=scan)   # whole page, no text layer
        doc.save(str(pdf_path))
        doc.close()
        note_path = _make_note_with_pdf(vault, pdf_path)

        sentinel = [{"fig_index": 0, "local_path": "/x/fig-00.png",
                     "ocr_text": "", "description": "from pdfimages"}]
        fallback = MagicMock(return_value=sentinel)

        with patch.object(figures, "_detect_figures_on_page", side_effect=RuntimeError("no api")), \
             patch.object(figures, "_extract_figures_pdfimages", fallback):
            results = figures.extract_figures(note_path, vault)

        fallback.assert_called_once()
        assert results == sentinel
