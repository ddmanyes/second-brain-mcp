from __future__ import annotations

from contextlib import contextmanager
import json
from uuid import uuid4

import pytest

from mcp_second_brain.article_enrichment import (
    EMBEDDING_DIM,
    MAX_CHUNKS,
    enrich_shared_article,
)
from mcp_second_brain.identity import Identity
from mcp_second_brain.note_row import project_note
from mcp_second_brain.request_budget import remaining_timeout


def _vec(seed: int = 0) -> list[float]:
    return [1.0 if index == seed else 0.0 for index in range(EMBEDDING_DIM)]


class _Rows:
    def __init__(self, *, one=None, many=None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Connection:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params):
        normalized = " ".join(sql.split())
        if normalized.startswith(
            "SELECT content_hash, owner_id, embedding FROM notes"
        ):
            note = self.store.note
            return _Rows(
                one=None
                if note is None
                else (note["content_hash"], note["owner_id"], note["embedding"])
            )
        if normalized.startswith("SELECT content_hash, owner_id FROM notes"):
            note = self.store.note
            return _Rows(
                one=None
                if note is None
                else (note["content_hash"], note["owner_id"])
            )
        if normalized.startswith("UPDATE notes SET embedding"):
            vector, _, expected_hash = params
            note = self.store.note
            if (
                note is not None
                and note["content_hash"] == expected_hash
                and note["owner_id"] is None
                and note["embedding"] is None
            ):
                note["embedding"] = json.loads(vector)
                self.store.vector_writes += 1
            return _Rows()
        if normalized.startswith(
            "SELECT chunk_idx, chunk_text, content_hash, embedding, owner_id"
        ):
            return _Rows(
                many=[
                    (
                        index,
                        text,
                        content_hash,
                        embedding,
                        owner_id,
                    )
                    for index, (text, content_hash, embedding, owner_id) in enumerate(
                        self.store.chunks
                    )
                ]
            )
        raise AssertionError(f"unexpected SQL: {normalized}")

    def cursor(self):
        return _Cursor()

    def commit(self):
        self.store.commits += 1


class _Store:
    def __init__(self, content_hash: str, *, owner_id=None, embedding=None):
        self.note = {
            "content_hash": content_hash,
            "owner_id": owner_id,
            "embedding": embedding,
        }
        self.chunks: list[tuple[str, str, list[float], str | None]] = []
        self.vector_writes = 0
        self.chunk_writes = 0
        self.commits = 0

    @contextmanager
    def _conn(self, *, timeout):
        assert 0 < timeout <= 2
        yield _Connection(self)

    def _write_chunks(self, cursor, path, content_hash, chunks, owner_id=None):
        assert isinstance(cursor, _Cursor)
        assert path.startswith("20-areas/research/")
        self.chunks = [
            (text, content_hash, vector, owner_id) for text, vector in chunks
        ]
        self.chunk_writes += 1


@pytest.fixture
def article(tmp_path):
    vault = tmp_path / "vault"
    rel = "20-areas/research/targeted-enrichment.md"
    path = vault / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        '---\ntitle: "Targeted Enrichment"\ntype: article\ntags: [research]\n---\n\n'
        "Opening evidence.\n\nLate section has tailchunkkeyword.\n",
        encoding="utf-8",
    )
    (vault / ".article-identities.json").write_text(
        json.dumps(
            {
                "doi:10.1234/targeted": {
                    "path": rel,
                    "index_status": "complete",
                }
            }
        ),
        encoding="utf-8",
    )
    return vault, rel, path, project_note(vault, path).content_hash


@pytest.fixture
def member():
    return Identity("member-a", "member", str(uuid4()), "a" * 64)


def test_enriches_once_and_skips_existing_rows(article, member):
    vault, rel, _, content_hash = article
    store = _Store(content_hash)
    calls = {"authorize": 0, "embed": 0, "chunks": 0, "figures": 0}

    def authorize():
        calls["authorize"] += 1
        return member

    def embed(text):
        calls["embed"] += 1
        assert "Targeted Enrichment" in text
        assert 0 < remaining_timeout(999) <= 60
        return _vec(1)

    def chunk_embed(body):
        calls["chunks"] += 1
        assert "tailchunkkeyword" in body
        assert 0 < remaining_timeout(999) <= 60
        return [("Opening evidence.", _vec(2)), ("tailchunkkeyword", _vec(3))]

    def figures():
        calls["figures"] += 1
        raise AssertionError("figures must stay disabled")

    first = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=authorize,
        embed=embed,
        chunk_embed=chunk_embed,
        figures=figures,
    )
    assert first == {
        "vectors": {"status": "complete", "updated": True, "count": 1},
        "chunks": {"status": "complete", "updated": True, "count": 2},
        "figures": {"status": "disabled", "updated": False, "count": 0},
    }
    assert store.vector_writes == store.chunk_writes == 1
    assert calls["figures"] == 0

    second = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=authorize,
        embed=lambda _: pytest.fail("existing vector recomputed"),
        chunk_embed=lambda _: pytest.fail("existing chunks recomputed"),
    )
    assert second["vectors"] == {
        "status": "complete",
        "updated": False,
        "count": 1,
    }
    assert second["chunks"] == {
        "status": "complete",
        "updated": False,
        "count": 2,
    }
    assert store.vector_writes == store.chunk_writes == 1


