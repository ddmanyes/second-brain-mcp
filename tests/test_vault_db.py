"""Tests for vault_db.py — Phase 1 (FTS) and Phase 2 (Ebbinghaus score)."""

import math
import sys
from pathlib import Path

import pytest



from mcp_second_brain import vault_db


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
        assert "b.md" in results      # Phase 8.3: full path with .md preserved
        assert "c.md" not in results

    def test_excludes_self(self, isolated_db):
        self._insert_with_vec("self.md", [1.0, 0.0])
        self._insert_with_vec("other.md", [1.0, 0.0])
        results = vault_db.find_related("self.md", threshold=0.5)
        assert "self.md" not in results

    def test_returns_full_paths_with_md(self, isolated_db):
        # Phase 8.3: find_related now returns .md paths; callers do removesuffix themselves
        self._insert_with_vec("folder/note.md", [1.0, 0.0])
        self._insert_with_vec("folder/similar.md", [0.95, 0.1])
        results = vault_db.find_related("folder/note.md", threshold=0.5)
        assert all(r.endswith(".md") for r in results)

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

    def test_multi_word_or_logic(self):
        # Phase 8.2: OR — either word matching is enough
        vault_db.upsert_figure(
            "30-resources/a.md", 0, "", "",
            "token compression bar chart", "Shows compression ratio", 64,
        )
        vault_db.upsert_figure(
            "30-resources/b.md", 0, "", "",
            "accuracy line graph", "Shows accuracy results", 64,
        )
        # "token" matches a.md; "accuracy" matches b.md → both hit
        results = vault_db.search_figures("token accuracy")
        paths = [r["note_path"] for r in results]
        assert "30-resources/a.md" in paths
        assert "30-resources/b.md" in paths

    def test_multi_word_matches_across_ocr_and_description(self, isolated_db):
        # "token" in ocr_text, "compression" in description → either field triggers
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


# ---------------------------------------------------------------------------
# Phase 7.4 — embed_text dim guard: upsert/sync_embeddings must not crash
# ---------------------------------------------------------------------------

class TestEmbedDimGuard:
    def test_upsert_does_not_crash_on_dim_mismatch(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "note.md"
        note.write_text(
            "---\ntitle: T\ndate: 2025-01-01\ntype: note\nstatus: active\ntags: []\n---\n\nbody\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: (_ for _ in ()).throw(
            ValueError("Embedding dim mismatch: got 384, expected 768. Set EMBED_DIM env var.")
        ))
        # Must not raise — should degrade to NULL embedding
        with vault_db._connect() as con:
            vault_db.upsert_note(con, vault, note)
        with vault_db._connect() as con:
            row = con.execute("SELECT embedding FROM notes WHERE path = 'note.md'").fetchone()
        assert row is not None
        assert row[0] is None  # graceful NULL, not a crash

    def test_sync_embeddings_does_not_crash_on_dim_mismatch(self, monkeypatch):
        with vault_db._connect() as con:
            con.execute("INSERT INTO notes (path, title) VALUES ('x.md', 'X')")
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: (_ for _ in ()).throw(
            ValueError("dim mismatch")
        ))
        result = vault_db.sync_embeddings()
        assert result["failed"] == 1
        assert result["updated"] == 0


# ---------------------------------------------------------------------------
# Phase 6.3 — RRF fusion: correctness and backward compat
# ---------------------------------------------------------------------------

