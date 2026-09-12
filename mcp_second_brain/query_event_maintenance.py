"""Explicit administrator preview/apply for query-event retention."""

from __future__ import annotations

import json
import os
import sys

from psycopg_pool import ConnectionPool

from .identity import hash_key
from .query_event_report import (
    AdminAuthenticationError,
    AdminKeyLookup,
    _bounded_int,
    _read_key,
    _SafeArgumentParser,
    _suppress_pool_logs,
)
from .query_events import Actor, ActorRole
from .store.query_event_repository import retention

_CONNECTION_TIMEOUT = 3.0
_POOL_TIMEOUT = 0.5


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-stdin",
        action="store_true",
        required=True,
        help="read the admin API key from standard input",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply one bounded retention batch",
    )
    parser.add_argument(
        "--batch-size",
        type=_bounded_int(1, 10000),
        default=1000,
    )
    return parser


def _require_admin(actor: Actor) -> Actor:
    if (
        type(actor) is not Actor
        or actor.role != ActorRole.ADMIN
        or actor.user_id is None
    ):
        raise AdminAuthenticationError("admin authentication failed")
    return actor


def run_maintenance(
    pool,
    *,
    raw_key: str,
    apply_changes: bool = False,
    batch_size: int = 1000,
    lookup_factory=AdminKeyLookup,
    retention_fn=retention,
) -> dict:
    """Authenticate an administrator and preview or apply one retention batch."""
    if type(apply_changes) is not bool:
        raise ValueError("apply_changes must be a boolean")
    if type(batch_size) is not int or not 1 <= batch_size <= 10000:
        raise ValueError("batch_size must be between 1 and 10000")
    if not isinstance(raw_key, str) or not raw_key:
        raise AdminAuthenticationError("admin authentication failed")

    digest = hash_key(raw_key)
    lookup = lookup_factory(pool)
    _require_admin(lookup.lookup(digest))
    if apply_changes:
        # A key can be revoked while the CLI is preparing the writable batch.
        # Revalidate immediately before checking out its maintenance connection.
        _require_admin(lookup.lookup(digest))

    with pool.connection(timeout=_POOL_TIMEOUT) as connection:
        result = retention_fn(
            connection,
            apply_changes=apply_changes,
            batch_size=batch_size,
        )
    if not isinstance(result, dict):
        raise TypeError("invalid retention result")

    counts = {}
    for name in ("raw_eligible", "daily_eligible", "raw_deleted", "daily_deleted"):
        value = result.get(name)
        if type(value) is not int or value < 0:
            raise RuntimeError("invalid retention result")
        counts[name] = value
    if result.get("applied") is not apply_changes:
        raise RuntimeError("invalid retention result")
    return {
        "applied": apply_changes,
        "batch_size": batch_size,
        **counts,
    }


def main(
    argv=None,
    *,
    stdin=None,
    stdout=None,
    stderr=None,
    environ=None,
    pool_factory=ConnectionPool,
    lookup_factory=AdminKeyLookup,
    retention_fn=retention,
) -> int:
    """CLI entrypoint with injectable I/O and database adapters for tests."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    environ = os.environ if environ is None else environ
    args = _parser().parse_args(argv)

    dsn = environ.get("SB_PG_MAINTENANCE_DSN", "")
    if not dsn:
        print("error: SB_PG_MAINTENANCE_DSN is required", file=stderr)
        return 2
    try:
        raw_key = _read_key(stdin)
    except (OSError, ValueError):
        print("error: key input invalid", file=stderr)
        return 2

    with _suppress_pool_logs():
        pool = None
        try:
            read_only = "off" if args.apply else "on"
            pool = pool_factory(
                dsn,
                min_size=0,
                max_size=1,
                open=False,
                timeout=_CONNECTION_TIMEOUT,
                reconnect_timeout=_CONNECTION_TIMEOUT,
                kwargs={
                    "autocommit": False,
                    "connect_timeout": int(_CONNECTION_TIMEOUT),
                    "options": f"-c default_transaction_read_only={read_only}",
                },
            )
            pool.open(wait=True, timeout=_CONNECTION_TIMEOUT)
            report = run_maintenance(
                pool,
                raw_key=raw_key,
                apply_changes=args.apply,
                batch_size=args.batch_size,
                lookup_factory=lookup_factory,
                retention_fn=retention_fn,
            )
            rendered = json.dumps(report, sort_keys=True)
        except AdminAuthenticationError:
            print("error: admin authentication failed", file=stderr)
            return 3
        except Exception:  # noqa: BLE001 -- fixed, secret-free CLI error
            print("error: query event maintenance unavailable", file=stderr)
            return 1
        finally:
            raw_key = None
            if pool is not None:
                try:
                    pool.close(timeout=_CONNECTION_TIMEOUT)
                except Exception:  # noqa: BLE001 -- fixed CLI error contract
                    pool = None

    try:
        print(rendered, file=stdout)
        return 0
    except OSError:
        print("error: query event maintenance unavailable", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
