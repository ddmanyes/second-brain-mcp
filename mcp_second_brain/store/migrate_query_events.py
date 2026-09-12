"""Apply the query telemetry schema and least-privilege RLS grants.

This migration is deliberately separate from application startup. Preview is
the default and has no database side effects. ``--apply`` reads a privileged
PostgreSQL DSN from a mode-0600 file and applies ``query_events_schema.sql`` in
one transaction. The ``sb_app`` role must already exist; it is provisioned by
the multi-user migration.

Usage:
    python -m mcp_second_brain.store.migrate_query_events --dsn-file <path> --apply
"""

from __future__ import annotations

import argparse
import stat
from pathlib import Path
from typing import Callable, Sequence

SCHEMA_PATH = Path(__file__).with_name("query_events_schema.sql")


def migration_preview() -> str:
    return (
        "Apply query_events_schema.sql (query_events/query_event_daily tables, "
        "RLS policies, and least-privilege sb_app grants)."
    )


def apply_query_event_schema(connection) -> None:
    """Apply the query-event schema atomically on a privileged connection."""
    if getattr(connection, "autocommit", False):
        raise ValueError("query event migration requires autocommit=False")
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        with connection.cursor() as cursor:
            cursor.execute(schema_sql)
        connection.commit()
    except Exception as exc:
        connection.rollback()
        raise RuntimeError(
            "query event schema/RLS migration failed; transaction rolled back"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply DDL")
    parser.add_argument(
        "--dsn-file",
        type=Path,
        help=(
            "mode-0600 file containing a privileged (non-sb_app) Postgres DSN; "
            "required with --apply"
        ),
    )
    return parser


def _read_dsn(dsn_file: Path) -> str:
    mode = stat.S_IMODE(dsn_file.stat().st_mode)
    if mode & 0o077:
        raise ValueError("insecure DSN file permissions")
    dsn = dsn_file.read_text(encoding="utf-8").strip()
    if not dsn:
        raise ValueError("empty DSN file")
    return dsn


def main(
    argv: Sequence[str] | None = None,
    *,
    connect_factory: Callable[[str], object] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    if not args.apply:
        print("DRY RUN: " + migration_preview())
        return 0
    if args.dsn_file is None:
        raise SystemExit("--dsn-file is required with --apply")
    connection = None
    failed = False
    try:
        dsn = _read_dsn(args.dsn_file)
        if connect_factory is None:
            import psycopg

            def connect_factory(d: str):
                return psycopg.connect(d, autocommit=False)

        connection = connect_factory(dsn)
        apply_query_event_schema(connection)
    except Exception:
        failed = True
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                failed = True
    if failed:
        raise SystemExit("query event schema/RLS migration failed") from None
    print("Query event schema + RLS applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
