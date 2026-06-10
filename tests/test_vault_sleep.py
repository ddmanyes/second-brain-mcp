"""Tests for vault_sleep.py — Phase 3 (compression/archive) and Phase 5 (prune)."""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest



from mcp_second_brain import vault_db
from mcp_second_brain import vault_sleep


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_db, "DB_PATH", tmp_path / "vault.db")
    monkeypatch.setattr(vault_db, "_schema_applied", False)
    monkeypatch.setattr(vault_db, "EMBED_AUTO_START", False)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    for folder in ["10-projects", "30-resources", "40-archive"]:
        (tmp_path / folder).mkdir()
    note = tmp_path / "30-resources/old-note.md"
    note.write_text(
        "---\ntitle: Old Resource\ndate: 2024-01-01\ntype: resource\nstatus: active\ntags: []\n---\n\n"
        "# Old Resource\n\nBackground context. More background. Key finding: DuckDB is fast. "
        "Method: FTS indexing. Result: 10× speedup. Next: deploy.\n",
        encoding="utf-8",
    )
    vault_db.sync_all(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Phase 3 + 9 — Tier selection (score × age dual-axis)
# ---------------------------------------------------------------------------

class TestTierForAge:
    """Legacy _tier_for_age wrapper — score=0 worst-case, same as old behaviour."""
    def test_young_note_is_large(self):
        assert vault_sleep._tier_for_age(30) == "large"

    def test_mid_age_is_base(self):
        assert vault_sleep._tier_for_age(181) == "base"

    def test_old_note_is_small(self):
        assert vault_sleep._tier_for_age(366) == "small"

    def test_boundary_180_is_large(self):
        assert vault_sleep._tier_for_age(180) == "large"

    def test_boundary_365_is_base(self):
        assert vault_sleep._tier_for_age(365) == "base"


class TestTierForProfile:
    """Phase 9: dual-axis tier selection."""

    def test_high_score_always_text(self):
        # score > 1.5 → text regardless of age
        assert vault_sleep._tier_for_profile(2.0, 500) == "text"
        assert vault_sleep._tier_for_profile(1.6, 1000) == "text"

    def test_score_at_boundary_not_text(self):
        assert vault_sleep._tier_for_profile(1.5, 500) != "text"

    def test_medium_score_rescues_old_note_to_base(self):
        # age=500 (would be small) but score=0.5 → base
        assert vault_sleep._tier_for_profile(0.5, 500) == "base"

    def test_good_score_rescues_old_note_to_large(self):
        # age=400 (would be small) but score=1.0 → large
        assert vault_sleep._tier_for_profile(1.0, 400) == "large"

    def test_low_score_old_note_stays_small(self):
        assert vault_sleep._tier_for_profile(0.1, 500) == "small"

    def test_young_note_always_large(self):
        # age ≤ 180 → large regardless of score
        assert vault_sleep._tier_for_profile(0.0, 100) == "large"
        assert vault_sleep._tier_for_profile(0.0, 180) == "large"

    def test_age_365_with_zero_score_is_base(self):
        assert vault_sleep._tier_for_profile(0.0, 365) == "base"

    def test_zero_score_matches_legacy_tier_for_age(self):
        for age in [30, 100, 181, 365, 366, 500]:
            assert vault_sleep._tier_for_profile(0.0, age) == vault_sleep._tier_for_age(age)


# ---------------------------------------------------------------------------
# Phase 3 — Frontmatter update
# ---------------------------------------------------------------------------

class TestUpdateFrontmatter:
    def test_adds_new_key(self):
        content = "---\ntitle: T\n---\n\nbody"
        result = vault_sleep._update_frontmatter(content, {"consolidated": "true"})
        assert "consolidated: true" in result

    def test_updates_existing_key(self):
        content = "---\ntitle: T\nstatus: active\n---\n\nbody"
        result = vault_sleep._update_frontmatter(content, {"status": "archived"})
        assert "status: archived" in result
        assert "status: active" not in result

    def test_preserves_body(self):
        content = "---\ntitle: T\n---\n\nmy body text"
        result = vault_sleep._update_frontmatter(content, {"x": "y"})
        assert "my body text" in result

    def test_no_frontmatter_adds_header(self):
        result = vault_sleep._update_frontmatter("just body", {"key": "val"})
        assert "---" in result
        assert "key: val" in result


# ---------------------------------------------------------------------------
# Phase 3 — Archive path
# ---------------------------------------------------------------------------

class TestArchivePath:
    def test_archive_in_40_archive(self, tmp_path):
        path = vault_sleep._archive_path(tmp_path, "30-resources/my-note.md")
        assert "40-archive" in str(path)

    def test_preserves_filename(self, tmp_path):
        path = vault_sleep._archive_path(tmp_path, "30-resources/my-note.md")
        assert path.name == "my-note.md"

    def test_contains_year_month(self, tmp_path):
        path = vault_sleep._archive_path(tmp_path, "30-resources/x.md")
        today = date.today()
        assert str(today.year) in str(path)


# ---------------------------------------------------------------------------
# Phase 3 — Naive compress (no LLM needed)
# ---------------------------------------------------------------------------

class TestNaiveCompress:
    def test_keeps_headings(self):
        content = "---\ntitle: T\n---\n\n# Heading\n\nSome text.\n\n## Sub\n\nMore text."
        result = vault_sleep._naive_compress(content)
        assert "# Heading" in result
        assert "## Sub" in result

    def test_keeps_frontmatter(self):
        content = "---\ntitle: My Note\ndate: 2024-01-01\n---\n\n# H\n\ntext"
        result = vault_sleep._naive_compress(content)
        assert "title: My Note" in result

    def test_result_shorter_than_input(self):
        long = "---\ntitle: T\n---\n\n" + "background text\n" * 50 + "# Key finding\n\nResult: 42\n"
        result = vault_sleep._naive_compress(long)
        assert len(result) < len(long)


# ---------------------------------------------------------------------------
# Phase 3 — run_sleep (with mocked LLM)
# ---------------------------------------------------------------------------

class TestRunSleep:
    def test_dry_run_does_not_modify_files(self, vault):
        note = vault / "30-resources/old-note.md"
        original_text = note.read_text()
        result = vault_sleep.run_sleep(vault, min_age_days=1, dry_run=True)
        assert note.read_text() == original_text
        assert result["skipped"] > 0

    def test_compresses_old_note(self, vault):
        with patch.object(vault_sleep, "_compress_with_gemini", return_value=None), \
             patch.object(vault_sleep, "_compress_with_claude", return_value=None), \
             patch.object(vault_sleep, "_fig") as mock_fig:
            mock_fig.render_note_to_png.return_value = None
            result = vault_sleep.run_sleep(vault, min_age_days=1, dry_run=False)

        assert result["processed"] >= 1
        archive_files = list((vault / "40-archive").rglob("*.md"))
        assert len(archive_files) >= 1

    def test_archive_contains_original(self, vault):
        assert (vault / "30-resources/old-note.md").exists()

        with patch.object(vault_sleep, "_compress_with_gemini", return_value=None), \
             patch.object(vault_sleep, "_compress_with_claude", return_value=None), \
             patch.object(vault_sleep, "_fig") as mock_fig:
            mock_fig.render_note_to_png.return_value = None
            vault_sleep.run_sleep(vault, min_age_days=1, dry_run=False)

        archive_files = list((vault / "40-archive").rglob("*.md"))
        assert any("Old Resource" in f.read_text() for f in archive_files)

    def test_no_candidates_returns_zero(self, vault):
        result = vault_sleep.run_sleep(vault, min_age_days=9999, dry_run=False)
        assert result["processed"] == 0
        assert result["candidates"] == 0


# ---------------------------------------------------------------------------
# Phase 5 — prune_archive
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 7 — L3 Rules extraction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 8 — Cross-note Consolidation
# ---------------------------------------------------------------------------

class TestConsolidation:
    def _insert_with_vec(self, path: str, vec: list[float]) -> None:
        blob = vault_db._vec_to_blob(vec)
        with vault_db._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO notes (path, title, note_type, embedding) VALUES (?, ?, 'resource', ?)",
                [path, path, blob],
            )

    def test_no_candidates_when_no_embeddings(self, isolated_db):
        with vault_db._connect() as con:
            con.execute("INSERT INTO notes (path, title) VALUES ('a.md', 'A')")
        clusters = vault_sleep.find_consolidation_candidates(threshold=0.8)
        assert clusters == []

    def test_clusters_similar_notes(self, isolated_db):
        self._insert_with_vec("a.md", [1.0, 0.0, 0.0])
        self._insert_with_vec("b.md", [0.99, 0.05, 0.0])  # similar to a
        self._insert_with_vec("c.md", [0.0, 1.0, 0.0])    # different
        clusters = vault_sleep.find_consolidation_candidates(threshold=0.9, min_cluster_size=2)
        assert len(clusters) == 1
        assert set(clusters[0]) == {"a.md", "b.md"}

    def test_min_cluster_size_respected(self, isolated_db):
        self._insert_with_vec("a.md", [1.0, 0.0])
        self._insert_with_vec("b.md", [0.99, 0.0])
        # min_cluster_size=3 → no clusters
        clusters = vault_sleep.find_consolidation_candidates(threshold=0.9, min_cluster_size=3)
        assert clusters == []

    def test_excludes_already_consolidated_notes(self, isolated_db):
        self._insert_with_vec("a.md", [1.0, 0.0])
        self._insert_with_vec("b.md", [0.99, 0.0])
        with vault_db._connect() as con:
            con.execute("UPDATE notes SET status = 'consolidated' WHERE path = 'a.md'")
        clusters = vault_sleep.find_consolidation_candidates(threshold=0.5, min_cluster_size=2)
        assert all("a.md" not in c for c in clusters)

    def test_dry_run_does_not_write_files(self, vault):
        # Seed two similar notes in DB
        for p in ["30-resources/x.md", "30-resources/y.md"]:
            full = vault / p
            full.write_text(f"---\ntitle: {p}\n---\n\nbody", encoding="utf-8")
        vault_db.sync_all(vault)
        # Force same-ish embeddings
        blob = vault_db._vec_to_blob([1.0, 0.0, 0.0])
        with vault_db._connect() as con:
            for p in ["30-resources/x.md", "30-resources/y.md"]:
                con.execute("UPDATE notes SET embedding = ? WHERE path = ?", [blob, p])

        result = vault_sleep.run_consolidation(vault, threshold=0.5, dry_run=True)
        consolidated_dir = vault / "20-areas" / "consolidated"
        assert not consolidated_dir.exists() or not list(consolidated_dir.glob("*.md"))
        assert result["consolidated"] == 0

    def test_consolidation_writes_file(self, vault, monkeypatch):
        for p in ["30-resources/x.md", "30-resources/y.md"]:
            full = vault / p
            full.write_text(f"---\ntitle: {p}\n---\n\nbody text here", encoding="utf-8")
        vault_db.sync_all(vault)
        blob = vault_db._vec_to_blob([1.0, 0.0, 0.0])
        with vault_db._connect() as con:
            for p in ["30-resources/x.md", "30-resources/y.md"]:
                con.execute("UPDATE notes SET embedding = ? WHERE path = ?", [blob, p])

        monkeypatch.setattr(vault_sleep, "consolidate_cluster", lambda *a, **kw: "## Summary\n\nMerged.")
        result = vault_sleep.run_consolidation(vault, threshold=0.5, dry_run=False)
        assert result["consolidated"] == 1
        consolidated_files = list((vault / "20-areas" / "consolidated").glob("*.md"))
        assert len(consolidated_files) == 1


