"""PostgreSQL persistence for sanitized query events; explicit migration only.

The supplied pool must enforce connection establishment timeouts. Each checkout,
statement and lock acquisition here has its own bounded deadline. The background
writer sets transaction-local identity from the captured event, never thread-local
request state. Read APIs exclusively use the injected trusted identity getter.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp_second_brain.query_events import Actor, ActorRole, QueryEvent

_SCHEMA = Path(__file__).with_name("query_events_schema.sql")
_COLUMNS = (
    "event_id",
    "request_id",
    "actor_id",
    "actor_role",
    "service",
    "tool",
    "operation",
    "started_at",
    "duration_ms",
    "status",
    "error_code",
    "result_count",
    "revision",
    "retrieval_revision",
)


def apply(connection) -> None:
    """Explicit privileged migration; caller supplies the target connection.

    No role creation, passwords, environment DSN lookup, or production auto-apply.
    The caller owns backup/review and commits its outer transaction if any.
    """
    with connection.transaction():
        connection.execute("SET LOCAL statement_timeout = '10s'")
        connection.execute("SET LOCAL lock_timeout = '1s'")
        connection.execute(_SCHEMA.read_text(encoding="utf-8"))


class QueryEventRepository:
    def __init__(
        self,
        pool,
        *,
        actor_getter: Callable[[], Actor],
        pool_timeout: float = 0.5,
        statement_timeout_ms: int = 500,
        lock_timeout_ms: int = 100,
    ):
        if not math.isfinite(pool_timeout) or not 0 < pool_timeout <= 5:
            raise ValueError("pool_timeout must be positive and at most 5 seconds")
        if (
            type(statement_timeout_ms) is not int
            or not 1 <= statement_timeout_ms <= 10000
        ):
            raise ValueError("statement timeout must be between 1 and 10000 ms")
        if (
            type(lock_timeout_ms) is not int
            or not 1 <= lock_timeout_ms <= statement_timeout_ms
        ):
            raise ValueError(
                "lock timeout must be positive and within statement timeout"
            )
        self._pool = pool
        self._actor_getter = actor_getter
        self._pool_timeout = pool_timeout
        self._statement_timeout = statement_timeout_ms
        self._lock_timeout = lock_timeout_ms

    @contextmanager
    def _connection(self, actor: Actor):
        if type(actor) is not Actor:
            raise TypeError("trusted actor must be Actor")
        with self._pool.connection(timeout=self._pool_timeout) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true), "
                    "set_config('lock_timeout', %s, true), "
                    "set_config('app.query_event_actor', %s, true), "
                    "set_config('app.query_event_role', %s, true)",
                    (
                        str(self._statement_timeout),
                        str(self._lock_timeout),
                        str(actor.user_id) if actor.user_id is not None else "",
                        actor.role.value,
                    ),
                )
                yield connection

    def verify_schema(self) -> None:
        """Read-only fail-closed startup check; no migration or role changes."""
        with self._connection(Actor()) as connection:
            row = connection.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            if row is None or row[0] or row[1]:
                raise RuntimeError("query events require a role subject to RLS")
            rows = connection.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname IN ('query_events', 'query_event_daily')"
            ).fetchall()
            if len(rows) != 2 or not all(row[1] and row[2] for row in rows):
                raise RuntimeError("query event schema/RLS migration is missing")
            cols = connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'query_events'"
            ).fetchall()
            if {row[0] for row in cols} != set(_COLUMNS):
                raise RuntimeError("query event schema columns do not match")
            daily_cols = connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'query_event_daily'"
            ).fetchall()
            if {row[0] for row in daily_cols} != {
                "day",
                "service",
                "tool",
                "operation",
                "status",
                "error_code",
                "event_count",
                "duration_total_ms",
                "duration_max_ms",
                "result_total",
                "result_observed",
                "zero_result_count",
                "search_observed",
            }:
                raise RuntimeError("query event daily columns do not match")
            rights = connection.execute(
                "SELECT has_table_privilege(current_user, 'public.query_events', 'SELECT'), "
                "has_table_privilege(current_user, 'public.query_events', 'INSERT'), "
                "has_table_privilege(current_user, 'public.query_events', 'UPDATE'), "
                "has_table_privilege(current_user, 'public.query_events', 'DELETE'), "
                "has_table_privilege(current_user, 'public.query_event_daily', 'SELECT')"
            ).fetchone()
            if tuple(rights) != (True, True, False, False, True):
                raise RuntimeError(
                    "query event runtime privileges are not least privilege"
                )
            policies = connection.execute(
                "SELECT tablename, policyname, permissive, roles, cmd, qual, with_check "
                "FROM pg_policies WHERE schemaname = 'public' "
                "AND tablename IN ('query_events', 'query_event_daily')"
            ).fetchall()
            # pg_get_expr canonical forms verified against the supported PG16
            # harness. Fail closed on predicate/role drift, including a policy
            # retaining its expected name while changing USING to true.
            admin = (
                "(current_setting('app.query_event_role'::text, true) = 'admin'::text)"
            )
            own = "(actor_id = (NULLIF(current_setting('app.query_event_actor'::text, true), ''::text))::uuid)"
            insert = (
                "((NOT (actor_id IS DISTINCT FROM (NULLIF(current_setting('app.query_event_actor'::text, true), ''::text))::uuid)) "
                "AND (actor_role = current_setting('app.query_event_role'::text, true)))"
            )
            expected = {
                (
                    "query_events",
                    "query_events_read",
                    "PERMISSIVE",
                    ("sb_app",),
                    "SELECT",
                    f"({admin} OR {own})",
                    None,
                ),
                (
                    "query_events",
                    "query_events_insert",
                    "PERMISSIVE",
                    ("sb_app",),
                    "INSERT",
                    None,
                    insert,
                ),
                (
                    "query_event_daily",
                    "query_event_daily_admin",
                    "PERMISSIVE",
                    ("sb_app",),
                    "SELECT",
                    admin,
                    None,
                ),
            }
            actual = {
                (row[0], row[1], row[2], tuple(row[3]), row[4], row[5], row[6])
                for row in policies
            }
            if actual != expected:
                raise RuntimeError("query event policies do not match")

    def write(self, event: QueryEvent) -> None:
        """Append once; idempotent retry of an event_id cannot duplicate telemetry."""
        if type(event) is not QueryEvent:
            raise TypeError("write requires QueryEvent")
        actor = Actor(event.actor_id, event.actor_role)
        values = tuple(getattr(event, key) for key in _COLUMNS)
        with self._connection(actor) as connection:
            connection.execute(
                "INSERT INTO public.query_events ("
                + ", ".join(_COLUMNS)
                + ") VALUES ("
                + ", ".join(["%s"] * len(_COLUMNS))
                + ") ON CONFLICT (event_id) DO NOTHING",
                values,
            )

    def summary(self, *, days: int = 30, limit: int = 100) -> list[dict]:
        """Own raw-event summary, or all raw+daily totals for an explicit admin.

        Non-admin history is limited by 30-day raw retention. Admin historical
        rollups are anonymous; no endpoint accepts a caller-selected actor ID.
        Uses UTC calendar-day boundaries and a bounded result row count.
        Percentiles are exact for raw-only groups; any rolled history makes them
        unavailable (null), never an average of daily percentiles. Zero-result
        rate uses only successful query events with a known result count.
        """
        if type(days) is not int or not 1 <= days <= 180:
            raise ValueError("days must be between 1 and 180")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        actor = self._actor_getter()
        if type(actor) is not Actor or (
            actor.user_id is None and actor.role != ActorRole.ADMIN
        ):
            raise PermissionError("authenticated event actor required")
        since = (datetime.now(UTC) - timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        raw = (
            "SELECT service, tool, operation, status, error_code, count(*) AS event_count, "
            "sum(duration_ms) AS duration_total_ms, max(duration_ms) AS duration_max_ms, "
            "coalesce(sum(result_count), 0) AS result_total, count(result_count) AS result_observed, "
            "count(*) FILTER (WHERE status = 'success' AND operation = 'query' AND result_count = 0) AS zero_result_count, "
            "count(*) FILTER (WHERE status = 'success' AND operation = 'query' AND result_count IS NOT NULL) AS search_observed, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS duration_p50_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS duration_p95_ms, "
            "true AS percentiles_available "
            "FROM public.query_events WHERE started_at >= %s "
        )
        params = [since]
        # Explicit own filter remains protective even if a privileged pool is
        # accidentally injected before startup verification.
        if actor.role != ActorRole.ADMIN:
            raw += "AND actor_id = %s "
            params.append(actor.user_id)
        raw += "GROUP BY service, tool, operation, status, error_code"
        if actor.role == ActorRole.ADMIN:
            raw += (
                " UNION ALL SELECT service, tool, operation, status, error_code, event_count, "
                "duration_total_ms, duration_max_ms, result_total, result_observed, "
                "zero_result_count, search_observed, NULL::double precision, NULL::double precision, false "
                "FROM public.query_event_daily WHERE day >= %s"
            )
            params.append(since.date())
        query = (
            "SELECT service, tool, operation, status, error_code, sum(event_count)::bigint, "
            "sum(duration_total_ms) / NULLIF(sum(event_count), 0), max(duration_max_ms), "
            "sum(result_total), sum(result_observed)::bigint, sum(zero_result_count)::bigint, "
            "sum(search_observed)::bigint, sum(zero_result_count)::float / NULLIF(sum(search_observed), 0), "
            "CASE WHEN bool_and(percentiles_available) THEN max(duration_p50_ms) END, "
            "CASE WHEN bool_and(percentiles_available) THEN max(duration_p95_ms) END, "
            "bool_and(percentiles_available) FROM (" + raw + ") totals "
            "GROUP BY service, tool, operation, status, error_code "
            "ORDER BY sum(event_count) DESC, service, tool, operation, status, error_code LIMIT %s"
        )
        params.append(limit)
        with self._connection(actor) as connection:
            rows = connection.execute(query, params).fetchall()
        keys = (
            "service",
            "tool",
            "operation",
            "status",
            "error_code",
            "event_count",
            "duration_avg_ms",
            "duration_max_ms",
            "result_total",
            "result_observed",
            "zero_result_count",
            "search_observed",
            "zero_result_rate",
            "duration_p50_ms",
            "duration_p95_ms",
            "percentiles_available",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]


def retention(
    connection,
    *,
    apply_changes: bool = False,
    now: datetime | None = None,
    batch_size: int = 1000,
) -> dict:
    """Privileged preview/apply: roll up raw >30d, purge anonymous daily >180d.

    One bounded batch per call; schedule repeated calls while preview reports
    backlog. DELETE RETURNING feeds the rollup in the same transaction, so only
    rows actually deleted are counted and retries or concurrent calls never
    double count. The caller explicitly injects a maintenance connection;
    sb_app cannot run it.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 10000:
        raise ValueError("batch_size must be between 1 and 10000")
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone aware")
    now = now.astimezone(UTC)
    raw_cutoff = now - timedelta(days=30)
    daily_cutoff = (now - timedelta(days=180)).date()
    with connection.transaction():
        connection.execute("SET LOCAL statement_timeout = '5s'")
        connection.execute("SET LOCAL lock_timeout = '100ms'")
        privileged = connection.execute(
            "SELECT has_table_privilege(current_user, 'public.query_events', 'DELETE'), "
            "has_table_privilege(current_user, 'public.query_event_daily', 'DELETE')"
        ).fetchone()
        bypass = connection.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if not all(privileged) or bypass is None or not bypass[0]:
            raise PermissionError("privileged maintenance connection required")
        raw_count = connection.execute(
            "SELECT count(*) FROM public.query_events WHERE started_at < %s",
            (raw_cutoff,),
        ).fetchone()[0]
        daily_count = connection.execute(
            "SELECT count(*) FROM public.query_event_daily WHERE day < %s",
            (daily_cutoff,),
        ).fetchone()[0]
        result = {
            "raw_eligible": raw_count,
            "daily_eligible": daily_count,
            "raw_deleted": 0,
            "daily_deleted": 0,
            "applied": apply_changes,
        }
        if not apply_changes:
            return result
        row = connection.execute(
            "WITH selected AS (SELECT event_id FROM public.query_events "
            "WHERE started_at < %s ORDER BY started_at LIMIT %s), "
            "removed AS (DELETE FROM public.query_events e USING selected s "
            "WHERE e.event_id = s.event_id RETURNING e.*), "
            "rolled AS (INSERT INTO public.query_event_daily "
            "SELECT (started_at AT TIME ZONE 'UTC')::date, service, tool, operation, status, error_code, "
            "count(*), sum(duration_ms), max(duration_ms), coalesce(sum(result_count), 0), count(result_count), "
            "count(*) FILTER (WHERE status = 'success' AND operation = 'query' AND result_count = 0), "
            "count(*) FILTER (WHERE status = 'success' AND operation = 'query' AND result_count IS NOT NULL) "
            "FROM removed WHERE (started_at AT TIME ZONE 'UTC')::date >= %s "
            "GROUP BY 1,2,3,4,5,6 "
            "ON CONFLICT (day,service,tool,operation,status,error_code) DO UPDATE SET "
            "event_count = query_event_daily.event_count + EXCLUDED.event_count, "
            "duration_total_ms = query_event_daily.duration_total_ms + EXCLUDED.duration_total_ms, "
            "duration_max_ms = greatest(query_event_daily.duration_max_ms, EXCLUDED.duration_max_ms), "
            "result_total = query_event_daily.result_total + EXCLUDED.result_total, "
            "result_observed = query_event_daily.result_observed + EXCLUDED.result_observed, "
            "zero_result_count = query_event_daily.zero_result_count + EXCLUDED.zero_result_count, "
            "search_observed = query_event_daily.search_observed + EXCLUDED.search_observed "
            "RETURNING 1) SELECT count(*) FROM removed",
            (raw_cutoff, batch_size, daily_cutoff),
        ).fetchone()
        result["raw_deleted"] = row[0]
        result["daily_deleted"] = connection.execute(
            "WITH selected AS (SELECT ctid FROM public.query_event_daily WHERE day < %s "
            "ORDER BY day LIMIT %s FOR UPDATE SKIP LOCKED) "
            "DELETE FROM public.query_event_daily d USING selected s WHERE d.ctid = s.ctid",
            (daily_cutoff, batch_size),
        ).rowcount
        return result
