import asyncio
import inspect
import threading
from unittest.mock import Mock

import pytest

from mcp_second_brain import server
from mcp_second_brain.identity import Identity, _current, set_identity
from mcp_second_brain.query_dispatch import QueryDispatcher
from mcp_second_brain.query_event_mcp import (
    TOOL_OPERATIONS,
    TOOL_OUTCOME_SUPPORT,
    UNMEASURABLE_OUTCOME_REASONS,
)
from mcp_second_brain.query_event_store import BoundedEventSink
from mcp_second_brain.query_events import ErrorCode, EventStatus
from mcp_second_brain.vault_paths import VaultPathError


def test_inventory_covers_every_registered_tool():
    assert {tool.name for tool in asyncio.run(server.mcp.list_tools())} == set(TOOL_OPERATIONS)


def test_every_catalog_tool_has_an_explicit_outcome_policy():
    assert len(TOOL_OPERATIONS) == 46
    assert set(TOOL_OUTCOME_SUPPORT) == set(TOOL_OPERATIONS)
    assert set(TOOL_OUTCOME_SUPPORT.values()) == {
        "supported",
        "genuinely_unmeasurable",
    }
    assert {
        tool
        for tool, support in TOOL_OUTCOME_SUPPORT.items()
        if support == "genuinely_unmeasurable"
    } == {"extract_figures_for"}
    assert set(UNMEASURABLE_OUTCOME_REASONS) == {
        "extract_figures_for",
        "extract_rules_tool:no_rules",
        "extract_rules_tool:batch_zero",
        "expand_semantic_keywords_tool:model_empty",
    }


@pytest.mark.parametrize("tool_name", sorted(TOOL_OPERATIONS))
def test_each_catalog_tool_marks_outcomes_at_its_source(tool_name):
    source = inspect.getsource(inspect.unwrap(getattr(server, tool_name)))
    assert "mark_outcome(" in source


def test_disabled_runtime_has_no_storage_dependency(monkeypatch):
    monkeypatch.delenv("SB_QUERY_EVENTS_ENABLED", raising=False)
    monkeypatch.setattr(server, "_store", object())
    assert server._start_query_events() is None


def test_enabled_runtime_rejects_single_user_or_wrong_backend(monkeypatch):
    monkeypatch.setenv("SB_QUERY_EVENTS_ENABLED", "1")
    monkeypatch.delenv("SB_MULTIUSER", raising=False)
    monkeypatch.setattr(server, "_store", object())
    with pytest.raises(RuntimeError, match="multiuser"):
        server._start_query_events()


@pytest.fixture
def recording(monkeypatch):
    events = []
    monkeypatch.setattr(server._query_recorder, "_sink", events.append)
    monkeypatch.setattr(server._query_recorder, "enabled", True)
    monkeypatch.setenv("SB_MULTIUSER", "1")
    monkeypatch.setenv("SB_RBAC_ENFORCE", "1")
    store = Mock()
    monkeypatch.setattr(server, "_store", store)
    token = set_identity(Identity("alice", "reader", "11111111-1111-1111-1111-111111111111"))
    yield events, store
    _current.reset(token)


def test_search_event_has_actual_count_and_no_query(recording):
    events, store = recording
    store.hybrid_search.return_value = []
    asyncio.run(server.mcp.call_tool("search_notes", {"query": "secret-query"}))
    assert len(events) == 1
    assert events[0].result_count == 0
    assert events[0].status == "success"
    assert "secret-query" not in str(events[0].to_payload())


def test_returned_permission_denial_is_not_success(recording):
    events, store = recording
    asyncio.run(server.mcp.call_tool("update_note", {"path": "private-note", "content": "secret"}))
    assert events[0].status == "denied"
    store.append_audit_log.assert_not_called()


def test_returned_admin_denial_is_not_success(recording):
    events, store = recording
    asyncio.run(server.mcp.call_tool("manage_api_key", {"action": "list"}))
    assert len(events) == 1
    assert events[0].status == EventStatus.DENIED
    assert events[0].error_code == ErrorCode.DENIED
    store.list_api_keys.assert_not_called()