class TestHybridSearchRRF:
    def _insert(self, path: str, title: str, body: str, note_type: str = "note") -> None:
        with vault_db._connect() as con:
            con.execute(
                "INSERT INTO notes (path, title, note_type, status, tags, note_date, content_hash, body_snippet)"
                " VALUES (?,?,?,'active','[]','2025-01-01',?,?)",
                [path, title, note_type, path, body],
            )

    def test_rrf_returns_results(self, monkeypatch):
        self._insert("rrf/note.md", "RRF Note", "machine learning rrf test")
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: None)
        results = vault_db.hybrid_search("machine learning", fusion="rrf")
        assert isinstance(results, list)

    def test_alpha_fusion_still_works(self, monkeypatch):
        self._insert("alpha/note.md", "Alpha Note", "machine learning alpha test")
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: None)
        results = vault_db.hybrid_search("machine learning", fusion="alpha")
        assert isinstance(results, list)

    def test_path_penalty_lowers_score_for_fixes(self, monkeypatch):
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: None)
        self._insert("fixes/debug-note.md", "Debug Note", "machine learning fix patch")
        self._insert("design/arch-note.md", "Arch Note", "machine learning architecture")

        results_penalised = vault_db.hybrid_search(
            "machine learning", fusion="rrf", apply_path_penalty=True
        )
        results_no_penalty = vault_db.hybrid_search(
            "machine learning", fusion="rrf", apply_path_penalty=False
        )

        def _score(results: list, path: str) -> float:
            for r in results:
                if r["path"] == path:
                    return r["score"]
            return 0.0

        score_with = _score(results_penalised, "fixes/debug-note.md")
        score_without = _score(results_no_penalty, "fixes/debug-note.md")
        # Penalised score must be lower (or zero if not in results)
        assert score_with <= score_without

    def test_title_boost_raises_exact_match(self, monkeypatch):
        monkeypatch.setattr(vault_db, "embed_text", lambda *a, **kw: None)
        self._insert("a/title-match.md", "machine learning overview", "some content here")
        self._insert("b/body-match.md", "General Notes", "machine learning is discussed here at length")

        results = vault_db.hybrid_search("machine learning", fusion="rrf")
        paths = [r["path"] for r in results]
        if "a/title-match.md" in paths and "b/body-match.md" in paths:
            idx_title = paths.index("a/title-match.md")
            idx_body = paths.index("b/body-match.md")
            # Title match should rank higher than body-only match
            assert idx_title <= idx_body


# ---------------------------------------------------------------------------
# Phase 4.1 — load_embedding_cache + find_related cache path
# ---------------------------------------------------------------------------

class TestEmbeddingCache:
    def _insert_with_vec(self, path: str, vec: list[float]) -> None:
        blob = vault_db._vec_to_blob(vec)
        with vault_db._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO notes (path, title, embedding) VALUES (?, ?, ?)",
                [path, path, blob],
            )

    def test_load_embedding_cache_returns_all(self):
        self._insert_with_vec("a.md", [1.0, 0.0])
        self._insert_with_vec("b.md", [0.0, 1.0])
        cache = vault_db.load_embedding_cache()
        assert "a.md" in cache
        assert "b.md" in cache
        assert len(cache["a.md"]) == 2

    def test_find_related_cache_matches_no_cache(self):
        self._insert_with_vec("ref.md", [1.0, 0.0, 0.0])
        self._insert_with_vec("similar.md", [0.98, 0.1, 0.0])
        self._insert_with_vec("unrelated.md", [0.0, 1.0, 0.0])

        cache = vault_db.load_embedding_cache()
        with_cache = vault_db.find_related("ref.md", threshold=0.5, _embedding_cache=cache)
        without_cache = vault_db.find_related("ref.md", threshold=0.5)
        assert set(with_cache) == set(without_cache)

    def test_find_related_cache_excludes_self(self):
        self._insert_with_vec("self.md", [1.0, 0.0])
        self._insert_with_vec("other.md", [1.0, 0.0])
        cache = vault_db.load_embedding_cache()
        results = vault_db.find_related("self.md", threshold=0.5, _embedding_cache=cache)
        assert "self.md" not in results


# ---------------------------------------------------------------------------
# Phase 12 — Semantic keywords indexing & FTS
# ---------------------------------------------------------------------------

