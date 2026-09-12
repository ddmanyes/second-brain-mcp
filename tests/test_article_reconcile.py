import json
from uuid import uuid4

import pytest

from mcp_second_brain.article_attribution import commit_shared_article
from mcp_second_brain.article_reconcile import reconcile_articles
from mcp_second_brain.identity import Identity, get_current_identity


def test_pending_metadata_repair_is_explicit_and_idempotent(tmp_path):
    member = Identity('a', 'member', str(uuid4()))
    admin = Identity('admin', 'admin', str(uuid4()))
    result = commit_shared_article(tmp_path, '20-areas/research/paper.md', '---\ntitle: Paper\n---\nBody', member, source='https://example.test/paper', index=lambda _: False)
    registry = tmp_path/'.article-identities.json'
    before = registry.read_bytes()
    indexed = []
    def index(path):
        assert get_current_identity() == admin
        indexed.append(path)
        return True
    preview = reconcile_articles(tmp_path, admin, index=index)
    assert preview['records'][0]['status'] == 'pending'
    assert registry.read_bytes() == before and indexed == []
    applied = reconcile_articles(tmp_path, admin, index=index, apply=True)
    assert applied['records'][0]['status'] == 'complete'
    assert len(indexed) == 1
    assert reconcile_articles(tmp_path, admin, index=index, apply=True)['records'] == []
    assert (tmp_path/result.path).read_text().count(member.user_uuid) >= 1
    with pytest.raises(PermissionError):
        reconcile_articles(tmp_path, member, index=index, apply=True)


def test_missing_reserved_document_never_claims_success(tmp_path):
    registry = {'doi:10.1234/x': {'path': '20-areas/research/missing.md', 'index_status': 'pending'}}
    (tmp_path/'.article-identities.json').write_text(json.dumps(registry))
    result = reconcile_articles(tmp_path, Identity('admin', 'admin', str(uuid4())), index=lambda _: pytest.fail('no document'), apply=True)
    assert result['records'][0]['status'] == 'missing_document'
    assert json.loads((tmp_path/'.article-identities.json').read_text()) == registry