def test_index_outage_classified_without_payload(recording):
    events, store = recording
    store.hybrid_search.side_effect = RuntimeError("sensitive-dsn")
    asyncio.run(server.mcp.call_tool("search_notes", {"query": "secret"}))
    assert events[0].status == "error"
    assert events[0].error_code == "unavailable"
    assert "sensitive-dsn" not in str(events[0].to_payload())


def test_registered_mcp_queries_overlap_and_keep_actor(recording):
    events, store = recording
    barrier = threading.Barrier(2, timeout=.5)
    worker_actors = []
    actor_lock = threading.Lock()

    def search(*args, **kwargs):
        identity = server.get_current_identity()
        with actor_lock:
            worker_actors.append(identity.user_uuid)
        barrier.wait()
        return []

    store.hybrid_search.side_effect = search

    async def request(uuid):
        token = set_identity(Identity("synthetic", "member", uuid))
        try:
            return await server.mcp.call_tool("search_notes", {"query": "synthetic"})
        finally:
            _current.reset(token)

    async def run():
        return await asyncio.gather(
            request("11111111-1111-1111-1111-111111111111"),
            request("22222222-2222-2222-2222-222222222222"),
        )

    asyncio.run(run())
    assert len(events) == 2
    assert len({event.actor_id for event in events}) == 2
    assert all(event.status == "success" and event.result_count == 0 for event in events)
    assert set(worker_actors) == {
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    }


def test_mcp_timeout_emits_exactly_one_event_and_retains_slot(recording, monkeypatch):
    events, store = recording
    dispatcher = QueryDispatcher(capacity=1, per_actor=1, timeout=.02)
    monkeypatch.setattr(server.mcp, "query_dispatcher", dispatcher)
    release = threading.Event()
    store.hybrid_search.side_effect = lambda *args, **kwargs: release.wait(.5) or []

    async def run():
        result = await server.mcp.call_tool("search_notes", {"query": "private"})
        assert "deadline exceeded" in str(result).lower()
        assert dispatcher.active == 1
        assert len(events) == 1
        assert events[0].status == EventStatus.ERROR
        assert events[0].error_code == ErrorCode.TIMEOUT
        release.set()
        assert await dispatcher.close(timeout=1)

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert len(events) == 1


