# ruff: noqa: F811

import concurrent.futures
import io
import json
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from mcp_second_brain.identity import hash_key
from mcp_second_brain.query_event_maintenance import main, run_maintenance
from mcp_second_brain.query_events import Actor, ActorRole
from mcp_second_brain.store.query_event_repository import retention
from tests.test_query_event_postgres import event, repository  # noqa: F401

ADMIN_UUID = UUID("11111111-1111-1111-1111-111111111111")


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, key_row=None):
        self.key_row = key_row
        self.executed = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, params=None):
        self.executed.append((query, params))
        row = self.key_row if "FROM public.api_keys" in query else None
        return _Cursor(row)


class _Pool:
    def __init__(self, key_row=None):
        self.connection_object = _Connection(key_row)
        self.connection_timeouts = []
        self.opened = None
        self.closed = False

    @contextmanager
    def connection(self, *, timeout):
        self.connection_timeouts.append(timeout)
        yield self.connection_object

    def open(self, *, wait, timeout):
        self.opened = (wait, timeout)

    def close(self, *, timeout=None):
        self.closed = timeout


def _success_result(applied):
    return {
        "raw_eligible": 7,
        "daily_eligible": 2,
        "raw_deleted": 3 if applied else 0,
        "daily_deleted": 1 if applied else 0,
        "applied": applied,
    }


def test_preview_is_default_nonmutating_read_only_and_closes_pool():
    raw_key = "private-admin-key"
    dsn = "postgresql://private-user:private-password@private-host/private-db"
    pool = _Pool()
    pool_call = {}
    lookup_calls = []
    retention_calls = []

    def pool_factory(received_dsn, **kwargs):
        pool_call.update(dsn=received_dsn, kwargs=kwargs)
        return pool

    class Lookup:
        def __init__(self, received_pool):
            assert received_pool is pool

        def lookup(self, digest):
            lookup_calls.append(digest)
            return Actor(ADMIN_UUID, ActorRole.ADMIN)

    def retention_fn(connection, *, apply_changes, batch_size):
        retention_calls.append((connection, apply_changes, batch_size))
        return _success_result(apply_changes)

    stdout = io.StringIO()
    stderr = io.StringIO()
    result = main(
        ["--key-stdin"],
        stdin=io.StringIO(raw_key),
        stdout=stdout,
        stderr=stderr,
        environ={"SB_PG_MAINTENANCE_DSN": dsn, "SB_API_KEY": raw_key},
        pool_factory=pool_factory,
        lookup_factory=Lookup,
        retention_fn=retention_fn,
    )

    assert result == 0
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue()) == {
        "applied": False,
        "batch_size": 1000,
        "daily_deleted": 0,
        "daily_eligible": 2,
        "raw_deleted": 0,
        "raw_eligible": 7,
    }
    assert lookup_calls == [hash_key(raw_key)]
    assert retention_calls == [(pool.connection_object, False, 1000)]
    assert pool_call["dsn"] == dsn
    assert pool_call["kwargs"]["min_size"] == 0
    assert pool_call["kwargs"]["max_size"] == 1
    assert pool_call["kwargs"]["kwargs"] == {
        "autocommit": False,
        "connect_timeout": 3,
        "options": "-c default_transaction_read_only=on",
    }
    assert pool.opened == (True, 3.0)
    assert pool.connection_timeouts == [0.5]
    assert pool.closed == 3.0
    assert raw_key not in stdout.getvalue() + stderr.getvalue()
    assert dsn not in stdout.getvalue() + stderr.getvalue()


