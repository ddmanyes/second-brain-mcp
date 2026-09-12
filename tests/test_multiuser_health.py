from contextlib import contextmanager
from types import SimpleNamespace

from mcp_second_brain import server
from mcp_second_brain.identity import Identity, _current


def test_member_health_does_not_scan_private_vault_or_expose_errors(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")

    class ForbiddenVault:
        def __getattr__(self, name):
            raise AssertionError("private-vault-canary")

    @contextmanager
    def connection(**_kwargs):
        yield SimpleNamespace(execute=lambda query: None)

    monkeypatch.setattr(server, "VAULT", ForbiddenVault())
    monkeypatch.setattr(server, "_store", SimpleNamespace(_conn=connection))
    token = _current.set(Identity("member", "member"))
    try:
        assert server.health_check() == '{"database": "ready", "mode": "multiuser"}'

        def unavailable(**_kwargs):
            raise RuntimeError("secret DSN and private query")

        monkeypatch.setattr(server, "_store", SimpleNamespace(_conn=unavailable))
        assert server.health_check() == "Multiuser health unavailable."
    finally:
        _current.reset(token)


def test_query_window_only_exposed_to_admin(monkeypatch):
    import json
    from mcp_second_brain.operational_alerts import QueryWindow

    @contextmanager
    def connection(**_kwargs):
        yield SimpleNamespace(execute=lambda query: None)

    monkeypatch.setenv("SB_MULTIUSER", "1")
    monkeypatch.setattr(server, "_store", SimpleNamespace(_conn=connection))
    monkeypatch.setattr(server, "_query_window", QueryWindow())
    for role in ("member", "admin"):
        token = _current.set(Identity(role, role))
        try:
            report = json.loads(server.health_check())
            assert ("query_window" in report) is (role == "admin")
            if role == "admin":
                assert report["query_window"] == {
                    "samples": 0,
                    "query_p95_ms": None,
                    "query_timeout_rate": None,
                }
        finally:
            _current.reset(token)
