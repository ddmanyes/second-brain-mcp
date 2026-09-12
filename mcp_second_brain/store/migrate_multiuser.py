"""Apply the SB_MULTIUSER=1 schema + RLS migration.

Lab-open plan (2026-09-12), Phase 2: "Migration 與 serving 分離". This is the
ONLY thing that should run postgres_schema.sql / postgres_rls_schema.sql when
SB_MULTIUSER=1 — postgres_store.py's PostgresStore._apply_schema() deliberately
skips both and only verifies they already ran (see its docstring), because
sb_app (the role the live multiuser server connects as) lacks the
CREATE EXTENSION / ALTER TABLE / CREATE POLICY / CREATE ROLE privileges this
performs.

Must connect as a privileged role (table owner or superuser) — never sb_app.
Mirrors Evo_PRISM_lab_access/scripts/migrate_lab_access.py's shape (dry-run
default, --dsn-file pointing at a mode-0600 file so the DSN/password never
appears on the command line or in shell history).

Usage:
    python -m mcp_second_brain.store.migrate_multiuser --dsn-file <path> --apply
"""

from __future__ import annotations

import argparse
import stat
from pathlib import Path
from typing import Callable, Sequence

SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
RLS_SCHEMA_PATH = Path(__file__).with_name("postgres_rls_schema.sql")


def migration_preview() -> str:
    return (
        "Apply postgres_schema.sql (owner_id/user_uuid/expires_at columns) + "
        "postgres_rls_schema.sql (sb_app role, RLS on notes/note_chunks/figures)."
    )


def apply_multiuser_schema(connection) -> None:
    if getattr(connection, "autocommit", False):
        raise ValueError("multiuser migration requires autocommit=False")
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    rls_sql = RLS_SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        with connection.cursor() as cursor:
            cursor.execute(schema_sql)
            cursor.execute(rls_sql)
        connection.commit()
    except Exception as exc:
        connection.rollback()
        raise RuntimeError(
            "multiuser schema/RLS migration failed; transaction rolled back"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply DDL")
    parser.add_argument(
        "--dsn-file",
        type=Path,
        help="mode-0600 file containing a privileged (non-sb_app) Postgres DSN; required with --apply",
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
    try:
        dsn = _read_dsn(args.dsn_file)
        if connect_factory is None:
            import psycopg

            def connect_factory(d: str):
                return psycopg.connect(d, autocommit=False)

        connection = connect_factory(dsn)
        apply_multiuser_schema(connection)
    except Exception:
        raise SystemExit("multiuser schema/RLS migration failed") from None
    finally:
        if connection is not None:
            connection.close()
    print("Multiuser schema + RLS applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
