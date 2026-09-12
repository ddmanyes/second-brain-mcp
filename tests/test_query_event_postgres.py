"""Opt-in real RLS/retention tests on the owned disposable PostgreSQL harness."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from mcp_second_brain.query_events import (
    Actor,
    ActorRole,
    Outcome,
    QueryEventRecorder,
    mark_outcome,
)
from mcp_second_brain.store.query_event_repository import (
    QueryEventRepository,
    apply,
    retention,
)


@pytest.fixture
def repository(multiuser_postgres):
    pg = multiuser_postgres
    pg.reset()
    with psycopg.connect(pg.dsn()) as admin:
        admin.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sb_app') "
            "THEN CREATE ROLE sb_app LOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$"
        )
        apply(admin)
    pg.set_sb_app_password()
    pool = ConnectionPool(
        pg.dsn(role="sb_app"), min_size=1, max_size=1, kwargs={"autocommit": False}
    )
    current = ContextVar("query_test_actor", default=Actor())
    repo = QueryEventRepository(pool, actor_getter=current.get)
    try:
        yield repo, current, pg
    finally:
        pool.close()


def event(actor, *, when=None, count=2):
    events = []
    recorder = QueryEventRecorder(
        service="lcdda",
        actor_getter=lambda: actor,
        sink=events.append,
        enabled=True,
        revision="a" * 40,
        retrieval_revision="b" * 40,
    )

    @recorder.instrument(tool="search_notes", operation="query")
    def search():
        mark_outcome(Outcome(result_count=count))
        return "private raw query never leaves recorder"

    search()
    return replace(events[0], started_at=when) if when is not None else events[0]


def test_schema_explicit_verify_and_actor_scoped_summary(repository):
    repo, current, _ = repository
    repo.verify_schema()
    alice = Actor(uuid4(), ActorRole.MEMBER)
    bob = Actor(uuid4(), ActorRole.MEMBER)
    repo.write(event(alice))
    repo.write(event(bob))
    current.set(alice)
    assert repo.summary()[0]["event_count"] == 1
    current.set(bob)
    assert repo.summary()[0]["event_count"] == 1
    current.set(Actor(uuid4(), ActorRole.ADMIN))
    assert repo.summary()[0]["event_count"] == 2
    current.set(Actor())
    with pytest.raises(PermissionError):
        repo.summary()


def test_direct_sql_rls_no_context_and_cross_actor_insert_denied(repository):
    repo, _, _ = repository
    alice = Actor(uuid4(), ActorRole.MEMBER)
    bob = Actor(uuid4(), ActorRole.MEMBER)
    item = event(alice)
    repo.write(item)
    with repo._pool.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM query_events").fetchone()[0] == 0
        )
        assert (
            connection.execute(
                "SELECT NULLIF(current_setting('app.query_event_actor', true), '')"
            ).fetchone()[0]
            is None
        )
    with repo._connection(bob) as connection:
        assert (
            connection.execute("SELECT count(*) FROM query_events").fetchone()[0] == 0
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with repo._connection(bob) as connection:
            connection.execute(
                "INSERT INTO query_events (event_id,request_id,actor_id,actor_role,service,tool,operation,"
                "started_at,duration_ms,status,error_code) VALUES (%s,%s,%s,'member','lcdda','search','query',"
                "now(),1,'success','none')",
                (uuid4(), uuid4(), alice.user_id),
            )


def test_runtime_cannot_delete_update_or_read_daily_as_member(repository):
    repo, _, _ = repository
    alice = Actor(uuid4(), ActorRole.MEMBER)
    repo.write(event(alice))
    for sql in (
        "DELETE FROM query_events",
        "UPDATE query_events SET duration_ms=0",
        "DELETE FROM query_event_daily",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with repo._connection(alice) as connection:
                connection.execute(sql)
    with repo._connection(alice) as connection:
        assert (
            connection.execute("SELECT count(*) FROM query_event_daily").fetchone()[0]
            == 0
        )
        assert connection.execute("SHOW statement_timeout").fetchone()[0] == "500ms"
        assert connection.execute("SHOW lock_timeout").fetchone()[0] == "100ms"


def test_idempotent_write_and_no_raw_payload_columns(repository):
    repo, _, _ = repository
    item = event(Actor(uuid4(), ActorRole.MEMBER))
    repo.write(item)
    repo.write(item)
    with repo._connection(Actor(None, ActorRole.ADMIN)) as connection:
        rows = connection.execute("SELECT * FROM query_events").fetchall()
        assert len(rows) == 1
        assert "private raw query" not in str(rows)
        assert len(rows[0]) == 14


def test_retention_preview_atomic_rollup_and_idempotent_apply(repository):
    repo, current, pg = repository
    now = datetime.now(UTC)
    alice = Actor(uuid4(), ActorRole.MEMBER)
    repo.write(event(alice, when=now - timedelta(days=31)))
    repo.write(event(alice, when=now - timedelta(days=31)))
    repo.write(event(alice, when=now - timedelta(days=181)))
    repo.write(event(alice, when=now))
    with psycopg.connect(pg.dsn()) as admin:
        preview = retention(admin, now=now)
        assert preview["raw_eligible"] == 3
        assert preview["raw_deleted"] == 0
        assert admin.execute("SELECT count(*) FROM query_events").fetchone()[0] == 4
        applied = retention(admin, now=now, apply_changes=True, batch_size=2)
        assert applied["raw_deleted"] == 2
        assert retention(admin, now=now, apply_changes=True)["raw_deleted"] == 1
        assert retention(admin, now=now, apply_changes=True)["raw_deleted"] == 0
        assert (
            admin.execute("SELECT sum(event_count) FROM query_event_daily").fetchone()[
                0
            ]
            == 2
        )
        columns = admin.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='query_event_daily'"
        ).fetchall()
        assert not {"actor_id", "request_id", "event_id", "query"} & {
            r[0] for r in columns
        }
    current.set(alice)
    assert repo.summary(days=180)[0]["event_count"] == 1
    current.set(Actor(None, ActorRole.ADMIN))
    assert repo.summary(days=180)[0]["event_count"] == 3


def test_retention_disallowed_to_runtime_and_old_daily_deleted(repository):
    repo, _, pg = repository
    with repo._pool.connection() as connection:
        with pytest.raises(PermissionError):
            retention(connection)
    with psycopg.connect(pg.dsn()) as admin:
        admin.execute(
            "INSERT INTO query_event_daily VALUES (current_date - 181, 'lcdda', 'search', 'query', "
            "'success', 'none', 1, 1, 1, 0, 0, 0, 0)"
        )
        result = retention(admin, apply_changes=True)
        assert result["daily_deleted"] == 1


def test_verify_refuses_superuser_and_missing_rls(repository):
    repo, current, pg = repository
    with ConnectionPool(pg.dsn(), min_size=1, max_size=1) as pool:
        admin_repo = QueryEventRepository(pool, actor_getter=current.get)
        with pytest.raises(RuntimeError, match="subject to RLS"):
            admin_repo.verify_schema()
    with psycopg.connect(pg.dsn()) as admin:
        admin.execute("ALTER TABLE query_events DISABLE ROW LEVEL SECURITY")
    with pytest.raises(RuntimeError, match="RLS migration"):
        repo.verify_schema()


def test_lock_contention_is_bounded(repository):
    repo, _, pg = repository
    with psycopg.connect(pg.dsn()) as admin:
        admin.execute("LOCK TABLE query_events IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            repo.write(event(Actor(uuid4(), ActorRole.MEMBER)))


def test_constructor_has_no_connection_or_ddl():
    class ForbiddenPool:
        def connection(self, **kwargs):
            pytest.fail("constructor connected")

    QueryEventRepository(ForbiddenPool(), actor_getter=Actor)


def test_raw_percentiles_and_zero_result_denominator(repository):
    repo, current, _ = repository
    alice = Actor(uuid4(), ActorRole.MEMBER)
    for duration, count in ((10, 0), (20, 2), (30, None)):
        repo.write(replace(event(alice, count=count), duration_ms=duration))
    current.set(alice)
    result = repo.summary()[0]
    assert result["event_count"] == 3
    assert result["zero_result_count"] == 1
    assert result["search_observed"] == 2
    assert result["zero_result_rate"] == 0.5
    assert result["duration_p50_ms"] == 20
    assert result["duration_p95_ms"] == pytest.approx(29)
    assert result["percentiles_available"] is True


def test_rolled_percentiles_unavailable_and_zero_counts_preserved(repository):
    repo, current, pg = repository
    alice = Actor(uuid4(), ActorRole.MEMBER)
    repo.write(event(alice, when=datetime.now(UTC) - timedelta(days=31), count=0))
    repo.write(event(alice, count=1))
    with psycopg.connect(pg.dsn()) as admin:
        retention(admin, apply_changes=True)
    current.set(Actor(None, ActorRole.ADMIN))
    result = repo.summary(days=180)[0]
    assert result["duration_p50_ms"] is None
    assert result["duration_p95_ms"] is None
    assert result["percentiles_available"] is False
    assert result["zero_result_rate"] == 0.5


@pytest.mark.parametrize(
    "change",
    [
        "ALTER POLICY query_events_read ON query_events USING (true)",
        "ALTER POLICY query_events_insert ON query_events WITH CHECK (true)",
        "ALTER POLICY query_events_read ON query_events TO PUBLIC",
    ],
)
def test_startup_refuses_same_name_policy_tampering(repository, change):
    repo, _, pg = repository
    with psycopg.connect(pg.dsn()) as admin:
        admin.execute(change)
    with pytest.raises(RuntimeError, match="policies do not match"):
        repo.verify_schema()


def test_admin_report_real_readonly_pool_and_revocation(repository):
    import io
    from mcp_second_brain.identity import hash_key
    from mcp_second_brain.query_event_report import main
    repo, _, pg = repository
    actor = Actor(uuid4(), ActorRole.ADMIN)
    repo.write(event(actor))
    key = "synthetic-report-key"
    from pathlib import Path
    schema = Path(__file__).parents[1] / "mcp_second_brain/store/postgres_schema.sql"
    with psycopg.connect(pg.dsn()) as admin:
        admin.execute(schema.read_text())
        admin.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS user_uuid uuid")
        admin.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at timestamptz")
        admin.execute("GRANT SELECT ON api_keys TO sb_app")
        admin.execute("INSERT INTO api_keys (key_hash,user_id,role,user_uuid) VALUES (%s,%s,%s,%s)",
                      [hash_key(key), "synthetic-admin", "admin", actor.user_id])
    output, errors = io.StringIO(), io.StringIO()
    assert main(["--key-stdin"], stdin=io.StringIO(key), stdout=output, stderr=errors,
                environ={"SB_PG_DSN": pg.dsn(role="sb_app")}) == 0
    import json
    assert json.loads(output.getvalue())["summary"][0]["event_count"] == 1
    assert key not in output.getvalue() + errors.getvalue()
    with psycopg.connect(pg.dsn()) as admin:
        admin.execute("UPDATE api_keys SET revoked_at=now() WHERE key_hash=%s", [hash_key(key)])
    assert main(["--key-stdin"], stdin=io.StringIO(key), stdout=io.StringIO(), stderr=io.StringIO(),
                environ={"SB_PG_DSN": pg.dsn(role="sb_app")}) == 3
