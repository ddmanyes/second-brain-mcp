import io
import json
import logging
from contextlib import contextmanager
from decimal import Decimal
from uuid import UUID

import pytest

from mcp_second_brain.identity import hash_key
from mcp_second_brain.query_event_report import (
    AdminKeyLookup,
    _read_key,
    generate_report,
    main,
)
from mcp_second_brain.query_events import Actor, ActorRole

ADMIN_UUID = UUID("11111111-1111-1111-1111-111111111111")


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self.row = row
        self.executed = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return _Cursor(self.row if "FROM public.api_keys" in query else None)


class _Pool:
    def __init__(self, row=None):
        self.connection_object = _Connection(row)
        self.connection_timeouts = []
        self.opened = False
        self.closed = False

    @contextmanager
    def connection(self, *, timeout):
        self.connection_timeouts.append(timeout)
        yield self.connection_object

    def open(self, *, wait, timeout):
        self.opened = (wait, timeout)

    def close(self, *, timeout=None):
        self.closed = True


class _Repository:
    instances = []

    def __init__(self, pool, *, actor_getter, **kwargs):
        self.pool = pool
        self.actor = actor_getter()
        self.kwargs = kwargs
        self.verified = False
        self.summary_args = None
        self.__class__.instances.append(self)

    def verify_schema(self):
        self.verified = True

    def summary(self, *, days, limit):
        self.summary_args = (days, limit)
        return [{"event_count": 2, "duration_avg_ms": Decimal("1.25")}]


def test_admin_database_key_allows_aggregate_report_without_secret_leak():
    raw_key = "private-admin-key"
    pool = _Pool(("admin", ADMIN_UUID, None, False))
    _Repository.instances.clear()

    report = generate_report(
        pool,
        raw_key=raw_key,
        days=7,
        limit=12,
        repository_factory=_Repository,
    )

    repository = _Repository.instances[-1]
    assert repository.actor == Actor(ADMIN_UUID, ActorRole.ADMIN)
    assert repository.verified
    assert repository.summary_args == (7, 12)
    assert report["summary"][0]["event_count"] == 2
    lookup_query, lookup_params = pool.connection_object.executed[-1]
    assert "public.api_keys" in lookup_query
    assert lookup_params == (hash_key(raw_key),)
    assert raw_key not in str(pool.connection_object.executed)
    assert all(
        query.lstrip().startswith("SELECT")
        for query, _ in pool.connection_object.executed
    )


@pytest.mark.parametrize(
    "row",
    [
        ("member", ADMIN_UUID, None, False),
        ("admin", ADMIN_UUID, object(), False),
        ("admin", ADMIN_UUID, None, True),
        ("admin", None, None, False),
        None,
    ],
)
def test_non_admin_revoked_expired_or_uuidless_key_is_denied(row):
    with pytest.raises(PermissionError, match="admin authentication failed"):
        AdminKeyLookup(_Pool(row)).lookup(hash_key("private-key"))


@pytest.mark.parametrize(
    "days,limit",
    [(0, 1), (181, 1), (True, 1), (1, 0), (1, 501), (1, True)],
)
def test_report_parameter_bounds_fail_before_identity_lookup(days, limit):
    class ForbiddenLookup:
        def __init__(self, pool):
            pytest.fail("invalid parameters must fail before key lookup")

    with pytest.raises(ValueError):
        generate_report(
            object(),
            raw_key="private-key",
            days=days,
            limit=limit,
            lookup_factory=ForbiddenLookup,
        )


def test_stdin_key_read_is_bounded_and_rejects_overflow_or_multiple_lines():
    class RecordingInput(io.StringIO):
        requested = []

        def read(self, size=-1):
            self.requested.append(size)
            return super().read(size)

    stream = RecordingInput("private-key\n")
    assert _read_key(stream) == "private-key"
    assert stream.requested == [512, 1]
    assert _read_key(io.StringIO("x" * 512)) == "x" * 512
    with pytest.raises(ValueError):
        _read_key(io.StringIO("x" * 513))
    with pytest.raises(ValueError):
        _read_key(io.StringIO("one\ntwo"))