class TestExtractRules:
    def test_no_rules_when_gemini_fails(self, vault, monkeypatch):
        monkeypatch.setattr(vault_sleep, "_extract_rules_with_gemini", lambda *a: [])
        rules = vault_sleep.extract_rules_for("30-resources/old-note.md", vault)
        assert rules == []

    def test_rules_returned_from_gemini(self, vault, monkeypatch):
        monkeypatch.setattr(
            vault_sleep, "_extract_rules_with_gemini",
            lambda *a: ["RULE: Always test before deploying", "RULE: Never skip backups"],
        )
        rules = vault_sleep.extract_rules_for("30-resources/old-note.md", vault)
        assert len(rules) == 2
        assert all(r.startswith("RULE:") for r in rules)

    def test_rules_written_to_file(self, vault, monkeypatch):
        monkeypatch.setattr(
            vault_sleep, "_extract_rules_with_gemini",
            lambda *a: ["RULE: Use DuckDB not SQLite"],
        )
        vault_sleep._append_rules_to_file(vault, "30-resources/old-note.md", ["RULE: Use DuckDB not SQLite"])
        rules_path = vault / "memory" / "rules.md"
        assert rules_path.exists()
        content = rules_path.read_text()
        assert "RULE: Use DuckDB not SQLite" in content
        assert "old-note" in content  # tagged with source stem

    def test_rules_replace_old_entries_same_note(self, vault):
        vault_sleep._append_rules_to_file(vault, "30-resources/old-note.md", ["RULE: Old rule"])
        vault_sleep._append_rules_to_file(vault, "30-resources/old-note.md", ["RULE: New rule"])
        content = (vault / "memory" / "rules.md").read_text()
        assert "RULE: New rule" in content
        assert "RULE: Old rule" not in content

    def test_run_rules_extraction_skips_low_access(self, vault, monkeypatch):
        monkeypatch.setattr(vault_sleep, "_extract_rules_with_gemini", lambda *a: ["RULE: X"])
        # old-note has access_count=0 (below min_access=5)
        result = vault_sleep.run_rules_extraction(vault, min_access=5)
        assert result["processed"] == 0

    def test_run_rules_extraction_runs_on_high_access(self, vault, monkeypatch):
        monkeypatch.setattr(vault_sleep, "_extract_rules_with_gemini", lambda *a: ["RULE: X"])
        # Force high access count
        for _ in range(6):
            vault_db.record_access("30-resources/old-note.md")
        result = vault_sleep.run_rules_extraction(vault, min_access=5)
        assert result["processed"] == 1
        assert result["total_rules"] == 1


