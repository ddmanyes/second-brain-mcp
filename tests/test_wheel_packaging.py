"""Packaging test — assert the built wheel actually ships what runtime needs.

Why this exists as a *separate* test from ``test_agent_instructions.py``:
those tests drive ``get_agent_instructions()`` against the repo checkout, where the
repo-root ``AGENTS.md`` satisfies the first candidate path. They therefore pass even
if the wheel packaging is broken — which is exactly the failure that shipped: the
live servers import the *installed* wheel, not the checkout, so a file missing from
the distribution degraded production while the whole suite stayed green.

This repo has now had two "it never made it into the distribution" incidents (a
missing console entry point, then a missing ``AGENTS.md``). Both were found by a
human hitting them in production. These tests inspect the built artifact itself so
the next one fails here instead.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _required_wheel_members() -> tuple[str, ...]:
    """Files that must be inside the wheel for a fresh install to behave like the
    checkout. Add to this whenever runtime resolves a path relative to the package.

    Templates are derived from NOTE_CONFIG rather than listed by hand: `new_note`
    resolves them at call time, so one that exists in the repo but never ships
    would fail only in production — the same shape as the AGENTS.md bug.
    """
    from mcp_second_brain.server import NOTE_CONFIG, _DEFAULT_CONFIG

    templates = {tpl for _folder, tpl in NOTE_CONFIG.values()}
    templates.add(_DEFAULT_CONFIG[1])
    return (
        "mcp_second_brain/AGENTS.md",
        "mcp_second_brain/server.py",
        *sorted(f"mcp_second_brain/{t}" for t in templates),
    )


REQUIRED_WHEEL_MEMBERS = _required_wheel_members()


def _build_wheel(out_dir: Path) -> Path:
    """Build a wheel into out_dir and return its path.

    Prefers `uv build` (fast, isolated); falls back to `python -m build`.
    Skips the test when neither build frontend is available rather than failing —
    a missing local toolchain is not a packaging regression.
    """
    if shutil.which("uv"):
        cmd = ["uv", "build", "--wheel", "--out-dir", str(out_dir)]
    elif importlib.util.find_spec("build") is not None:
        cmd = ["python", "-m", "build", "--wheel", "--outdir", str(out_dir)]
    else:
        pytest.skip("no wheel build frontend available (need `uv` or `build`)")

    result = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        pytest.fail(f"wheel build failed:\n{result.stdout}\n{result.stderr}")

    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel once per module."""
    return _build_wheel(tmp_path_factory.mktemp("wheel"))


@pytest.mark.parametrize("member", REQUIRED_WHEEL_MEMBERS)
def test_wheel_contains_required_member(built_wheel: Path, member: str):
    """Every runtime-resolved file must be inside the distribution.

    `AGENTS.md` is the one that actually broke: `packages = ["mcp_second_brain"]`
    excluded the repo-root copy, so `get_agent_instructions()` fell through to the
    not-found placeholder on every installed deployment.
    """
    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()
    assert member in names, (
        f"{member} is missing from the built wheel — the repo checkout still has it, "
        f"so tests that read the checkout will not catch this. Check "
        f"[tool.hatch.build.targets.wheel] / force-include in pyproject.toml."
    )


def test_wheel_packages_agents_md_with_real_content(built_wheel: Path):
    """The packaged AGENTS.md must be the real manual, not an empty placeholder.

    Guards the content, not just the path: shipping a truncated or stub file would
    satisfy the membership check while still starving remote agents of the SOP.
    """
    with zipfile.ZipFile(built_wheel) as zf:
        packaged = zf.read("mcp_second_brain/AGENTS.md").decode("utf-8")
    for marker in ("Tool Reference", "Recall ladder", "Security Rules"):
        assert marker in packaged, f"packaged AGENTS.md is missing '{marker}'"


def test_wheel_declares_console_entry_point(built_wheel: Path):
    """The `second-brain` console script must survive packaging.

    A previously shipped build lost its entry point, so assert the declaration's
    content rather than merely the presence of an entry_points.txt.
    """
    with zipfile.ZipFile(built_wheel) as zf:
        entry_point_files = [
            n for n in zf.namelist() if n.endswith("entry_points.txt")
        ]
        assert entry_point_files, "wheel declares no entry_points.txt"
        declared = zf.read(entry_point_files[0]).decode("utf-8")
    assert "second-brain" in declared, f"console script missing:\n{declared}"
    assert "mcp_second_brain.server:main" in declared, (
        f"console script points somewhere unexpected:\n{declared}"
    )
