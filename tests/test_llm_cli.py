"""Tests for llm_cli.py's max_tokens threading (F3, 2026-08-19).

_local_chat() defaulted max_tokens to a hardcoded 1024, silently truncating any reply that
needed more — indistinguishable from a malformed/empty reply to the caller. These tests pin
down that the value is now threaded through from llm_text() and overridable per call.
"""
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
