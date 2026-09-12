import io
from uuid import uuid4

from mcp_second_brain.article_maintenance import main
from mcp_second_brain.identity import Identity


def test_preview_and_denial_do_not_index(tmp_path, monkeypatch):
    monkeypatch.setenv('SB_MULTIUSER', '1')
    monkeypatch.setenv('SECOND_BRAIN_PATH', str(tmp_path))
    monkeypatch.setenv('SB_PG_DSN', 'synthetic-secret-dsn')
    class Store:
        identity = Identity('admin', 'admin', str(uuid4()))
        def __init__(self, *args, **kwargs):
            pass
        def get_identity_for_key(self, digest):
            assert digest != 'secret-key'
            return self.identity
        def index_shared_article_metadata(self, *args):
            raise AssertionError('preview must not index')
        def close(self):
            pass
    output, error = io.StringIO(), io.StringIO()
    assert main(['--key-stdin'], stdin=io.StringIO('secret-key'), stdout=output, stderr=error, store_factory=Store) == 0
    assert '"applied": false' in output.getvalue()
    Store.identity = Identity('member', 'member', str(uuid4()))
    assert main(['--key-stdin','--apply'], stdin=io.StringIO('secret-key'), stdout=output, stderr=error, store_factory=Store) == 3
    assert 'secret' not in output.getvalue() + error.getvalue()
