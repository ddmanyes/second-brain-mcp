"""Synthetic regression tests for multiuser retrieval side channels."""

from unittest.mock import Mock

import pytest

from mcp_second_brain import server
from mcp_second_brain.identity import Identity, _current, set_identity


@pytest.fixture
def member_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")
    monkeypatch.setattr(server, "VAULT", tmp_path)
    private = tmp_path / "90-personal/22222222-2222-2222-2222-222222222222/private.md"
    private.parent.mkdir(parents=True)
    private.write_text("secret-canary", encoding="utf-8")
    store = Mock()
    store.hybrid_search.return_value = []
    monkeypatch.setattr(server, "_store", store)
    token = set_identity(Identity("alice", "member", "11111111-1111-1111-1111-111111111111"))
    yield tmp_path, store
    _current.reset(token)


@pytest.mark.parametrize("failed", [False, True])
def test_empty_or_failed_index_never_scans_private_files(member_vault, failed):
    _, store = member_vault
    if failed:
        store.hybrid_search.side_effect = RuntimeError("private-dsn-secret")
    result = server.search_notes("secret-canary")
    assert "private.md" not in result
    assert "[file scan]" not in result
    assert "private-dsn-secret" not in result
    assert ("unavailable" in result.lower()) == failed


def test_snippet_query_does_not_persist_raw_text(member_vault):
    vault, store = member_vault
    server.search_snippets("private-query-canary")
    assert not (vault / ".query-log.jsonl").exists()
    store.append_audit_log.assert_not_called()


def test_graph_query_does_not_persist_raw_text(member_vault):
    vault, _ = member_vault
    server.query_graph("private-query-canary", mode="snippets")
    assert not (vault / ".query-log.jsonl").exists()


def test_private_note_never_enters_shared_markdown_index(member_vault):
    vault, _ = member_vault
    server._append_to_index("90-personal/11111111-1111-1111-1111-111111111111/a.md", "secret", "2026-09-12")
    assert not (vault / "memory/index.md").exists()


def test_shared_article_links_do_not_publish_private_paths(member_vault):
    vault, store = member_vault
    article = vault / "article.md"
    article.write_text("---\ntitle: Public\n---\n\nBody", encoding="utf-8")
    store.find_related.return_value = [
        "shared.md", "90-personal/11111111-1111-1111-1111-111111111111/private.md"
    ]
    assert server._inject_related_links(article, "article.md") == 1
    assert "90-personal" not in article.read_text()