def test_apply_is_explicit_one_batch_writable_and_reauthenticates():
    raw_key = "private-admin-key"
    pool = _Pool()
    pool_call = {}
    lookup_calls = []
    retention_calls = []

    def pool_factory(_dsn, **kwargs):
        pool_call.update(kwargs)
        return pool

    class Lookup:
        def __init__(self, received_pool):
            assert received_pool is pool

        def lookup(self, digest):
            lookup_calls.append(digest)
            return Actor(ADMIN_UUID, ActorRole.ADMIN)

    def retention_fn(connection, *, apply_changes, batch_size):
        retention_calls.append((connection, apply_changes, batch_size))
        return _success_result(apply_changes)

    stdout = io.StringIO()
    assert (
        main(
            ["--key-stdin", "--apply", "--batch-size", "37"],
            stdin=io.StringIO(raw_key),
            stdout=stdout,
            stderr=io.StringIO(),
            environ={"SB_PG_MAINTENANCE_DSN": "maintenance-dsn"},
            pool_factory=pool_factory,
            lookup_factory=Lookup,
            retention_fn=retention_fn,
        )
        == 0
    )

    assert json.loads(stdout.getvalue())["applied"] is True
    assert json.loads(stdout.getvalue())["batch_size"] == 37
    assert lookup_calls == [hash_key(raw_key), hash_key(raw_key)]
    assert retention_calls == [(pool.connection_object, True, 37)]
    assert pool_call["kwargs"]["options"].endswith("read_only=off")
    assert pool.closed == 3.0


def test_missing_maintenance_dsn_never_falls_back_or_touches_pool():
    def forbidden_pool(*args, **kwargs):
        pytest.fail("missing maintenance DSN must fail before pool creation")

    stderr = io.StringIO()
    result = main(
        ["--key-stdin"],
        stdin=io.StringIO("private-admin-key"),
        stdout=io.StringIO(),
        stderr=stderr,
        environ={"SB_PG_DSN": "must-not-be-used"},
        pool_factory=forbidden_pool,
    )

    assert result == 2
    assert stderr.getvalue() == "error: SB_PG_MAINTENANCE_DSN is required\n"


@pytest.mark.parametrize(
    "key_row",
    [
        ("member", ADMIN_UUID, None, False),
        ("admin", ADMIN_UUID, None, True),
    ],
)
def test_nonadmin_or_expired_key_is_denied_and_never_runs_retention(key_row):
    pool = _Pool(key_row)

    def forbidden_retention(*args, **kwargs):
        pytest.fail("denied key must not run retention")

    stderr = io.StringIO()
    result = main(
        ["--key-stdin", "--apply"],
        stdin=io.StringIO("private-admin-key"),
        stdout=io.StringIO(),
        stderr=stderr,
        environ={"SB_PG_MAINTENANCE_DSN": "maintenance-dsn"},
        pool_factory=lambda *args, **kwargs: pool,
        retention_fn=forbidden_retention,
    )

    assert result == 3
    assert stderr.getvalue() == "error: admin authentication failed\n"
    assert pool.closed == 3.0


def test_apply_reauthentication_denial_prevents_batch():
    pool = _Pool()
    actors = iter(
        [
            Actor(ADMIN_UUID, ActorRole.ADMIN),
            Actor(ADMIN_UUID, ActorRole.MEMBER),
        ]
    )

    class Lookup:
        def __init__(self, _pool):
            pass

        def lookup(self, _digest):
            return next(actors)

    with pytest.raises(PermissionError, match="admin authentication failed"):
        run_maintenance(
            pool,
            raw_key="private-admin-key",
            apply_changes=True,
            lookup_factory=Lookup,
            retention_fn=lambda *args, **kwargs: pytest.fail(
                "revoked admin must not run retention"
            ),
        )
    assert pool.connection_timeouts == []


def test_invalid_stdin_and_batch_bounds_fail_without_pool(capsys):
    called = False

    def pool_factory(*args, **kwargs):
        nonlocal called
        called = True

    stderr = io.StringIO()
    assert (
        main(
            ["--key-stdin"],
            stdin=io.StringIO("x" * 513),
            stderr=stderr,
            environ={"SB_PG_MAINTENANCE_DSN": "maintenance-dsn"},
            pool_factory=pool_factory,
        )
        == 2
    )
    assert stderr.getvalue() == "error: key input invalid\n"
    with pytest.raises(SystemExit) as caught:
        main(
            ["--key-stdin", "--batch-size", "10001"],
            stdin=io.StringIO("unused"),
            environ={"SB_PG_MAINTENANCE_DSN": "maintenance-dsn"},
            pool_factory=pool_factory,
        )
    assert caught.value.code == 2
    assert "invalid arguments" in capsys.readouterr().err
    assert not called


