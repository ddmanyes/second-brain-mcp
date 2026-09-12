"""visibility.py — the single place that decides who can see or write what.

Lab-open plan (2026-09-12): lcdda goes from "1-2 pilot users, whole vault
shared" to "lab-wide, each member's own notes private to them". This module
is the *only* place that private-note visibility is decided; every other
module (server.py, postgres_store.py) calls into it rather than re-deriving
the rule. See the plan's "三層防護" table — this module backs L2 (file path)
and L3 (namespace); L1 (Postgres RLS, postgres_rls_schema.sql) is independent
and enforces the same `owner_id IS NULL OR owner_id = sb.actor_id` rule at the
database layer so a caller that reaches Postgres by any path neither this
module nor server.py anticipated is still contained.

Role x visibility (role table from the plan):

    role    | shared area | own private area | someone else's private area
    --------|-------------|-------------------|------------------------------
    reader  | read        | (none)            | (none)
    member  | read        | read+write        | (none)
    writer  | read+write  | read+write        | (none)
    admin   | read+write  | read+write        | read (audit only)

Private notes live under `90-personal/<user-uuid>/` — the folder segment
right after `90-personal` IS the owner's canonical user_uuid (from
lab_identity_invitations / lab_person_profiles, the EP lab-access registry —
see identity.Identity.user_uuid), not a human-chosen slug. That keeps L1
(Postgres owner_id, a UUID column) and L3 (this module's path parsing) using
literally the same value with no separate mapping table that could drift.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Optional

from uuid import UUID

from .identity import Identity
from .vault_paths import VaultPathError

__all__ = [
    "PRIVATE_ROOT_DIR",
    "multiuser_enabled",
    "slugify_user",
    "private_root",
    "owner_of_path",
    "can_read",
    "can_write",
    "assert_visible",
    "foreign_private_note_stems",
]

PRIVATE_ROOT_DIR = "90-personal"

_ESCAPE_MSG = "Error: path must be within the vault."
_MULTIUSER_ENV = "SB_MULTIUSER"


def multiuser_enabled() -> bool:
    """Single definition of the SB_MULTIUSER gate (also read by
    postgres_store.py) — :9100 (personal sb) and :9106 (lcdda-harvest) leave
    this unset and must be completely unaffected by anything in this module.
    """
    return os.environ.get(_MULTIUSER_ENV, "").strip().lower() in ("1", "true", "yes")


def slugify_user(identity: Identity) -> str:
    """The private-folder segment for this identity: their canonical user_uuid.

    Only 'member' (and, for admin/writer's own optional private notes, those
    roles too) ever calls this — and per the lab-open plan every such
    identity was created through EP's roster/activation flow, which always
    sets user_uuid. A member/writer/admin identity with no user_uuid is a
    bug upstream (auth.py or register_api_key), not something to paper over
    with a fallback slug that would silently disagree with the Postgres
    owner_id column (a UUID, not an arbitrary string).
    """
    if not identity.user_uuid:
        raise VaultPathError(
            "Error: identity has no user_uuid — cannot resolve a private area."
        )
    try:
        return str(UUID(identity.user_uuid))
    except (ValueError, AttributeError, TypeError):
        raise VaultPathError("Error: identity.user_uuid is not a valid UUID.") from None


def private_root(identity: Identity) -> str:
    """Vault-relative folder for this identity's private notes, e.g.
    '90-personal/3fa85f64-5717-4562-b3fc-2c963f66afa6'."""
    return f"{PRIVATE_ROOT_DIR}/{slugify_user(identity)}"


def owner_of_path(rel: str) -> Optional[str]:
    """Return the owning user_uuid encoded in a vault-relative path, or None
    if the path is outside 90-personal/ (i.e. shared).

    Raises VaultPathError if the path is under 90-personal/ but its owner
    segment is not a syntactically valid UUID — fail closed rather than
    silently treating a malformed private path as shared (owner_id=NULL)
    or as some other person's folder.
    """
    normalised = rel.replace("\\", "/")
    parts = PurePosixPath(normalised).parts
    if not parts or parts[0] != PRIVATE_ROOT_DIR:
        return None
    if len(parts) < 2 or not parts[1]:
        raise VaultPathError(_ESCAPE_MSG)
    try:
        return str(UUID(parts[1]))
    except ValueError:
        raise VaultPathError(_ESCAPE_MSG) from None


def _slug_or_none(identity: Identity) -> Optional[str]:
    """Like slugify_user, but None (not a raise) for an identity that owns no
    private area at all (no/invalid user_uuid) — e.g. the env-key admin
    fallback, or a legacy writer/admin key from before this plan. Such an
    identity can never be the owner of any 90-personal/<uuid>/ path, which is
    a plain 'no' for can_read/can_write's comparisons, not an error."""
    try:
        return slugify_user(identity)
    except VaultPathError:
        return None