class TestPruneArchive:
    def _make_old_archive(self, vault: Path, name: str, days_old: int) -> Path:
        today = date.today()
        month_dir = vault / "40-archive" / f"{today.year}-{today.month:02d}"
        month_dir.mkdir(parents=True, exist_ok=True)
        f = month_dir / name
        f.write_text("---\ntitle: Old\n---\n\nbody", encoding="utf-8")
        # Set mtime to simulate age
        import os, time
        old_time = time.time() - days_old * 86400
        os.utime(f, (old_time, old_time))
        return f

    def test_dry_run_does_not_delete(self, vault):
        f = self._make_old_archive(vault, "old-note.md", days_old=400)
        vault_db.update_snapshot("30-resources/old-note.md", "/fake/snap.png", "small", 100)
        result = vault_sleep.prune_archive(vault, min_age_days=365, dry_run=True)
        assert f.exists()
        assert result["deleted"] == 0

    def test_deletes_old_file_with_snapshot(self, vault):
        f = self._make_old_archive(vault, "old-note.md", days_old=400)
        vault_db.update_snapshot("30-resources/old-note.md", "/fake/snap.png", "small", 100)
        result = vault_sleep.prune_archive(vault, min_age_days=365, dry_run=False)
        assert not f.exists()
        assert result["deleted"] >= 1

    def test_skips_young_files(self, vault):
        f = self._make_old_archive(vault, "old-note.md", days_old=10)
        vault_db.update_snapshot("30-resources/old-note.md", "/fake/snap.png", "small", 100)
        result = vault_sleep.prune_archive(vault, min_age_days=365, dry_run=False)
        assert f.exists()
        statuses = [e["status"] for e in result["log"]]
        assert "too_young" in statuses

    def test_skips_files_without_snapshot(self, vault):
        f = self._make_old_archive(vault, "no-snap-note.md", days_old=400)
        result = vault_sleep.prune_archive(vault, min_age_days=365, dry_run=False)
        assert f.exists()
        statuses = [e["status"] for e in result["log"]]
        assert "no_snapshot" in statuses

    def test_no_archive_dir(self, tmp_path):
        result = vault_sleep.prune_archive(tmp_path, min_age_days=365, dry_run=False)
        assert result["deleted"] == 0
        assert "No archive" in result["message"]


