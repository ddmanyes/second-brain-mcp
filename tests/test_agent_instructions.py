"""Regression test for get_agent_instructions() base-layer delivery.

Root cause this guards against: the wheel's ``[tool.hatch.build.targets.wheel]``
config only packaged ``mcp_second_brain`` and never included the repo-root
``AGENTS.md``. Every live server (sb :9100, lcdda :9104, lcdda-harvest :9106)
imports the *installed* wheel, not the repo checkout, so ``get_agent_instructions()``
silently fell through both candidate paths in server.py and returned the
"⚠️ 找不到 AGENTS.md" placeholder instead of the base layer (Tool Reference,
Recall ladder, SOP, safety rules) — for an unknown period, with no failing
test to catch it, because the personal-layer vault AGENTS.md was still
appended after the placeholder and made responses look non-empty.

Fixed by ``[tool.hatch.build.targets.wheel.force-include]`` in pyproject.toml,
which populates the second candidate path server.py already probed
(``_here.parent / "AGENTS.md"``, packaged alongside mcp_second_brain).

These tests drive the real ``get_agent_instructions()`` function against the
repo checkout (where the repo-root candidate resolves the base layer). They
won't catch a broken wheel build directly, but they pin down the contract —
no placeholder, base-layer markers present, personal layer still appended —
so a regression in the candidate-probing logic itself fails loudly here
instead of degrading silently in production again.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_second_brain import server


def test_get_agent_instructions_no_placeholder():
    """Must never fall back to the not-found placeholder."""
    result = server.get_agent_instructions()
    assert "找不到 AGENTS.md" not in result


def test_get_agent_instructions_contains_base_layer_markers():
    """Must contain markers unique to the base AGENTS.md (Tool Reference,
    Recall ladder), not just whatever personal layer got appended after it."""
    result = server.get_agent_instructions()
    assert "Recall ladder" in result
    assert "Tool Reference" in result


def test_get_agent_instructions_appends_personal_layer(tmp_path: Path):
    """Personal-layer vault AGENTS.md, when present, must still be appended
    after the base layer -- the fix must not regress the two-layer contract."""
    marker = "PERSONAL-LAYER-TEST-MARKER-4f2c9a"
    (tmp_path / "AGENTS.md").write_text(
        f"# Personal rules\n\n{marker}\n", encoding="utf-8"
    )

    original_vault = server.VAULT
    server.VAULT = tmp_path
    try:
        result = server.get_agent_instructions()
    finally:
        server.VAULT = original_vault

    assert "找不到 AGENTS.md" not in result
    assert "Recall ladder" in result, "base layer must still be present"
    assert marker in result, "personal layer must be appended after base layer"
    assert result.index("Recall ladder") < result.index(marker), (
        "base layer must come before the personal layer"
    )


def test_get_agent_instructions_no_personal_layer_when_vault_file_absent(
    tmp_path: Path,
):
    """If the vault has no AGENTS.md, only the base layer is returned --
    no crash, no stray separator."""
    original_vault = server.VAULT
    server.VAULT = tmp_path
    try:
        result = server.get_agent_instructions()
    finally:
        server.VAULT = original_vault

    assert "找不到 AGENTS.md" not in result
    assert "Recall ladder" in result
