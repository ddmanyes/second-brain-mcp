import asyncio
import threading
import time

import pytest

from mcp_second_brain.auth import APIKeyMiddleware
from mcp_second_brain.identity import Identity, get_current_identity


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_slow_auth_does_not_block_loop_and_overflow_rejects():
    release = threading.Event()
    entered = threading.Event()
    identities = []

    def lookup(key):
        entered.set()
        release.wait(2)
        return Identity(key, 'member')

    async def app(scope, receive, send):
        identities.append(get_current_identity().user_id)
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})

    middleware = APIKeyMiddleware(app, set(), lookup_fn=lookup)

    async def request(key):
        messages = []

        async def send(message):
            messages.append(message)

        await middleware({'type': 'http', 'path': '/mcp', 'headers': [(b'x-api-key', key.encode())]}, None, send)
        return messages[0]['status']

    tasks = [asyncio.create_task(request(str(n))) for n in range(4)]
    try:
        deadline = time.monotonic() + 1
        while not entered.is_set() or middleware._lookup_dispatch.active != 4:
            assert time.monotonic() < deadline
            await asyncio.sleep(.001)
        assert await request('overflow') == 503
        assert identities == []
    finally:
        release.set()
        assert await asyncio.gather(*tasks) == [204] * 4
        assert await middleware._lookup_dispatch.close()
    assert set(identities) == {'0', '1', '2', '3'}


@pytest.mark.anyio
async def test_oversized_key_never_reaches_lookup():
    def lookup(key):
        pytest.fail('oversized key reached database')

    async def app(*args):
        pytest.fail('unauthorized request reached app')

    messages = []

    async def send(message):
        messages.append(message)

    middleware = APIKeyMiddleware(app, set(), lookup_fn=lookup)
    await middleware({'type': 'http', 'headers': [(b'x-api-key', b'x' * 513)]}, None, send)
    assert messages[0]['status'] == 401


def test_multiuser_never_promotes_unknown_environment_key(monkeypatch):
    from mcp_second_brain.auth import _authenticate
    monkeypatch.setenv('SB_MULTIUSER', '1')
    assert _authenticate('legacy-admin', {'legacy-admin'}, lambda _: None) is None
    monkeypatch.delenv('SB_MULTIUSER')
    assert _authenticate('legacy-admin', {'legacy-admin'}, lambda _: None).role == 'admin'


def test_short_environment_key_identity_never_contains_credential(monkeypatch):
    from mcp_second_brain.auth import _authenticate
    from mcp_second_brain.identity import hash_key
    monkeypatch.delenv('SB_MULTIUSER', raising=False)
    result = _authenticate('secret', {'secret'}, None)
    assert result.user_id == 'env:' + hash_key('secret')
    assert 'secret' not in repr(result)
