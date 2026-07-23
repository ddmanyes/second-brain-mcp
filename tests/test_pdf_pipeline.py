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
        """Second extract_figures run must not re-send any page to the VLM,
        including text-only pages (negative cache)."""
        from mcp_second_brain import figures
        from unittest.mock import MagicMock

        vault = isolated_fig_env
        # 2 pages: page 0 has a figure, page 1 is text-only (blank-cache check)
        pdf = _make_pdf_with_drawing(vault / "src.pdf", n_pages=2, draw_on=0)
        note_path = _make_note_with_pdf(vault, pdf)

        def detect(png, num):
            # _detect_figures_on_page 回傳 (detections, usage_dict)
            if num == 0:
                return [{"bbox": [100, 150, 900, 900], "caption": "Fig 1", "type": "figure"}], {}
            return [], {}

        mock_detect = MagicMock(side_effect=detect)
        analyse = lambda p, caption="": {"ocr_text": "", "description": "d"}  # noqa: E731

        with patch.object(figures, "_detect_figures_on_page", mock_detect), \
             patch.object(figures, "analyse_figure", side_effect=analyse):
            figures.extract_figures(note_path, vault)
            calls_after_first = mock_detect.call_count
            assert calls_after_first == 2  # both pages processed once

            figures.extract_figures(note_path, vault)
            # negative cache: no page (incl. the text-only one) is re-sent
            assert mock_detect.call_count == calls_after_first


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
        """If every page detection raises, extract_figures uses pdfimages."""
        from mcp_second_brain import figures

        vault = isolated_fig_env
        pdf = _make_pdf_with_drawing(vault / "src.pdf", n_pages=1, draw_on=0)
        note_path = _make_note_with_pdf(vault, pdf)

        sentinel = [{"fig_index": 0, "local_path": "/x/fig-00.png",
                     "ocr_text": "", "description": "from pdfimages"}]
        fallback = MagicMock(return_value=sentinel)

        with patch.object(figures, "_detect_figures_on_page", side_effect=RuntimeError("no api")), \
             patch.object(figures, "_extract_figures_pdfimages", fallback):
            results = figures.extract_figures(note_path, vault)

        fallback.assert_called_once()
        assert results == sentinel
