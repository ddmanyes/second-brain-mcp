"""Phase 4 verification for the lab-open plan's private-note isolation.

Everything here runs against a disposable, owned PostgreSQL container
(tests/support/postgres_harness.py) — this file NEVER connects to the real
lcdda Postgres at 127.0.0.1:5434, and NEVER runs without the explicit
--run-postgres opt-in (tests/conftest.py). Embedding/reranker network calls
are disabled (DISABLE_EMBEDDING=1) so nothing here depends on a running
llama-server, and litnet_answer's Claude synthesis step is exercised only up
to query_graph (its retrieval substrate) — never the LLM call itself.

Covers plan §Phase 4 items 1-6:
  1. Two members' notes are isolated across search_notes / search_snippets /
     search_articles / find_related_notes / top_notes / search_figures /
     query_graph / read_note.
  2. Direct path access to another member's note is denied.
  3. new_note routes a member's note into their own private area.
  4. A member still sees every pre-existing shared (owner_id IS NULL) note.
  5. The RLS bypass test: sb_app + sb.actor_id alone (no Python) hides rows.
  6. Revoked/expired keys resolve to KeyState.REVOKED (401), not admin.
Item 7 (SB_MULTIUSER-unset regression) and 8 (existing suite green) are
covered by test_multiuser_regression.py and the rest of tests/ respectively.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from mcp_second_brain import server
from mcp_second_brain.identity import Identity, KeyState, _current, set_identity
from mcp_second_brain.store.migrate_multiuser import apply_multiuser_schema
from mcp_second_brain.store.postgres_store import PostgresStore

pytestmark = pytest.mark.usefixtures("_reset_store_singleton")

_TEMPLATES = {
    "decisions": "templates/decision-template.md",
    "project": "templates/project-template.md",
    "research": "templates/research-note-template.md",
    "note": "templates/note-template.md",
    "mcp": "templates/mcp-project-template.md",
}


def _apply_schema(pg) -> None:
    conn = psycopg.connect(pg.dsn(role="postgres"))
    try:
        apply_multiuser_schema(conn)
    finally:
        conn.close()
    pg.set_sb_app_password()


@contextlib.contextmanager
def _as(identity: Identity | None):
    if identity is None:
        yield
        return
    token = set_identity(identity)
    try:
        yield
    finally:
        _current.reset(token)


def _base_vec(seed: int) -> list[float]:
    """A deterministic, near-orthogonal-enough 1024-dim vector — good enough
    for find_related_notes' cosine threshold without a real embedding model."""
    return [1.0 if i == seed % 1024 else 0.0001 * ((i + seed) % 7) for i in range(1024)]


def _write_note_file(vault: Path, rel: str, title: str, note_type: str = "note") -> Path:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        f'---\ntitle: "{title}"\ndate: 2026-09-12\ntype: {note_type}\nstatus: active\ntags: []\n---\n\n'
        f"Body text for {title}.\n",
        encoding="utf-8",
    )
    return full


def _seed_note_row(
    conn, *, path: str, title: str, owner_id: str | None, body: str, embedding_seed: int | None = None
) -> None:
    # note_type='research' so search_articles' structured-metadata filter
    # (note_type='research' OR authors/doi/pmid/pmcid IS NOT NULL) matches —
    # 'research' is not in KNOWLEDGE_EXCLUDE, so search_notes/search_snippets
    # (which exclude that list) are unaffected.
    vec = _base_vec(embedding_seed) if embedding_seed is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notes (path, title, note_type, status, body_snippet, owner_id, embedding)
            VALUES (%s, %s, 'research', 'active', %s, %s, %s::vector)
            ON CONFLICT (path) DO UPDATE SET
                title = EXCLUDED.title, body_snippet = EXCLUDED.body_snippet,
                owner_id = EXCLUDED.owner_id, embedding = EXCLUDED.embedding
            """,
            [path, title, body, owner_id, str(vec) if vec else None],
        )
    conn.commit()


def _seed_figure_row(conn, *, note_path: str, owner_id: str | None, ocr_text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO figures (note_path, fig_index, ocr_text, description, owner_id)
            VALUES (%s, 0, %s, '', %s)
            ON CONFLICT (note_path, fig_index) DO UPDATE SET
                ocr_text = EXCLUDED.ocr_text, owner_id = EXCLUDED.owner_id
            """,
            [note_path, ocr_text, owner_id],
        )
    conn.commit()


