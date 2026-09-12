"""Real disposable PostgreSQL timeout and pool recovery checks."""
import time

import psycopg
import pytest

from mcp_second_brain.request_budget import request_budget
from mcp_second_brain.store.postgres_store import PostgresStore
from tests.test_multiuser_rls import _apply_schema


def test_statement_deadline_releases_transaction(clean_multiuser_postgres, monkeypatch):
    pg = clean_multiuser_postgres
    _apply_schema(pg)
    monkeypatch.setenv('SB_MULTIUSER', '1')
    store = PostgresStore(pg.dsn(role='sb_app'), min_size=0, max_size=1)
    try:
        started = time.monotonic()
        with pytest.raises((psycopg.errors.QueryCanceled, TimeoutError)):
            with request_budget(.2), store._conn() as conn:
                conn.execute('SELECT pg_sleep(2)')
        assert time.monotonic() - started < 1
        with store._conn() as conn:
            assert conn.execute('SELECT 1').fetchone() == (1,)
    finally:
        store.close()


def test_pool_wait_obeys_remaining_budget(clean_multiuser_postgres, monkeypatch):
    from psycopg_pool import PoolTimeout
    pg = clean_multiuser_postgres
    _apply_schema(pg)
    monkeypatch.setenv('SB_MULTIUSER', '1')
    store = PostgresStore(pg.dsn(role='sb_app'), min_size=0, max_size=1)
    try:
        with store._conn():
            started = time.monotonic()
            with pytest.raises(PoolTimeout):
                with request_budget(.05), store._conn():
                    pytest.fail('exhausted pool granted a connection')
            assert time.monotonic() - started < .5
        with store._conn() as conn:
            assert conn.execute('SELECT 1').fetchone() == (1,)
    finally:
        store.close()
