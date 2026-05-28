#!/usr/bin/env python3
"""Second Brain MCP Server — domain-specific tools for the personal knowledge vault."""

import os
import re
import threading
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP

import vault_db
import vault_sleep as _vs
import figures as _fig

VAULT = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    Path.home() / "Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/second-brain"
)).expanduser().resolve()

mcp = FastMCP("second-brain")

# Single source of truth: note type → (folder, template)
NOTE_CONFIG: dict[str, tuple[str, str]] = {
    "decision":  ("decisions",         "templates/decision-template.md"),
    "adr":       ("decisions",         "templates/decision-template.md"),
    "project":   ("10-projects",       "templates/project-template.md"),
    "research":  ("20-areas/research", "templates/research-note-template.md"),
    "paper":     ("20-areas/research", "templates/research-note-template.md"),
    "finding":   ("20-areas/research", "templates/research-note-template.md"),
    "coding":    ("20-areas/coding",   "templates/note-template.md"),
    "tool":      ("20-areas/coding",   "templates/note-template.md"),
    "resource":  ("30-resources",      "templates/note-template.md"),
    "reference": ("30-resources",      "templates/note-template.md"),
}
_DEFAULT_CONFIG = ("00-inbox", "templates/note-template.md")


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text


def _append_to_index(rel: str, label: str, today: str) -> None:
    index_path = VAULT / "memory" / "index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    if rel not in index_text:
        with index_path.open("a", encoding="utf-8") as f:
            f.write(f"\n- [{label}]({rel}) — {today}")