@pytest.fixture
def two_members(tmp_path, clean_multiuser_postgres, monkeypatch):
    """Full multiuser fixture: schema+RLS applied, sb_app-backed PostgresStore
    wired into server.py, a temp vault with two members' notes plus one
    shared note, seeded both on disk (so _vault_path/read_note work) and in
    Postgres (so RLS is exercised) without any embedding-server dependency.
    """
    pg = clean_multiuser_postgres
    _apply_schema(pg)
    monkeypatch.setenv("SB_MULTIUSER", "1")
    monkeypatch.setenv("DISABLE_EMBEDDING", "1")

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "templates").mkdir()
    for tmpl_rel in set(_TEMPLATES.values()):
        (vault / tmpl_rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / tmpl_rel).write_text(
            '---\ntitle: "{{title}}"\ndate: {{date}}\ntype: note\nstatus: active\ntags: []\n---\n\n',
            encoding="utf-8",
        )

    store = PostgresStore(pg.dsn(role="sb_app"))
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "VAULT", vault)

    a_uuid, b_uuid = str(uuid4()), str(uuid4())
    identity_a = Identity(user_id="member-a", role="member", user_uuid=a_uuid)
    identity_b = Identity(user_id="member-b", role="member", user_uuid=b_uuid)

    admin_conn = psycopg.connect(pg.dsn(role="postgres"))

    a_note = f"90-personal/{a_uuid}/20-areas/research/alpha-secret-topic.md"
    b_note = f"90-personal/{b_uuid}/20-areas/research/bravo-secret-topic.md"
    shared_note = "20-areas/research/shared-public-topic.md"

    _write_note_file(vault, a_note, "Alpha Secret Topic")
    _write_note_file(vault, b_note, "Bravo Secret Topic")
    _write_note_file(vault, shared_note, "Shared Public Topic")

    _seed_note_row(admin_conn, path=a_note, title="Alpha Secret Topic",
                   owner_id=a_uuid, body="alphasecrettopic unique keyword content", embedding_seed=1)
    _seed_note_row(admin_conn, path=b_note, title="Bravo Secret Topic",
                   owner_id=b_uuid, body="bravosecrettopic unique keyword content", embedding_seed=2)
    _seed_note_row(admin_conn, path=shared_note, title="Shared Public Topic",
                   owner_id=None, body="sharedpublictopic unique keyword content", embedding_seed=3)
    _seed_figure_row(admin_conn, note_path=a_note, owner_id=a_uuid, ocr_text="alphafigureocr")
    _seed_figure_row(admin_conn, note_path=b_note, owner_id=b_uuid, ocr_text="bravofigureocr")

    # A private LitNet edge for each member + one shared edge, so query_graph's
    # structured-edge path (Path 1) has something to filter.
    graph_path = vault / ".graph" / "statements.jsonl"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"subject": "alphagene", "relation": "ACTIVATES", "object": "alphaphenotype",
                 "evidence": "alpha evidence", "note": "alpha-secret-topic"},
                {"subject": "bravogene", "relation": "ACTIVATES", "object": "bravophenotype",
                 "evidence": "bravo evidence", "note": "bravo-secret-topic"},
                {"subject": "sharedgene", "relation": "ACTIVATES", "object": "sharedphenotype",
                 "evidence": "shared evidence", "note": "shared-public-topic"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    yield {
        "pg": pg, "vault": vault, "store": store, "admin_conn": admin_conn,
        "a_uuid": a_uuid, "b_uuid": b_uuid,
        "identity_a": identity_a, "identity_b": identity_b,
        "a_note": a_note, "b_note": b_note, "shared_note": shared_note,
    }
    admin_conn.close()
    store.close()


# ---------------------------------------------------------------------------
# 1. Cross-member isolation across every listed tool
# ---------------------------------------------------------------------------

class TestCrossMemberIsolation:
    def test_search_notes(self, two_members):
        with _as(two_members["identity_a"]):
            out = server.search_notes("bravosecrettopic")
        assert "Bravo Secret Topic" not in out
        with _as(two_members["identity_a"]):
            out = server.search_notes("alphasecrettopic")
        assert "Alpha Secret Topic" in out

    def test_search_snippets(self, two_members):
        with _as(two_members["identity_a"]):
            out = server.search_snippets("bravosecrettopic")
        assert two_members["b_note"] not in out
        assert "Bravo Secret Topic" not in out

    def test_search_articles(self, two_members):
        with _as(two_members["identity_a"]):
            result = server.search_articles(title="Bravo Secret Topic")
        assert result["count"] == 0
        with _as(two_members["identity_a"]):
            result = server.search_articles(title="Alpha Secret Topic")
        assert result["count"] == 1

    def test_search_figures(self, two_members):
        with _as(two_members["identity_a"]):
            out = server.search_figures("bravofigureocr")
        # The query string itself legitimately echoes in a "No figures found
        # matching: <query>" message — assert on the zero-results message,
        # not string containment of the query.
        assert out == "No figures found matching: bravofigureocr"
        with _as(two_members["identity_a"]):
            out = server.search_figures("alphafigureocr")
        assert "alphafigureocr" in out
        assert two_members["a_note"] in out

    def test_find_related_notes(self, two_members):
        # Bravo's embedding (seed=2) is closer to Alpha's (seed=1) than the
        # shared note's (seed=3) only by coincidence of the deterministic
        # vector construction; what matters is A must never see B's path
        # in the result regardless of similarity ranking.
        with _as(two_members["identity_a"]):
            out = server.find_related_notes(two_members["a_note"], limit=10, threshold=0.0)
        assert two_members["b_note"] not in out
        assert "Bravo Secret Topic" not in out

    def test_top_notes(self, two_members):
        admin_conn = two_members["admin_conn"]
        with admin_conn.cursor() as cur:
            cur.execute(
                "UPDATE notes SET last_accessed = now(), access_count = 5 WHERE path = ANY(%s)",
                [[two_members["a_note"], two_members["b_note"], two_members["shared_note"]]],
            )
        admin_conn.commit()
        with _as(two_members["identity_a"]):
            out_recency = server.top_notes(by="recency", limit=20)
            out_score = server.top_notes(by="score", limit=20)
        for out in (out_recency, out_score):
            assert "Bravo Secret Topic" not in out
            assert "Alpha Secret Topic" in out
            assert "Shared Public Topic" in out

    def test_search_grouped(self, two_members):
        # Same vault_db-bypasses-Postgres/RLS bug as find_related_notes/
        # top_notes above (see server.py's search_grouped comment) — before
        # the fix this always queried the local DuckDB index (empty/stale in
        # this Postgres-backed fixture) instead of going through _store, so
        # it would either see nothing or, worse on a real deployment with a
        # populated DuckDB file, leak every member's private note.
        with _as(two_members["identity_a"]):
            out = server.search_grouped("bravosecrettopic")
        assert "Bravo Secret Topic" not in out
        with _as(two_members["identity_a"]):
            out = server.search_grouped("alphasecrettopic")
        assert "Alpha Secret Topic" in out

    def test_query_graph_structured_edges(self, two_members):
        with _as(two_members["identity_a"]):
            out = server.query_graph("gene", mode="edges")
        assert "bravogene" not in out
        assert "alphagene" in out
        assert "sharedgene" in out  # shared edges visible to everyone

    def test_read_note_denies_foreign_private_path(self, two_members):
        with _as(two_members["identity_a"]):
            out = server.read_note(two_members["b_note"])
        assert "must be within the vault" in out

    def test_read_note_allows_own_and_shared(self, two_members):
        with _as(two_members["identity_a"]):
            own = server.read_note(two_members["a_note"])
            shared = server.read_note(two_members["shared_note"])
        assert "Alpha Secret Topic" in own
        assert "Shared Public Topic" in shared


# ---------------------------------------------------------------------------
# 2. Direct path access denial (already exercised above; a focused unit
#    version at the visibility layer, independent of any tool body)
# ---------------------------------------------------------------------------

def test_direct_foreign_private_path_denied_at_vault_path(two_members):
    from mcp_second_brain.vault_paths import VaultPathError

    with _as(two_members["identity_a"]):
        with pytest.raises(VaultPathError):
            server._vault_path(two_members["b_note"])


# ---------------------------------------------------------------------------
# 3. new_note routes a member into their own private area
# ---------------------------------------------------------------------------

def test_new_note_redirects_member_into_own_private_area(two_members):
    with _as(two_members["identity_a"]):
        result = server.new_note("coding", "My Member Note")
    assert result.startswith("Created:")
    created_rel = result.split("Created: ", 1)[1].split(" ", 1)[0]
    assert created_rel.startswith(f"90-personal/{two_members['a_uuid']}/")
    assert not created_rel.startswith("20-areas/")


def test_new_note_reader_and_writer_are_unaffected(two_members, monkeypatch):
    # writer keeps writing to the shared classification folder — the
    # redirect is 'member'-specific, not a general behaviour change.
    monkeypatch.setenv("SB_RBAC_ENFORCE", "0")
    writer = Identity(user_id="writer-1", role="writer")
    with _as(writer):
        result = server.new_note("coding", "Writer Note")
    assert result.startswith("Created:")
    created_rel = result.split("Created: ", 1)[1].split(" ", 1)[0]
    assert created_rel.startswith("20-areas/coding/")


# ---------------------------------------------------------------------------
# 4. member reads all pre-existing shared content
# ---------------------------------------------------------------------------

def test_member_sees_all_shared_notes_via_index_stats(two_members):
    with _as(two_members["identity_a"]):
        stats = two_members["store"].db_stats()
    # owner_id IS NULL (shared) rows + A's own row are visible; B's is not.
    assert stats["total_notes"] == 2


# ---------------------------------------------------------------------------
# 5. RLS bypass test — raw SQL, no Python decision at all
# ---------------------------------------------------------------------------

def test_rls_enforced_by_postgres_itself_not_by_python(two_members):
    pg = two_members["pg"]
    total_as_postgres = None
    with psycopg.connect(pg.dsn(role="postgres")) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM notes")
        total_as_postgres = cur.fetchone()[0]

    with psycopg.connect(pg.dsn(role="sb_app")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('sb.actor_id', %s, false), set_config('sb.actor_is_admin', 'off', false)",
            [two_members["a_uuid"]],
        )
        cur.execute("SELECT count(*) FROM notes")
        total_as_member_a = cur.fetchone()[0]
        cur.execute("SELECT path FROM notes")
        visible_paths = {row[0] for row in cur.fetchall()}

    assert total_as_postgres == 3  # superuser bypasses RLS entirely
    assert total_as_member_a == 2  # A's own + shared, B's is hidden by Postgres alone
    assert two_members["b_note"] not in visible_paths
    assert two_members["a_note"] in visible_paths
    assert two_members["shared_note"] in visible_paths


def test_rls_admin_flag_sees_everything(two_members):
    pg = two_members["pg"]
    with psycopg.connect(pg.dsn(role="sb_app")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('sb.actor_id', '', false), set_config('sb.actor_is_admin', 'on', false)"
        )
        cur.execute("SELECT count(*) FROM notes")
        assert cur.fetchone()[0] == 3


def test_sb_app_role_lacks_bypassrls_and_superuser(two_members):
    """The precondition the whole plan calls out: if sb_app could bypass RLS,
    every policy above would silently do nothing."""
    pg = two_members["pg"]
    with psycopg.connect(pg.dsn(role="postgres")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'sb_app'"
        )
        rolsuper, rolbypassrls = cur.fetchone()
    assert rolsuper is False
    assert rolbypassrls is False


# ---------------------------------------------------------------------------
# 6. Revoked / expired keys
# ---------------------------------------------------------------------------

class TestKeyRevocationAndExpiry:
    def test_revoked_key_is_keystate_revoked(self, two_members):
        store = two_members["store"]
        store.register_api_key("deadbeef" * 8, "someone", "member", user_uuid=two_members["a_uuid"])
        store.revoke_api_key("deadbeef" * 8)
        assert store.get_identity_for_key("deadbeef" * 8) is KeyState.REVOKED

    def test_expired_key_is_keystate_revoked_not_admin(self, two_members):
        store = two_members["store"]
        key_hash = "0123abcd" * 8
        store.register_api_key(key_hash, "someone", "member", user_uuid=two_members["a_uuid"])
        with two_members["admin_conn"].cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET expires_at = %s WHERE key_hash = %s",
                [datetime.now(timezone.utc) - timedelta(days=1), key_hash],
            )
        two_members["admin_conn"].commit()
        result = store.get_identity_for_key(key_hash)
        assert result is KeyState.REVOKED  # never falls through to env-admin fallback

    def test_unexpired_key_resolves_to_member_identity_with_uuid(self, two_members):
        store = two_members["store"]
        key_hash = "fedcba98" * 8
        store.register_api_key(
            key_hash, "someone", "member", user_uuid=two_members["a_uuid"], expires_days=30
        )
        identity = store.get_identity_for_key(key_hash)
        assert isinstance(identity, Identity)
        assert identity.role == "member"
        assert identity.user_uuid == two_members["a_uuid"]
        assert identity.credential_id == key_hash
        from mcp_second_brain.intake_identity import IntakeIdentityBridge
        from types import SimpleNamespace
        actor = SimpleNamespace(user_id=identity.user_uuid, credential_id=key_hash, admin=False)
        bridge = IntakeIdentityBridge(store.get_identity_for_key)
        assert bridge.verify(actor)
        store.revoke_api_key(key_hash)
        assert not bridge.verify(actor)


def test_managed_article_metadata_index_is_shared_without_embedding(two_members, monkeypatch):
    from mcp_second_brain.store import postgres_store
    monkeypatch.setattr(postgres_store._vdb, "embed_text", lambda _: pytest.fail("metadata commit called a model"))
    vault, store = two_members["vault"], two_members["store"]
    path = vault / "20-areas/research/2026_Test_Shared.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\ntitle: "Synthetic shared article"\ntype: article\ntags: [research]\n---\n\nSynthetic evidence.')
    with _as(two_members["identity_a"]):
        assert store.index_shared_article_metadata(vault, path) is True
    with _as(two_members["identity_b"]):
        with store._conn() as conn:
            row = conn.execute("SELECT owner_id, title FROM notes WHERE path=%s", [path.relative_to(vault).as_posix()]).fetchone()
        assert row == (None, "Synthetic shared article")
    with _as(two_members["identity_a"]):
        with pytest.raises(ValueError):
            store.index_shared_article_metadata(vault, vault / two_members["a_note"])
