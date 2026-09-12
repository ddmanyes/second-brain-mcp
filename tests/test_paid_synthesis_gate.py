"""Member requests never retrieve paid model credentials implicitly."""
import pytest

from mcp_second_brain import server
from mcp_second_brain.identity import Identity, _current, set_identity


@pytest.mark.parametrize('role', ['reader', 'member', 'writer', 'admin'])
@pytest.mark.parametrize('enabled', ['0', '1'])
def test_multiuser_synthesis_requires_admin_and_explicit_opt_in(monkeypatch, role, enabled):
    monkeypatch.setenv('SB_MULTIUSER', '1')
    monkeypatch.setenv('SB_ALLOW_PAID_SYNTHESIS', enabled)
    token = set_identity(Identity('operator', role))
    try:
        assert server._paid_synthesis_allowed() is (role == 'admin' and enabled == '1')
    finally:
        _current.reset(token)


def test_denied_synthesis_does_not_retrieve_or_read_keychain(monkeypatch):
    monkeypatch.setenv('SB_MULTIUSER', '1')
    monkeypatch.delenv('SB_ALLOW_PAID_SYNTHESIS', raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('denied synthesis performed external work')
    monkeypatch.setattr(server, 'query_graph', forbidden)
    monkeypatch.setattr(server.subprocess, 'run', forbidden)
    assert 'disabled' in server.litnet_answer('private query', entity='gene')
    with pytest.raises(PermissionError):
        server._la_synth('private query', 'unused-model')


def test_legacy_service_preserves_existing_synthesis(monkeypatch):
    monkeypatch.delenv('SB_MULTIUSER', raising=False)
    assert server._paid_synthesis_allowed()