def _safe_yaml(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").strip()


@mcp.tool()
def get_context() -> str:
    """Load session context: current goals + top-20 most recently active notes.
    Call this at the start of every session to orient yourself.
    """
    def _read(path: Path) -> str:
        label = path.relative_to(VAULT) if path.is_relative_to(VAULT) else path
        return path.read_text(encoding="utf-8") if path.exists() else f"(not found: {label})"

    goals = _read(VAULT / "memory" / "goals.md")

    try:
        top = vault_db.top_by_score(limit=20)
        if not top:
            top = vault_db.top_by_recency(limit=20)
        if top:
            rows = "\n".join(
                f"- [{r['title']}]({r['path']})"
                + (f" _(score: {r['score']})_" if "score" in r else "")
                for r in top
            )
            index_section = f"## Active Notes (top 20 by Ebbinghaus score)\n\n{rows}"
        else:
            raise ValueError("empty")
    except Exception:
        # Fallback to static index if DB not yet initialised
        index_section = "## Vault Index\n\n" + _read(VAULT / "memory" / "index.md")

    return f"## Current Goals\n\n{goals}\n\n---\n\n{index_section}"


@mcp.tool()
def new_note(note_type: str, title: str, content: str = "") -> str:
    """Create a new note in the vault using the correct folder and template.

    Args:
        note_type: Type of note — decision, project, research, coding, resource, or inbox
        title: Human-readable title (will be converted to kebab-case filename)
        content: Optional initial content to append after the template
    """
    folder, tmpl_rel = NOTE_CONFIG.get(note_type.lower(), _DEFAULT_CONFIG)
    tmpl_path = VAULT / tmpl_rel

    if not tmpl_path.exists():
        return f"Error: template not found: {tmpl_rel}"

    today = date.today().isoformat()
    filled = tmpl_path.read_text(encoding="utf-8").replace("{{title}}", title).replace("{{date}}", today)
    if content:
        filled += f"\n{content}\n"

    slug = _slugify(title)
    dest = VAULT / folder / f"{slug}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        return f"Note already exists: {dest.relative_to(VAULT)}"

    dest.write_text(filled, encoding="utf-8")
    rel = str(dest.relative_to(VAULT))
    _append_to_index(rel, rel, today)
    return f"Created: {rel}"


@mcp.tool()
def search_notes(query: str) -> str:
    """Full-text search across all markdown notes in the vault (DuckDB FTS).

    Args:
        query: Search term (case-insensitive)
    """
    try:
        hits = vault_db.fts_search(query, limit=20)
    except Exception:
        hits = []

    if not hits:
        # Fallback: file scan (pre-DB or query returned nothing)
        results = []
        q = query.lower()
        for md_file in sorted(VAULT.rglob("*.md")):
            if ".obsidian" in md_file.parts or ".claude" in md_file.parts:
                continue
            text = md_file.read_text(encoding="utf-8", errors="ignore")
            if q in text.lower():
                rel = md_file.relative_to(VAULT)
                for line in text.splitlines():
                    if q in line.lower():
                        results.append(f"- [{rel}]({rel})\n  > {line.strip()}")
                        break
        if not results:
            return f"No notes found matching: {query}"
        return f"Found {len(results)} note(s) [file scan]:\n\n" + "\n".join(results)

    lines = [f"- [{h['title']}]({h['path']}) (score: {h['score']:.2f})" for h in hits]
    return f"Found {len(hits)} note(s):\n\n" + "\n".join(lines)


@mcp.tool()
def get_decisions(project: str = "") -> str:
    """Get decision records from the vault.

    Args:
        project: Filter by project name (optional). If empty, returns all decisions.
    """
    decisions_dir = VAULT / "decisions"
    if not decisions_dir.exists():
        return "No decisions directory found."

    files = sorted(decisions_dir.glob("*.md"))
    if not files:
        return "No decision records found."

    results = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        if project and project.lower() not in text.lower():
            continue
        title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
        title = title_match.group(1) if title_match else f.stem
        results.append(f"- [{title}](decisions/{f.name})")

    if not results:
        msg = f"No decisions found for project: {project}" if project else "No decisions match."
        return msg
    return "\n".join(results)


@mcp.tool()
def update_goals(new_content: str) -> str:
    """Replace the contents of memory/goals.md with new content.

    Args:
        new_content: Full new content for goals.md (markdown format)
    """
    goals_path = VAULT / "memory" / "goals.md"
    goals_path.parent.mkdir(parents=True, exist_ok=True)
    goals_path.write_text(new_content, encoding="utf-8")
    return "goals.md updated."


@mcp.tool()
def read_note(path: str) -> str:
    """Read a specific note by its relative path within the vault.

    Args:
        path: Relative path from vault root, e.g. 'decisions/my-decision.md'
    """
    full_path = (VAULT / path).resolve()
    if not full_path.is_relative_to(VAULT):
        return "Error: path must be within the vault."
    if not full_path.exists():
        return f"Note not found: {path}"
    try:
        vault_db.record_access(path)
    except Exception:
        pass  # access tracking is best-effort
    return full_path.read_text(encoding="utf-8")


@mcp.tool()
def sync_index() -> str:
    """Rebuild the DuckDB index by scanning all vault markdown files.
    Run this after adding notes manually, or when setting up on a new machine.
    """
    count = vault_db.sync_all(VAULT)
    stats = vault_db.db_stats()
    return (
        f"Synced {count} files → {stats['total_notes']} notes in index.\n"
        f"DB: {stats['db_path']}\n"
        f"By type: {stats['by_type']}"
    )


@mcp.tool()
def index_stats() -> str:
    """Show vault index statistics: total notes, breakdown by type, DB location."""
    try:
        stats = vault_db.db_stats()
        lines = [f"Total: {stats['total_notes']} notes", f"DB: {stats['db_path']}", ""]
        lines += [f"  {t}: {c}" for t, c in stats["by_type"].items()]
        return "\n".join(lines)
    except Exception as e:
        return f"Index not initialised yet. Run sync_index() first. ({e})"


@mcp.tool()
def vault_sleep(dry_run: bool = False) -> str:
    """Compress old low-activity notes to slim down the vault.

    Finds notes older than 90 days with Ebbinghaus score ≤ 0.5,
    compresses them via LLM, and archives the originals.

    Args:
        dry_run: If True, show candidates without making changes.
    """
    result = _vs.run_sleep(VAULT, dry_run=dry_run)
    lines = [
        f"Candidates: {result['candidates']}",
        f"Processed:  {result['processed']}",
        f"Skipped:    {result['skipped']}",
        f"Errors:     {result['errors']}",
        "",
    ]
    for entry in result.get("log", []):
        status = entry["status"]
        path = entry["path"]
        if status == "compressed":
            snap = "📷" if entry.get("snapshot") else "  "
            lines.append(f"  ✓ {snap} [{entry['tier']}] {path} (age {entry['age']}d)")
        elif status == "dry_run":
            lines.append(f"  ~ [{entry['tier']}] {path} (age {entry['age']}d)")
        else:
            lines.append(f"  ✗ {path} — {entry.get('reason', status)}")
    return "\n".join(lines)


@mcp.tool()
def sleep_status() -> str:
    """Check current sleep triggers and list candidates without compressing."""
    triggers = _vs.check_triggers(VAULT)
    candidates = vault_db.sleep_candidates()

    lines = ["## Sleep Status", ""]
    if triggers:
        lines += ["**Triggers active:**"] + [f"- {t}" for t in triggers]
    else:
        lines.append("No triggers active.")

    lines += ["", f"**Candidates ({len(candidates)}):**"]
    if candidates:
        for c in candidates:
            tier = _vs._tier_for_age(c["age_days"])
            lines.append(f"- [{tier:5}] score={c['score']:.3f} age={c['age_days']}d  {c['path']}")
    else:
        lines.append("None (all notes are recent or active).")

    return "\n".join(lines)


_md_converter = MarkItDown()


@mcp.tool()
def save_article(source: str, title: str = "", tags: str = "") -> str:
    """Convert a web article or PDF into a markdown note and save it to 30-resources/.

    Args:
        source: URL of a web article, or absolute path to a local PDF/DOCX file.
        title: Optional title override. If empty, inferred from the source filename or URL.
        tags: Comma-separated tags to add to frontmatter, e.g. 'bioinformatics,clustering'.
    """
    try:
        body = _md_converter.convert(source).text_content.strip()
    except Exception as e:
        return f"Conversion failed: {e}"

    if not title:
        h1 = re.search(r'^#\s+(.+)', body, re.MULTILINE)
        if h1:
            title = h1.group(1).strip()
        else:
            parsed = urlparse(source)
            stem = Path(parsed.path).stem if parsed.path else "article"
            title = stem.replace("-", " ").replace("_", " ").title()

    today = date.today().isoformat()
    slug = _slugify(title)
    dest = VAULT / "30-resources" / f"{slug}.md"

    if dest.exists():
        return f"Already saved: 30-resources/{slug}.md"

    tag_list = f"[{', '.join(t.strip() for t in tags.split(',') if t.strip())}]" if tags else "[]"
    frontmatter = (
        f'---\ntitle: "{_safe_yaml(title)}"\ndate: {today}\ntype: resource\n'
        f'status: active\ntags: {tag_list}\nsource: "{_safe_yaml(source)}"\n---\n\n'
    )
    dest.write_text(frontmatter + body, encoding="utf-8")

    rel = f"30-resources/{slug}.md"
    _append_to_index(rel, title, today)

    # Sync new note into DuckDB and trigger figure extraction asynchronously
    try:
        with vault_db._connect() as con:
            vault_db.upsert_note(con, VAULT, dest)
    except Exception:
        pass

    # Trigger figure extraction in background (non-blocking)
    threading.Thread(
        target=_fig.process_article, args=(rel, VAULT), daemon=True
    ).start()

    return f"Saved: {rel} (figure extraction started in background)"


@mcp.tool()
def extract_figures_for(note_path: str) -> str:
    """Manually trigger figure extraction for a saved article.

    Args:
        note_path: Relative path within vault, e.g. '30-resources/my-article.md'
    """
    full = (VAULT / note_path).resolve()
    if not full.is_relative_to(VAULT) or not full.exists():
        return f"Note not found: {note_path}"
    result = _fig.process_article(note_path, VAULT)
    return result


@mcp.tool()
def search_figures(query: str) -> str:
    """Search figures by OCR text or semantic description across all saved articles.

    Args:
        query: Search term, e.g. 'UMAP', 'TYRP1', 'cluster', 'p < 0.001'
    """
    hits = vault_db.search_figures(query, limit=10)
    if not hits:
        return f"No figures found matching: {query}"
    lines = [f"Found {len(hits)} figure(s) matching '{query}':\n"]
    for h in hits:
        lines.append(f"**{h['note_path']}** (fig {h['fig_index']})")
        if h["description"]:
            lines.append(f"  → {h['description']}")
        if h["ocr_text"]:
            snippet = h["ocr_text"][:120].replace("\n", " ")
            lines.append(f"  OCR: {snippet}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def snapshot_note_tool(note_path: str, tier: str = "base") -> str:
    """Render a markdown note to PNG snapshot for token-efficient storage.

    Args:
        note_path: Relative path within vault, e.g. 'decisions/my-note.md'
        tier: Resolution tier — 'large' (400 tokens), 'base' (256), 'small' (100)
    """
    full = (VAULT / note_path).resolve()
    if not full.is_relative_to(VAULT) or not full.exists():
        return f"Note not found: {note_path}"

    result = _fig.snapshot_note(note_path, VAULT, tier)
    if not result["success"]:
        return f"Rendering failed for: {note_path}"

    text_tokens = full.stat().st_size // 4
    saved = text_tokens - result["token_est"]
    pct = int(100 * (1 - result["token_est"] / max(text_tokens, 1)))
    return (
        f"Snapshot saved: {result['path']}\n"
        f"Tier: {tier} (~{result['token_est']} tokens)\n"
        f"vs text: ~{text_tokens} tokens → saves {saved} tokens ({pct}% reduction)\n"
        f"File size: {result['size_kb']} KB"
    )


@mcp.tool()
def read_note_as_image(path: str) -> str:
    """Read a note preferring PNG snapshot if available (fewer tokens), else text.

    Returns a description of how the note was served and its content summary.
    Args:
        path: Relative path from vault root
    """
    full_path = (VAULT / path).resolve()
    if not full_path.is_relative_to(VAULT) or not full_path.exists():
        return f"Note not found: {path}"

    # Check for existing snapshot in DB
    with vault_db._connect() as con:
        row = con.execute(
            "SELECT snapshot_path, snapshot_tier, snapshot_token_est FROM notes WHERE path=?",
            [path]
        ).fetchone()

    if row and row[0] and Path(row[0]).exists():
        snap_path, tier, token_est = row
        vault_db.record_access(path)
        return (
            f"[IMAGE MODE] Serving snapshot ({tier}, ~{token_est} tokens)\n"
            f"Snapshot: {snap_path}\n"
            f"To analyse this image, use extract_figures_for('{path}') or view the PNG directly."
        )

    # Fallback: return text (capped at 8000 tokens ≈ 32KB)
    vault_db.record_access(path)
    text = full_path.read_text(encoding="utf-8")
    token_est = len(text) // 4
    _MAX_CHARS = 32_000
    truncated = len(text) > _MAX_CHARS
    excerpt = text[:_MAX_CHARS] + ("\n\n[…truncated]" if truncated else "")
    hint = f"run snapshot_note_tool('{path}') to create one"
    return f"[TEXT MODE] ~{token_est} tokens (no snapshot yet — {hint})\n\n{excerpt}"


if __name__ == "__main__":
    mcp.run()
