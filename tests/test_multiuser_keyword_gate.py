"""Private note writes never trigger unbounded background model fallback."""
from pathlib import Path

import pytest

from mcp_second_brain import server


def test_multiuser_keywords_do_not_start_threads_or_call_models(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")
    def forbidden(*args, **kwargs):
        pytest.fail("multiuser keyword enrichment performed external work")
    monkeypatch.setattr(server.threading, "Thread", forbidden)
    monkeypatch.setattr(server.llm_cli, "llm_text", forbidden)
    assert server._extract_semantic_keywords_via_gemini("private content") == []
    assert server._run_keyword_enrichment_async(Path("unused.md"), "private content") is None


def test_legacy_keywords_keep_existing_model_path(monkeypatch):
    monkeypatch.delenv("SB_MULTIUSER", raising=False)
    monkeypatch.setattr(server.llm_cli, "llm_text", lambda *a, **kw: '["legacy"]')
    assert server._extract_semantic_keywords_via_gemini("content") == ["legacy"]
