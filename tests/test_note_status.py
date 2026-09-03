"""The note status vocabulary must agree across code, docstring, and AGENTS.md.

`mark_note_status` used to accept only {active, archived, consolidated,
archive_backup} while the Frontmatter Spec documented a decision lifecycle of
proposed → accepted → superseded. The tool therefore rejected every value the
spec told an agent to use, so decision notes could only be corrected by editing
the vault file directly — bypassing the tool that also syncs the DB.

These tests pin the three statements of that vocabulary to each other so the next
edit cannot silently reintroduce the drift.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_second_brain import server

AGENTS_MD = Path(server.__file__).resolve().parent.parent / "AGENTS.md"


def test_decision_lifecycle_is_accepted():
    """The documented decision lifecycle must actually be settable."""
    for status in ("proposed", "accepted", "superseded"):
        assert status in server.NOTE_STATUS_ALLOWED


def test_lifecycle_and_managed_sets_are_disjoint():
    """Author-facing and tool-owned statuses must not overlap."""
    assert not (server.NOTE_STATUS_LIFECYCLE & server.NOTE_STATUS_MANAGED)
    assert server.NOTE_STATUS_ALLOWED == (
        server.NOTE_STATUS_LIFECYCLE | server.NOTE_STATUS_MANAGED
    )


@pytest.mark.parametrize("status", sorted(server.NOTE_STATUS_ALLOWED))
def test_every_allowed_status_is_documented_in_docstring(status: str):
    """The tool docstring is what an agent reads; it must list what it accepts."""
    assert status in (server.mark_note_status.__doc__ or "")


@pytest.mark.parametrize("status", sorted(server.NOTE_STATUS_ALLOWED))
def test_every_allowed_status_appears_in_agents_md(status: str):
    """AGENTS.md is the spec agents are handed; it must not omit a valid value."""
    assert status in AGENTS_MD.read_text(encoding="utf-8")


def test_rejects_unknown_status_and_names_both_groups():
    """An invalid value must come back with actionable guidance, not just a refusal."""
    result = server.mark_note_status("does/not/matter.md", "bogus-status")
    assert "Invalid status" in result
    assert "proposed" in result and "archive_backup" in result
