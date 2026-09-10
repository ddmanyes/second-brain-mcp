"""Tests for llm_cli.py's max_tokens threading (F3, 2026-08-19).

_local_chat() defaulted max_tokens to a hardcoded 1024, silently truncating any reply that
needed more — indistinguishable from a malformed/empty reply to the caller. These tests pin
down that the value is now threaded through from llm_text() and overridable per call.
"""
import base64
import io
import json
from unittest.mock import MagicMock, patch

from mcp_second_brain import llm_cli


def _fake_response(content: str):
    """A context-manager mock matching what urllib.request.urlopen(...) as r: json.load(r) expects."""
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestLocalChatMaxTokens:
    def test_default_max_tokens_is_1024(self, monkeypatch):
        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "http://localhost:11434/v1")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return _fake_response("ok")

        with patch.object(llm_cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = llm_cli._local_chat("hi", timeout=10)

        assert out == "ok"
        assert captured["payload"]["max_tokens"] == 1024

    def test_max_tokens_override_is_sent(self, monkeypatch):
        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "http://localhost:11434/v1")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return _fake_response("ok")

        with patch.object(llm_cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            llm_cli._local_chat("hi", timeout=10, max_tokens=4096)

        assert captured["payload"]["max_tokens"] == 4096

    def test_multimodal_data_url_uses_the_real_image_media_type(self, monkeypatch, tmp_path):
        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "http://localhost:11434/v1")
        image = tmp_path / "figure.jpeg"
        image.write_bytes(b"jpeg-placeholder")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return _fake_response("ok")

        with patch.object(llm_cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            llm_cli._local_chat("read", image_path=image, timeout=10)

        image_url = captured["payload"]["messages"][0]["content"][0]["image_url"]["url"]
        assert image_url.startswith("data:image/jpeg;base64,")

    def test_multimodal_data_url_normalizes_mislabelled_webp_for_local_vlm(
        self, monkeypatch, tmp_path
    ):
        from PIL import Image

        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "http://localhost:11434/v1")
        image = tmp_path / "mislabelled.png"
        Image.new("RGB", (72, 101), "blue").save(image, format="WEBP")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return _fake_response("ok")

        with patch.object(llm_cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            llm_cli._local_chat("read", image_path=image, timeout=10)

        image_url = captured["payload"]["messages"][0]["content"][0]["image_url"]["url"]
        assert image_url.startswith("data:image/png;base64,")
        normalized = Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1])))
        assert max(normalized.size) == 768

    def test_multimodal_request_can_use_a_dedicated_vision_endpoint(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "http://localhost:11434/v1")
        monkeypatch.setattr(llm_cli, "_LOCAL_MODEL", "gemma-12b")
        monkeypatch.setenv("SB_VISION_LLM_BASE_URL", "http://localhost:11437/v1")
        monkeypatch.setenv("SB_VISION_LLM_MODEL", "gemma-4-26b-a4b")
        image = tmp_path / "figure.png"
        image.write_bytes(b"png-placeholder")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data)
            return _fake_response("ok")

        with patch.object(llm_cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            llm_cli._local_chat("read", image_path=image, timeout=10)

        assert captured["url"] == "http://localhost:11437/v1/chat/completions"
        assert captured["payload"]["model"] == "gemma-4-26b-a4b"

    def test_text_request_ignores_the_dedicated_vision_endpoint(self, monkeypatch):
        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "http://localhost:11434/v1")
        monkeypatch.setattr(llm_cli, "_LOCAL_MODEL", "gemma-12b")
        monkeypatch.setenv("SB_VISION_LLM_BASE_URL", "http://localhost:11437/v1")
        monkeypatch.setenv("SB_VISION_LLM_MODEL", "gemma-4-26b-a4b")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data)
            return _fake_response("ok")

        with patch.object(llm_cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            llm_cli._local_chat("extract statements", timeout=10)

        assert captured["url"] == "http://localhost:11434/v1/chat/completions"
        assert captured["payload"]["model"] == "gemma-12b"


class TestLlmTextThreadsMaxTokens:
    def test_llm_text_default_matches_local_chat_default(self, monkeypatch):
        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "http://localhost:11434/v1")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return _fake_response("ok")

        with patch.object(llm_cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            llm_cli.llm_text("hi")

        assert captured["payload"]["max_tokens"] == 1024

    def test_llm_text_passes_max_tokens_through_to_local_chat(self, monkeypatch):
        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "http://localhost:11434/v1")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data)
            return _fake_response("ok")

        with patch.object(llm_cli.urllib.request, "urlopen", side_effect=fake_urlopen):
            llm_cli.llm_text("hi", max_tokens=4096)

        assert captured["payload"]["max_tokens"] == 4096

    def test_llm_text_max_tokens_does_not_leak_into_claude_cli_fallback(self, monkeypatch):
        """max_tokens is a local-backend-only concept — the CLI fallback path takes no such
        argument, so a caller requesting a high max_tokens must not break the fallback."""
        monkeypatch.setattr(llm_cli, "_LOCAL_BASE", "")  # local backend skipped entirely
        monkeypatch.setattr(llm_cli, "_CLAUDE_CLI", "/usr/bin/claude")
        with patch.object(llm_cli, "_run", return_value="claude reply") as mock_run:
            out = llm_cli.llm_text("hi", max_tokens=4096)
        assert out == "claude reply"
        mock_run.assert_called_once()
