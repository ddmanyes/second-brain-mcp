"""Pin the docs to the code they describe.

Everything here is currently consistent — these tests exist to keep it that way.
The failure they guard against is the one this codebase keeps hitting: a declared
interface drifts away from the actual behaviour and nothing complains, because
the mismatch degrades silently instead of raising.

Precedents, all silent until a human tripped over them:
  - the wheel never packaged AGENTS.md, so remote agents got a placeholder manual
  - `mark_note_status` rejected the very decision lifecycle the spec documented
  - AGENTS.md listed a status vocabulary that contradicted itself eight lines apart

A tool that exists but is missing from the Tool Reference table is invisible to
every agent that reads the manual, which is the same class of failure.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from mcp_second_brain import server
from mcp_second_brain.server import NOTE_CONFIG, _DEFAULT_CONFIG

REPO_ROOT = Path(server.__file__).resolve().parent.parent
PACKAGE_ROOT = Path(server.__file__).resolve().parent


@pytest.fixture(scope="module")
def registered_tools() -> set[str]:
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


@pytest.fixture(scope="module")
def documented_tools() -> set[str]:
    """Tool names cited as `name(...)` inside the AGENTS.md Tool Reference table."""
    agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    table = agents_md.split("## Tool Reference")[1].split("\n---")[0]
    return set(re.findall(r"`(\w+)\s*\(", table))


def test_every_registered_tool_is_in_the_tool_reference(
    registered_tools: set[str], documented_tools: set[str]
):
    """An undocumented tool is invisible to any agent working from the manual."""
    missing = sorted(registered_tools - documented_tools)
    assert not missing, (
        f"registered but absent from the AGENTS.md Tool Reference table: {missing}. "
        f"Add a row citing it as `tool_name(args)`."
    )


def test_tool_reference_does_not_advertise_missing_tools(
    registered_tools: set[str], documented_tools: set[str]
):
    """A documented tool that does not exist sends agents after a phantom."""
    phantom = sorted(documented_tools - registered_tools)
    assert not phantom, (
        f"listed in the AGENTS.md Tool Reference table but not registered: {phantom}"
    )


# AGENTS.md asks the count to be kept in sync; both READMEs repeat it.
TOOL_COUNT_CLAIMS = (
    ("AGENTS.md", r"`server\.py`\s*\((\d+) tools"),
    ("README.md", r"Full tool reference \((\d+) tools\)"),
    ("README.zh.md", r"完整工具清單（(\d+) 個）"),
)


@pytest.mark.parametrize("filename,pattern", TOOL_COUNT_CLAIMS)
def test_documented_tool_count_matches_reality(
    filename: str, pattern: str, registered_tools: set[str]
):
    text = (REPO_ROOT / filename).read_text(encoding="utf-8")
    match = re.search(pattern, text)
    assert match, f"{filename} no longer states a tool count in the expected form"
    assert int(match.group(1)) == len(registered_tools), (
        f"{filename} claims {match.group(1)} tools but {len(registered_tools)} "
        f"are registered — update the number."
    )


def _configured_templates() -> list[str]:
    """Every template path NOTE_CONFIG can route a new note to, deduplicated."""
    paths = [tpl for _folder, tpl in NOTE_CONFIG.values()]
    paths.append(_DEFAULT_CONFIG[1])
    return sorted(set(paths))


@pytest.mark.parametrize("template", _configured_templates())
def test_every_configured_template_exists(template: str):
    """`new_note` resolves these at call time; a missing one fails only in production."""
    assert (PACKAGE_ROOT / template).is_file(), (
        f"NOTE_CONFIG routes notes to {template}, which does not exist in the package."
    )