# ---------------------------------------------------------------------------
# Phase 6.1 — Archive status correctness
# ---------------------------------------------------------------------------

class TestArchiveStatusOnOriginalNote:
    """Phase 6.1: run_sleep must mark the *original* note as 'archived' and
    the archive copy as 'archive_backup', not the other way around."""

    def test_original_note_marked_archived(self, vault):
        with patch.object(vault_sleep, "_compress_with_gemini", return_value=None), \
             patch.object(vault_sleep, "_compress_with_claude", return_value=None), \
             patch.object(vault_sleep, "_fig") as mock_fig:
            mock_fig.render_note_to_png.return_value = None
            vault_sleep.run_sleep(vault, min_age_days=1, dry_run=False)

        with vault_db._connect() as con:
            row = con.execute(
                "SELECT status FROM notes WHERE path = ?",
                ["30-resources/old-note.md"],
            ).fetchone()

        assert row is not None, "Original note not found in DB after sleep"
        assert row[0] == "archived", (
            f"Expected original note status='archived', got {row[0]!r}"
        )

    def test_archive_copy_marked_archive_backup(self, vault):
        with patch.object(vault_sleep, "_compress_with_gemini", return_value=None), \
             patch.object(vault_sleep, "_compress_with_claude", return_value=None), \
             patch.object(vault_sleep, "_fig") as mock_fig:
            mock_fig.render_note_to_png.return_value = None
            vault_sleep.run_sleep(vault, min_age_days=1, dry_run=False)

        archive_files = list((vault / "40-archive").rglob("*.md"))
        assert archive_files, "No archive copy was created"
        archive_rel = str(archive_files[0].relative_to(vault))

        with vault_db._connect() as con:
            row = con.execute(
                "SELECT status FROM notes WHERE path = ?",
                [archive_rel],
            ).fetchone()

        assert row is not None, f"Archive copy {archive_rel!r} not found in DB"
        assert row[0] == "archive_backup", (
            f"Expected archive copy status='archive_backup', got {row[0]!r}"
        )