def can_read(rel: str, identity: Optional[Identity]) -> bool:
    """Whether `identity` may read the note at vault-relative path `rel`.

    identity=None (stdio/dev, no auth middleware installed) always passes —
    consistent with the rest of the codebase's "auth is opt-in" doctrine
    (identity.check_write_permission / check_admin_permission do the same).
    This is what keeps SB_MULTIUSER-unset deployments (:9100, :9106)
    unaffected: without auth there is no identity to compare against, so
    every path is visible exactly as it always was.
    """
    owner = owner_of_path(rel)
    if owner is None:
        return True  # shared area — visible to everyone (role table: all 4 rows read shared)
    if identity is None:
        return False  # an authenticated-only concept; no identity means no private access
    if identity.is_admin():
        return True  # admin audit read of anyone's private notes
    if identity.role not in ("member", "writer"):
        return False  # reader's own-private cell is "(none)" in the role table
    return _slug_or_none(identity) == owner


def can_write(rel: str, identity: Optional[Identity]) -> bool:
    """Whether `identity` may write the note at vault-relative path `rel`.

    Deliberately narrow: this only adds the NEW restriction (member confined
    to their own 90-personal/ area; nobody but the owner — not even admin —
    writes into someone else's private area). Shared-area writer/reader/admin
    authorization is unchanged and stays owned by identity.check_write_permission
    (its SB_RBAC_ENFORCE audit-vs-enforce toggle is a separate, already-shipped
    concern this module must not re-decide or it would silently change that
    behaviour for SB_MULTIUSER-unset deployments too).
    """
    owner = owner_of_path(rel)
    if identity is None:
        return True  # unauthenticated — unaffected, existing behaviour
    if identity.role == "member":
        # member may ONLY write inside their own private area — never shared,
        # never someone else's private area. This is what actually confines a
        # member's write despite check_write_permission letting them attempt
        # any write tool (see identity.py's can_write() docstring).
        return owner is not None and _slug_or_none(identity) == owner
    if owner is None:
        return True  # shared area — deferred to check_write_permission, unchanged
    # A private-area target: only its own owner may write it. Admin's "audit"
    # privilege from the role table is read-only, so admin does not get a
    # bypass here either.
    return _slug_or_none(identity) == owner


def assert_visible(rel: str, identity: Optional[Identity], *, for_write: bool = False) -> None:
    """Raise VaultPathError if `identity` may not access `rel`.

    The single call _vault_path() makes (server.py) so every one of its 24+
    call sites gets this check for free, without each tool re-deriving it.

    Gated on multiuser_enabled(): with SB_MULTIUSER unset (:9100, :9106) this
    is a hard no-op regardless of vault contents — the regression guarantee
    does not depend on those vaults happening to have no 90-personal/ folder,
    it is enforced here explicitly.
    """
    if not multiuser_enabled():
        return
    ok = can_write(rel, identity) if for_write else can_read(rel, identity)
    if not ok:
        raise VaultPathError(_ESCAPE_MSG)


def foreign_private_note_stems(vault_root, identity: Optional[Identity]) -> frozenset:
    """Filename stems (no extension) of every note under 90-personal/ that is
    NOT visible to `identity` — used to filter the shared LitNet edge store
    (.graph/statements.jsonl), whose 'note' field is just a filename stem
    (see server.py's _load_edges()).

    Defence-in-depth for a source outside this repo (lcdda-ingest's
    extract_statements.py should itself skip 90-personal/ at extraction
    time — this repo does not own that pipeline). Filtering at READ time
    means even a statement wrongly promoted from a private note is never
    served back through query_graph/litnet_answer to anyone but its owner.

    A no-op (empty set) when multiuser is disabled or 90-personal/ does not
    exist, so :9100/:9106 and any lcdda deployment without private notes yet
    pay no cost and see no behaviour change.
    """
    if not multiuser_enabled():
        return frozenset()
    root = Path(vault_root) / PRIVATE_ROOT_DIR
    if not root.is_dir():
        return frozenset()
    if identity is not None and identity.is_admin():
        return frozenset()  # admin audits everything, including LitNet provenance
    own_root: Optional[Path] = None
    if identity is not None and identity.role in ("member", "writer") and identity.user_uuid:
        try:
            own_root = (Path(vault_root) / private_root(identity)).resolve()
        except VaultPathError:
            own_root = None
    stems: set[str] = set()
    for md_file in root.rglob("*.md"):
        resolved = md_file.resolve()
        if own_root is not None and own_root in resolved.parents:
            continue  # inside identity's own private folder — not "foreign"
        stems.add(md_file.stem)
    return frozenset(stems)