def test_cli_uses_only_dsn_env_read_only_single_connection_pool_and_json():
    raw_key = "private-admin-key"
    dsn = "postgresql://private-user:private-password@private-host/private-db"
    pool = _Pool()
    pool_call = {}

    def pool_factory(received_dsn, **kwargs):
        pool_call.update(dsn=received_dsn, kwargs=kwargs)
        return pool

    class Lookup:
        def __init__(self, received_pool):
            assert received_pool is pool

        def lookup(self, digest):
            assert digest == hash_key(raw_key)
            return Actor(ADMIN_UUID, ActorRole.ADMIN)

    stdout = io.StringIO()
    stderr = io.StringIO()
    _Repository.instances.clear()
    result = main(
        ["--key-stdin", "--days", "7", "--limit", "2"],
        stdin=io.StringIO(raw_key),
        stdout=stdout,
        stderr=stderr,
        environ={"SB_PG_DSN": dsn, "SB_API_KEY": "must-not-be-read"},
        pool_factory=pool_factory,
        lookup_factory=Lookup,
        repository_factory=_Repository,
    )

    assert result == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload == {
        "days": 7,
        "limit": 2,
        "summary": [{"duration_avg_ms": 1.25, "event_count": 2}],
    }
    assert pool_call["dsn"] == dsn
    assert pool_call["kwargs"]["min_size"] == 0
    assert pool_call["kwargs"]["max_size"] == 1
    assert "default_transaction_read_only=on" in pool_call["kwargs"]["kwargs"]["options"]
    assert pool.opened == (True, 3.0)
    assert pool.closed
    assert raw_key not in stdout.getvalue() + stderr.getvalue()
    assert dsn not in stdout.getvalue() + stderr.getvalue()


def test_cli_runtime_error_is_fixed_and_does_not_expose_key_or_dsn():
    raw_key = "private-admin-key"
    dsn = "postgresql://private-user:private-password@private-host/private-db"
    stderr = io.StringIO()

    class FailingPool(_Pool):
        def open(self, *, wait, timeout):
            raise RuntimeError(f"{raw_key} {dsn}")

    result = main(
        ["--key-stdin"],
        stdin=io.StringIO(raw_key),
        stdout=io.StringIO(),
        stderr=stderr,
        environ={"SB_PG_DSN": dsn},
        pool_factory=lambda *args, **kwargs: FailingPool(),
    )

    assert result == 1
    assert stderr.getvalue() == "error: query event report unavailable\n"
    assert raw_key not in stderr.getvalue()
    assert dsn not in stderr.getvalue()


def test_pool_background_logs_are_suppressed_only_during_cli_lifecycle():
    raw_key = "private-admin-key"
    dsn = "postgresql://private-user:private-password@private-host/private-db"
    log_output = io.StringIO()
    handler = logging.StreamHandler(log_output)
    loggers = [
        logging.getLogger("psycopg.pool"),
        logging.getLogger("psycopg_pool.sched"),
    ]
    original = [(logger.level, logger.propagate, tuple(logger.filters)) for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        logger.addHandler(handler)

    class LoggingPool(_Pool):
        def open(self, *, wait, timeout):
            logging.getLogger("psycopg.pool").warning(
                "background connect failed: %s %s", raw_key, dsn
            )
            logging.getLogger("psycopg_pool.sched").warning(
                "scheduler failed: %s", dsn
            )
            raise RuntimeError("unavailable")

        def close(self, *, timeout=None):
            logging.getLogger("psycopg.pool").warning(
                "background close failed: %s", raw_key
            )

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        result = main(
            ["--key-stdin"],
            stdin=io.StringIO(raw_key),
            stdout=stdout,
            stderr=stderr,
            environ={"SB_PG_DSN": dsn},
            pool_factory=lambda *args, **kwargs: LoggingPool(),
        )
        for logger in loggers:
            logger.warning("visible after CLI")
    finally:
        for logger, (level, propagate, filters) in zip(loggers, original, strict=True):
            logger.removeHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
            assert tuple(logger.filters) == filters

    combined = stdout.getvalue() + stderr.getvalue() + log_output.getvalue()
    assert result == 1
    assert stderr.getvalue() == "error: query event report unavailable\n"
    assert log_output.getvalue().count("visible after CLI") == 2
    assert raw_key not in combined
    assert dsn not in combined


def test_cli_requires_dsn_and_stdin_flag_without_opening_pool(capsys):
    called = False

    def pool_factory(*args, **kwargs):
        nonlocal called
        called = True

    stderr = io.StringIO()
    assert main(
        ["--key-stdin"],
        stdin=io.StringIO("private-key"),
        stderr=stderr,
        environ={},
        pool_factory=pool_factory,
    ) == 2
    assert stderr.getvalue() == "error: SB_PG_DSN is required\n"
    with pytest.raises(SystemExit) as caught:
        main(
            [],
            stdin=io.StringIO("private-key"),
            environ={"SB_PG_DSN": "private-dsn"},
            pool_factory=pool_factory,
        )
    assert caught.value.code == 2
    assert "invalid arguments" in capsys.readouterr().err
    assert not called


def test_cli_unknown_key_argument_never_echoes_secret(capsys):
    secret = "private-key-on-argv"
    with pytest.raises(SystemExit):
        main(
            ["--key", secret, "--key-stdin"],
            stdin=io.StringIO("unused"),
            environ={"SB_PG_DSN": "private-dsn"},
        )
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
