"""Unit tests for visibility.py's pure decision functions, with
SB_MULTIUSER=1 (the only mode in which they do anything — see
test_multiuser_regression.py for the unset-mode no-op guarantee).
"""

from __future__ import annotations

import pytest

from mcp_second_brain import visibility
from mcp_second_brain.identity import Identity
from mcp_second_brain.vault_paths import VaultPathError

A = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
B = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _multiuser_on(monkeypatch):
    monkeypatch.setenv("SB_MULTIUSER", "1")


def member(uuid_str=A):
    return Identity(user_id="m", role="member", user_uuid=uuid_str)


def writer(uuid_str=None):
    return Identity(user_id="w", role="writer", user_uuid=uuid_str)


def reader():
    return Identity(user_id="r", role="reader")


def admin():
    return Identity(user_id="admin", role="admin")


class TestSlugifyAndPrivateRoot:
    def test_slugify_returns_canonical_uuid(self):
        assert visibility.slugify_user(member(A)) == A

    def test_private_root_shape(self):
        assert visibility.private_root(member(A)) == f"90-personal/{A}"

    def test_slugify_raises_without_user_uuid(self):
        with pytest.raises(VaultPathError):
            visibility.slugify_user(member(None))

    def test_slugify_raises_on_malformed_user_uuid(self):
        bad = Identity(user_id="m", role="member", user_uuid="not-a-uuid")
        with pytest.raises(VaultPathError):
            visibility.slugify_user(bad)


class TestOwnerOfPath:
    def test_shared_path_has_no_owner(self):
        assert visibility.owner_of_path("20-areas/research/paper.md") is None

    def test_private_path_owner_is_the_uuid_segment(self):
        assert visibility.owner_of_path(f"90-personal/{A}/note.md") == A

    def test_malformed_owner_segment_raises(self):
        with pytest.raises(VaultPathError):
            visibility.owner_of_path("90-personal/not-a-uuid/note.md")

    def test_bare_90_personal_with_no_segment_raises(self):
        with pytest.raises(VaultPathError):
            visibility.owner_of_path("90-personal")

    def test_backslash_normalised_like_forward_slash(self):
        assert visibility.owner_of_path(f"90-personal\\{A}\\note.md") == A


class TestCanRead:
    def test_everyone_reads_shared(self):
        for identity in (member(A), writer(), reader(), admin(), None):
            assert visibility.can_read("20-areas/x.md", identity) is True

    def test_owner_reads_own_private(self):
        assert visibility.can_read(f"90-personal/{A}/x.md", member(A)) is True

    def test_member_cannot_read_someone_elses_private(self):
        assert visibility.can_read(f"90-personal/{B}/x.md", member(A)) is False

    def test_writer_cannot_read_someone_elses_private(self):
        assert visibility.can_read(f"90-personal/{B}/x.md", writer(A)) is False

    def test_reader_cannot_read_even_their_own_shaped_private_path(self):
        # readers never get a private area per the role table's "(none)" cell
        weird = Identity(user_id="r", role="reader", user_uuid=A)
        assert visibility.can_read(f"90-personal/{A}/x.md", weird) is False

    def test_admin_audits_anyones_private(self):
        assert visibility.can_read(f"90-personal/{B}/x.md", admin()) is True

    def test_unauthenticated_cannot_read_any_private(self):
        assert visibility.can_read(f"90-personal/{A}/x.md", None) is False


class TestCanWrite:
    def test_member_can_write_own_private(self):
        assert visibility.can_write(f"90-personal/{A}/x.md", member(A)) is True

    def test_member_cannot_write_shared(self):
        assert visibility.can_write("20-areas/x.md", member(A)) is False

    def test_member_cannot_write_someone_elses_private(self):
        assert visibility.can_write(f"90-personal/{B}/x.md", member(A)) is False

    def test_writer_can_write_shared(self):
        assert visibility.can_write("20-areas/x.md", writer()) is True

    def test_writer_can_write_own_private(self):
        assert visibility.can_write(f"90-personal/{A}/x.md", writer(A)) is True

    def test_admin_cannot_write_someone_elses_private_audit_is_read_only(self):
        assert visibility.can_write(f"90-personal/{B}/x.md", admin()) is False

    def test_admin_can_write_shared(self):
        assert visibility.can_write("20-areas/x.md", admin()) is True

    def test_unauthenticated_write_is_unaffected(self):
        assert visibility.can_write(f"90-personal/{A}/x.md", None) is True
        assert visibility.can_write("20-areas/x.md", None) is True


class TestAssertVisible:
    def test_raises_vaultpatherror_on_denial(self):
        with pytest.raises(VaultPathError):
            visibility.assert_visible(f"90-personal/{B}/x.md", member(A))

    def test_does_not_raise_when_allowed(self):
        visibility.assert_visible(f"90-personal/{A}/x.md", member(A))
        visibility.assert_visible("20-areas/x.md", member(A))

    def test_for_write_uses_write_semantics(self):
        with pytest.raises(VaultPathError):
            visibility.assert_visible("20-areas/x.md", member(A), for_write=True)
        visibility.assert_visible("20-areas/x.md", member(A), for_write=False)


class TestForeignPrivateNoteStems:
    def test_admin_sees_nothing_excluded(self, tmp_path):
        _make(tmp_path, A, "one.md")
        _make(tmp_path, B, "two.md")
        assert visibility.foreign_private_note_stems(tmp_path, admin()) == frozenset()

    def test_member_excludes_others_includes_own(self, tmp_path):
        _make(tmp_path, A, "mine.md")
        _make(tmp_path, B, "theirs.md")
        excluded = visibility.foreign_private_note_stems(tmp_path, member(A))
        assert "theirs" in excluded
        assert "mine" not in excluded

    def test_no_90_personal_dir_returns_empty(self, tmp_path):
        assert visibility.foreign_private_note_stems(tmp_path, member(A)) == frozenset()

    def test_unauthenticated_excludes_all_private_stems(self, tmp_path):
        _make(tmp_path, A, "one.md")
        excluded = visibility.foreign_private_note_stems(tmp_path, None)
        assert "one" in excluded


def _make(vault, owner_uuid, filename) -> None:
    d = vault / "90-personal" / owner_uuid
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text("body", encoding="utf-8")
