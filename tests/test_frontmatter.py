"""Tests for frontmatter.py — the surgical frontmatter-set deep module.

These are the primary test surface for the block surgery that was previously hand-copied
(and subtly mis-implemented) across server.py and vault_sleep.py. Value serialization is a
caller concern, so every ``value`` here is already the intended verbatim YAML representation.
"""

from mcp_second_brain import frontmatter as fm


# --- existing field is replaced in place -----------------------------------------------

def test_replaces_existing_field():
    content = "---\ntitle: Old\nstatus: active\n---\n\nbody\n"
    out = fm.set_fields(content, {"status": "archived"})
    assert out == "---\ntitle: Old\nstatus: archived\n---\n\nbody\n"


def test_absent_field_is_appended_inside_block():
    content = "---\ntitle: Foo\n---\n\nbody\n"
    out = fm.set_fields(content, {"status": "active"})
    assert out == "---\ntitle: Foo\nstatus: active\n---\n\nbody\n"


def test_missing_frontmatter_block_is_created():
    content = "just a body, no frontmatter\n"
    out = fm.set_fields(content, {"status": "active"})
    assert out == "---\nstatus: active\n---\n\njust a body, no frontmatter\n"


def test_multiple_fields_one_pass():
    content = "---\ntitle: Foo\n---\n\nbody\n"
    out = fm.set_fields(content, {"neighbor_keywords": '["a", "b"]', "cluster_topic": '"x"'})
    assert 'neighbor_keywords: ["a", "b"]' in out
    assert 'cluster_topic: "x"' in out


# --- correctness bugs the old implementations had --------------------------------------

def test_value_with_regex_backreference_is_literal():
    """A value containing \\1 or & must be written verbatim, not interpreted by re.sub."""
    content = "---\nsource: old\n---\n\nbody\n"
    out = fm.set_fields(content, {"source": r'"a\1b & c"'})
    assert 'source: "a\\1b & c"' in out


def test_field_name_with_regex_metachar_is_escaped():
    content = "---\na.b: old\n---\n\nbody\n"
    out = fm.set_fields(content, {"a.b": "new"})
    assert "a.b: new" in out
    # must not have accidentally matched some other line via unescaped '.'
    assert out.count("new") == 1


def test_prefix_field_is_not_matched():
    """Setting 'status' must not clobber a 'status_detail' line."""
    content = "---\nstatus_detail: keep\n---\n\nbody\n"
    out = fm.set_fields(content, {"status": "active"})
    assert "status_detail: keep" in out
    assert "status: active" in out


def test_body_line_starting_with_field_is_untouched():
    """Regression: mark_note_status used to re.sub the whole document."""
    content = "---\nstatus: active\n---\n\nstatus: this is body text\n"
    out = fm.set_fields(content, {"status": "archived"})
    assert out == "---\nstatus: archived\n---\n\nstatus: this is body text\n"


# --- fidelity: unicode / verbatim list -------------------------------------------------

def test_unicode_value_preserved_verbatim():
    content = "---\ntitle: x\n---\n\nbody\n"
    out = fm.set_fields(content, {"title": '"中文標題"'})
    assert 'title: "中文標題"' in out


def test_wikilink_related_list_verbatim():
    content = "---\ntitle: x\n---\n\nbody\n"
    out = fm.set_fields(content, {"related": "[[[a/b]], [[c/d]]]"})
    assert "related: [[[a/b]], [[c/d]]]" in out


# --- file wrapper ----------------------------------------------------------------------

def test_set_fields_in_file_round_trip(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\nstatus: active\n---\n\nbody\n", encoding="utf-8")
    fm.set_fields_in_file(p, {"status": "archived"})
    assert p.read_text(encoding="utf-8") == "---\nstatus: archived\n---\n\nbody\n"


def test_set_fields_in_file_skips_write_when_unchanged(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("---\nstatus: active\n---\n\nbody\n", encoding="utf-8")
    mtime_before = p.stat().st_mtime_ns
    fm.set_fields_in_file(p, {"status": "active"})  # same value → no rewrite
    assert p.stat().st_mtime_ns == mtime_before
