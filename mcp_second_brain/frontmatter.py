"""Surgical frontmatter editing — the single home for "set a field in a note's YAML frontmatter".

Before this module, the read→match→replace/insert→write dance was hand-copied in four
incompatible forms (three ``_inject_*`` helpers in ``server.py`` plus ``mark_note_status``,
and a fifth in ``vault_sleep``). Each copy re-derived the block regex and each got the
replacement subtly wrong: no ``re.escape`` on the field name, backreference-unsafe string
replacement (a value containing ``\\1`` or ``&`` would corrupt), and a silent no-op when the
note had no frontmatter block (dropping the update).

``set_fields`` is the deep module that owns exactly that surgery — and *only* that surgery.

Value serialization is deliberately **not** this module's job. The vault's quoting
conventions are per-field and cannot be derived from the value (``semantic_keywords`` is a
quoted list, ``tags`` and ``related`` are bare, ``status`` is a bare scalar, ``title`` is a
quoted scalar). Callers pass the already-formatted YAML representation as a string; this
module writes it verbatim. That keeps output byte-for-byte identical to the pre-existing
writers, so migrating a caller produces zero diff on existing notes.
"""

import re
from pathlib import Path

# Matches a leading ``---\n … \n---\n`` frontmatter block. Kept identical to the regex the
# migrated callers used, so behavior is preserved for every existing note.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def set_fields(content: str, fields: dict[str, str]) -> str:
    """Set ``key: value`` lines inside ``content``'s frontmatter block and return the result.

    - ``value`` is written **verbatim** — it must already be the intended YAML representation
      (e.g. ``'"a title"'``, ``'[a, b]'``, ``'active'``). This module never quotes or escapes it.
    - An existing field's line is replaced (field name is ``re.escape``-anchored; the value is
      substituted via a callable so regex metacharacters in it are never interpreted).
    - A missing field is appended inside the block.
    - If ``content`` has no frontmatter block, one is created (total function — never a
      silent no-op).

    Pure: no I/O. Multiple fields are applied left-to-right in one pass.
    """
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        header = "---\n" + "\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n---\n\n"
        return header + content

    # Trailing newline guarantees the block-style pattern below always has a line ending to
    # anchor on, even when the target field is the last line in the frontmatter block.
    fm_text = match.group(1) + "\n"
    for key, value in fields.items():
        line = f"{key}: {value}"
        if re.search(rf"^{re.escape(key)}:", fm_text, re.MULTILINE):
            # A field may be written YAML block-style (``key:`` on its own line, followed by
            # indented ``- item`` lines) instead of inline (``key: [a, b]``). Replacing only
            # the ``key:`` line left those indented items behind as orphaned, invalid YAML —
            # this was fix-2026-08-18-update-note-破壞-block-style-related-frontmatter.
            # Match the key line *and* any trailing block-list items, so both styles collapse
            # to the single inline line callers pass in. Replace via a callable so
            # backreferences/metacharacters in ``value`` stay literal.
            block_pattern = rf"^{re.escape(key)}:[^\n]*\n(?:[ \t]+-[^\n]*\n)*"
            fm_text = re.sub(block_pattern, lambda _: line + "\n", fm_text, count=1, flags=re.MULTILINE)
        else:
            fm_text = fm_text.rstrip("\n") + f"\n{line}\n"

    fm_text = fm_text.rstrip("\n")
    return f"---\n{fm_text}\n---\n\n" + content[match.end():]


def set_fields_in_file(path: Path, fields: dict[str, str]) -> None:
    """Read ``path``, apply :func:`set_fields`, and write back only if the content changed."""
    text = path.read_text(encoding="utf-8")
    new_text = set_fields(text, fields)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