class TestSemanticKeywords:
    def test_semantic_keywords_stored_in_db(self, vault: Path):
        """Notes with semantic_keywords frontmatter should have them indexed in DB."""
        note = vault / "10-projects" / "alpha.md"
        text = note.read_text(encoding="utf-8")
        # Inject semantic_keywords into frontmatter
        updated = text.replace(
            "---\ntitle: Alpha Project",
            '---\ntitle: Alpha Project\nsemantic_keywords: ["股市下跌", "熊市", "機器學習"]',
        )
        note.write_text(updated, encoding="utf-8")
        vault_db.sync_all(vault)
        with vault_db._connect() as con:
            row = con.execute(
                "SELECT semantic_keywords FROM notes WHERE path = ?",
                ["10-projects/alpha.md"],
            ).fetchone()
        assert row is not None
        assert row[0] is not None
        import json as _json
        kw = _json.loads(row[0])
        assert "股市下跌" in kw

    def test_fts_matches_via_semantic_keywords(self, vault: Path):
        """BM25 FTS should match a query term that appears in semantic_keywords."""
        note = vault / "10-projects" / "alpha.md"
        text = note.read_text(encoding="utf-8")
        updated = text.replace(
            "---\ntitle: Alpha Project",
            '---\ntitle: Alpha Project\nsemantic_keywords: ["熊市", "股市崩盤"]',
        )
        note.write_text(updated, encoding="utf-8")
        vault_db.sync_all(vault)
        results = vault_db.fts_search("熊市")
        paths = [r["path"] for r in results]
        assert "10-projects/alpha.md" in paths

    def test_semantic_keywords_parse_json_array(self):
        """upsert_note parses JSON array correctly."""
        text = '---\ntitle: T\ndate: 2026-01-01\nsemantic_keywords: ["a", "b", "c"]\n---\n\nbody'
        fm = vault_db._parse_frontmatter(text)
        assert fm.get("semantic_keywords") == '["a", "b", "c"]'

    def test_semantic_keywords_parse_yaml_array(self, vault: Path):
        """upsert_note parses unquoted YAML-style list [a, b, c] into JSON."""
        note = vault / "10-projects" / "alpha.md"
        text = note.read_text(encoding="utf-8")
        updated = text.replace(
            "---\ntitle: Alpha Project",
            "---\ntitle: Alpha Project\nsemantic_keywords: [熊市, 崩盤, 下跌]",
        )
        note.write_text(updated, encoding="utf-8")
        vault_db.sync_all(vault)
        with vault_db._connect() as con:
            row = con.execute(
                "SELECT semantic_keywords FROM notes WHERE path = ?",
                ["10-projects/alpha.md"],
            ).fetchone()
        assert row and row[0] is not None
        import json as _json
        kw = _json.loads(row[0])
        assert "熊市" in kw

    def test_upsert_preserves_existing_keywords_on_hash_match(self, vault: Path):
        """Re-syncing an unchanged note should not clear its semantic_keywords."""
        note = vault / "10-projects" / "alpha.md"
        text = note.read_text(encoding="utf-8")
        updated = text.replace(
            "---\ntitle: Alpha Project",
            '---\ntitle: Alpha Project\nsemantic_keywords: ["持久關鍵字"]',
        )
        note.write_text(updated, encoding="utf-8")
        vault_db.sync_all(vault)
        # Second sync — content unchanged, hash matches → row kept
        vault_db.sync_all(vault)
        with vault_db._connect() as con:
            row = con.execute(
                "SELECT semantic_keywords FROM notes WHERE path = ?",
                ["10-projects/alpha.md"],
            ).fetchone()
        assert row and row[0] is not None
        import json as _json
        assert "持久關鍵字" in _json.loads(row[0])

    def test_upsert_coalesce_keeps_keywords_when_frontmatter_missing(self, vault: Path):
        """If note is re-synced without semantic_keywords in frontmatter,
        existing DB value should be preserved via COALESCE."""
        note = vault / "10-projects" / "alpha.md"
        # First write with keywords
        text = note.read_text(encoding="utf-8")
        with_kw = text.replace(
            "---\ntitle: Alpha Project",
            '---\ntitle: Alpha Project\nsemantic_keywords: ["保留關鍵字"]',
        )
        note.write_text(with_kw, encoding="utf-8")
        vault_db.sync_all(vault)

        # Second write — remove semantic_keywords from frontmatter, change body to force hash miss
        without_kw = text + "\n\n<!-- updated -->"
        note.write_text(without_kw, encoding="utf-8")
        vault_db.sync_all(vault)

        with vault_db._connect() as con:
            row = con.execute(
                "SELECT semantic_keywords FROM notes WHERE path = ?",
                ["10-projects/alpha.md"],
            ).fetchone()
        assert row and row[0] is not None
        import json as _json
        assert "保留關鍵字" in _json.loads(row[0])


