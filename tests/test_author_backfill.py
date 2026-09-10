from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mcp_second_brain.author_backfill import (
    apply_backfill,
    plan_backfill,
    write_manifest,
)
from mcp_second_brain.note_row import FRONTMATTER_RE, parse_frontmatter


class FakeProvider:
    def __init__(self, result: dict | None):
        self.result = result
        self.calls: list[dict[str, str]] = []

    def lookup(self, **identity: str) -> dict | None:
        self.calls.append(identity)
        return self.result


def _write_note(vault: Path, relative: str, frontmatter: str, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return path


def _body_hash(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    body = FRONTMATTER_RE.sub("", text, count=1)
    return hashlib.sha256(body.encode()).hexdigest()


def test_plan_uses_only_frontmatter_identifier_and_emits_deterministic_manifest(tmp_path):
    note = _write_note(
        tmp_path,
        "20-areas/research/paper.md",
        'title: "A Study"\ntype: research\ndoi: "10.1234/ABC"\n',
        "The body mentions 10.9999/reference-only but it is not the paper identifier.\n",
    )
    provider = FakeProvider(
        {
            "title": "A Study",
            "authors": ["Sung-Jan Lin", "Ada Lovelace"],
            "author_ids": ["0000-0001-2345-6789", ""],
            "doi": "10.1234/abc",
            "pmid": "123",
            "journal": "Example Journal",
            "publication_year": 2024,
        }
    )

    manifest = plan_backfill(tmp_path, provider=provider, limit=20)

    assert provider.calls == [{"doi": "10.1234/abc"}]
    assert manifest["summary"] == {"eligible": 1, "planned": 1, "needs_review": 0}
    assert manifest["entries"] == [
        {
            "path": "20-areas/research/paper.md",
            "match_basis": "doi_exact",
            "confidence": "deterministic",
            "content_sha256": hashlib.sha256(note.read_bytes()).hexdigest(),
            "body_sha256": _body_hash(note),
            "metadata": {
                "authors": ["Sung-Jan Lin", "Ada Lovelace"],
                "author_ids": ["0000-0001-2345-6789", ""],
                "doi": "10.1234/abc",
                "pmid": "123",
                "journal": "Example Journal",
                "publication_year": 2024,
            },
        }
    ]


def test_plan_never_uses_body_or_references_as_author_evidence(tmp_path):
    _write_note(
        tmp_path,
        "30-resources/unknown.md",
        'title: "Unknown"\ntype: resource\n',
        "Sung-Jan Lin. References: doi:10.1000/reference-only\n",
    )
    provider = FakeProvider({"authors": ["Wrong Person"], "doi": "10.1000/reference-only"})

    manifest = plan_backfill(tmp_path, provider=provider)

    assert provider.calls == [{"title": "Unknown"}]
    assert manifest["summary"] == {"eligible": 1, "planned": 0, "needs_review": 1}
    assert manifest["entries"] == []


def test_plan_requires_exact_title_when_no_identifier_exists(tmp_path):
    _write_note(
        tmp_path,
        "30-resources/title.md",
        'title: "Spatial Atlas"\ntype: resource\n',
        "Body\n",
    )
    provider = FakeProvider({"title": "A Spatial Atlas", "authors": ["Sung-Jan Lin"]})

    manifest = plan_backfill(tmp_path, provider=provider)

    assert manifest["entries"] == []
    assert manifest["review"][0]["reason"] == "title_mismatch"


def test_plan_skips_notes_that_already_have_authors_and_obeys_limit(tmp_path):
    _write_note(
        tmp_path,
        "30-resources/01-existing.md",
        'title: "Existing"\ntype: resource\nauthors: ["Someone"]\n',
        "Body\n",
    )
    _write_note(
        tmp_path,
        "30-resources/02-a.md",
        'title: "A"\ntype: resource\ndoi: "10.1/a"\n',
        "Body\n",
    )
    _write_note(
        tmp_path,
        "30-resources/03-b.md",
        'title: "B"\ntype: resource\ndoi: "10.1/b"\n',
        "Body\n",
    )
    provider = FakeProvider({"title": "A", "authors": ["Author A"], "doi": "10.1/a"})

    manifest = plan_backfill(tmp_path, provider=provider, limit=1)

    assert len(provider.calls) == 1
    assert manifest["summary"]["eligible"] == 1
    assert manifest["summary"]["planned"] == 1


def test_plan_treats_an_empty_authors_array_as_missing(tmp_path):
    _write_note(
        tmp_path,
        "30-resources/empty.md",
        'title: "Empty"\ntype: resource\nauthors: []\ndoi: "10.1234/empty"\n',
        "Body\n",
    )
    provider = FakeProvider(
        {"title": "Empty", "authors": ["Sung-Jan Lin"], "doi": "10.1234/empty"}
    )

    manifest = plan_backfill(tmp_path, provider=provider)

    assert manifest["summary"]["planned"] == 1


def test_apply_preserves_body_and_indexes_written_note(tmp_path):
    note = _write_note(
        tmp_path,
        "30-resources/paper.md",
        'title: "Paper"\ntype: resource\ndoi: "10.1234/paper"\n',
        "# Paper\n\nUnchanged body.\n",
    )
    provider = FakeProvider(
        {"title": "Paper", "authors": ["Sung-Jan Lin"], "doi": "10.1234/paper"}
    )
    manifest = plan_backfill(tmp_path, provider=provider)
    original_body_hash = _body_hash(note)
    indexed: list[Path] = []

    result = apply_backfill(tmp_path, manifest, indexer=indexed.append)

    assert result == {"applied": 1, "skipped": 0, "errors": []}
    assert _body_hash(note) == original_body_hash
    fm = parse_frontmatter(note.read_text(encoding="utf-8"))
    assert json.loads(fm["authors"]) == ["Sung-Jan Lin"]
    assert fm["doi"] == "10.1234/paper"
    assert indexed == [note.resolve()]


def test_apply_refuses_changed_file_and_non_deterministic_entries(tmp_path):
    note = _write_note(
        tmp_path,
        "30-resources/paper.md",
        'title: "Paper"\ntype: resource\ndoi: "10.1234/paper"\n',
        "Body\n",
    )
    provider = FakeProvider(
        {"title": "Paper", "authors": ["Sung-Jan Lin"], "doi": "10.1234/paper"}
    )
    manifest = plan_backfill(tmp_path, provider=provider)
    note.write_text(note.read_text(encoding="utf-8") + "Changed\n", encoding="utf-8")
    manifest["entries"].append(
        {
            **manifest["entries"][0],
            "path": "30-resources/review.md",
            "confidence": "review",
        }
    )

    result = apply_backfill(tmp_path, manifest)

    assert result["applied"] == 0
    assert result["skipped"] == 2
    assert {error["reason"] for error in result["errors"]} == {
        "content_changed",
        "confidence_not_deterministic",
    }


def test_apply_rejects_manifest_over_limit(tmp_path):
    with pytest.raises(ValueError, match="at most 20"):
        apply_backfill(
            tmp_path,
            {"entries": [{"path": f"{index}.md"} for index in range(21)]},
            limit=20,
        )


def test_apply_refuses_manifest_for_a_different_vault(tmp_path):
    manifest = {"version": 1, "vault": str(tmp_path / "elsewhere"), "entries": []}

    with pytest.raises(ValueError, match="different vault"):
        apply_backfill(tmp_path, manifest)


def test_manifest_writer_is_atomic_json(tmp_path):
    destination = tmp_path / "plans" / "backfill.json"

    write_manifest(destination, {"version": 1, "entries": []})

    assert json.loads(destination.read_text(encoding="utf-8")) == {
        "version": 1,
        "entries": [],
    }
    assert not destination.with_suffix(".json.part").exists()
