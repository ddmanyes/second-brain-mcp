"""Explicit MCP inventory and opt-in instrumentation at registration."""

import asyncio
import inspect
import math
import threading
from functools import wraps

from mcp.server.fastmcp import FastMCP

from . import visibility
from .identity import get_current_identity
from .query_dispatch import QueryBusy, QueryDispatcher
from .query_events import ErrorCode, EventStatus, Outcome, mark_outcome

QUERY_TOOLS = frozenset({
    "search_notes", "search_articles", "search_snippets", "query_graph",
    "search_news_tool", "search_figures", "find_related_notes", "search_grouped", "top_notes",
})
READ_TOOLS = frozenset({
    "auth_context", "get_context", "get_decisions", "read_note", "index_stats", "sleep_status",
    "read_note_as_image", "read_figure", "get_agent_instructions", "health_check",
})
WRITE_TOOLS = frozenset({
    "new_note", "litnet_answer", "update_goals", "update_note", "append_to_note",
    "mark_note_status", "sync_notes", "sync_index", "sync_chunks_tool", "vault_sleep",
    "extract_rules_tool", "expand_semantic_keywords_tool", "enrich_neighbor_keywords_tool",
    "save_article", "update_links_tool", "extract_figures_for", "reconcile_figures",
    "backfill_figure_text", "restore_missing_pdf_images", "snapshot_note_tool",
    "consolidate_tool", "prune_archive_tool", "annotate_figure", "init_vault",
})
ADMIN_TOOLS = frozenset({"manage_api_key", "query_audit_log", "audit_article_records"})
TOOL_OPERATIONS = {
    **dict.fromkeys(QUERY_TOOLS, "query"), **dict.fromkeys(READ_TOOLS, "read"),
    **dict.fromkeys(WRITE_TOOLS, "write"), **dict.fromkeys(ADMIN_TOOLS, "admin"),
}

# A reviewable catalog contract: every registered tool is either classified at
# the return source, or its successful-looking return is genuinely opaque. The
# latter must remain UNKNOWN; callers must never infer it from prose.
TOOL_OUTCOME_SUPPORT = dict.fromkeys(TOOL_OPERATIONS, "supported")
TOOL_OUTCOME_SUPPORT["extract_figures_for"] = "genuinely_unmeasurable"

# Exact opaque branches within otherwise supported tools. Keeping these reasons
# next to the catalog prevents a later wrapper from treating them as success.
UNMEASURABLE_OUTCOME_REASONS = {
    "extract_figures_for": (
        "figures.process_article exposes only a prose summary that can represent "
        "success or failure"
    ),
    "extract_rules_tool:no_rules": (
        "extract_rules_for returns the same empty list for no rules and model "
        "unavailability"
    ),
    "extract_rules_tool:batch_zero": (
        "run_rules_extraction omits eligible count, so zero processed can mean "
        "no candidates or model unavailability"
    ),
    "expand_semantic_keywords_tool:model_empty": (
        "keyword extraction returns the same empty list for no suitable keywords "
        "and backend unavailability"
    ),
}


class ObservedFastMCP(FastMCP):
    def __init__(self, *args, recorder, **kwargs):
        self.recorder = recorder
        self.query_dispatcher = QueryDispatcher()
        self._query_call_lock = threading.Lock()
        self._query_calls = 0
        self._query_calls_closed = False
        self._query_call_waiters = set()
        super().__init__(*args, **kwargs)

    @staticmethod
    def _finish_waiter(waiter):
        if not waiter.done():
            waiter.set_result(None)

    def _begin_query_call(self):
        with self._query_call_lock:
            if self._query_calls_closed:
                return False
            self._query_calls += 1
            return True

    def _finish_query_call(self):
        waiters = ()
        with self._query_call_lock:
            self._query_calls -= 1
            if self._query_calls == 0:
                waiters = tuple(self._query_call_waiters)
                self._query_call_waiters.clear()
        for loop, waiter in waiters:
            try:
                loop.call_soon_threadsafe(self._finish_waiter, waiter)
            except RuntimeError:
                pass

    async def _drain_query_calls(self, timeout):
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        with self._query_call_lock:
            if self._query_calls == 0:
                return True
            entry = (loop, waiter)
            self._query_call_waiters.add(entry)
        try:
            await asyncio.wait_for(waiter, timeout)
            return True
        except TimeoutError:
            with self._query_call_lock:
                return self._query_calls == 0
        finally:
            with self._query_call_lock:
                self._query_call_waiters.discard(entry)
            if not waiter.done():
                waiter.cancel()

    async def close_queries(self, *, timeout=1.0):
        """Stop MCP query admission and drain workers plus event wrappers.

        True guarantees that accepted query/read calls reached the outer
        recorder's ``finish`` before returning. False means a worker or wrapper
        remains live; a blocked Python thread cannot be terminated safely.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("shutdown timeout must be finite and nonnegative")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("shutdown timeout must be finite and nonnegative")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        with self._query_call_lock:
            self._query_calls_closed = True
        self.query_dispatcher.stop_accepting()
        workers_drained = await self.query_dispatcher.close(
            timeout=max(0.0, deadline - loop.time())
        )
        calls_drained = await self._drain_query_calls(
            max(0.0, deadline - loop.time())
        )
        return workers_drained and calls_drained

    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)

        def decorate(fn):
            operation = TOOL_OPERATIONS[fn.__name__]
            observed = self.recorder.instrument(tool=fn.__name__, operation=operation)(fn)
            if operation not in {"query", "read"}:
                return register(observed)

            if inspect.iscoroutinefunction(fn):
                observed_dispatch = observed
            else:

                @wraps(fn)
                async def dispatch(*call_args, **call_kwargs):
                    if not visibility.multiuser_enabled():
                        return fn(*call_args, **call_kwargs)
                    identity = get_current_identity()
                    if identity is None:
                        mark_outcome(Outcome(EventStatus.DENIED, ErrorCode.DENIED))
                        return "Authenticated identity required."
                    actor = identity.user_uuid or (
                        "admin" if identity.is_admin() else None
                    )
                    if actor is None:
                        mark_outcome(Outcome(EventStatus.DENIED, ErrorCode.DENIED))
                        return "A registered user identity is required."
                    try:
                        return await self.query_dispatcher.run(
                            actor, fn, *call_args, **call_kwargs
                        )
                    except QueryBusy:
                        mark_outcome(
                            Outcome(EventStatus.ERROR, ErrorCode.UNAVAILABLE)
                        )
                        return "Query service busy. Please retry later."
                    except TimeoutError:
                        mark_outcome(Outcome(EventStatus.ERROR, ErrorCode.TIMEOUT))
                        return "Query deadline exceeded. Please retry later."

                observed_dispatch = self.recorder.instrument(
                    tool=fn.__name__, operation=operation
                )(dispatch)

            @wraps(observed_dispatch)
            async def finish_observed(*call_args, **call_kwargs):
                if not self._begin_query_call():
                    return "Query service busy. Please retry later."
                try:
                    return await observed_dispatch(*call_args, **call_kwargs)
                finally:
                    # This runs after recorder.finish, so close_queries can safely
                    # close the event sink once the outer count reaches zero.
                    self._finish_query_call()

            register(finish_observed)
            # Preserve direct Python-call contracts used by internal helpers.
            return observed

        return decorate
