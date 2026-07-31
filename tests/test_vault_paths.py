"""Tests for vault_paths.resolve_in_vault — the single containment seam.

Before this module existed the same check was hand-copied into 16 tool bodies
and one copy had silently dropped the containment half. These tests exist so
the traversal invariant is proven once, at the seam, instead of never.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_second_brain.vault_paths import VaultPathError, resolve_in_vault


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    (tmp_path / "vault" / "decisions").mkdir(parents=True)
    (tmp_path / "vault" / "decisions" / "note.md").write_text("hi", encoding="utf-8")
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    return tmp_path / "vault"


class TestInsideVault:
    def test_returns_resolved_path(self, vault):
        got = resolve_in_vault(vault, "decisions/note.md")
        assert got == (vault / "decisions" / "note.md").resolve()

    def test_interior_dotdot_that_stays_inside_is_allowed(self, vault):
        got = resolve_in_vault(vault, "decisions/../decisions/note.md")
        assert got == (vault / "decisions" / "note.md").resolve()

    def test_unresolved_vault_root_still_works(self, vault):
        """The vault root itself need not be pre-resolved."""
        got = resolve_in_vault(vault / "decisions" / "..", "decisions/note.md")
        assert got == (vault / "decisions" / "note.md").resolve()


class TestEscape:
    @pytest.mark.parametrize(
        "rel",
        [
            "../outside.md",
            "decisions/../../outside.md",
            "../../etc/passwd",
            "/etc/passwd",  # absolute path swallows the join
        ],
    )
    def test_traversal_is_rejected(self, vault, rel):
        with pytest.raises(VaultPathError) as exc:
            resolve_in_vault(vault, rel)
        assert "within the vault" in str(exc.value)

    def test_symlink_pointing_outside_is_rejected(self, vault, tmp_path):
        link = vault / "escape.md"
        link.symlink_to(tmp_path / "outside.md")
        with pytest.raises(VaultPathError):
            resolve_in_vault(vault, "escape.md")

    def test_escape_is_rejected_even_when_not_required_to_exist(self, vault):
        """must_exist=False relaxes existence, never containment."""
        with pytest.raises(VaultPathError) as exc:
            resolve_in_vault(vault, "../new-note.md", must_exist=False)
        assert "within the vault" in str(exc.value)


class TestExistence:
    def test_missing_file_raises_by_default(self, vault):
        with pytest.raises(VaultPathError) as exc:
            resolve_in_vault(vault, "decisions/nope.md")
        assert str(exc.value) == "Note not found: decisions/nope.md"

    def test_missing_hint_is_appended(self, vault):
        with pytest.raises(VaultPathError) as exc:
            resolve_in_vault(vault, "a.md", missing_hint=". Use new_note to create it.")
        assert str(exc.value) == "Note not found: a.md. Use new_note to create it."

    def test_must_exist_false_allows_a_path_being_created(self, vault):
        got = resolve_in_vault(vault, "decisions/new.md", must_exist=False)
        assert got == (vault / "decisions" / "new.md").resolve()
        assert not got.exists()
