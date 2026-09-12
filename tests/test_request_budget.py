import contextvars
import threading
import time

import pytest

from mcp_second_brain.request_budget import (
    REQUEST_TIMEOUT_MESSAGE,
    remaining_timeout,
    request_budget,
    request_embedding,
    stage,
    timing_snapshot,
)


def test_budget_caps_defaults_expires_and_resets_after_exit():
    with request_budget(0.03):
        remaining = remaining_timeout(10)
        assert 0 < remaining <= 0.03
        time.sleep(0.04)
        with pytest.raises(TimeoutError, match=f"^{REQUEST_TIMEOUT_MESSAGE}$"):
            remaining_timeout(10)

    assert remaining_timeout(7.5) == 7.5
    assert timing_snapshot() == {}


def test_nested_budget_uses_earliest_deadline_and_shares_timings():
    with request_budget(0.2):
        outer = remaining_timeout(1)
        with request_budget(0.04):
            inner = remaining_timeout(1)
            assert 0 < inner < outer
            with stage("db"):
                time.sleep(0.005)
        restored = remaining_timeout(1)
        assert restored > inner
        assert timing_snapshot()["db"] >= 4


def test_stage_rejects_unbounded_labels_and_accumulates_only_fixed_keys():
    with request_budget(1):
        with stage("pool_wait"):
            time.sleep(0.002)
        with stage("pool_wait"):
            time.sleep(0.002)
        snapshot = timing_snapshot()
        assert set(snapshot) == {"pool_wait"}
        assert snapshot["pool_wait"] >= 3
        with pytest.raises(ValueError, match="invalid request timing stage"):
            with stage("raw query text"):
                pass


def test_embedding_memoizes_none_and_leaves_other_queries_uncached():
    calls = []

    def provider(query):
        calls.append(query)
        return None

    with request_budget(1):
        assert request_embedding("private first query", provider) is None
        assert request_embedding("private first query", provider) is None
        assert calls == ["private first query"]
        assert timing_snapshot()["query_embedding"] >= 0
        assert request_embedding("private second query", provider) is None
        assert request_embedding("private second query", provider) is None
        assert calls == [
            "private first query",
            "private second query",
            "private second query",
        ]

    assert request_embedding("outside", provider) is None
    assert request_embedding("outside", provider) is None
    assert calls[-2:] == ["outside", "outside"]


def test_copied_thread_context_shares_request_memo_and_stage_timings():
    calls = []
    result = []

    def provider(query):
        calls.append(query)
        return [1.0, 2.0]

    with request_budget(1):
        assert request_embedding("one", provider) == [1.0, 2.0]
        copied = contextvars.copy_context()

        def in_context():
            with stage("file_read"):
                result.append(request_embedding("one", provider))

        thread = threading.Thread(target=lambda: copied.run(in_context))
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert result == [[1.0, 2.0]]
        assert calls == ["one"]
        assert "file_read" in timing_snapshot()


@pytest.mark.parametrize(
    "label",
    ["pool_wait", "db", "query_embedding", "rerank", "file_read", "queue_wait", "job_run"],
)
def test_every_bounded_stage_label_is_accepted(label):
    with request_budget(1):
        with stage(label):
            pass
        assert label in timing_snapshot()
