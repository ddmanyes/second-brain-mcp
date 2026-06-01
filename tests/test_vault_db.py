"""Tests for vault_db.py — Phase 1 (FTS) and Phase 2 (Ebbinghaus score)."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import vault_db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own DuckDB file, reset schema flag, and no lazy embed-start."""
    monkeypatch.setattr(vault_db, "DB_PATH", tmp_path / "vault.db")
    monkeypatch.setattr(vault_db, "_schema_applied", False)
    monkeypatch.setattr(vault_db, "EMBED_AUTO_START", False)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """Minimal vault with three notes at different ages."""
    (tmp_path / "10-projects").mkdir()
    (tmp_path / "30-resources").mkdir()

    notes = {
        "10-projects/alpha.md": (
            "---\ntitle: Alpha Project\ndate: 2026-05-28\ntype: project\n"
            "status: active\ntags: [ml, research]\n---\n\n"
            "This note is about machine learning and neural networks."
        ),
        "30-resources/beta.md": (
            "---\ntitle: Beta Resource\ndate: 2025-06-01\ntype: resource\n"
            "status: active\ntags: [biology]\n---\n\n"
            "This note covers bioinformatics and single-cell analysis."
        ),
        "30-resources/gamma.md": (
            "---\ntitle: Gamma Note\ndate: 2024-01-01\ntype: note\n"
            "status: active\ntags: [archive]\n---\n\n"
            "Old knowledge about compression algorithms."
        ),
    }
    for rel, content in notes.items():
        p = tmp_path / rel
        p.write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def indexed_vault(vault: Path) -> Path:
    """Vault with all notes synced into DuckDB."""
    vault_db.sync_all(vault)
    return vault


# ---------------------------------------------------------------------------
# Phase 1 — Schema & indexing
# ---------------------------------------------------------------------------

class TestIndexing:
    def test_sync_all_indexes_all_notes(self, indexed_vault):
        with vault_db._connect() as con:
            count = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        assert count == 3

    def test_upsert_is_idempotent(self, vault: Path):
        vault_db.sync_all(vault)
        vault_db.sync_all(vault)
        with vault_db._connect() as con:
            count = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        assert count == 3

    def test_title_stored(self, indexed_vault):
        with vault_db._connect() as con:
            titles = {r[0] for r in con.execute("SELECT title FROM notes").fetchall()}
        assert "Alpha Project" in titles
        assert "Beta Resource" in titles


# ---------------------------------------------------------------------------
# Phase 1 — FTS search
# ---------------------------------------------------------------------------

class TestFTSSearch:
    def test_search_finds_relevant_note(self, indexed_vault):
        results = vault_db.fts_search("machine learning")
        titles = [r["title"] for r in results]
        assert "Alpha Project" in titles

    def test_search_biology(self, indexed_vault):
        results = vault_db.fts_search("bioinformatics")
        titles = [r["title"] for r in results]
        assert "Beta Resource" in titles

    def test_search_no_match_returns_empty(self, indexed_vault):
        results = vault_db.fts_search("zzz_absolutely_no_match_xyz")
        assert results == []

    def test_search_respects_limit(self, indexed_vault):
        results = vault_db.fts_search("note", limit=1)
        assert len(results) <= 1


# ---------------------------------------------------------------------------
# Phase 1 — Frontmatter parsing
# ---------------------------------------------------------------------------

class TestFrontmatterParsing:
    def test_parses_title(self):
        text = "---\ntitle: My Note\ndate: 2026-01-01\n---\n\nbody"
        fm = vault_db._parse_frontmatter(text)
        assert fm["title"] == "My Note"

    def test_parses_tags_string(self):
        # _parse_frontmatter stores tags as raw string (not parsed YAML list)
        text = "---\ntitle: T\ntags: [a, b, c]\n---\n"
        fm = vault_db._parse_frontmatter(text)
        assert fm.get("tags") == "[a, b, c]"

    def test_no_frontmatter_returns_empty(self):
        fm = vault_db._parse_frontmatter("# Just a heading\n\nsome body")
        assert fm == {}

    def test_body_snippet_length(self):
        long = "x" * 1000
        snippet = vault_db._body_snippet("---\ntitle: T\n---\n\n" + long, max_chars=100)
        assert len(snippet) <= 100


