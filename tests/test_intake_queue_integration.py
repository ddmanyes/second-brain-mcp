"""Optional cross-package integration; run with lcdda-ingest on PYTHONPATH."""

from uuid import uuid4
import pytest

from mcp_second_brain.identity import Identity, KeyState, _current, set_identity
from mcp_second_brain.intake_identity import IntakeIdentityBridge

queue_module = pytest.importorskip("lcdda_ingest.queue")
HarvestWorkspace = pytest.importorskip("lcdda_ingest.workspace").HarvestWorkspace


def test_two_users_queue_visibility_revocation_and_cancel(tmp_path):
    a, b = str(uuid4()), str(uuid4())
    identities = {
        "a" * 64: Identity("a", "member", a, "a" * 64),
        "b" * 64: Identity("b", "member", b, "b" * 64),
    }
    bridge = IntakeIdentityBridge(identities.get)
    queue = queue_module.DurableQueue(
        HarvestWorkspace(tmp_path), actor_provider=bridge.actor, verifier=bridge.verify
    )
    token = set_identity(identities["a" * 64])
    try:
        job = queue.enqueue("ingest", {"source": "https://example.org/paper"}, [], None)
        assert job["owner_id"] == a
        assert len(queue.visible("ingest")) == 1
        _current.set(identities["b" * 64])
        assert queue.visible("ingest", job["id"]) == []
        assert not queue.cancel(job["id"])
        _current.set(identities["a" * 64])
        identities["a" * 64] = KeyState.REVOKED
        with pytest.raises(queue_module.QueueDenied):
            queue.visible("ingest")
        identities["a" * 64] = Identity("a", "member", a, "a" * 64)
        assert queue.cancel(job["id"])
    finally:
        _current.reset(token)


def test_legacy_env_identity_cannot_submit(tmp_path):
    bridge = IntakeIdentityBridge(lambda _: None)
    queue = queue_module.DurableQueue(
        HarvestWorkspace(tmp_path), actor_provider=bridge.actor, verifier=bridge.verify
    )
    token = set_identity(Identity("env", "admin"))
    try:
        with pytest.raises(queue_module.QueueDenied):
            queue.enqueue("ingest", {}, [], None)
        assert not list(tmp_path.glob("jobs/*.json"))
    finally:
        _current.reset(token)


def test_http_authenticated_requests_queue_their_own_identity(tmp_path):
    import asyncio

    async def scenario():
        import asyncio
        import json
        import httpx
        from mcp_second_brain.auth import APIKeyMiddleware
        from mcp_second_brain.identity import hash_key

        keys = {"synthetic-key-a": str(uuid4()), "synthetic-key-b": str(uuid4())}
        records = {hash_key(key): Identity("synthetic", "member", owner, hash_key(key))
                   for key, owner in keys.items()}
        bridge = IntakeIdentityBridge(records.get)
        workspace = HarvestWorkspace(tmp_path)
        queue = queue_module.DurableQueue(workspace, actor_provider=bridge.actor,
                                         verifier=bridge.verify)
        async def application(scope, receive, send):
            await asyncio.sleep(0)
            job = queue.enqueue("ingest", {}, [], None)
            body = json.dumps({"job_id": job["id"]}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body})
        app = APIKeyMiddleware(application, set(), lookup_fn=lambda key: records.get(hash_key(key)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            responses = await asyncio.gather(*(client.post("/", headers={"X-API-Key": key}) for key in keys))
            for (key, owner), response in zip(keys.items(), responses, strict=True):
                assert response.status_code == 200
                job = workspace.read_job(response.json()["job_id"])
                assert job["owner_id"] == owner
                assert job["credential_id"] == hash_key(key)
                assert key not in json.dumps(job)
            records[hash_key("synthetic-key-a")] = KeyState.REVOKED
            assert (await client.post("/", headers={"X-API-Key": "synthetic-key-a"})).status_code == 401
        assert len(workspace.list_jobs("ingest")) == 2

    asyncio.run(scenario())