def test_null_providers_keep_existing_data_pending(article, member):
    vault, rel, _, content_hash = article
    store = _Store(content_hash)
    old_chunks = [("old", "old-hash", _vec(9), None)]
    store.chunks = old_chunks.copy()

    result = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=lambda: member,
        embed=lambda _: None,
        chunk_embed=lambda _: None,
    )

    assert result["vectors"] == {
        "status": "pending",
        "code": "embedding_unavailable",
    }
    assert result["chunks"] == {
        "status": "pending",
        "code": "chunking_unavailable",
    }
    assert store.note["embedding"] is None
    assert store.chunks == old_chunks
    assert store.vector_writes == store.chunk_writes == 0


def test_rejects_invalid_vectors_and_too_many_chunks_without_writes(article, member):
    vault, rel, _, content_hash = article
    store = _Store(content_hash)

    result = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=lambda: member,
        embed=lambda _: [float("nan")] * EMBEDDING_DIM,
        chunk_embed=lambda _: [(f"chunk-{i}", _vec()) for i in range(MAX_CHUNKS + 1)],
    )

    assert result["vectors"] == {"status": "pending", "code": "invalid_vector"}
    assert result["chunks"] == {"status": "pending", "code": "invalid_chunks"}
    assert store.vector_writes == store.chunk_writes == 0


def test_reauthorizes_before_write(article, member):
    vault, rel, _, content_hash = article
    store = _Store(content_hash)
    checks = 0

    def authorize():
        nonlocal checks
        checks += 1
        return member if checks <= 2 else None

    result = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=authorize,
        embed=lambda _: _vec(),
        chunk_embed=lambda _: pytest.fail("revoked actor reached chunk provider"),
    )

    assert result["vectors"] == {
        "status": "pending",
        "code": "authorization_denied",
    }
    assert result["chunks"] == {
        "status": "pending",
        "code": "authorization_denied",
    }
    assert store.vector_writes == store.chunk_writes == 0


def test_hash_drift_after_model_call_prevents_all_writes(article, member):
    vault, rel, path, content_hash = article
    store = _Store(content_hash)

    def drift(_):
        path.write_text(path.read_text() + "changed", encoding="utf-8")
        return _vec()

    result = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=lambda: member,
        embed=drift,
        chunk_embed=lambda _: pytest.fail("drift reached chunk provider"),
    )

    assert result["vectors"] == {
        "status": "pending",
        "code": "content_hash_mismatch",
    }
    assert result["chunks"] == {
        "status": "pending",
        "code": "content_hash_mismatch",
    }
    assert store.vector_writes == store.chunk_writes == 0


def test_database_hash_cas_drift_prevents_all_writes(article, member):
    vault, rel, _, content_hash = article
    store = _Store(content_hash)

    def drift_database(_):
        store.note["content_hash"] = "0" * 32
        return _vec()

    result = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=lambda: member,
        embed=drift_database,
        chunk_embed=lambda _: pytest.fail("database drift reached chunk provider"),
    )

    assert result["vectors"] == {
        "status": "pending",
        "code": "content_hash_mismatch",
    }
    assert result["chunks"] == {
        "status": "pending",
        "code": "content_hash_mismatch",
    }
    assert store.vector_writes == store.chunk_writes == 0


def test_requires_registry_and_shared_database_row(article, member):
    vault, rel, _, content_hash = article
    store = _Store(content_hash)
    (vault / ".article-identities.json").unlink()

    missing = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=lambda: member,
        embed=lambda _: pytest.fail("unregistered article embedded"),
        chunk_embed=lambda _: pytest.fail("unregistered article chunked"),
    )
    assert missing["vectors"]["code"] == "article_not_registered"
    assert missing["chunks"]["code"] == "article_not_registered"

    (vault / ".article-identities.json").write_text(
        json.dumps({"id": {"path": rel, "index_status": "complete"}}),
        encoding="utf-8",
    )
    store.note["owner_id"] = str(uuid4())
    private = enrich_shared_article(
        store,
        vault,
        rel,
        expected_hash=content_hash,
        authorize=lambda: member,
        embed=lambda _: pytest.fail("private row embedded"),
        chunk_embed=lambda _: pytest.fail("private row chunked"),
    )
    assert private["vectors"]["code"] == "article_not_shared"
    assert private["chunks"]["code"] == "article_not_shared"
