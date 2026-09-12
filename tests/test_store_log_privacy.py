from contextlib import contextmanager
from types import SimpleNamespace

from mcp_second_brain.store import postgres_store


def test_multiuser_chunk_failures_do_not_log_paths_or_model_body(monkeypatch, capsys):
    monkeypatch.setenv("SB_MULTIUSER", "1")

    @contextmanager
    def connection():
        yield SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: None))

    store = SimpleNamespace(_conn=connection)

    def unavailable(*args, **kwargs):
        raise OSError("secret-file-path")

    assert (
        postgres_store.PostgresStore._plan_chunks_for_note(
            store, "private-note-canary", "hash", SimpleNamespace(read_text=unavailable)
        )
        is None
    )

    def model_failure(*args):
        raise postgres_store.LateChunkingUnavailable("secret-model-response")

    monkeypatch.setattr(postgres_store, "chunk_and_embed", model_failure)
    assert (
        postgres_store.PostgresStore._plan_chunks_for_note(
            store,
            "private-note-canary",
            "hash",
            SimpleNamespace(read_text=lambda **kw: "body"),
        )
        is None
    )
    output = capsys.readouterr().err
    assert "private-note-canary" not in output
    assert "secret" not in output
    assert "unavailable" in output
