"""
vault_sleep.py — Phase 3 Vault Sleep consolidation.

All compression routes through Gemini CLI (free tier, ~15k tokens/month for 50 notes)
with Claude API as fallback. No local model required.

Resolution tiers (from DeepSeek-OCR paper):
  90–180 days  → large  (~400 tokens, ~10× compression)
  180–365 days → base   (~256 tokens, ~20× compression)
  > 365 days   → small  (~100 tokens, ~60× compression)
"""

import re
import subprocess
from datetime import date
from pathlib import Path

import vault_db
import figures as _fig

# Resolution tiers: token estimates imported from figures to avoid duplication
TIERS = _fig.SNAPSHOT_TIERS


def _tier_for_age(age_days: int) -> str:
    if age_days <= 180:
        return "large"
    if age_days <= 365:
        return "base"
    return "small"


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
        tier = _tier_for_age(age)
        note_path = vault / rel

        if not note_path.exists():
            errors += 1
            log.append({"path": rel, "status": "error", "reason": "file not found"})
            continue

        if dry_run:
            log.append({"path": rel, "status": "dry_run", "tier": tier, "age": age})
            skipped += 1
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
