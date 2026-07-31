"""Vault path resolution — the single seam a caller-supplied relative path must
pass through before the server is allowed to touch it.

The invariant this module owns: *a path handed in by a caller resolves to a file
inside the vault*. Before this module existed the invariant was hand-copied into
16 tool bodies in five different shapes, and one of them (enrich_neighbor_keywords)
had silently dropped the containment half of the check — a caller-controlled
``../..`` could write outside the vault. Rules written only in CLAUDE.md are
carried by discipline; rules written as a module are carried by the code.

Callers get a resolved ``Path`` or a ``VaultPathError`` whose message is already
worded for the MCP caller — so error wording is uniform too, not per-tool prose.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["VaultPathError", "resolve_in_vault"]


class VaultPathError(ValueError):
    """A caller-supplied path escaped the vault, or the note does not exist.

    ``str(exc)`` is the message meant to be returned verbatim to the MCP caller.
    """


_ESCAPE_MSG = "Error: path must be within the vault."


def resolve_in_vault(
    vault: Path,
    rel: str,
    *,
    must_exist: bool = True,
    missing_hint: str = "",
) -> Path:
    """Resolve ``rel`` against ``vault`` and prove it stayed inside.

    Args:
        vault: vault root (need not be pre-resolved).
        rel: caller-supplied vault-relative path, e.g. ``'decisions/foo.md'``.
        must_exist: also require the resolved file to exist (default True).
            Pass False for a path being created (new note, new folder).
        missing_hint: appended to the not-found message, e.g.
            ``". Use new_note to create it."``.

    Returns:
        The resolved absolute path, guaranteed to be inside ``vault``.

    Raises:
        VaultPathError: on escape (symlinks and ``..`` are resolved first) or,
            when ``must_exist``, on a missing file.
    """
    root = Path(vault).resolve()
    full = (root / rel).resolve()
    if not full.is_relative_to(root):
        raise VaultPathError(_ESCAPE_MSG)
    if must_exist and not full.exists():
        raise VaultPathError(f"Note not found: {rel}{missing_hint}")
    return full