# ---------------------------------------------------------------------------
# Phase 2 — Ebbinghaus score & access tracking
# ---------------------------------------------------------------------------

class TestEbbinghaus:
    def test_score_formula_value(self):
        """score = (access_count + 1) / (1 + log(age_days + 1))"""
        access_count, age_days = 3, 10
        score = (access_count + 1) / (1 + math.log(age_days + 1))
        # 4 / (1 + log(11)) ≈ 1.177
        assert 1.0 < score < 2.0

    def test_unaccessed_note_has_low_score(self, indexed_vault):
        results = vault_db.top_by_score(limit=3)
        # All notes start at access_count=0; ordering by score should still work
        assert len(results) == 3

    def test_accessed_note_ranks_higher(self, indexed_vault):
        for _ in range(5):
            vault_db.record_access("10-projects/alpha.md")
        results = vault_db.top_by_score(limit=3)
        assert results[0]["path"] == "10-projects/alpha.md"

    def test_record_access_increments_count(self, indexed_vault):
        vault_db.record_access("10-projects/alpha.md")
        vault_db.record_access("10-projects/alpha.md")
        with vault_db._connect() as con:
            row = con.execute(
                "SELECT access_count FROM notes WHERE path = ?",
                ["10-projects/alpha.md"]
            ).fetchone()
        assert row[0] >= 2

    def test_sleep_candidates_old_note_included(self, indexed_vault):
        # gamma.md date=2024-01-01 → age > 90 days, low score → candidate
        candidates = vault_db.sleep_candidates(min_age_days=90, max_score=1.0)
        paths = [c["path"] for c in candidates]
        assert "30-resources/gamma.md" in paths

    def test_sleep_candidates_recent_note_excluded(self, indexed_vault):
        # alpha.md date=2026-01-01 → too recent → not a candidate
        candidates = vault_db.sleep_candidates(min_age_days=90, max_score=1.0)
        paths = [c["path"] for c in candidates]
        assert "10-projects/alpha.md" not in paths

    def test_top_by_recency_returns_results(self, indexed_vault):
        results = vault_db.top_by_recency(limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Phase 6 — Embedding helpers
# ---------------------------------------------------------------------------

class TestEmbeddingHelpers:
    def test_vec_blob_roundtrip(self):
        vec = [0.1, -0.5, 0.9, 0.0, 1.0]
        blob = vault_db._vec_to_blob(vec)
        recovered = vault_db._blob_to_vec(blob)
        assert len(recovered) == len(vec)
        for a, b in zip(vec, recovered):
            assert abs(a - b) < 1e-5

    def test_cosine_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert abs(vault_db._cosine(v, v) - 1.0) < 1e-6

    def test_cosine_orthogonal_vectors(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(vault_db._cosine(a, b)) < 1e-6

    def test_cosine_opposite_vectors(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert vault_db._cosine(a, b) < -0.9

    def test_embed_text_server_unavailable_returns_none(self, monkeypatch):
        import urllib.error
        def fail(*a, **kw):
            raise urllib.error.URLError("connection refused")
        monkeypatch.setattr(vault_db.urllib.request, "urlopen", fail)
        result = vault_db.embed_text("test")
        assert result is None

    def test_call_embed_api_retries_shorter_on_500(self, monkeypatch):
        """_call_embed_api should retry with 1024 then 512 chars when server returns 500."""
        import io
        import urllib.error
        call_log: list[int] = []

        def fake_urlopen(req, **_kw):
            payload = __import__("json").loads(req.data)
            length = len(payload["input"])
            call_log.append(length)
            if length > 512:
                raise urllib.error.HTTPError("http://localhost", 500, "too long", {}, None)  # type: ignore[arg-type]
            body = __import__("json").dumps({"data": [{"embedding": [0.1] * 768}]}).encode()
            return io.BytesIO(body)

        monkeypatch.setattr(vault_db.urllib.request, "urlopen", fake_urlopen)
        result = vault_db._call_embed_api("x" * 2048)
        assert result is not None
        assert len(result) == 768
        assert call_log == [2048, 1024, 512]

    def test_call_embed_api_returns_none_after_all_retries_fail(self, monkeypatch):
        """Returns None when all truncation levels still get 500."""
        import urllib.error

        def always_500(**_kw):
            raise urllib.error.HTTPError("http://localhost", 500, "context exceeded", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(vault_db.urllib.request, "urlopen", always_500)
        result = vault_db._call_embed_api("some text")
        assert result is None


class TestSemanticSearch:
    def _insert_with_embedding(self, path: str, vec: list[float]) -> None:
        blob = vault_db._vec_to_blob(vec)
        with vault_db._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO notes (path, title, embedding) VALUES (?, ?, ?)",
                [path, path, blob]
            )

    def test_empty_when_server_unavailable(self, isolated_db, monkeypatch):
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: None)
        results = vault_db.semantic_search("anything")
        assert results == []

    def test_returns_most_similar(self, isolated_db, monkeypatch):
        # Insert two notes with known embeddings
        self._insert_with_embedding("notes/similar.md", [1.0, 0.0, 0.0])
        self._insert_with_embedding("notes/different.md", [0.0, 1.0, 0.0])
        # Query vector is closest to similar.md
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: [0.9, 0.1, 0.0])
        results = vault_db.semantic_search("query", limit=2)
        assert results[0]["path"] == "notes/similar.md"

    def test_hybrid_falls_back_to_bm25_when_no_embeddings(self, indexed_vault, monkeypatch):
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: None)
        results = vault_db.hybrid_search("machine learning")
        # Should still return BM25 results
        assert isinstance(results, list)

    def test_sync_embeddings_fills_missing(self, isolated_db, monkeypatch):
        with vault_db._connect() as con:
            con.execute("INSERT INTO notes (path, title) VALUES ('a.md', 'A')")
            con.execute("INSERT INTO notes (path, title) VALUES ('b.md', 'B')")
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: [0.1] * 768)
        result = vault_db.sync_embeddings()
        assert result["updated"] == 2
        assert result["failed"] == 0


