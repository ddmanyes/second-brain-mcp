"""
vault_sleep.py — Phase 3 Vault Sleep consolidation.

All compression routes through Gemini CLI (free tier, ~15k tokens/month for 50 notes)
with Claude API as fallback. No local model required.

Resolution tiers (Phase 9: score × age dual-axis, from Experience Compression Spectrum):
  High score (>1.5)          → text   (no compression, keep full markdown)
  score >0.8 or age ≤180d    → large  (~400 tokens, ~10× compression)
  score >0.3 or age ≤365d    → base   (~256 tokens, ~20× compression)
  otherwise                  → small  (~100 tokens, ~60× compression)
"""

import re
import subprocess
from datetime import date
from pathlib import Path

import vault_db
import figures as _fig

# Resolution tiers: token estimates imported from figures to avoid duplication
TIERS = _fig.SNAPSHOT_TIERS


def _tier_for_profile(score: float, age_days: int) -> str:
    """Select compression tier from Ebbinghaus score + age (Phase 9 dual-axis).

    High-score notes are kept at higher resolution even if old, implementing
    the adaptive level selection described in Experience Compression Spectrum.
    """
    if score > 1.5:
        return "text"   # frequently accessed — keep full markdown, skip PNG tier
    if score > 0.8 or age_days <= 180:
        return "large"
    if score > 0.3 or age_days <= 365:
        return "base"
    return "small"


def _tier_for_age(age_days: int) -> str:
    """Legacy wrapper — uses score=0 (worst case) to match old age-only behaviour."""
    return _tier_for_profile(0.0, age_days)


# ---------------------------------------------------------------------------
# Compression prompt
# ---------------------------------------------------------------------------

COMPRESS_PROMPT = """You are a knowledge distillation assistant. Compress the following note.

KEEP:
- Key findings, decisions, and conclusions
- Methods, tools, and parameters used
- Numerical results and metrics
- Action items or next steps

DISCARD:
- Background and motivation paragraphs
- Repetitive explanations
- Introduction boilerplate

Output ONLY the compressed markdown note preserving the original frontmatter structure. Keep body under 300 words."""


# ---------------------------------------------------------------------------
# Model backends
# ---------------------------------------------------------------------------