def test_failures_and_unknown_arguments_never_echo_raw_secrets(capsys):
    raw_key = "private-admin-key"
    dsn = "postgresql://private-user:private-password@private-host/private-db"

    class FailingPool(_Pool):
        def open(self, *, wait, timeout):
            raise RuntimeError(f"{raw_key} {dsn}")

    pool = FailingPool()
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert (
        main(
            ["--key-stdin"],
            stdin=io.StringIO(raw_key),
            stdout=stdout,
            stderr=stderr,
            environ={"SB_PG_MAINTENANCE_DSN": dsn},
            pool_factory=lambda *args, **kwargs: pool,
        )
        == 1
    )
    assert stderr.getvalue() == "error: query event maintenance unavailable\n"
    assert pool.closed == 3.0

    argv_secret = "private-key-on-argv"
    with pytest.raises(SystemExit):
        main(
            ["--key", argv_secret, "--key-stdin"],
            stdin=io.StringIO("unused"),
            environ={"SB_PG_MAINTENANCE_DSN": "maintenance-dsn"},
        )
    captured = capsys.readouterr()
    combined = stdout.getvalue() + stderr.getvalue() + captured.out + captured.err
    assert raw_key not in combined
    assert dsn not in combined
    assert argv_secret not in combined


def test_result_is_fixed_shape_and_rejects_untrusted_values():
    pool = _Pool()

    class Lookup:
        def __init__(self, _pool):
            pass

        def lookup(self, _digest):
            return Actor(ADMIN_UUID, ActorRole.ADMIN)

    result = run_maintenance(
        pool,
        raw_key="private-admin-key",
        lookup_factory=Lookup,
        retention_fn=lambda *args, **kwargs: {
            **_success_result(False),
            "unexpected": "private-payload",
        },
    )
    assert "unexpected" not in result

    with pytest.raises(RuntimeError, match="invalid retention result"):
        run_maintenance(
            pool,
            raw_key="private-admin-key",
            lookup_factory=Lookup,
            retention_fn=lambda *args, **kwargs: {
                **_success_result(False),
                "raw_eligible": "private-payload",
            },
        )