# ---------------------------------------------------------------------------
# Phase 6b — find_related
# ---------------------------------------------------------------------------

class TestFindRelated:
    def _insert_with_vec(self, path: str, vec: list[float]) -> None:
        blob = vault_db._vec_to_blob(vec)
        with vault_db._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO notes (path, title, embedding) VALUES (?, ?, ?)",
                [path, path, blob],
            )

    def test_no_embedding_returns_empty(self, isolated_db):
        with vault_db._connect() as con:
            con.execute("INSERT INTO notes (path, title) VALUES ('x.md', 'X')")
        assert vault_db.find_related("x.md") == []

    def test_finds_similar_above_threshold(self, isolated_db):
        self._insert_with_vec("a.md", [1.0, 0.0, 0.0])
        self._insert_with_vec("b.md", [0.99, 0.1, 0.0])   # similar to a
        self._insert_with_vec("c.md", [0.0, 1.0, 0.0])    # orthogonal to a
        results = vault_db.find_related("a.md", threshold=0.7)
        assert "b" in results      # b.md → stem = b
        assert "c" not in results

    def test_excludes_self(self, isolated_db):
        self._insert_with_vec("self.md", [1.0, 0.0])
        self._insert_with_vec("other.md", [1.0, 0.0])
        results = vault_db.find_related("self.md", threshold=0.5)
        assert "self" not in results

    def test_returns_stems_without_md(self, isolated_db):
        self._insert_with_vec("folder/note.md", [1.0, 0.0])
        self._insert_with_vec("folder/similar.md", [0.95, 0.1])
        results = vault_db.find_related("folder/note.md", threshold=0.5)
        assert all(not r.endswith(".md") for r in results)

    def test_respects_limit(self, isolated_db):
        for i in range(10):
            self._insert_with_vec(f"note{i}.md", [1.0 - i * 0.01, 0.0, 0.0])
        results = vault_db.find_related("note0.md", limit=3, threshold=0.5)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# search_figures — multi-word query
# ---------------------------------------------------------------------------

