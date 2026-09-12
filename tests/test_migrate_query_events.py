"""Query-event migration CLI tests; PostgreSQL coverage is disposable only."""

from __future__ import annotations

import os

import psycopg
import pytest

from mcp_second_brain.store import migrate_query_events as migration


def test_preview_does_not_read_dsn_or_connect(tmp_path, capsys):
    missing = tmp_path / "dsn-that-must-not-be-read"

    def forbidden_connect(_dsn):
        pytest.fail("preview connected to PostgreSQL")

    assert (
        migration.main(["--dsn-file", str(missing)], connect_factory=forbidden_connect)
        == 0
    )
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == "DRY RUN: " + migration.migration_preview() + "\n"


def test_apply_requires_private_dsn_file(tmp_path):
    dsn_file = tmp_path / "admin.dsn"
    dsn_file.write_text("postgresql://admin:private-dsn-secret@localhost/db")
    dsn_file.chmod(0o640)

    with pytest.raises(SystemExit, match="query event schema/RLS migration failed"):
        migration.main(
            ["--dsn-file", str(dsn_file), "--apply"],
            connect_factory=lambda _dsn: pytest.fail("insecure file was accepted"),
        )


def test_apply_failure_does_not_disclose_dsn_or_key(tmp_path, capsys):
    secret = "private-dsn-and-key-secret"
    dsn_file = tmp_path / "admin.dsn"
    dsn_file.write_text(
        f"postgresql://admin:{secret}@localhost/db?application_name={secret}"
    )
    dsn_file.chmod(0o600)

    def fail_with_dsn(dsn):
        raise RuntimeError(f"connection failed for {dsn}")

    with pytest.raises(SystemExit) as error:
        migration.main(
            ["--dsn-file", str(dsn_file), "--apply"],
            connect_factory=fail_with_dsn,
        )
    captured = capsys.readouterr()
    visible = str(error.value) + captured.out + captured.err
    assert str(error.value) == "query event schema/RLS migration failed"
    assert secret not in visible


def test_close_failure_does_not_disclose_dsn(tmp_path, capsys, monkeypatch):
    secret = "private-close-secret"
    dsn_file = tmp_path / "admin.dsn"
    dsn_file.write_text(f"postgresql://admin:{secret}@localhost/db")
    dsn_file.chmod(0o600)

    class Connection:
        autocommit = False

        def close(self):
            raise RuntimeError(f"close failed for {secret}")

    monkeypatch.setattr(migration, "apply_query_event_schema", lambda _connection: None)
    with pytest.raises(SystemExit) as error:
        migration.main(
            ["--dsn-file", str(dsn_file), "--apply"],
            connect_factory=lambda _dsn: Connection(),
        )
    captured = capsys.readouterr()
    visible = str(error.value) + captured.out + captured.err
    assert str(error.value) == "query event schema/RLS migration failed"
    assert secret not in visible


def test_apply_rejects_autocommit_before_executing_schema():
    class AutocommitConnection:
        autocommit = True

        def cursor(self):
            pytest.fail("autocommit connection executed schema")

    with pytest.raises(ValueError, match="autocommit=False"):
        migration.apply_query_event_schema(AutocommitConnection())


def test_preview_apply_and_repeat_are_safe_on_owned_postgres(
    multiuser_postgres, tmp_path, capsys
):
    pg = multiuser_postgres
    pg.reset()
    with psycopg.connect(pg.dsn(role="postgres")) as admin:
        admin.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sb_app') "
            "THEN CREATE ROLE sb_app LOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$"
        )
        admin.execute("ALTER ROLE sb_app NOSUPERUSER NOBYPASSRLS")

    dsn_file = tmp_path / "owned-admin.dsn"
    dsn_file.write_text(pg.dsn(role="postgres"), encoding="utf-8")
    dsn_file.chmod(0o600)

    assert migration.main(["--dsn-file", str(dsn_file)]) == 0
    with psycopg.connect(pg.dsn(role="postgres")) as admin:
        assert (
            admin.execute("SELECT to_regclass('public.query_events')").fetchone()[0]
            is None
        )
        assert (
            admin.execute("SELECT to_regclass('public.query_event_daily')").fetchone()[
                0
            ]
            is None
        )

    assert migration.main(["--dsn-file", str(dsn_file), "--apply"]) == 0
    assert migration.main(["--dsn-file", str(dsn_file), "--apply"]) == 0

    with psycopg.connect(pg.dsn(role="postgres")) as admin:
        tables = admin.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class JOIN pg_namespace ON pg_namespace.oid=relnamespace "
            "WHERE nspname='public' AND relname IN "
            "('query_events', 'query_event_daily') ORDER BY relname"
        ).fetchall()
        assert tables == [
            ("query_event_daily", True, True),
            ("query_events", True, True),
        ]
        grants = admin.execute(
            "SELECT grantee, table_name, privilege_type "
            "FROM information_schema.table_privileges "
            "WHERE table_schema='public' "
            "AND table_name IN ('query_events', 'query_event_daily') "
            "AND grantee IN ('sb_app', 'PUBLIC') "
            "ORDER BY grantee, table_name, privilege_type"
        ).fetchall()
        assert grants == [
            ("sb_app", "query_event_daily", "SELECT"),
            ("sb_app", "query_events", "INSERT"),
            ("sb_app", "query_events", "SELECT"),
        ]
        role = admin.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='sb_app'"
        ).fetchone()
        assert role == (False, False)
        policies = admin.execute(
            "SELECT tablename, policyname, cmd, roles "
            "FROM pg_policies WHERE schemaname='public' "
            "AND tablename IN ('query_events', 'query_event_daily') "
            "ORDER BY tablename, policyname"
        ).fetchall()
        assert policies == [
            ("query_event_daily", "query_event_daily_admin", "SELECT", ["sb_app"]),
            ("query_events", "query_events_insert", "INSERT", ["sb_app"]),
            ("query_events", "query_events_read", "SELECT", ["sb_app"]),
        ]

    output = capsys.readouterr()
    assert "postgresql://" not in output.out + output.err
    assert pg.postgres_password not in output.out + output.err
    assert os.fspath(dsn_file) not in output.out + output.err
