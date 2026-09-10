from __future__ import annotations

from pathlib import Path

import pytest

from mcp_second_brain import vault_db
from mcp_second_brain.store.duckdb_store import DuckDBStore


@pytest.fixture()
def article_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault_db, "DB_PATH", tmp_path / "vault.db")
    monkeypatch.setattr(vault_db, "_schema_applied", False)
    monkeypatch.setattr(vault_db, "EMBED_AUTO_START", False)
    monkeypatch.setattr(vault_db, "DISABLE_EMBEDDING", True)
    vault = tmp_path / "vault"
    vault.mkdir()
    notes = {
        "lin.md": (
            '---\ntitle: Hair Biology\ntype: research\nstatus: active\ntags: [research]\n'
            'authors: ["Sung-Jan Lin", "Ada Lovelace"]\n'
            'author_ids: ["0000-0002-1825-0097", ""]\n'
            'doi: "10.1000/lin"\npmid: "100"\npmcid: "PMC100"\n'
            'journal: "Skin Journal"\npublication_year: 2024\n'
            'canonical_url: "https://example.test/lin"\n---\n\narticle body'
        ),
        "other-lin.md": (
            '---\ntitle: Another Lin Paper\ntype: research\nstatus: active\ntags: [research]\n'
            'authors: ["Shih-Chieh Lin"]\nauthor_ids: []\n'
            'doi: "10.1000/other"\npublication_year: 2023\n---\n\narticle body'
        ),
        "reference-only.md": (
            "---\ntitle: Reference Only\ntype: research\nstatus: active\ntags: [research]\n"
            "---\n\nReferences include Sung-Jan Lin, but he is not an author."
        ),
    }
    for name, text in notes.items():
        (vault / name).write_text(text, encoding="utf-8")
    store = DuckDBStore()
    store.sync_all(vault)
    return store


@pytest.mark.parametrize("query", ["Lin SJ", "S.-J. Lin", "Lin, Sung-Jan"])
def test_author_alias_matches_only_structured_article_authors(article_store, query):
    hits = article_store.search_articles(author=query)

    assert [hit["path"] for hit in hits] == ["lin.md"]
    assert hits[0]["matched_author"] == "Sung-Jan Lin"
    assert hits[0]["match_type"] == "alias"
    assert hits[0]["ambiguous"] is False


def test_orcid_exact_returns_the_linked_author(article_store):
    hits = article_store.search_articles(author="0000-0002-1825-0097")

    assert [hit["path"] for hit in hits] == ["lin.md"]
    assert hits[0]["matched_author"] == "Sung-Jan Lin"
    assert hits[0]["match_type"] == "orcid"


def test_surname_only_is_explicitly_ambiguous(article_store):
    hits = article_store.search_articles(author="Lin")

    assert {hit["path"] for hit in hits} == {"lin.md", "other-lin.md"}
    assert all(hit["ambiguous"] for hit in hits)
    assert all(hit["match_type"] == "surname" for hit in hits)


def test_identifier_and_year_filters_return_bibliographic_fields(article_store):
    hits = article_store.search_articles(doi="https://doi.org/10.1000/LIN.", year=2024)

    assert hits == [
        {
            "path": "lin.md",
            "title": "Hair Biology",
            "authors": ["Sung-Jan Lin", "Ada Lovelace"],
            "author_ids": ["0000-0002-1825-0097", ""],
            "matched_author": "",
            "match_type": "identifier",
            "ambiguous": False,
            "doi": "10.1000/lin",
            "pmid": "100",
            "pmcid": "PMC100",
            "journal": "Skin Journal",
            "publication_year": 2024,
            "canonical_url": "https://example.test/lin",
        }
    ]
