"""Crash-point simulations preserve one canonical shared document and attribution."""
import json
from uuid import uuid4

import pytest

from mcp_second_brain import article_attribution as articles
from mcp_second_brain.article_reconcile import reconcile_articles
from mcp_second_brain.identity import Identity


@pytest.mark.parametrize('crash_write', [1, 2, 3])
def test_restart_at_atomic_publication_boundaries(tmp_path, monkeypatch, crash_write):
    member = Identity('member', 'member', str(uuid4()))
    admin = Identity('admin', 'admin', str(uuid4()))
    original = articles._atomic_write
    calls = 0

    def crash(path, text):
        nonlocal calls
        calls += 1
        original(path, text)
        if calls == crash_write:
            raise SystemExit('synthetic process death')

    monkeypatch.setattr(articles, '_atomic_write', crash)
    args = (tmp_path, '20-areas/research/2026_Test_Paper.md', '---\ntitle: Paper\n---\nCanonical body', member)
    with pytest.raises(SystemExit):
        articles.commit_shared_article(*args, source='https://example.test/paper', index=lambda _: True)
    monkeypatch.setattr(articles, '_atomic_write', original)
    # Recovery may index an already-published document. Missing document is never
    # reconstructed by reconciliation: only an explicitly re-submitted source can.
    repaired = reconcile_articles(tmp_path, admin, index=lambda _: True, apply=True)
    if crash_write == 1:
        assert repaired['records'][0]['status'] == 'missing_document'
    retried = articles.commit_shared_article(*args, source='https://example.test/paper', index=lambda _: True)
    assert retried.index_status == 'complete'
    assert len(list((tmp_path/'20-areas/research').glob('*.md'))) == 1
    metadata = articles.parse_frontmatter((tmp_path/retried.path).read_text())
    assert metadata['uploaded_by'] == member.user_uuid
    assert json.loads(metadata['contributor_ids']) == [member.user_uuid]
    assert all(row['index_status'] == 'complete' for row in json.loads((tmp_path/'.article-identities.json').read_text()).values())
