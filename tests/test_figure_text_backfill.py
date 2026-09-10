"""Bounded, resumable local-VLM figure text backfill tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mcp_second_brain.figure_text_backfill import backfill_figure_text


class FakeStore:
    def __init__(self, rows: list[dict]):
        self.rows = {
            (row["note_path"], row["fig_index"]): dict(row) for row in rows
        }
        self.upserts: list[dict] = []

    def get_figures_for_note(self, note_path: str) -> list[dict]:
        return [
            dict(row)
            for (path, _index), row in sorted(self.rows.items())
            if path == note_path
        ]

    def upsert_figure(self, **row) -> None:
        self.upserts.append(dict(row))
        self.rows[(row["note_path"], row["fig_index"])] = dict(row)


def _row(note_path: str, image: Path, index: int = 0) -> dict:
    return {
        "note_path": note_path,
        "fig_index": index,
        "image_url": f"file://{image}",
        "local_path": str(image),
        "ocr_text": "",
        "description": "",
        "caption": "",
        "token_est": 123,
    }


def _note_and_image(vault: Path, name: str, index: int = 0) -> tuple[str, Path]:
    note_path = f"20-areas/research/{name}.md"
    note = vault / note_path
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ntitle: Test\n---\n", encoding="utf-8")
    image = vault / f"figures/{name}/fig-{index:02d}.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"png")
    return note_path, image


def test_dry_run_is_bounded_and_never_calls_model(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_VISION_BACKEND", "local-only")
    note_path, first = _note_and_image(tmp_path, "paper", 0)
    _unused, second = _note_and_image(tmp_path, "paper", 1)
    store = FakeStore([_row(note_path, first, 0), _row(note_path, second, 1)])
    analyse = MagicMock()

    result = backfill_figure_text(
        [note_path], tmp_path, store, dry_run=True, image_limit=1, analyse=analyse
    )

    assert result["summary"]["planned"] == 1
    assert result["summary"]["remaining_candidates"] == 2
    assert result["items"][0]["action"] == "would_analyse"
    analyse.assert_not_called()
    assert store.upserts == []


def test_apply_writes_each_success_and_preserves_identity_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_VISION_BACKEND", "local-only")
    note_path, image = _note_and_image(tmp_path, "paper")
    store = FakeStore([_row(note_path, image)])

    result = backfill_figure_text(
        [note_path],
        tmp_path,
        store,
        dry_run=False,
        image_limit=20,
        analyse=lambda _path, _caption: {
            "ocr_text": "axis label",
            "description": "A plot.",
            "_usage": {"input": 0, "output": 0},
        },
    )

    assert result["summary"]["applied"] == 1
    assert store.upserts == [
        {
            **_row(note_path, image),
            "ocr_text": "axis label",
            "description": "A plot.",
        }
    ]


def test_model_failure_keeps_row_queued_and_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_VISION_BACKEND", "local-only")
    note_path, first = _note_and_image(tmp_path, "paper", 0)
    _unused, second = _note_and_image(tmp_path, "paper", 1)
    store = FakeStore([_row(note_path, first, 0), _row(note_path, second, 1)])
    analyse = MagicMock(
        side_effect=[None, {"ocr_text": "ok", "description": "plot", "_usage": {}}]
    )

    result = backfill_figure_text(
        [note_path], tmp_path, store, dry_run=False, image_limit=20, analyse=analyse
    )

    assert result["summary"]["failed"] == 1
    assert result["summary"]["applied"] == 1
    assert result["summary"]["halted"] is False
    assert len(store.upserts) == 1


def test_apply_is_idempotent_on_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_VISION_BACKEND", "local-only")
    note_path, image = _note_and_image(tmp_path, "paper")
    store = FakeStore([_row(note_path, image)])
    analyse = MagicMock(
        return_value={"ocr_text": "ok", "description": "plot", "_usage": {}}
    )

    first = backfill_figure_text(
        [note_path], tmp_path, store, dry_run=False, image_limit=20, analyse=analyse
    )
    second = backfill_figure_text(
        [note_path], tmp_path, store, dry_run=False, image_limit=20, analyse=analyse
    )

    assert first["summary"]["applied"] == 1
    assert second["summary"]["applied"] == 0
    assert analyse.call_count == 1


def test_non_local_policy_fails_before_model_or_write(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_VISION_BACKEND", "anthropic-first")
    note_path, image = _note_and_image(tmp_path, "paper")
    store = FakeStore([_row(note_path, image)])
    analyse = MagicMock()

    with pytest.raises(ValueError, match="local-only"):
        backfill_figure_text(
            [note_path], tmp_path, store, dry_run=False, image_limit=20, analyse=analyse
        )

    analyse.assert_not_called()
    assert store.upserts == []


def test_store_failure_halts_remaining_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_VISION_BACKEND", "local-only")
    note_path, first = _note_and_image(tmp_path, "paper", 0)
    _unused, second = _note_and_image(tmp_path, "paper", 1)

    class FailingStore(FakeStore):
        def upsert_figure(self, **row) -> None:
            self.upserts.append(dict(row))
            raise RuntimeError("database unavailable")

    store = FailingStore([_row(note_path, first, 0), _row(note_path, second, 1)])
    analyse = MagicMock(
        return_value={"ocr_text": "ok", "description": "plot", "_usage": {}}
    )

    result = backfill_figure_text(
        [note_path], tmp_path, store, dry_run=False, image_limit=20, analyse=analyse
    )

    assert result["summary"]["halted"] is True
    assert result["summary"]["failed"] == 1
    assert analyse.call_count == 1


def test_mcp_tool_has_local_bounded_defaults():
    from mcp_second_brain import server

    tools = asyncio.run(server.mcp.list_tools())
    matches = [tool for tool in tools if tool.name == "backfill_figure_text"]

    assert len(matches) == 1
    schema = matches[0].inputSchema["properties"]
    assert schema["dry_run"]["default"] is True
    assert schema["note_limit"]["maximum"] == 20
    assert schema["image_limit"]["maximum"] == 20
