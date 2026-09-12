"""No-network tests for multiuser startup and transaction identity binding."""

from contextlib import contextmanager

import pytest

from mcp_second_brain.identity import Identity, _current, set_identity
from mcp_second_brain.store.postgres_store import PostgresStore


class FakePool:
    def __init__(self, row=(True,) * 8):
        self.row = row
        self.calls = []
        self.closed = False

    @contextmanager
    def connection(self, *, timeout=None):
        self.checkout_timeout = timeout
        yield self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


def make_store(row=(True,) * 8, multiuser=True):
    store = object.__new__(PostgresStore)
    store._multiuser = multiuser
    store._pool = FakePool(row)
    return store


@pytest.mark.parametrize("identity, expected", [
    (None, ["", "off"]),
    (Identity("reader", "reader"), ["", "off"]),
    (Identity("a", "member", "00000000-0000-0000-0000-000000000001"),
     ["00000000-0000-0000-0000-000000000001", "off"]),
    (Identity("maintenance", "admin"), ["", "on"]),
])
def test_transaction_gucs_require_explicit_admin(identity, expected):
    store = make_store()
    token = _current.set(identity)
    try:
        with store._conn():
            pass
    finally:
        _current.reset(token)
    assert store._pool.calls[0][1] == [*expected, "2000"]
    assert store._pool.checkout_timeout == 0.5
    assert "true" in store._pool.calls[0][0]  # transaction-local GUCs


def test_anonymous_checkout_clears_prior_admin_state():
    store = make_store()
    token = set_identity(Identity("maintenance", "admin"))
    try:
        with store._conn():
            pass
    finally:
        _current.reset(token)
    token = _current.set(None)
    try:
        with store._conn():
            pass
    finally:
        _current.reset(token)
    assert [params for _, params in store._pool.calls] == [["", "on", "2000"], ["", "off", "2000"]]


def test_singleuser_checkout_remains_unmodified():
    store = make_store(multiuser=False)
    with store._conn():
        pass
    assert store._pool.calls == []
    assert store._pool.checkout_timeout is None


def test_startup_accepts_verified_security_contract():
    make_store()._verify_multiuser_schema()


@pytest.mark.parametrize("failed_check", range(8))
def test_startup_rejects_each_missing_security_precondition(failed_check):
    checks = [True] * 8
    checks[failed_check] = False
    with pytest.raises(RuntimeError, match="SB_MULTIUSER"):
        make_store(tuple(checks))._verify_multiuser_schema()


def test_startup_rejects_missing_verification_row():
    with pytest.raises(RuntimeError):
        make_store(None)._verify_multiuser_schema()


def test_failed_startup_closes_pool(monkeypatch):
    from mcp_second_brain.store import postgres_store

    pool = FakePool((False,) * 8)
    monkeypatch.setattr(postgres_store, "ConnectionPool", lambda *a, **kw: pool)
    monkeypatch.setattr(postgres_store._visibility, "multiuser_enabled", lambda: True)
    with pytest.raises(RuntimeError):
        PostgresStore("synthetic-do-not-connect")
    assert pool.closed


@pytest.fixture
def isolated_multiuser_schema(clean_multiuser_postgres, monkeypatch):
    import psycopg
    from mcp_second_brain.store.migrate_multiuser import apply_multiuser_schema

    pg = clean_multiuser_postgres
    with psycopg.connect(pg.dsn()) as conn:
        apply_multiuser_schema(conn)
    pg.set_sb_app_password()
    monkeypatch.setenv("SB_MULTIUSER", "1")
    yield pg
    # Role settings survive schema resets inside the shared test container.
    with psycopg.connect(pg.dsn()) as conn:
        conn.execute("ALTER ROLE sb_app NOSUPERUSER NOBYPASSRLS")
        conn.execute("REVOKE postgres FROM sb_app")
        conn.execute("ALTER ROLE sb_app RESET search_path")


def test_postgres_accepts_safe_role_and_rls(isolated_multiuser_schema):
    store = PostgresStore(isolated_multiuser_schema.dsn(role="sb_app"))
    store.close()


@pytest.mark.parametrize("unsafe_sql", [
    "ALTER TABLE notes DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE note_chunks DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE figures DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE notes NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE note_chunks NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE figures NO FORCE ROW LEVEL SECURITY",
    "ALTER ROLE sb_app SUPERUSER",
    "ALTER ROLE sb_app BYPASSRLS",
    "ALTER TABLE notes OWNER TO sb_app",
    "GRANT postgres TO sb_app",
    "ALTER ROLE sb_app SET search_path = pg_catalog",
])
def test_postgres_refuses_unsafe_startup(isolated_multiuser_schema, unsafe_sql):
    import psycopg

    pg = isolated_multiuser_schema
    with psycopg.connect(pg.dsn()) as conn:
        conn.execute(unsafe_sql)
    with pytest.raises(RuntimeError, match="SB_MULTIUSER"):
        PostgresStore(pg.dsn(role="sb_app"))


def test_postgres_anonymous_sees_shared_only_and_admin_is_explicit(isolated_multiuser_schema):
    import psycopg

    pg = isolated_multiuser_schema
    with psycopg.connect(pg.dsn()) as conn:
        conn.execute(
            "INSERT INTO notes (path, title, owner_id) VALUES "
            "('shared.md', 'Shared', NULL), "
            "('private.md', 'Private', '00000000-0000-0000-0000-000000000001')"
        )
    store = PostgresStore(pg.dsn(role="sb_app"), min_size=1, max_size=1)
    token = _current.set(None)
    try:
        with store._conn() as conn:
            assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
        admin_token = set_identity(Identity("authorized-maintenance", "admin"))
        try:
            with store._conn() as conn:
                assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
        finally:
            _current.reset(admin_token)
        with store._conn() as conn:
            assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    finally:
        _current.reset(token)
        store.close()