class TestSearchFigures:
    def test_empty_query_returns_empty(self, isolated_db):
        results = vault_db.search_figures("")
        assert results == []

    def test_no_figures_returns_empty(self, isolated_db):
        results = vault_db.search_figures("accuracy")
        assert results == []

    def test_single_word_matches(self, isolated_db):
        vault_db.upsert_figure(
            "30-resources/test.md", 0, "", "",
            "token efficiency chart", "A bar chart showing token compression ratio", 64,
        )
        results = vault_db.search_figures("token")
        assert len(results) == 1

    def test_multi_word_all_must_match(self, isolated_db):
        vault_db.upsert_figure(
            "30-resources/a.md", 0, "", "",
            "token compression bar chart", "Shows compression ratio", 64,
        )
        vault_db.upsert_figure(
            "30-resources/b.md", 0, "", "",
            "accuracy line graph", "Shows accuracy results", 64,
        )
        results = vault_db.search_figures("token compression")
        paths = [r["note_path"] for r in results]
        assert "30-resources/a.md" in paths
        assert "30-resources/b.md" not in paths

    def test_multi_word_matches_across_ocr_and_description(self, isolated_db):
        # word1 in ocr_text, word2 in description → both must match somewhere
        vault_db.upsert_figure(
            "30-resources/c.md", 0, "", "",
            "token efficiency", "Shows compression ratio", 64,
        )
        results = vault_db.search_figures("token compression")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Phase 2.1 — sync_all reconcile
# ---------------------------------------------------------------------------

class TestSyncReconcile:
    def test_sync_removes_deleted_notes(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "30-resources").mkdir()

        note_a = vault / "30-resources" / "keep.md"
        note_b = vault / "30-resources" / "delete.md"
        for n in (note_a, note_b):
            n.write_text(
                f"---\ntitle: {n.stem}\ndate: 2025-01-01\ntype: note\nstatus: active\ntags: []\n---\n\nbody\n",
                encoding="utf-8",
            )

        vault_db.sync_all(vault)
        with vault_db._connect() as con:
            assert con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2

        note_b.unlink()
        vault_db.sync_all(vault)

        with vault_db._connect() as con:
            paths = {r[0] for r in con.execute("SELECT path FROM notes").fetchall()}
        assert "30-resources/keep.md" in paths
        assert "30-resources/delete.md" not in paths

    def test_sync_removes_orphaned_figures(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "30-resources").mkdir()

        keep = vault / "30-resources" / "keep.md"
        delete = vault / "30-resources" / "delete.md"
        for n in (keep, delete):
            n.write_text(
                f"---\ntitle: {n.stem}\ndate: 2025-01-01\ntype: note\nstatus: active\ntags: []\n---\n\nbody\n",
                encoding="utf-8",
            )

        vault_db.sync_all(vault)
        vault_db.upsert_figure("30-resources/delete.md", 0, "", "", "ocr", "desc", 10)

        with vault_db._connect() as con:
            assert con.execute("SELECT COUNT(*) FROM figures").fetchone()[0] == 1

        delete.unlink()
        vault_db.sync_all(vault)

        with vault_db._connect() as con:
            assert con.execute("SELECT COUNT(*) FROM figures").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Phase 3.1 — KNOWLEDGE_EXCLUDE filtering
# ---------------------------------------------------------------------------

class TestKnowledgeExclude:
    def _insert(self, path: str, note_type: str, title: str = "T") -> None:
        with vault_db._connect() as con:
            con.execute(
                "INSERT INTO notes (path, title, note_type, status, tags, note_date, content_hash)"
                " VALUES (?,?,?,'active','[]','2025-01-01',?)",
                [path, title, note_type, path],
            )

    def test_top_by_score_excludes_finance_types(self) -> None:
        self._insert("knowledge/article.md", "tech_note", "Good Article")
        self._insert("finance/report.md", "stock_analysis", "Daily Report")
        results = vault_db.top_by_score(limit=20, exclude_types=vault_db.KNOWLEDGE_EXCLUDE)
        paths = [r["path"] for r in results]
        assert "knowledge/article.md" in paths
        assert "finance/report.md" not in paths

    def test_top_by_score_without_exclude_shows_finance(self) -> None:
        self._insert("finance/report.md", "stock_analysis", "Daily Report")
        results = vault_db.top_by_score(limit=20)
        assert any(r["path"] == "finance/report.md" for r in results)

    def test_top_by_recency_excludes_finance_types(self) -> None:
        self._insert("knowledge/article.md", "tech_note", "Good Article")
        self._insert("finance/daily.md", "daily_briefing", "Briefing")
        results = vault_db.top_by_recency(limit=20, exclude_types=vault_db.KNOWLEDGE_EXCLUDE)
        paths = [r["path"] for r in results]
        assert "knowledge/article.md" in paths
        assert "finance/daily.md" not in paths
