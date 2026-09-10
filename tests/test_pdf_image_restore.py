from __future__ import annotations

import asyncio
from pathlib import Path

from mcp_second_brain.pdf_image_restore import restore_missing_pdf_images


class FakeStore:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def get_figures_for_note(self, note_path: str) -> list[dict]:
        return [dict(row) for row in self.rows if row["note_path"] == note_path]


def _note(vault: Path, pdf: Path) -> str:
    rel = "20-areas/research/paper.md"
    note = vault / rel
    note.parent.mkdir(parents=True)
    note.write_text(f"| **Source PDF** | {pdf} |\n", encoding="utf-8")
    return rel


def _rows(vault: Path, note_path: str) -> list[dict]:
    root = vault / "figures/paper"
    root.mkdir(parents=True)
    (root / "fig-00.png").write_bytes(b"first")
    return [
        {"note_path": note_path, "fig_index": 0, "local_path": str(root / "fig-00.png")},
        {"note_path": note_path, "fig_index": 1, "local_path": str(root / "fig-01.png")},
    ]


def _extract(_pdf: Path) -> list[dict]:
    return [
        {"page": 0, "ordinal": 0, "xref": 10, "ext": "png", "data": b"first"},
        {"page": 0, "ordinal": 1, "xref": 11, "ext": "png", "data": b"second"},
    ]


def test_restore_is_dry_run_bounded_and_idempotent(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    note_path = _note(tmp_path, pdf)
    rows = _rows(tmp_path, note_path)
    store = FakeStore(rows)

    dry = restore_missing_pdf_images(
        [note_path], tmp_path, store, dry_run=True, image_limit=20, extract=_extract
    )
    assert dry["summary"] == {
        "candidates": 1,
        "planned": 1,
        "applied": 0,
        "failed": 0,
        "halted": False,
    }
    assert not (tmp_path / "figures/paper/fig-01.png").exists()

    applied = restore_missing_pdf_images(
        [note_path], tmp_path, store, dry_run=False, image_limit=20, extract=_extract
    )
    assert applied["summary"]["applied"] == 1
    assert (tmp_path / "figures/paper/fig-01.png").read_bytes() == b"second"

    repeated = restore_missing_pdf_images(
        [note_path], tmp_path, store, dry_run=False, image_limit=20, extract=_extract
    )
    assert repeated["summary"]["candidates"] == 0
    assert repeated["summary"]["applied"] == 0


def test_existing_mismatch_fails_closed_before_writing(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    note_path = _note(tmp_path, pdf)
    rows = _rows(tmp_path, note_path)
    (tmp_path / "figures/paper/fig-00.png").write_bytes(b"wrong")

    result = restore_missing_pdf_images(
        [note_path], tmp_path, FakeStore(rows), dry_run=False, extract=_extract
    )

    assert result["summary"]["failed"] == 1
    assert result["summary"]["halted"] is True
    assert not (tmp_path / "figures/paper/fig-01.png").exists()
    assert result["items"][0]["reason"] == "existing_file_mismatch"


def test_sequence_must_match_contiguous_database_rows(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    note_path = _note(tmp_path, pdf)
    rows = _rows(tmp_path, note_path)[:1]

    result = restore_missing_pdf_images(
        [note_path], tmp_path, FakeStore(rows), dry_run=True, extract=_extract
    )

    assert result["summary"]["failed"] == 1
    assert result["items"][0]["reason"] == "sequence_row_count_mismatch"


def test_explicit_source_pdf_override_avoids_unavailable_file_provider_path(tmp_path):
    original = tmp_path / "unavailable.pdf"
    note_path = _note(tmp_path, original)
    rows = _rows(tmp_path, note_path)
    local_copy = tmp_path / "local-copy.pdf"
    local_copy.write_bytes(b"pdf")

    result = restore_missing_pdf_images(
        [note_path],
        tmp_path,
        FakeStore(rows),
        dry_run=True,
        source_pdfs=[str(local_copy)],
        extract=_extract,
    )

    assert result["summary"]["planned"] == 1
    assert result["items"][0]["pdf_path"] == str(local_copy)


def test_mcp_tool_exposes_bounded_dry_run_schema():
    from mcp_second_brain import server

    tools = asyncio.run(server.mcp.list_tools())
    matches = [tool for tool in tools if tool.name == "restore_missing_pdf_images"]
    assert len(matches) == 1
    schema = matches[0].inputSchema["properties"]
    assert schema["dry_run"]["default"] is True
    assert schema["note_limit"]["maximum"] == 20
    assert schema["image_limit"]["maximum"] == 20
