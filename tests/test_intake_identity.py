from types import SimpleNamespace
from uuid import uuid4

from mcp_second_brain.identity import Identity, KeyState
from mcp_second_brain.intake_identity import IntakeIdentityBridge


def test_rechecks_revocation_role_and_owner():
    owner = str(uuid4())
    actor = SimpleNamespace(user_id=owner, credential_id="a" * 64, admin=False)
    state = [Identity("member", "member", owner)]
    bridge = IntakeIdentityBridge(lambda ref: state[0])
    assert bridge.verify(actor)
    for denied in (
        KeyState.REVOKED,
        None,
        Identity("reader", "reader", owner),
        Identity("other", "member", str(uuid4())),
    ):
        state[0] = denied
        assert not bridge.verify(actor)


def test_outage_and_invalid_reference_fail_closed():
    def unavailable(_):
        raise RuntimeError("secret payload")

    bridge = IntakeIdentityBridge(unavailable)
    assert not bridge.verify(
        SimpleNamespace(user_id=str(uuid4()), credential_id="a" * 64, admin=False)
    )
    assert not bridge.verify(
        SimpleNamespace(user_id=str(uuid4()), credential_id="raw-key", admin=False)
    )


def test_admin_flag_cannot_promote_member():
    owner = str(uuid4())
    actor = SimpleNamespace(user_id=owner, credential_id="a" * 64, admin=True)
    bridge = IntakeIdentityBridge(lambda _: Identity("member", "member", owner))
    assert not bridge.verify(actor)


def test_credential_is_not_in_identity_repr():
    assert "a" * 64 not in repr(Identity("member", "member", str(uuid4()), "a" * 64))