# ---------------------------------------------------------------------------
# Phase 12 — server.py helper functions
# ---------------------------------------------------------------------------

class TestServerHelpers:
    def _make_note(self, tmp_path: Path, fm_extra: str = "") -> Path:
        note = tmp_path / "test_note.md"
        note.write_text(
            f"---\ntitle: Test Note\ndate: 2026-01-01\ntype: note\nstatus: active\n{fm_extra}---\n\nbody text here",
            encoding="utf-8",
        )
        return note

    def test_inject_semantic_keywords_writes_to_frontmatter(self, tmp_path: Path):
        import sys
        
        from mcp_second_brain.server import _inject_semantic_keywords
        note = self._make_note(tmp_path)
        _inject_semantic_keywords(note, ["關鍵字A", "關鍵字B"])
        text = note.read_text(encoding="utf-8")
        assert "semantic_keywords:" in text
        assert "關鍵字A" in text

    def test_inject_semantic_keywords_overwrites_existing(self, tmp_path: Path):
        import sys
        
        from mcp_second_brain.server import _inject_semantic_keywords
        note = self._make_note(tmp_path, fm_extra='semantic_keywords: ["舊的"]\n')
        _inject_semantic_keywords(note, ["新的"])
        text = note.read_text(encoding="utf-8")
        assert "新的" in text
        assert "舊的" not in text

    def test_inject_semantic_keywords_idempotent(self, tmp_path: Path):
        import sys
        
        from mcp_second_brain.server import _inject_semantic_keywords
        note = self._make_note(tmp_path)
        _inject_semantic_keywords(note, ["詞A"])
        _inject_semantic_keywords(note, ["詞A"])
        text = note.read_text(encoding="utf-8")
        assert text.count("semantic_keywords:") == 1

    def test_extract_semantic_keywords_no_gemini(self, monkeypatch):
        """When gemini CLI is absent, extraction returns empty list without raising."""
        import sys
        
        from mcp_second_brain import server
        monkeypatch.setattr(server.shutil, "which", lambda _: None)
        result = server._extract_semantic_keywords_via_gemini("some content")
        assert result == []

    def test_extract_semantic_keywords_gemini_mock(self, monkeypatch, tmp_path):
        """Mock subprocess to return fixed JSON; verify correct parse."""
        import sys
        
        from mcp_second_brain import server
        monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/bin/gemini")

        class _Result:
            stdout = '["股市下跌", "金融危機", "熊市"]'
            returncode = 0

        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _Result())
        result = server._extract_semantic_keywords_via_gemini("市場大跌，投資人恐慌")
        assert result == ["股市下跌", "金融危機", "熊市"]

    def test_extract_semantic_keywords_gemini_fallback(self, monkeypatch):
        """When Gemini returns comma-separated plain text, fallback parsing still works."""
        import sys
        
        from mcp_second_brain import server

        monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/bin/gemini")

        class _Result:
            stdout = "股市下跌, 熊市, 崩盤"
            returncode = 0

        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _Result())
        result = server._extract_semantic_keywords_via_gemini("content")
        assert "股市下跌" in result
        assert "熊市" in result

    def test_extract_semantic_keywords_gemini_failure_graceful(self, monkeypatch):
        """subprocess raising exception returns empty list, never raises."""
        import sys
        
        from mcp_second_brain import server

        monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/bin/gemini")
        monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")))
        result = server._extract_semantic_keywords_via_gemini("content")
        assert result == []

    def test_maybe_sync_triggers_incremental(self, tmp_path: Path, monkeypatch):
        """_maybe_sync calls sync_incremental (not sync_all) when DB is stale and vault has newer .md."""
        import sys
        
        from mcp_second_brain import server

        db_file = tmp_path / "vault.db"
        db_file.write_bytes(b"")
        import os
        old_time = server.time.time() - 3600
        os.utime(str(db_file), (old_time, old_time))

        md_file = tmp_path / "new.md"
        md_file.write_text("# new", encoding="utf-8")

        monkeypatch.setattr(server.vault_db, "DB_PATH", db_file)
        incremental_called = []
        sync_all_called = []
        monkeypatch.setattr(server.vault_db, "sync_incremental", lambda v: incremental_called.append(v) or {"updated": 1})
        monkeypatch.setattr(server.vault_db, "sync_all", lambda v: sync_all_called.append(v) or {"synced": 1, "embed_failed": 0})

        server._maybe_sync(tmp_path)
        assert incremental_called, "_maybe_sync should have called sync_incremental"
        assert not sync_all_called, "_maybe_sync should NOT call sync_all when DB exists"

    def test_maybe_sync_first_install(self, tmp_path: Path, monkeypatch):
        """_maybe_sync calls sync_all (full build) when DB does not exist yet."""
        import sys
        
        from mcp_second_brain import server

        db_file = tmp_path / "nonexistent.db"
        monkeypatch.setattr(server.vault_db, "DB_PATH", db_file)
        sync_all_called = []
        incremental_called = []
        monkeypatch.setattr(server.vault_db, "sync_all", lambda v: sync_all_called.append(v) or {"synced": 0, "embed_failed": 0})
        monkeypatch.setattr(server.vault_db, "sync_incremental", lambda v: incremental_called.append(v) or {"updated": 0})

        server._maybe_sync(tmp_path)
        assert sync_all_called, "_maybe_sync should call sync_all on first install"
        assert not incremental_called, "_maybe_sync should NOT call sync_incremental on first install"

    def test_expand_tool_skip_existing_single_note(self, tmp_path: Path, monkeypatch):
        """Single note with existing keywords is skipped when force=False."""
        import sys
        
        from mcp_second_brain import server

        monkeypatch.setattr(server, "VAULT", tmp_path)
        monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/bin/gemini")
        called = []
        monkeypatch.setattr(server, "_extract_semantic_keywords_via_gemini", lambda c: called.append(c) or ["詞"])

        note = tmp_path / "existing.md"
        note.write_text(
            '---\ntitle: T\ndate: 2026-01-01\ntype: note\nstatus: active\n'
            'semantic_keywords: ["已有關鍵字"]\n---\n\nbody',
            encoding="utf-8",
        )
        result = server.expand_semantic_keywords_tool(note_path="existing.md", force=False)
        assert called == [], "Should not call Gemini for note that already has keywords"
        assert '"skipped": 1' in result or "skipped" in result

    def test_expand_tool_batch_processes_notes_without_keywords(self, tmp_path: Path, monkeypatch):
        """Batch mode (note_path='') should process notes that have no keywords in DB."""
        import sys
        
        from mcp_second_brain import server
        from mcp_second_brain import vault_db as _vdb
        from unittest.mock import patch

        monkeypatch.setattr(server, "VAULT", tmp_path)
        monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/bin/gemini")
        processed_paths = []

        def _fake_extract(content):
            processed_paths.append(content[:10])
            return ["test_kw"]

        monkeypatch.setattr(server, "_extract_semantic_keywords_via_gemini", _fake_extract)
        monkeypatch.setattr(server, "_inject_semantic_keywords", lambda p, kw: None)

        note = tmp_path / "no_kw.md"
        note.write_text("---\ntitle: T\ndate: 2026-01-01\ntype: note\nstatus: active\n---\n\nbody", encoding="utf-8")

        # Mock vault_db to return one path with no keywords
        with patch.object(server.vault_db, "_connect") as mock_con:
            mock_ctx = mock_con.return_value.__enter__.return_value
            mock_ctx.execute.return_value.fetchall.return_value = [("no_kw.md",)]
            mock_ctx.execute.return_value.fetchone.return_value = None
            # Use a second context for upsert_note
            with patch.object(server.vault_db, "upsert_note", return_value=None):
                with patch.object(server.vault_db, "_ensure_fts", return_value=None):
                    server.expand_semantic_keywords_tool(note_path="", force=False)

        assert processed_paths, "Batch mode should have called Gemini extractor (bug fix regression test)"

    def test_maybe_sync_skips_when_fresh(self, tmp_path: Path, monkeypatch):
        """_maybe_sync skips all sync when DB was just updated (within 30 min)."""
        import sys
        
        from mcp_second_brain import server

        db_file = tmp_path / "vault.db"
        db_file.write_bytes(b"")
        # DB mtime is current (fresh)

        monkeypatch.setattr(server.vault_db, "DB_PATH", db_file)
        sync_all_called = []
        incremental_called = []
        monkeypatch.setattr(server.vault_db, "sync_all", lambda v: sync_all_called.append(v) or {})
        monkeypatch.setattr(server.vault_db, "sync_incremental", lambda v: incremental_called.append(v) or {})

        server._maybe_sync(tmp_path)
        assert not sync_all_called, "_maybe_sync should not call sync_all when fresh"
        assert not incremental_called, "_maybe_sync should not call sync_incremental when fresh"