def _compress_with_gemini(content: str) -> str | None:
    """Primary: Gemini CLI (free tier covers ~100k tokens/month)."""
    try:
        result = subprocess.run(
            ["gemini", "-p", f"{COMPRESS_PROMPT}\n\n---\n\n{content}"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _compress_with_claude(content: str) -> str | None:
    """Fallback: Claude API via claude CLI."""
    try:
        result = subprocess.run(
            ["claude", "-p", f"{COMPRESS_PROMPT}\n\n---\n\n{content}"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _naive_compress(content: str) -> str:
    """Last resort: keep frontmatter + headings + first line of each section."""
    lines = content.splitlines()
    fm_end = None
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                fm_end = i
                break

    if fm_end is None:
        frontmatter = ""
        body_lines = lines
    else:
        frontmatter = "\n".join(lines[:fm_end + 1])
        body_lines = lines[fm_end + 1:]
    kept, last_was_heading = [], False

    for line in body_lines:
        if line.startswith("#"):
            kept.append(line)
            last_was_heading = True
        elif last_was_heading and line.strip():
            kept.append(line)
            last_was_heading = False

    return frontmatter + "\n\n" + "\n".join(kept[:40])


def _compress_note(content: str) -> str | None:
    """Gemini → Claude → naive fallback."""
    return (
        _compress_with_gemini(content)
        or _compress_with_claude(content)
        or _naive_compress(content)
    )


# ---------------------------------------------------------------------------
# Archive & frontmatter helpers
# ---------------------------------------------------------------------------

def _archive_path(vault: Path, rel: str) -> Path:
    today = date.today()
    return vault / "40-archive" / f"{today.year}-{today.month:02d}" / Path(rel).name


def _update_frontmatter(content: str, updates: dict) -> str:
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not fm_match:
        header = "---\n" + "\n".join(f"{k}: {v}" for k, v in updates.items()) + "\n---\n\n"
        return header + content

    fm_text = fm_match.group(1)
    for key, val in updates.items():
        if re.search(rf"^{key}:", fm_text, re.MULTILINE):
            fm_text = re.sub(rf"^{key}:.*$", f"{key}: {val}", fm_text, flags=re.MULTILINE)
        else:
            fm_text += f"\n{key}: {val}"

    return f"---\n{fm_text}\n---\n\n" + content[fm_match.end():]


# ---------------------------------------------------------------------------
# Main sleep runner
# ---------------------------------------------------------------------------

def run_sleep(
    vault: Path,
    min_age_days: int = 90,
    max_score: float = 0.5,
    dry_run: bool = False,
) -> dict:
    """Find sleep candidates and compress them via Gemini/Claude."""
    candidates = vault_db.sleep_candidates(
        min_age_days=min_age_days, max_score=max_score
    )

    if not candidates:
        return {
            "processed": 0, "skipped": 0, "errors": 0,
            "candidates": 0, "log": [], "message": "No candidates found.",
        }

    processed, skipped, errors = 0, 0, 0
    log: list[dict] = []

    for candidate in candidates:
        rel = candidate["path"]
        age = candidate["age_days"]
        score = candidate.get("score", 0.0)
        tier = _tier_for_profile(score, age)
        note_path = vault / rel

        if not note_path.exists():
            errors += 1
            log.append({"path": rel, "status": "error", "reason": "file not found"})
            continue

        if dry_run:
            log.append({"path": rel, "status": "dry_run", "tier": tier, "age": age, "score": score})
            skipped += 1
            continue

        # High-score notes keep full text — skip compression, only render snapshot
        if tier == "text":
            try:
                _fig.render_note_to_png(rel, vault, "large")
            except Exception:
                pass
            skipped += 1
            log.append({"path": rel, "status": "skipped_high_score", "score": score, "age": age})
            continue

        original = note_path.read_text(encoding="utf-8")
        compressed = _compress_note(original)

        if not compressed:
            errors += 1
            log.append({"path": rel, "status": "error", "reason": "all backends failed"})
            continue

        today = date.today().isoformat()
        compressed = _update_frontmatter(compressed, {
            "consolidated": "true",
            "consolidated_date": today,
            "tier": tier,
            "token_est": TIERS[tier]["token_est"],
            "archive_path": str(_archive_path(vault, rel).relative_to(vault)),
        })

        archive = _archive_path(vault, rel)
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(original, encoding="utf-8")
        note_path.write_text(compressed, encoding="utf-8")

        try:
            with vault_db._connect() as con:
                vault_db.upsert_note(con, vault, note_path)
                con.execute(
                    "UPDATE notes SET status = 'archived' WHERE path = ?",
                    [str(archive.relative_to(vault))],
                )
        except Exception:
            pass

        # Phase 4C: render compressed note to PNG snapshot at the correct tier
        snapshot_path = None
        try:
            snap = _fig.render_note_to_png(rel, vault, tier)
            snapshot_path = str(snap) if snap else None
        except Exception:
            pass

        processed += 1
        log.append({
            "path": rel,
            "status": "compressed",
            "tier": tier,
            "age": age,
            "snapshot": snapshot_path,
        })

    return {
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
        "candidates": len(candidates),
        "log": log,
    }


def prune_archive(
    vault: Path,
    min_age_days: int = 365,
    dry_run: bool = False,
) -> dict:
    """Delete archived originals that are old enough AND whose active note has a snapshot.

    Safety rule: only delete if a snapshot exists — the snapshot IS the long-term memory.
    """
    archive_dir = vault / "40-archive"
    if not archive_dir.exists():
        return {"deleted": 0, "skipped": 0, "log": [], "message": "No archive directory."}

    # Build set of note stems that have snapshots in DuckDB
    with vault_db._connect() as con:
        rows = con.execute(
            "SELECT path FROM notes WHERE snapshot_path IS NOT NULL AND snapshot_path != ''"
        ).fetchall()
    snapshotted_stems = {Path(r[0]).stem for r in rows}

    today = date.today()
    deleted, skipped = 0, 0
    log: list[dict] = []

    for archived in archive_dir.rglob("*.md"):
        rel = archived.relative_to(vault)
        mtime = date.fromtimestamp(archived.stat().st_mtime)
        age_days = (today - mtime).days

        if age_days < min_age_days:
            skipped += 1
            log.append({"path": str(rel), "status": "too_young", "age": age_days})
            continue

        if archived.stem not in snapshotted_stems:
            skipped += 1
            log.append({"path": str(rel), "status": "no_snapshot", "age": age_days})
            continue

        if dry_run:
            skipped += 1
            log.append({"path": str(rel), "status": "dry_run", "age": age_days})
            continue

        archived.unlink()
        deleted += 1
        log.append({"path": str(rel), "status": "deleted", "age": age_days})

    return {"deleted": deleted, "skipped": skipped, "log": log}


# ---------------------------------------------------------------------------
# Phase 7 — L3 Declarative Rules extraction
# ---------------------------------------------------------------------------

_RULES_FILE = "memory/rules.md"

_RULES_PROMPT = """You are a knowledge distillation assistant. Read this note carefully.

Extract 3-5 GENERAL, REUSABLE principles or constraints that any future AI session working on this project should always follow.

Focus on:
- Hard-won design decisions (what to do AND what NOT to do)
- Discovered anti-patterns or footguns
- Critical constraints (security, correctness, compatibility)
- Non-obvious workflow rules

Output ONLY a bullet list, one rule per line, starting each with "RULE:".
Be specific and actionable. Skip obvious best practices.

Example:
RULE: vault.db must never be synced to Google Drive — multi-machine writes cause corruption; rebuild with sync_index() instead
RULE: FastMCP Image type cannot be used in Union return annotations — Pydantic schema generation fails at server startup
"""


def _extract_rules_with_gemini(content: str) -> list[str]:
    """Call Gemini CLI to extract declarative rules from note content."""
    try:
        result = subprocess.run(
            ["gemini", "-p", f"{_RULES_PROMPT}\n\n---\n\n{content[:8000]}"],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        lines = result.stdout.strip().splitlines()
        return [l.strip() for l in lines if l.strip().startswith("RULE:")]
    except Exception:
        return []


def extract_rules_for(note_path: str, vault: Path) -> list[str]:
    """Extract L3 rules from a single note. Returns list of rule strings."""
    full = vault / note_path
    if not full.exists():
        return []
    content = full.read_text(encoding="utf-8")
    rules = _extract_rules_with_gemini(content)

    if rules:
        with vault_db._connect() as con:
            con.execute(
                "UPDATE notes SET rules_extracted_at = current_timestamp WHERE path = ?",
                [note_path],
            )
    return rules


def _append_rules_to_file(vault: Path, note_path: str, rules: list[str]) -> None:
    """Write rules into memory/rules.md, tagged with source note."""
    rules_file = vault / _RULES_FILE
    rules_file.parent.mkdir(parents=True, exist_ok=True)

    stem = Path(note_path).stem
    new_lines = [f"- [{stem}] {r}" for r in rules]

    if rules_file.exists():
        existing = rules_file.read_text(encoding="utf-8")
        # Remove old rules from this note (re-extraction replaces them)
        kept = [l for l in existing.splitlines() if f"[{stem}]" not in l]
        body = "\n".join(kept).strip()
    else:
        body = "---\ntitle: Auto-extracted Rules\ntype: memory\nstatus: active\ntags: [rules, memory]\n---\n\n# Auto-extracted Rules\n\n*Updated automatically from high-access notes.*"

    rules_file.write_text(body + "\n" + "\n".join(new_lines) + "\n", encoding="utf-8")


def run_rules_extraction(vault: Path, min_access: int = 5, stale_days: int = 90) -> dict:
    """Batch-extract L3 rules from frequently-accessed notes.

    Triggers on: access_count >= min_access AND
                 (rules_extracted_at IS NULL OR stale > stale_days days ago)
    """
    with vault_db._connect() as con:
        rows = con.execute(
            """
            SELECT path FROM notes
            WHERE access_count >= ?
              AND (rules_extracted_at IS NULL
                   OR rules_extracted_at < current_timestamp - INTERVAL (?) DAY)
            ORDER BY access_count DESC
            """,
            [min_access, stale_days],
        ).fetchall()

    processed, total_rules = 0, 0
    log: list[dict] = []

    for (note_path,) in rows:
        rules = extract_rules_for(note_path, vault)
        if rules:
            _append_rules_to_file(vault, note_path, rules)
            processed += 1
            total_rules += len(rules)
            log.append({"path": note_path, "rules": len(rules)})

    return {"processed": processed, "total_rules": total_rules, "log": log}


# ---------------------------------------------------------------------------
# Phase 8 — Cross-note Recursive Consolidation
# ---------------------------------------------------------------------------

_CONSOLIDATION_PROMPT = """You are a knowledge synthesis assistant. You will receive several related notes on a similar topic.

Your task: synthesise them into ONE concise consolidated note.

KEEP:
- Key findings, decisions, and conclusions shared across notes
- Differences and nuances between notes (summarise as sub-bullets)
- Methods, tools, parameters, and numerical results
- Action items or next steps that are still open

DISCARD:
- Duplicated background and motivation
- Boilerplate introductions

Output a single markdown note with:
- A brief ## Summary (2-3 sentences)
- ## Key Findings (bullet list of distilled insights)
- ## Differences (only if notes disagree on something)
- ## Sources (list of source note stems)

Keep body under 400 words."""


def find_consolidation_candidates(
    threshold: float = 0.85,
    min_cluster_size: int = 2,
    note_type_filter: str | None = None,
) -> list[list[str]]:
    """Find clusters of semantically similar notes for consolidation.

    Uses cosine similarity on embeddings. Returns list of clusters,
    each cluster is a list of note paths.
    Only considers notes with embeddings.
    """
    with vault_db._connect() as con:
        query = "SELECT path, note_type, embedding FROM notes WHERE embedding IS NOT NULL AND (status IS NULL OR status != 'consolidated')"
        if note_type_filter:
            query += " AND note_type = ?"
            rows = con.execute(query, [note_type_filter]).fetchall()
        else:
            rows = con.execute(query).fetchall()

    if len(rows) < min_cluster_size:
        return []

    paths = [r[0] for r in rows]
    vecs = [vault_db._blob_to_vec(r[2]) for r in rows]

    # Greedy clustering: group notes with pairwise similarity >= threshold
    used = set()
    clusters: list[list[str]] = []

    for i, (p, v) in enumerate(zip(paths, vecs)):
        if i in used:
            continue
        cluster = [p]
        used.add(i)
        for j in range(i + 1, len(paths)):
            if j in used:
                continue
            sim = vault_db._cosine(v, vecs[j])
            if sim >= threshold:
                cluster.append(paths[j])
                used.add(j)
        if len(cluster) >= min_cluster_size:
            clusters.append(cluster)

    return clusters


def consolidate_cluster(cluster: list[str], vault: Path) -> str | None:
    """Synthesise a cluster of notes into one abstract note via Gemini CLI."""
    contents = []
    for path in cluster:
        full = vault / path
        if full.exists():
            text = full.read_text(encoding="utf-8")[:3000]
            stem = Path(path).stem
            contents.append(f"=== {stem} ===\n{text}")

    if not contents:
        return None

    combined = "\n\n".join(contents)
    try:
        result = subprocess.run(
            ["gemini", "-p", f"{_CONSOLIDATION_PROMPT}\n\n---\n\n{combined}"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def run_consolidation(
    vault: Path,
    threshold: float = 0.85,
    min_cluster_size: int = 2,
    dry_run: bool = True,
) -> dict:
    """Find clusters and consolidate them. Default dry_run=True for safety."""
    clusters = find_consolidation_candidates(threshold=threshold, min_cluster_size=min_cluster_size)

    if not clusters:
        return {"clusters": 0, "consolidated": 0, "log": [], "message": "No clusters found above threshold."}

    consolidated, log = 0, []

    for cluster in clusters:
        stems = [Path(p).stem for p in cluster]
        topic = stems[0]  # use first note's stem as topic name

        if dry_run:
            log.append({"status": "dry_run", "cluster": cluster, "size": len(cluster)})
            continue

        merged = consolidate_cluster(cluster, vault)
        if not merged:
            log.append({"status": "error", "cluster": cluster, "reason": "Gemini unavailable"})
            continue

        # Write consolidated note
        today = date.today().isoformat()
        dest = vault / "20-areas" / "consolidated" / f"consolidated-{topic}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)

        frontmatter = (
            f"---\ntitle: \"Consolidated: {topic}\"\ndate: {today}\n"
            f"type: note\nstatus: active\ntags: [consolidated]\n"
            f"sources: [{', '.join(stems)}]\n---\n\n"
        )
        dest.write_text(frontmatter + merged, encoding="utf-8")

        # Mark source notes as consolidated in DB
        for path in cluster:
            with vault_db._connect() as con:
                con.execute(
                    "UPDATE notes SET status = 'consolidated' WHERE path = ?", [path]
                )

        consolidated += 1
        log.append({"status": "consolidated", "cluster": cluster, "output": str(dest.relative_to(vault))})

    return {"clusters": len(clusters), "consolidated": consolidated, "log": log}


def check_triggers(vault: Path, resource_threshold: int = 50) -> list[str]:
    """Return active sleep trigger conditions."""
    triggered = []
    resources = list((vault / "30-resources").glob("*.md"))
    if len(resources) >= resource_threshold:
        triggered.append(
            f"30-resources has {len(resources)} files (threshold: {resource_threshold})"
        )
    candidates = vault_db.sleep_candidates()
    if candidates:
        triggered.append(
            f"{len(candidates)} notes are sleep candidates (age>90d, score≤0.5)"
        )
    return triggered
