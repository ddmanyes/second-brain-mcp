"""Opt-in disposable-PostgreSQL acceptance for targeted article enrichment."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest

from mcp_second_brain import server, vault_db
from mcp_second_brain.article_attribution import commit_shared_article
from mcp_second_brain.article_enrichment import EMBEDDING_DIM, enrich_shared_article
from mcp_second_brain.identity import Identity, _current, set_identity
from mcp_second_brain.note_row import project_note
from mcp_second_brain.store.migrate_multiuser import apply_multiuser_schema
from mcp_second_brain.store.postgres_store import PostgresStore

pytestmark = pytest.mark.usefixtures("_reset_store_singleton")


def _vec(seed: int) -> list[float]:
    return [1.0 if index == seed else 0.0 for index in range(EMBEDDING_DIM)]


@contextmanager
def _as(identity):
    token = set_identity(identity)
    try:
        yield
    finally:
        _current.reset(token)


def _migrate(pg) -> None:
    with psycopg.connect(pg.dsn(role="postgres")) as connection:
        apply_multiuser_schema(connection)
    pg.set_sb_app_password()


def test_enriched_managed_article_is_readable_by_another_member(
    tmp_path, clean_multiuser_postgres, monkeypatch
):
    pg = clean_multiuser_postgres
    _migrate(pg)
    monkeypatch.setenv("SB_MULTIUSER", "1")
    monkeypatch.setattr(vault_db, "DISABLE_EMBEDDING", True)
    monkeypatch.setattr(vault_db, "EMBED_AUTO_START", False)

    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "20-areas/research/managed-targeted-enrichment.md"
    prefix = "opening evidence " * 80
    tail = "distaltailmarker appears only in the final article section"
    content = (
        '---\ntitle: "Managed Targeted Enrichment"\ntype: article\n'
        'status: active\ntags: [research]\n---\n\n'
        f"{prefix}\n\n{tail}\n"
    )
    a_uuid, b_uuid = str(uuid4()), str(uuid4())
    member_a = Identity("member-a", "member", a_uuid, "a" * 64)
    member_b = Identity("member-b", "member", b_uuid, "b" * 64)
    store = PostgresStore(pg.dsn(role="sb_app"), min_size=0, max_size=3)
    admin = psycopg.connect(pg.dsn(role="postgres"))
    try:
        with _as(member_a):
            committed = commit_shared_article(
                vault,
                rel,
                content,
                member_a,
                source="https://example.org/managed-targeted-enrichment",
                index=lambda path: store.index_shared_article_metadata(vault, path),
            )
        assert committed.index_status == "complete"
        path = vault / committed.path
        expected_hash = project_note(vault, path).content_hash

        result = enrich_shared_article(
            store,
            vault,
            committed.path,
            expected_hash=expected_hash,
            authorize=lambda: member_a,
            embed=lambda _: _vec(1),
            chunk_embed=lambda body: [
                (body[:400], _vec(2)),
                (tail, _vec(3)),
            ],
        )
        assert result["vectors"] == {
            "status": "complete",
            "updated": True,
            "count": 1,
        }
        assert result["chunks"] == {
            "status": "complete",
            "updated": True,
            "count": 2,
        }
        assert result["figures"]["status"] == "disabled"

        private_rel = f"90-personal/{a_uuid}/private-canary.md"
        private_path = vault / private_rel
        private_path.parent.mkdir(parents=True)
        private_path.write_text(
            '---\ntitle: "Private Canary"\ntype: note\nstatus: active\n---\n\n'
            "alphaprivatecanary",
            encoding="utf-8",
        )
        private_hash = project_note(vault, private_path).content_hash
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO notes
                    (path, title, note_type, status, content_hash, body_snippet, owner_id)
                VALUES (%s, 'Private Canary', 'note', 'active', %s,
                        'alphaprivatecanary', %s)
                """,
                [private_rel, private_hash, a_uuid],
            )
        admin.commit()

        monkeypatch.setattr(server, "_store", store)
        monkeypatch.setattr(server, "VAULT", vault)
        with _as(member_b):
            search = server.search_notes("distaltailmarker")
            article = server.read_note(committed.path)
            private_search = server.search_notes("alphaprivatecanary")
            private_read = server.read_note(private_rel)

        assert "Managed Targeted Enrichment" in search
        assert tail in article
        assert "No notes found" in private_search
        assert "must be within the vault" in private_read

        with _as(member_b), store._conn() as connection:
            note_row = connection.execute(
                "SELECT owner_id, embedding IS NOT NULL FROM notes WHERE path = %s",
                [committed.path],
            ).fetchone()
            chunks = connection.execute(
                "SELECT content_hash, owner_id FROM note_chunks "
                "WHERE note_path = %s ORDER BY chunk_idx",
                [committed.path],
            ).fetchall()
        assert note_row == (None, True)
        assert chunks == [(expected_hash, None), (expected_hash, None)]
    finally:
        admin.close()
        store.close()