def test_mcp_cancellation_emits_exactly_one_event_and_retains_slot(recording, monkeypatch):
    events, store = recording
    dispatcher = QueryDispatcher(capacity=1, per_actor=1, timeout=1)
    monkeypatch.setattr(server.mcp, "query_dispatcher", dispatcher)
    started = threading.Event()
    release = threading.Event()

    def search(*args, **kwargs):
        started.set()
        release.wait(.5)
        return []

    store.hybrid_search.side_effect = search

    async def run():
        task = asyncio.create_task(
            server.mcp.call_tool("search_notes", {"query": "private"})
        )
        assert await asyncio.to_thread(started.wait, .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert dispatcher.active == 1
        assert len(events) == 1
        assert events[0].status == EventStatus.CANCELLED
        assert events[0].error_code == ErrorCode.CANCELLED
        release.set()
        assert await dispatcher.close(timeout=1)

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert len(events) == 1


def test_mcp_close_waits_for_recorder_before_event_sink_close(recording, monkeypatch):
    _, store = recording
    dispatcher = QueryDispatcher(capacity=1, per_actor=1, timeout=1)
    monkeypatch.setattr(server.mcp, "query_dispatcher", dispatcher)
    monkeypatch.setattr(server.mcp, "_query_calls", 0)
    monkeypatch.setattr(server.mcp, "_query_calls_closed", False)
    monkeypatch.setattr(server.mcp, "_query_call_waiters", set())
    written = []
    sink = BoundedEventSink(written.append, capacity=2)
    sink.start()
    monkeypatch.setattr(server._query_recorder, "_sink", sink)
    started = threading.Event()
    release = threading.Event()

    def search(*args, **kwargs):
        started.set()
        release.wait(.5)
        return []

    store.hybrid_search.side_effect = search

    async def run():
        task = asyncio.create_task(
            server.mcp.call_tool("search_notes", {"query": "private"})
        )
        assert await asyncio.to_thread(started.wait, .5)
        release.set()

        # Intentionally close before awaiting the request. MCP-level draining
        # must include recorder.finish, not merely the worker Future callback.
        assert await server.mcp.close_queries(timeout=1)
        server._query_recorder.disable()
        assert sink.close(timeout=1)
        await task

    try:
        asyncio.run(run())
    finally:
        release.set()
        sink.close(timeout=1)

    stats = sink.stats
    assert len(written) == 1
    assert stats.accepted == 1
    assert stats.written == 1
    assert stats.rejected == 0


def test_direct_python_query_call_remains_synchronous_and_observed_once(recording):
    events, store = recording
    store.hybrid_search.return_value = []

    assert not inspect.iscoroutinefunction(server.search_notes)
    assert "No notes found" in server.search_notes("direct-private-query")
    assert len(events) == 1
    assert events[0].status == EventStatus.SUCCESS
    assert events[0].result_count == 0


def test_invalid_result_branch_is_classified_without_parsing_text(recording):
    events, _ = recording

    assert "must be" in server.top_notes(by="not-a-mode")
    assert len(events) == 1
    assert events[0].status == EventStatus.ERROR
    assert events[0].error_code == ErrorCode.INVALID_REQUEST


def test_partial_structured_failure_reports_error_and_actual_success_count(recording):
    events, store = recording
    token = set_identity(
        Identity(
            "writer",
            "writer",
            "11111111-1111-1111-1111-111111111111",
        )
    )
    store.sync_chunks.return_value = {
        "updated": 2,
        "failed": 1,
        "candidates": 3,
        "remaining": 0,
    }
    try:
        result = server.sync_chunks_tool(limit=3)
    finally:
        _current.reset(token)

    assert "2 notes backfilled, 1 failed" in result
    assert len(events) == 1
    assert events[0].status == EventStatus.ERROR
    assert events[0].error_code == ErrorCode.UNAVAILABLE
    assert events[0].result_count == 2


def test_opaque_helper_result_remains_unknown_and_payload_free(recording, monkeypatch):
    events, _ = recording
    token = set_identity(
        Identity(
            "writer",
            "writer",
            "11111111-1111-1111-1111-111111111111",
        )
    )
    monkeypatch.setattr(server, "_vault_path", lambda path, **kwargs: object())
    monkeypatch.setattr(
        server._fig,
        "process_article",
        lambda *args: "opaque private helper payload",
    )
    monkeypatch.setattr(server.vault_db, "get_figures_for_note", lambda path: [])
    try:
        assert server.extract_figures_for("private.md") == "opaque private helper payload"
    finally:
        _current.reset(token)

    assert len(events) == 1
    assert events[0].status == EventStatus.UNKNOWN
    assert events[0].error_code == ErrorCode.UNCLASSIFIED
    assert "opaque private helper payload" not in str(events[0].to_payload())


def test_result_count_uses_visible_records_not_rendered_lines(recording, monkeypatch):
    events, store = recording
    store.find_related.return_value = ["visible.md", "hidden.md"]

    def visible_path(path, **kwargs):
        if path == "hidden.md":
            raise VaultPathError("private")
        return Mock(exists=lambda: False)

    monkeypatch.setattr(server, "_vault_path", visible_path)
    result = server.find_related_notes("source.md")

    assert "visible" in result
    assert "hidden" not in result
    assert len(events) == 1
    assert events[0].status == EventStatus.SUCCESS
    assert events[0].result_count == 1


def test_single_user_mcp_query_bypasses_dispatcher(monkeypatch):
    monkeypatch.delenv("SB_MULTIUSER", raising=False)
    monkeypatch.setattr(server._query_recorder, "enabled", False)
    store = Mock()
    store.hybrid_search.return_value = []
    monkeypatch.setattr(server, "_store", store)
    dispatcher = Mock()
    monkeypatch.setattr(server.mcp, "query_dispatcher", dispatcher)

    result = asyncio.run(
        server.mcp.call_tool("search_notes", {"query": "single-user-query"})
    )

    assert "No notes found" in str(result)
    dispatcher.run.assert_not_called()
