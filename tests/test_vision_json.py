"""Tests for the VLM seam — llm_cli.vision_json and its figures.py callers.

The point of the seam is that "the model answered nothing" and "the model looked
and found nothing" are different answers. Before it existed both collapsed into an
empty dict, so a figure row could claim we had read an image we never saw, and an
unparsable detection reply was cached permanently as "this page has no figures".
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_second_brain import figures, llm_cli


@pytest.fixture()
def png(tmp_path: Path) -> Path:
    """A byte-level stand-in — enough for callers that only read the file."""
    p = tmp_path / "fig.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


@pytest.fixture()
def real_png(tmp_path: Path) -> Path:
    """A decodable PNG — _detect_figures_on_page opens it to read the page size."""
    from PIL import Image as PILImage

    p = tmp_path / "page.png"
    PILImage.new("RGB", (1000, 1000), "white").save(p)
    return p


def _no_sdk():
    """Force the CLI fallback path (no anthropic backend)."""
    return patch.object(llm_cli, "_anthropic_vision", return_value=None)


# ---------------------------------------------------------------------------
# vision_json
# ---------------------------------------------------------------------------

class TestVisionJson:
    def test_missing_image_returns_none(self, tmp_path):
        assert llm_cli.vision_json("p", tmp_path / "nope.png") is None

    def test_no_backend_answer_returns_none(self, png):
        with _no_sdk(), patch.object(llm_cli, "llm_image", return_value=None):
            assert llm_cli.vision_json("p", png) is None

    def test_unparsable_reply_returns_none(self, png):
        """A reply with no JSON is a failure, not an empty answer."""
        with _no_sdk(), patch.object(llm_cli, "llm_image", return_value="I cannot help"):
            assert llm_cli.vision_json("p", png) is None

    def test_parses_object_out_of_code_fence(self, png):
        raw = 'Sure!\n```json\n{"ocr_text": "IC50", "description": "a plot"}\n```'
        with _no_sdk(), patch.object(llm_cli, "llm_image", return_value=raw):
            answer = llm_cli.vision_json("p", png, expect="object")
        assert answer is not None
        assert answer.data["ocr_text"] == "IC50"
        assert answer.backend == "cli"
        assert answer.usage == {"input": 0, "output": 0}

    def test_empty_array_is_a_valid_answer_not_a_failure(self, png):
        with _no_sdk(), patch.object(llm_cli, "llm_image", return_value="[]"):
            answer = llm_cli.vision_json("p", png, expect="array")
        assert answer is not None, "an empty array means the model looked and saw nothing"
        assert answer.data == []

    def test_sdk_usage_is_reported(self, png):
        sdk = ('{"ocr_text": "x", "description": "y"}', {"input": 12, "output": 3})
        with patch.object(llm_cli, "_anthropic_vision", return_value=sdk):
            answer = llm_cli.vision_json("p", png)
        assert answer.usage == {"input": 12, "output": 3}
        assert answer.backend == "anthropic"

    def test_falls_back_to_cli_when_sdk_reply_is_unparsable(self, png):
        with patch.object(llm_cli, "_anthropic_vision", return_value=("garbage", {})), \
             patch.object(llm_cli, "llm_image", return_value='{"ocr_text": "ok"}'):
            answer = llm_cli.vision_json("p", png)
        assert answer is not None
        assert answer.backend == "cli"


# ---------------------------------------------------------------------------
# analyse_figure
# ---------------------------------------------------------------------------

class TestAnalyseFigure:
    def test_returns_none_when_vlm_gives_no_answer(self, png):
        with patch.object(llm_cli, "vision_json", return_value=None):
            assert figures.analyse_figure(png) is None

    def test_returns_fields_on_success(self, png):
        answer = llm_cli.VisionAnswer(
            data={"ocr_text": "axis label", "description": "a bar chart"},
            usage={"input": 5, "output": 2},
            backend="anthropic",
        )
        with patch.object(llm_cli, "vision_json", return_value=answer):
            got = figures.analyse_figure(png, caption="Figure 1")
        assert got == {
            "ocr_text": "axis label",
            "description": "a bar chart",
            "_usage": {"input": 5, "output": 2},
        }

    def test_caption_is_threaded_into_the_prompt(self, png):
        with patch.object(llm_cli, "vision_json", return_value=None) as mock:
            figures.analyse_figure(png, caption="Figure 3: survival")
        assert "Figure 3: survival" in mock.call_args[0][0]

    def test_warn_wrapper_keeps_the_figure_but_announces_the_failure(self, png, capsys):
        with patch.object(figures, "analyse_figure", return_value=None):
            got = figures._analysis_or_warn(png)
        assert got["ocr_text"] == "" and got["description"] == ""
        assert "VLM analysis unavailable" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _detect_figures_on_page
# ---------------------------------------------------------------------------

class TestDetectFiguresOnPage:
    def test_no_answer_raises_so_the_page_is_retried(self, real_png):
        """Regression: an unusable reply used to return [] and be cached forever."""
        with patch.object(llm_cli, "vision_json", return_value=None):
            with pytest.raises(figures.VisionUnavailable):
                figures._detect_figures_on_page(real_png, 0)

    def test_empty_array_returns_no_detections_without_raising(self, real_png):
        answer = llm_cli.VisionAnswer(data=[], usage={"input": 1, "output": 1}, backend="cli")
        with patch.object(llm_cli, "vision_json", return_value=answer):
            dets, usage = figures._detect_figures_on_page(real_png, 0)
        assert dets == []
        assert usage == {"input": 1, "output": 1}

    def test_normalised_bbox_is_converted_to_pixels(self, tmp_path):
        from PIL import Image as PILImage

        page = tmp_path / "page.png"
        PILImage.new("RGB", (1000, 2000), "white").save(page)

        answer = llm_cli.VisionAnswer(
            data=[{"bbox": [100, 200, 900, 800], "caption": "Fig 1", "type": "figure"}],
            usage={"input": 0, "output": 0},
            backend="cli",
        )
        with patch.object(llm_cli, "vision_json", return_value=answer):
            dets, _ = figures._detect_figures_on_page(page, 1)

        assert len(dets) == 1
        assert dets[0]["bbox"] == [100, 400, 900, 1600]
        assert dets[0]["caption"] == "Fig 1"

    def test_header_band_bbox_is_pushed_down(self, tmp_path):
        from PIL import Image as PILImage

        page = tmp_path / "page.png"
        PILImage.new("RGB", (1000, 1000), "white").save(page)

        answer = llm_cli.VisionAnswer(
            data=[{"bbox": [0, 0, 900, 800], "caption": "", "type": "figure"}],
            usage={},
            backend="cli",
        )
        with patch.object(llm_cli, "vision_json", return_value=answer):
            dets, _ = figures._detect_figures_on_page(page, 1)

        # HEADER_GUARD_Y = 60 normalised → 60px on a 1000px-tall page
        assert dets[0]["bbox"][1] == 60
