"""Tests for the PDF pipeline upgrade (IMPLEMENTATION_PLAN.md).

Covers:
  - Phase 1: pymupdf4llm primary text extraction
  - Phase 2: page-render + VLM crop figure extraction + page-hash negative cache
  - Phase 3a/3b: VLM token budget + caption threading
  - Phase 5: read_figure recall ladder + real token_est
"""

import re
from pathlib import Path
from unittest.mock import patch

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

        # markdown heading present (font-size heuristic → ##)
        assert "##" in body
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

        detect = lambda png, num: [  # noqa: E731
            {"bbox": [100, 150, 900, 900], "caption": "Figure 1. A vector chart.",
             "type": "figure"}
        ]
        analyse = lambda p: {"ocr_text": "axis labels", "description": "a chart"}  # noqa: E731

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
        """Second extract_figures run must not re-send any page to the VLM,
        including text-only pages (negative cache)."""
        from mcp_second_brain import figures
        from unittest.mock import MagicMock

        vault = isolated_fig_env
        # 2 pages: page 0 has a figure, page 1 is text-only (blank-cache check)
        pdf = _make_pdf_with_drawing(vault / "src.pdf", n_pages=2, draw_on=0)
        note_path = _make_note_with_pdf(vault, pdf)

        def detect(png, num):
            if num == 0:
                return [{"bbox": [100, 150, 900, 900], "caption": "Fig 1", "type": "figure"}]
            return []

        mock_detect = MagicMock(side_effect=detect)
        analyse = lambda p: {"ocr_text": "", "description": "d"}  # noqa: E731

        with patch.object(figures, "_detect_figures_on_page", mock_detect), \
             patch.object(figures, "analyse_figure", side_effect=analyse):
            figures.extract_figures(note_path, vault)
            calls_after_first = mock_detect.call_count
            assert calls_after_first == 2  # both pages processed once

            figures.extract_figures(note_path, vault)
            # negative cache: no page (incl. the text-only one) is re-sent
            assert mock_detect.call_count == calls_after_first
