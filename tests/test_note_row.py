"""Tests for the markdown → index projection.

This logic used to be duplicated in both store backends and could only be
exercised by standing up a database. Testing it here — pure, no DB — is the
point of the extraction: the cnyes ticker case, the three legal spellings of
semantic_keywords, and the large-file cut-off are table tests now.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from mcp_second_brain.note_row import (
    NoteRow,
    body_snippet,
    embed_text_for,
    normalise_keyword_list,
    parse_date,
    parse_frontmatter,
    project_note,
)

FM = "---\ntitle: Alpha\ndate: 2026-07-31\ntype: note\nstatus: active\ntags: [a, b]\n---\n\n"


def _write(vault: Path, rel: str, text: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_no_block_gives_no_keys(self):
        assert parse_frontmatter("# just a heading\n") == {}

    def test_strips_quotes_from_values(self):
        fm = parse_frontmatter('---\ntitle: "Quoted"\nsource: \'single\'\n---\n\nbody')
        assert fm["title"] == "Quoted"
        assert fm["source"] == "single"

    def test_lines_without_colon_are_ignored(self):
        fm = parse_frontmatter("---\ntitle: T\ngarbage line\n---\n\nbody")
        assert fm == {"title": "T"}

    def test_value_may_contain_colons(self):
        fm = parse_frontmatter("---\nsource: https://example.com/x\n---\n\nbody")
        assert fm["source"] == "https://example.com/x"


# ---------------------------------------------------------------------------
# semantic/neighbor keyword tolerance — three spellings exist in the vault
# ---------------------------------------------------------------------------

class TestNormaliseKeywordList:
    def test_absent_is_none(self):
        assert normalise_keyword_list("") is None
        assert normalise_keyword_list("   ") is None
        assert normalise_keyword_list(None) is None

    def test_json_array_round_trips(self):
        assert json.loads(normalise_keyword_list('["架構", "測試"]')) == ["架構", "測試"]

    def test_bare_comma_string(self):
        assert json.loads(normalise_keyword_list("架構, 測試 , ")) == ["架構", "測試"]

    def test_malformed_bracketed_degrades_to_comma_split(self):
        """Hand-edited unquoted lists must not be dropped."""
        assert json.loads(normalise_keyword_list("[架構, 測試]")) == ["架構", "測試"]

    def test_python_list_input(self):
        assert json.loads(normalise_keyword_list(["a", "b"])) == ["a", "b"]

    def test_empty_list_is_none(self):
        assert normalise_keyword_list([]) is None

    def test_cjk_is_not_escaped(self):
        assert "架構" in normalise_keyword_list('["架構"]')


# ---------------------------------------------------------------------------
# Body / embedding text
# ---------------------------------------------------------------------------

class TestBodyAndEmbedText:
    def test_snippet_excludes_frontmatter(self):
        assert body_snippet(FM + "the body").strip() == "the body"

    def test_snippet_is_capped(self):
        assert len(body_snippet(FM + "x" * 999)) == 500

    def test_embed_text_strips_code_and_urls(self):
        out = embed_text_for(FM + "prose\n```\nrm -rf /\n```\nhttps://x.com/?a=b more")
        assert "rm -rf" not in out
        assert "https://" not in out
        assert "prose" in out

    def test_embed_text_keeps_cjk_drops_fullwidth_punctuation(self):
        out = embed_text_for(FM + "架構（重要）")
        assert "架構" in out
        assert "（" not in out

    def test_markdown_link_keeps_label(self):
        assert "label" in embed_text_for(FM + "[label](https://x.com)")

    def test_embed_text_strips_references_section(self):
        """References are the cited papers' claims, not this note's own — Phase B-0."""
        text = (
            FM
            + "Main claim about pathway X.\n\n"
            + "## References\n\n1. Some Cited Paper Title, SomeJournal 2020."
        )
        out = embed_text_for(text)
        assert "Main claim about pathway X" in out
        assert "Cited Paper Title" not in out

    def test_embed_text_default_cap_exceeds_old_900_limit(self):
        """Phase B-0: max_chars moved 900 -> ~32,000 to align with bge-m3's context."""
        out = embed_text_for(FM + "x" * 5000)
        assert len(out) > 900


class TestParseDate:
    def test_iso(self):
        assert parse_date("2026-07-31") == date(2026, 7, 31)

    @pytest.mark.parametrize("bad", ["", "not-a-date", "2026-13-01", None])
    def test_invalid_is_none(self, bad):
        assert parse_date(bad) is None


# ---------------------------------------------------------------------------
# project_note
# ---------------------------------------------------------------------------