def test_real_cli_preview_apply_idempotency_and_privileged_dsn(repository):
    repo, _, pg = repository
    raw_key = "synthetic-maintenance-admin-key"
    admin_id = uuid4()
    schema = Path(__file__).parents[1] / "mcp_second_brain/store/postgres_schema.sql"
    with psycopg.connect(pg.dsn()) as connection:
        connection.execute(schema.read_text(encoding="utf-8"))
        connection.execute("GRANT SELECT ON public.api_keys TO sb_app")
        connection.execute(
            "INSERT INTO public.api_keys "
            "(key_hash, user_id, role, user_uuid) VALUES (%s, %s, 'admin', %s)",
            (hash_key(raw_key), "maintenance-admin", admin_id),
        )

    actor = Actor(uuid4(), ActorRole.MEMBER)
    old = datetime.now(UTC) - timedelta(days=31)
    repo.write(event(actor, when=old))
    repo.write(event(actor, when=old + timedelta(seconds=1)))
    repo.write(event(actor))

    pool_calls = []

    def recording_pool_factory(dsn, **kwargs):
        pool_calls.append((dsn, kwargs))
        return ConnectionPool(dsn, **kwargs)

    maintenance_dsn = pg.dsn()
    application_dsn = pg.dsn(role="sb_app")

    preview_output = io.StringIO()
    preview_error = io.StringIO()
    assert (
        main(
            ["--key-stdin"],
            stdin=io.StringIO(raw_key),
            stdout=preview_output,
            stderr=preview_error,
            environ={"SB_PG_MAINTENANCE_DSN": maintenance_dsn},
            pool_factory=recording_pool_factory,
        )
        == 0
    )
    preview = json.loads(preview_output.getvalue())
    assert preview_error.getvalue() == ""
    assert preview == {
        "applied": False,
        "batch_size": 1000,
        "daily_deleted": 0,
        "daily_eligible": 0,
        "raw_deleted": 0,
        "raw_eligible": 2,
    }
    with psycopg.connect(maintenance_dsn) as connection:
        assert (
            connection.execute("SELECT count(*) FROM query_events").fetchone()[0] == 3
        )
        assert (
            connection.execute("SELECT count(*) FROM query_event_daily").fetchone()[0]
            == 0
        )

    refused_output = io.StringIO()
    refused_error = io.StringIO()
    assert (
        main(
            ["--key-stdin", "--apply"],
            stdin=io.StringIO(raw_key),
            stdout=refused_output,
            stderr=refused_error,
            environ={"SB_PG_MAINTENANCE_DSN": application_dsn},
            pool_factory=recording_pool_factory,
        )
        == 1
    )
    assert refused_output.getvalue() == ""
    assert refused_error.getvalue() == "error: query event maintenance unavailable\n"
    with psycopg.connect(maintenance_dsn) as connection:
        assert (
            connection.execute("SELECT count(*) FROM query_events").fetchone()[0] == 3
        )
        assert (
            connection.execute("SELECT count(*) FROM query_event_daily").fetchone()[0]
            == 0
        )

    applied_output = io.StringIO()
    assert (
        main(
            ["--key-stdin", "--apply"],
            stdin=io.StringIO(raw_key),
            stdout=applied_output,
            stderr=io.StringIO(),
            environ={"SB_PG_MAINTENANCE_DSN": maintenance_dsn},
            pool_factory=recording_pool_factory,
        )
        == 0
    )
    applied = json.loads(applied_output.getvalue())
    assert applied["raw_eligible"] == 2
    assert applied["raw_deleted"] == 2
    assert applied["daily_deleted"] == 0
    assert applied["applied"] is True
    with psycopg.connect(maintenance_dsn) as connection:
        assert (
            connection.execute("SELECT count(*) FROM query_events").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT sum(event_count) FROM query_event_daily"
            ).fetchone()[0]
            == 2
        )

    repeated_output = io.StringIO()
    assert (
        main(
            ["--key-stdin", "--apply"],
            stdin=io.StringIO(raw_key),
            stdout=repeated_output,
            stderr=io.StringIO(),
            environ={"SB_PG_MAINTENANCE_DSN": maintenance_dsn},
            pool_factory=recording_pool_factory,
        )
        == 0
    )
    repeated = json.loads(repeated_output.getvalue())
    assert repeated["raw_eligible"] == 0
    assert repeated["raw_deleted"] == 0
    with psycopg.connect(maintenance_dsn) as connection:
        assert (
            connection.execute(
                "SELECT sum(event_count) FROM query_event_daily"
            ).fetchone()[0]
            == 2
        )

    assert len(pool_calls) == 4
    assert pool_calls[0][0] == maintenance_dsn
    assert pool_calls[0][1]["kwargs"]["options"].endswith("read_only=on")
    assert pool_calls[1][0] == application_dsn
    assert all(
        call[1]["kwargs"]["options"].endswith("read_only=off")
        for call in pool_calls[1:]
    )
    combined = (
        preview_output.getvalue()
        + preview_error.getvalue()
        + refused_output.getvalue()
        + refused_error.getvalue()
        + applied_output.getvalue()
        + repeated_output.getvalue()
    )
    assert raw_key not in combined
    assert maintenance_dsn not in combined
    assert application_dsn not in combined