class TestSyncIncremental:
    """Tests for vault_db.sync_incremental()."""

    def test_sync_incremental_only_changed(self, tmp_path: Path, monkeypatch):
        """sync_incremental upserts only md files newer than DB mtime."""
        import sys
        
        from mcp_second_brain import vault_db
        import os
        from unittest.mock import MagicMock, patch

        db_file = tmp_path / "vault.db"
        db_file.write_bytes(b"")
        old_time = __import__("time").time() - 3600
        os.utime(str(db_file), (old_time, old_time))
        monkeypatch.setattr(vault_db, "DB_PATH", db_file)

        # 2 old files, 1 new file
        for name in ("old1.md", "old2.md"):
            f = tmp_path / name
            f.write_text(f"# {name}", encoding="utf-8")
            os.utime(str(f), (old_time - 10, old_time - 10))

        new_file = tmp_path / "new.md"
        new_file.write_text("---\ntitle: T\ndate: 2026-01-01\ntype: note\nstatus: active\n---\n\nbody", encoding="utf-8")
        # new_file mtime is current (newer than DB)

        upserted = []
        monkeypatch.setattr(vault_db, "upsert_note", lambda con, vault, f: upserted.append(f.name))
        monkeypatch.setattr(vault_db, "_ensure_fts", lambda con: None)

        mock_con = MagicMock()
        with patch.object(vault_db, "_connect") as mock_connect:
            mock_connect.return_value.__enter__ = lambda s: mock_con
            mock_connect.return_value.__exit__ = lambda s, *a: None
            result = vault_db.sync_incremental(tmp_path)

        assert result == {"updated": 1}
        assert upserted == ["new.md"]

    def test_sync_incremental_no_changes(self, tmp_path: Path, monkeypatch):
        """sync_incremental returns skipped when no md files are newer than DB."""
        import sys
        
        from mcp_second_brain import vault_db
        import os

        db_file = tmp_path / "vault.db"
        db_file.write_bytes(b"")
        # DB is current; write old md files
        old_time = __import__("time").time() - 3600
        for name in ("a.md", "b.md"):
            f = tmp_path / name
            f.write_text("# hi", encoding="utf-8")
            os.utime(str(f), (old_time, old_time))

        monkeypatch.setattr(vault_db, "DB_PATH", db_file)
        upserted = []
        monkeypatch.setattr(vault_db, "upsert_note", lambda con, vault, f: upserted.append(f.name))

        result = vault_db.sync_incremental(tmp_path)
        assert result == {"updated": 0, "skipped": "all fresh"}
        assert upserted == []