class TestProjectNote:
    def test_basic_fields(self, vault):
        f = _write(vault, "a/note.md", FM + "hello")
        row = project_note(vault, f)
        assert isinstance(row, NoteRow)
        assert row.path == "a/note.md"
        assert row.title == "Alpha"
        assert row.note_type == "note"
        assert row.status == "active"
        assert row.note_date == date(2026, 7, 31)
        assert row.tags_json == "[a, b]"
        assert row.body_snippet == "hello"

    def test_title_falls_back_to_stem(self, vault):
        f = _write(vault, "bare.md", "no frontmatter here")
        assert project_note(vault, f).title == "bare"

    def test_defaults_when_frontmatter_is_empty(self, vault):
        f = _write(vault, "bare.md", "body")
        row = project_note(vault, f)
        assert row.note_type == "note"
        assert row.status == "active"
        assert row.tags_json == "[]"  # missing tags defaults to a real empty JSON array

    def test_scalar_tags_are_wrapped_into_an_array(self, vault):
        f = _write(vault, "n.md", "---\ntitle: T\ntags: solo\n---\n\nbody")
        assert json.loads(project_note(vault, f).tags_json) == ["solo"]

    def test_cnyes_archive_prepends_tickers_to_snippet(self, vault):
        text = (
            '---\ntitle: Brief\ntype: cnyes_archive\ntickers: ["2330", "AAPL"]\n---\n\n'
            "US market table first, stock codes much later."
        )
        f = _write(vault, "news.md", text)
        row = project_note(vault, f)
        assert row.body_snippet.startswith("2330 AAPL ")
        assert len(row.body_snippet) <= 500

    def test_cnyes_malformed_tickers_fall_back_to_raw(self, vault):
        f = _write(vault, "n.md", "---\ntype: cnyes_archive\ntickers: 2330\n---\n\nbody")
        assert project_note(vault, f).body_snippet.startswith("2330 ")

    def test_large_file_hashes_whole_but_reads_head(self, vault):
        big = FM + ("A" * 40_000) + "TAIL_MARKER"
        f = _write(vault, "big.md", big)
        row = project_note(vault, f)
        assert "TAIL_MARKER" not in row.body_snippet
        # the hash must still cover the tail, or an edit past the cut-off is invisible
        f.write_text(big + "CHANGED", encoding="utf-8")
        assert project_note(vault, f).content_hash != row.content_hash

    def test_large_file_read_limit_covers_embed_text_max_chars(self, vault):
        """Regression: LARGE_FILE_READ_LIMIT must stay >= embed_text_for's max_chars.

        The old 16KB read limit truncated a large note's text before max_chars ever
        got to run, so raising max_chars to ~32,000 without also raising the read
        limit was a silent no-op for any note over the 32KB large-file threshold —
        which is most research notes (median is 81,074 chars). This marker sits
        past the old 16KB cutoff but inside the new one.
        """
        marker = "MARKER_PAST_OLD_16KB_CUTOFF"
        big = FM + ("A" * 20_000) + marker + ("B" * 15_000)
        f = _write(vault, "big.md", big)

        seen = {}

        def fake_embed(text):
            seen["text"] = text
            return [0.1]

        project_note(vault, f, embed=fake_embed)
        assert marker in seen["text"]

    def test_embedding_is_injected_and_uses_title_tags_body(self, vault):
        seen = {}

        def fake_embed(text):
            seen["text"] = text
            return [0.5, 0.5]

        f = _write(vault, "n.md", FM + "the prose")
        row = project_note(vault, f, embed=fake_embed)
        assert row.embedding == [0.5, 0.5]
        assert "Alpha" in seen["text"] and "the prose" in seen["text"]

    def test_embedding_failure_is_recorded_not_raised(self, vault, capsys):
        f = _write(vault, "n.md", FM + "body")
        row = project_note(vault, f, embed=lambda _: None)
        assert row.embedding is None
        assert "embedding failed" in capsys.readouterr().err

    def test_embedding_dim_error_is_caught(self, vault, capsys):
        def bad_embed(_):
            raise ValueError("expected 1024, got 768")

        f = _write(vault, "n.md", FM + "body")
        row = project_note(vault, f, embed=bad_embed)
        assert row.embedding is None
        assert "dim error" in capsys.readouterr().err

    def test_no_embed_callable_means_no_vector(self, vault):
        f = _write(vault, "n.md", FM + "body")
        assert project_note(vault, f).embedding is None

    def test_violations_come_from_the_injected_validator(self, vault):
        f = _write(vault, "n.md", FM + "body")
        row = project_note(vault, f, validate=lambda fm, rel: ["missing x"])
        assert json.loads(row.violations_json) == ["missing x"]

    def test_no_violations_is_none_not_empty_json(self, vault):
        f = _write(vault, "n.md", FM + "body")
        assert project_note(vault, f, validate=lambda fm, rel: []).violations_json is None

    def test_keyword_fields_are_normalised(self, vault):
        text = (
            "---\ntitle: K\nsemantic_keywords: [\"架構\", \"測試\"]\n"
            "neighbor_keywords: alpha, beta\ncluster_topic: infra\n---\n\nbody"
        )
        f = _write(vault, "k.md", text)
        row = project_note(vault, f)
        assert json.loads(row.semantic_keywords) == ["架構", "測試"]
        assert json.loads(row.neighbor_keywords) == ["alpha", "beta"]
        assert row.cluster_topic == "infra"

    def test_blank_cluster_topic_is_none(self, vault):
        f = _write(vault, "k.md", "---\ntitle: K\ncluster_topic:\n---\n\nbody")
        assert project_note(vault, f).cluster_topic is None