def test_real_apply_uses_exact_dedicated_maintenance_table_grants(repository):
    repo, _, pg = repository
    raw_key = "synthetic-least-privilege-maintenance-key"
    admin_id = uuid4()
    role = "sb_test_telemetry_maintenance"
    password = f"synthetic-{uuid4().hex}"
    schema = Path(__file__).parents[1] / "mcp_second_brain/store/postgres_schema.sql"

    with psycopg.connect(pg.dsn()) as admin:
        admin.execute(schema.read_text(encoding="utf-8"))
        admin.execute("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS user_uuid uuid")
        admin.execute(
            "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at timestamptz"
        )
        admin.execute(
            "INSERT INTO public.api_keys "
            "(key_hash, user_id, role, user_uuid) VALUES (%s, %s, 'admin', %s)",
            (hash_key(raw_key), "least-privilege-maintenance", admin_id),
        )
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER BYPASSRLS PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role))
        )
        admin.execute(
            sql.SQL("GRANT SELECT ON public.api_keys TO {}").format(
                sql.Identifier(role)
            )
        )
        admin.execute(
            sql.SQL("GRANT SELECT, DELETE ON public.query_events TO {}").format(
                sql.Identifier(role)
            )
        )
        admin.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON public.query_event_daily TO {}"
            ).format(sql.Identifier(role))
        )

    actor = Actor(uuid4(), ActorRole.MEMBER)
    repo.write(event(actor, when=datetime.now(UTC) - timedelta(days=31)))
    settings = conninfo_to_dict(pg.dsn())
    settings.update(user=role, password=password)
    maintenance_dsn = make_conninfo(**settings)

    output = io.StringIO()
    error = io.StringIO()
    result = main(
        ["--key-stdin", "--apply"],
        stdin=io.StringIO(raw_key),
        stdout=output,
        stderr=error,
        environ={"SB_PG_MAINTENANCE_DSN": maintenance_dsn},
    )

    assert result == 0
    assert error.getvalue() == ""
    assert json.loads(output.getvalue()) == {
        "applied": True,
        "batch_size": 1000,
        "daily_deleted": 0,
        "daily_eligible": 0,
        "raw_deleted": 1,
        "raw_eligible": 1,
    }
    with psycopg.connect(maintenance_dsn) as maintenance:
        assert maintenance.execute(
            "SELECT has_table_privilege(current_user, "
            "'public.query_events', 'SELECT,DELETE')"
        ).fetchone()[0]
        assert not maintenance.execute(
            "SELECT has_table_privilege(current_user, 'public.query_events', 'UPDATE')"
        ).fetchone()[0]
        assert maintenance.execute(
            "SELECT has_table_privilege(current_user, "
            "'public.query_event_daily', 'SELECT,INSERT,UPDATE,DELETE')"
        ).fetchone()[0]
        assert not maintenance.execute(
            "SELECT has_table_privilege(current_user, 'public.notes', 'SELECT')"
        ).fetchone()[0]

    repo.write(event(actor, when=datetime.now(UTC) - timedelta(days=31)))
    with psycopg.connect(pg.dsn()) as admin:
        admin.execute(
            "CREATE FUNCTION public.test_slow_query_event_delete() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN PERFORM pg_sleep(0.5); RETURN OLD; END $$"
        )
        admin.execute(
            "CREATE TRIGGER test_slow_query_event_delete "
            "BEFORE DELETE ON public.query_events FOR EACH ROW "
            "EXECUTE FUNCTION public.test_slow_query_event_delete()"
        )

    barrier = threading.Barrier(2)

    def concurrent_apply():
        with psycopg.connect(maintenance_dsn) as maintenance:
            barrier.wait(timeout=2)
            try:
                return retention(
                    maintenance,
                    now=datetime.now(UTC),
                    apply_changes=True,
                )
            except psycopg.errors.LockNotAvailable:
                return "lock_timeout"

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: concurrent_apply(), range(2)))
    assert time.monotonic() - started < 3
    successful = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    assert sum(outcome["raw_deleted"] for outcome in successful) == 1
    assert all(
        outcome == "lock_timeout"
        or (isinstance(outcome, dict) and outcome["raw_deleted"] in {0, 1})
        for outcome in outcomes
    )
    with psycopg.connect(pg.dsn()) as admin:
        assert admin.execute("SELECT count(*) FROM query_events").fetchone()[0] == 0
        assert (
            admin.execute("SELECT sum(event_count) FROM query_event_daily").fetchone()[
                0
            ]
            == 2
        )
    assert raw_key not in output.getvalue() + error.getvalue()
    assert maintenance_dsn not in output.getvalue() + error.getvalue()
