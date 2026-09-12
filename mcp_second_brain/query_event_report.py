"""Read-only, admin-authenticated JSON report for sanitized query events."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from psycopg_pool import ConnectionPool

from .identity import hash_key
from .query_events import Actor, ActorRole
from .store.query_event_repository import QueryEventRepository

_KEY_INPUT_LIMIT = 512
_CONNECTION_TIMEOUT = 3.0
_POOL_TIMEOUT = 0.5
_STATEMENT_TIMEOUT_MS = 500
_LOCK_TIMEOUT_MS = 100


class AdminAuthenticationError(PermissionError):
    pass


class _DropPoolLog(logging.Filter):
    def filter(self, record):
        del record
        return False


@contextmanager
def _suppress_pool_logs():
    """Temporarily drop dependency logs that may contain connection details.

    Installed psycopg_pool emits background connection errors through
    ``psycopg.pool`` and scheduler callback errors through
    ``psycopg_pool.sched``. The filter exists only for this CLI's pool
    lifecycle and is removed after its bounded close.
    """
    pool_filter = _DropPoolLog()
    loggers = tuple(
        logging.getLogger(name) for name in ("psycopg.pool", "psycopg_pool.sched")
    )
    for logger in loggers:
        logger.addFilter(pool_filter)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(pool_filter)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Avoid reflecting unknown arguments, which may contain a pasted secret."""

    def error(self, message):
        del message
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


def _bounded_int(minimum, maximum):
    def parse(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError("invalid integer") from None
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError("integer outside allowed range")
        return parsed

    return parse


def _parser():
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--days", type=_bounded_int(1, 180), default=30)
    parser.add_argument("--limit", type=_bounded_int(1, 500), default=100)
    parser.add_argument(
        "--key-stdin",
        action="store_true",
        required=True,
        help="read the admin API key from standard input",
    )
    return parser


def _read_key(stream) -> str:
    """Read a 512-character maximum plus one separately read overflow sentinel."""
    supplied = stream.read(_KEY_INPUT_LIMIT)
    overflow = stream.read(1)
    if not isinstance(supplied, str) or not supplied or overflow:
        raise ValueError("invalid key input")
    if supplied.endswith("\n"):
        supplied = supplied[:-1]
        if supplied.endswith("\r"):
            supplied = supplied[:-1]
    if not supplied or "\n" in supplied or "\r" in supplied:
        raise ValueError("invalid key input")
    return supplied


class AdminKeyLookup:
    """Resolve one database key hash without any environment-key fallback."""

    def __init__(self, pool, *, pool_timeout=_POOL_TIMEOUT):
        self._pool = pool
        self._pool_timeout = pool_timeout

    def lookup(self, key_digest: str) -> Actor:
        with self._pool.connection(timeout=self._pool_timeout) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true), "
                    "set_config('lock_timeout', %s, true)",
                    (str(_STATEMENT_TIMEOUT_MS), str(_LOCK_TIMEOUT_MS)),
                )
                row = connection.execute(
                    "SELECT role, user_uuid, revoked_at, "
                    "(expires_at IS NOT NULL AND expires_at <= now()) AS expired "
                    "FROM public.api_keys WHERE key_hash = %s",
                    (key_digest,),
                ).fetchone()
        if row is None or row[0] != "admin" or row[1] is None or row[2] is not None or row[3]:
            raise AdminAuthenticationError("admin authentication failed")
        try:
            actor_id = UUID(str(row[1]))
        except (TypeError, ValueError, AttributeError):
            raise AdminAuthenticationError("admin authentication failed") from None
        return Actor(actor_id, ActorRole.ADMIN)


def generate_report(
    pool,
    *,
    raw_key: str,
    days: int = 30,
    limit: int = 100,
    lookup_factory=AdminKeyLookup,
    repository_factory=QueryEventRepository,
) -> dict:
    """Authenticate one admin and return a sanitized, aggregate-only report."""
    if type(days) is not int or not 1 <= days <= 180:
        raise ValueError("days must be between 1 and 180")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if not isinstance(raw_key, str) or not raw_key:
        raise AdminAuthenticationError("admin authentication failed")
    actor = lookup_factory(pool).lookup(hash_key(raw_key))
    repository = repository_factory(
        pool,
        actor_getter=lambda: actor,
        pool_timeout=_POOL_TIMEOUT,
        statement_timeout_ms=_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms=_LOCK_TIMEOUT_MS,
    )
    repository.verify_schema()
    return {
        "days": days,
        "limit": limit,
        "summary": repository.summary(days=days, limit=limit),
    }


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("unsupported report value")


def main(
    argv=None,
    *,
    stdin=None,
    stdout=None,
    stderr=None,
    environ=None,
    pool_factory=ConnectionPool,
    lookup_factory=AdminKeyLookup,
    repository_factory=QueryEventRepository,
) -> int:
    """CLI entrypoint with injectable I/O and database adapters for offline tests."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    environ = os.environ if environ is None else environ
    args = _parser().parse_args(argv)
    dsn = environ.get("SB_PG_DSN", "")
    if not dsn:
        print("error: SB_PG_DSN is required", file=stderr)
        return 2
    try:
        raw_key = _read_key(stdin)
    except (OSError, ValueError):
        print("error: key input invalid", file=stderr)
        return 2

    with _suppress_pool_logs():
        pool = None
        try:
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
                    "options": "-c default_transaction_read_only=on",
                },
            )
            pool.open(wait=True, timeout=_CONNECTION_TIMEOUT)
            report = generate_report(
                pool,
                raw_key=raw_key,
                days=args.days,
                limit=args.limit,
                lookup_factory=lookup_factory,
                repository_factory=repository_factory,
            )
            rendered = json.dumps(report, default=_json_default, sort_keys=True)
        except AdminAuthenticationError:
            print("error: admin authentication failed", file=stderr)
            return 3
        except Exception:  # noqa: BLE001 -- fixed, secret-free CLI error
            print("error: query event report unavailable", file=stderr)
            return 1
        finally:
            raw_key = None
            if pool is not None:
                try:
                    pool.close(timeout=_CONNECTION_TIMEOUT)
                except Exception:  # noqa: BLE001 -- fixed CLI error contract
                    pass

    try:
        print(rendered, file=stdout)
        return 0
    except OSError:
        print("error: query event report unavailable", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
