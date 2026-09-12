"""Phase 4 item 7: with SB_MULTIUSER unset, behaviour is unchanged.

:9100 (personal sb) and :9106 (lcdda-harvest) share this wheel with :9104
(lcdda) and never set SB_MULTIUSER — every lab-open-plan code path this file
exercises must be a hard no-op for them regardless of what a vault happens to
contain (no dependency on "there's no 90-personal/ folder anyway" — see each
assertion below for why).

No Postgres needed — everything here is pure-Python behaviour of
visibility.py, identity.py and the write_tool/_vault_path seam.
"""

from __future__ import annotations

import contextlib

import pytest

from mcp_second_brain import server, visibility
from mcp_second_brain.identity import VALID_ROLES, Identity, _current, set_identity
from mcp_second_brain.vault_paths import VaultPathError


@contextlib.contextmanager
def _as(identity):
    if identity is None:
        yield
        return
    token = set_identity(identity)
    try:
        yield
    finally:
        _current.reset(token)


@pytest.fixture(autouse=True)
def _ensure_multiuser_unset(monkeypatch):
    monkeypatch.delenv("SB_MULTIUSER", raising=False)


class TestMultiuserGateItself:
    def test_multiuser_disabled_by_default(self):
        assert visibility.multiuser_enabled() is False

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
    def test_falsy_values_stay_disabled(self, monkeypatch, value):
        monkeypatch.setenv("SB_MULTIUSER", value)
        assert visibility.multiuser_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES"])
    def test_truthy_values_enable_it(self, monkeypatch, value):
        monkeypatch.setenv("SB_MULTIUSER", value)
        assert visibility.multiuser_enabled() is True


class TestAssertVisibleIsNoOpWhenDisabled:
    """assert_visible must return None for literally any path/identity
    combination when multiuser is off — including a path that LOOKS like a
    foreign private note, which is exactly the scenario a regression would
    silently break if a vault ever happened to contain such a folder."""

    def test_foreign_looking_private_path_is_not_blocked(self):
        someone_elses_uuid = "11111111-1111-1111-1111-111111111111"
        reader = Identity(user_id="reader-1", role="reader")
        # Would be denied under multiuser (reader owns no private area at all)
        # — must be a complete no-op with SB_MULTIUSER unset.
        visibility.assert_visible(f"90-personal/{someone_elses_uuid}/secret.md", reader)

    def test_admin_bypass_semantics_dont_leak_in_as_a_new_restriction(self):
        writer = Identity(user_id="writer-1", role="writer")
        visibility.assert_visible("20-areas/research/anything.md", writer, for_write=True)

    def test_malformed_90_personal_segment_does_not_raise(self):
        """owner_of_path() would raise on a non-UUID segment under
        90-personal/ when multiuser is on; assert_visible's early return must
        pre-empt that entirely when it's off, not just happen to swallow it."""
        identity = Identity(user_id="x", role="reader")
        visibility.assert_visible("90-personal/not-a-uuid-at-all/note.md", identity)


class TestForeignPrivateNoteStemsIsEmptyWhenDisabled:
    def test_empty_even_if_90_personal_exists_on_disk(self, tmp_path):
        (tmp_path / "90-personal" / "someone" / "x.md").parent.mkdir(parents=True)
        (tmp_path / "90-personal" / "someone" / "x.md").write_text("body", encoding="utf-8")
        stems = visibility.foreign_private_note_stems(tmp_path, None)
        assert stems == frozenset()


class TestVaultPathUnaffected:
    """server._vault_path() must resolve exactly as it did before this plan
    existed when SB_MULTIUSER is unset, for any identity."""

    def test_resolves_a_90_personal_shaped_path_for_a_reader(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "VAULT", tmp_path)
        target = tmp_path / "90-personal" / "someone-elses-uuid" / "note.md"
        target.parent.mkdir(parents=True)
        target.write_text("secret-shaped-but-not-actually-gated", encoding="utf-8")
        reader = Identity(user_id="reader-1", role="reader")
        with _as(reader):
            resolved = server._vault_path("90-personal/someone-elses-uuid/note.md")
        assert resolved == target

    def test_still_raises_on_real_escape_attempts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "VAULT", tmp_path)
        with pytest.raises(VaultPathError):
            server._vault_path("../../etc/passwd")


class TestNewNoteIsNotRedirectedWithoutMultiuser:
    def test_member_role_exists_but_is_never_minted_without_multiuser(self):
        """'member' is a valid role name (Identity accepts it regardless of
        env), but new_note's redirect to it is only ever reached in practice
        via a key that get_identity_for_key() resolved with role='member' —
        and that resolution only happens through lcdda's own SB_MULTIUSER=1
        auth path. This documents that boundary rather than re-testing
        new_note's redirect logic itself (covered in test_multiuser_rls.py)."""
        assert "member" in VALID_ROLES


class TestIdentityBackwardCompatibility:
    def test_identity_without_user_uuid_still_constructs(self):
        i = Identity(user_id="legacy", role="admin")
        assert i.user_uuid is None

    def test_writer_and_admin_can_write_unchanged(self):
        assert Identity(user_id="w", role="writer").can_write() is True
        assert Identity(user_id="a", role="admin").can_write() is True

    def test_reader_still_cannot_write(self):
        assert Identity(user_id="r", role="reader").can_write() is False
