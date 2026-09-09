"""Zero-Vision figure reconciliation tests.

These tests exercise the public reconciliation seam.  They deliberately avoid
the VLM path: deterministic disk/Markdown evidence is the only input allowed.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from mcp_second_brain.figure_reconciliation import reconcile_figures

_PNG_BUFFER = io.BytesIO()
Image.new("RGB", (1, 1), "white").save(_PNG_BUFFER, format="PNG")
_PNG = _PNG_BUFFER.getvalue()


class FakeStore:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = {
            (row["note_path"], row["fig_index"]): dict(row) for row in (rows or [])
        }
        self.upserts: list[dict] = []

    def get_figure(self, note_path: str, fig_index: int) -> dict | None:
        row = self.rows.get((note_path, fig_index))
        return dict(row) if row else None

    def get_figures_for_note(self, note_path: str) -> list[dict]:
        return [
            dict(row)
            for (path, _index), row in sorted(self.rows.items())
            if path == note_path
        ]

    def upsert_figure(self, **row) -> None:
        self.upserts.append(dict(row))
        self.rows[(row["note_path"], row["fig_index"])] = dict(row)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        content_type: str = "image/png",
        status_code: int = 200,
        url: str = "https://example.org/figure.png",
    ):
        self.body = body
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        self.status_code = status_code
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, _chunk_size: int):
        yield self.body


def _make_note(vault: Path, name: str, body: str = "body") -> str:
    note_path = f"20-areas/research/{name}.md"
    path = vault / note_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: Test\n---\n\n{body}", encoding="utf-8")
    return note_path


def _row(note_path: str, local_path: str) -> dict:
    return {
        "note_path": note_path,
        "fig_index": 0,
        "image_url": "file:///old/fig-00.png",
        "local_path": local_path,
        "ocr_text": "old OCR",
        "description": "old description",
        "caption": "old caption",
        "token_est": 12,
    }


def test_exact_local_match_is_planned_with_checksum_and_applies(tmp_path):
    note_path = _make_note(tmp_path, "2026_Test_Paper")
    image = tmp_path / "figures/2026-test-paper/fig-00.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(_PNG)
    store = FakeStore()

    dry = reconcile_figures([note_path], tmp_path, store, dry_run=True, limit=20)

    action = dry["notes"][0]["actions"][0]
    assert action["action"] == "index_local_file"
    assert action["checksum"]
    assert store.upserts == []

    applied = reconcile_figures([note_path], tmp_path, store, dry_run=False, limit=20)

    assert applied["summary"]["applied"] == 1
    assert store.upserts[0]["local_path"] == str(image.resolve())


def test_unique_extension_mismatch_repairs_missing_row_path(tmp_path):
    note_path = _make_note(tmp_path, "2026_Test_Paper")
    missing = tmp_path / "figures/2026-test-paper/fig-00.png"
    actual = missing.with_suffix(".jpg")
    actual.parent.mkdir(parents=True)
    actual.write_bytes(_PNG)
    store = FakeStore([_row(note_path, str(missing))])

    result = reconcile_figures([note_path], tmp_path, store, dry_run=False, limit=20)

    assert result["notes"][0]["actions"][0]["action"] == "repair_local_path"
    assert store.upserts[0]["local_path"] == str(actual.resolve())
    assert store.upserts[0]["description"] == "old description"


def test_multiple_local_candidates_are_never_guessed(tmp_path):
    note_path = _make_note(tmp_path, "2026_Test_Paper")
    folder = tmp_path / "figures/2026-test-paper"
    folder.mkdir(parents=True)
    (folder / "fig-00.png").write_bytes(_PNG)
    (folder / "fig-00.jpg").write_bytes(_PNG)
    store = FakeStore()

    result = reconcile_figures([note_path], tmp_path, store, dry_run=False, limit=20)

    action = result["notes"][0]["actions"][0]
    assert action["action"] == "manual_queue"
    assert action["reason"] == "multiple_local_candidates"
    assert store.upserts == []


def test_symlink_candidate_outside_vault_is_rejected(tmp_path):
    note_path = _make_note(tmp_path, "2026_Test_Paper")
    outside = tmp_path.parent / "outside-figure.png"
    outside.write_bytes(_PNG)
    candidate = tmp_path / "figures/2026-test-paper/fig-00.png"
    candidate.parent.mkdir(parents=True)
    candidate.symlink_to(outside)

    result = reconcile_figures(
        [note_path], tmp_path, FakeStore(), dry_run=False, limit=20
    )

    action = result["notes"][0]["actions"][0]
    assert action["action"] == "manual_queue"
    assert action["reason"] == "unsafe_local_candidate"


def test_path_traversal_note_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="outside vault"):
        reconcile_figures(
            ["../outside.md"], tmp_path, FakeStore(), dry_run=True, limit=20
        )


def test_remote_non_image_is_not_materialized(tmp_path, monkeypatch):
    note_path = _make_note(
        tmp_path, "2026_Test_Paper", "![Figure 1](https://example.org/figure.png)"
    )
    monkeypatch.setattr(
        "mcp_second_brain.figure_reconciliation._is_ssrf_safe", lambda _url: True
    )
    get = MagicMock(return_value=FakeResponse(b"not an image", "text/html"))
    monkeypatch.setattr("mcp_second_brain.figure_reconciliation.requests.get", get)

    result = reconcile_figures(
        [note_path], tmp_path, FakeStore(), dry_run=False, limit=20
    )

    action = result["notes"][0]["actions"][0]
    assert action["action"] == "manual_queue"
    assert action["reason"] == "download_not_image"
    assert not list((tmp_path / "figures").rglob("fig-00.*"))


def test_remote_image_over_twenty_mb_is_rejected(tmp_path, monkeypatch):
    note_path = _make_note(
        tmp_path, "2026_Test_Paper", "![Figure 1](https://example.org/figure.png)"
    )
    monkeypatch.setattr(
        "mcp_second_brain.figure_reconciliation._is_ssrf_safe", lambda _url: True
    )
    body = b"x" * (20 * 1024 * 1024 + 1)
    monkeypatch.setattr(
        "mcp_second_brain.figure_reconciliation.requests.get",
        MagicMock(return_value=FakeResponse(body)),
    )

    result = reconcile_figures(
        [note_path], tmp_path, FakeStore(), dry_run=False, limit=20
    )

    action = result["notes"][0]["actions"][0]
    assert action["reason"] == "download_too_large"
    assert not list((tmp_path / "figures").rglob("fig-00.*"))


def test_empty_remote_alt_stays_in_manual_queue_without_network(tmp_path, monkeypatch):
    note_path = _make_note(
        tmp_path, "2026_Test_Paper", "![](https://example.org/figure.png)"
    )
    get = MagicMock()
    monkeypatch.setattr("mcp_second_brain.figure_reconciliation.requests.get", get)

    result = reconcile_figures(
        [note_path], tmp_path, FakeStore(), dry_run=False, limit=20
    )

    action = result["notes"][0]["actions"][0]
    assert action["action"] == "manual_queue"
    assert action["reason"] == "empty_alt_text"
    get.assert_not_called()


def test_remote_apply_is_idempotent_after_interrupted_resume(tmp_path, monkeypatch):
    note_path = _make_note(
        tmp_path, "2026_Test_Paper", "![Figure 1](https://example.org/figure.png)"
    )
    monkeypatch.setattr(
        "mcp_second_brain.figure_reconciliation._is_ssrf_safe", lambda _url: True
    )
    get = MagicMock(return_value=FakeResponse(_PNG))
    monkeypatch.setattr("mcp_second_brain.figure_reconciliation.requests.get", get)
    store = FakeStore()

    first = reconcile_figures([note_path], tmp_path, store, dry_run=False, limit=20)
    second = reconcile_figures([note_path], tmp_path, store, dry_run=False, limit=20)

    assert first["summary"]["applied"] == 1
    assert second["summary"]["applied"] == 0
    assert len(store.upserts) == 1
    get.assert_called_once()


def test_batch_is_bounded_to_twenty_notes(tmp_path):
    notes = [_make_note(tmp_path, f"Paper_{i}") for i in range(21)]

    with pytest.raises(ValueError, match="at most 20"):
        reconcile_figures(notes, tmp_path, FakeStore(), dry_run=True, limit=20)


def test_projection_failure_halts_remaining_batch(tmp_path):
    first = _make_note(tmp_path, "First")
    second = _make_note(tmp_path, "Second")
    for slug in ("first", "second"):
        image = tmp_path / f"figures/{slug}/fig-00.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(_PNG)

    class FailingStore(FakeStore):
        def upsert_figure(self, **row) -> None:
            self.upserts.append(dict(row))
            raise RuntimeError("database unavailable")

    store = FailingStore()
    result = reconcile_figures(
        [first, second], tmp_path, store, dry_run=False, limit=20
    )

    assert result["summary"]["failed"] == 1
    assert result["summary"]["halted"] is True
    assert len(store.upserts) == 1


def test_mcp_tool_exposes_dry_run_and_rejects_unbounded_batch(tmp_path, monkeypatch):
    from mcp_second_brain import server

    tools = asyncio.run(server.mcp.list_tools())
    matches = [tool for tool in tools if tool.name == "reconcile_figures"]
    assert len(matches) == 1
    schema = matches[0].inputSchema["properties"]
    assert schema["dry_run"]["default"] is True
    assert schema["limit"]["maximum"] == 20

    monkeypatch.setattr(server, "VAULT", tmp_path)
    content, _ = asyncio.run(
        server.mcp.call_tool(
            "reconcile_figures",
            {"note_paths": [f"paper-{i}.md" for i in range(21)]},
        )
    )
    assert "at most 20" in content[0].text
