"""Fail-closed, pre-connection checks for destructive PostgreSQL tests."""

from __future__ import annotations

from collections.abc import Mapping


def reject_external_postgres_settings(environ: Mapping[str, str]) -> None:
    # A generated URI must not inherit libpq service/target/session overrides.
    if "SB_PG_TEST_DSN" in environ or any(key.startswith("PG") for key in environ):
        raise RuntimeError(
            "External PostgreSQL settings are forbidden for destructive tests; "
            "unset SB_PG_TEST_DSN and PG* settings and use --run-postgres."
        )


def validate_owned_dsn(
    dsn: str, *, database: str, port: int, user: str, password: str
) -> None:
    """Parse without connecting; allow exactly the freshly owned endpoint.

    This is defense in depth, not ownership proof: the harness must verify its
    container label, image, mounts and published port before invoking this.
    Errors deliberately omit the DSN and credentials.
    """
    from psycopg.conninfo import conninfo_to_dict

    try:
        parsed = conninfo_to_dict(dsn)
    except Exception:
        raise RuntimeError("Invalid disposable PostgreSQL test DSN") from None
    expected = {
        "host": "127.0.0.1",
        "port": str(port),
        "dbname": database,
        "user": user,
        "password": password,
        "connect_timeout": "5",
    }
    if (
        not database.startswith("sb_test_")
        or len(database.removeprefix("sb_test_")) != 12
        or any(c not in "0123456789abcdef" for c in database.removeprefix("sb_test_"))
        or not 1024 <= port <= 65535
        or parsed != expected
    ):
        raise RuntimeError("PostgreSQL test DSN does not match the owned disposable target")
