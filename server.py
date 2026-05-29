#!/usr/bin/env python3
"""Second Brain MCP Server — domain-specific tools for the personal knowledge vault."""

import os
import re
import sys
import threading
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP, Image

import vault_db
import vault_sleep as _vs
import figures as _fig

VAULT = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    Path.home() / "second-brain"
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
    import json as _json
    return _json.dumps(value.strip())[1:-1]  # JSON-escaped without outer quotes


_ALLOWED_LOCAL_EXTENSIONS = {".pdf", ".docx", ".pptx", ".txt", ".md"}


def _validate_source(source: str) -> str | None:
    """Return source if safe to pass to MarkItDown, else None.

    - http/https: allowed only when the resolved IP is not private/loopback (SSRF guard).
    - Local paths: allowed only for document extensions (.pdf/.docx/.pptx/.txt/.md).
    - Everything else (file://, ftp://, bare /etc/passwd, etc.): rejected.
    """
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        return source if _fig._is_ssrf_safe(source) else None
    if parsed.scheme in ("", "file") or not parsed.scheme:
        p = Path(source).resolve()
        if p.suffix.lower() in _ALLOWED_LOCAL_EXTENSIONS and p.exists():
            return str(p)
        return None
    return None


def _inject_related_links(note_path: Path, rel: str) -> int:
    """Find semantically related notes and write them into the frontmatter `related` field.

    Returns count of links added (0 = no embedding server or no matches).
    """
    related = vault_db.find_related(rel, limit=5, threshold=0.7)
    if not related:
        return 0

    links = ", ".join(f"[[{r}]]" for r in related)
    text = note_path.read_text(encoding="utf-8")

    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not fm_match:
        return 0

    fm_text = fm_match.group(1)
    if "related:" in fm_text:
        fm_text = re.sub(r"^related:.*$", f"related: [{links}]", fm_text, flags=re.MULTILINE)
    else:
        fm_text += f"\nrelated: [{links}]"

    new_text = f"---\n{fm_text}\n---\n\n" + text[fm_match.end():]
    if new_text != text:
        note_path.write_text(new_text, encoding="utf-8")
    return len(related)


@mcp.tool()
def get_context() -> str:
    """Load session context: current goals + top-20 most recently active notes.
    Call this at the start of every session to orient yourself.
    """
    def _read(path: Path) -> str:
        label = path.relative_to(VAULT) if path.is_relative_to(VAULT) else path
        return path.read_text(encoding="utf-8") if path.exists() else f"(not found: {label})"

    goals = _read(VAULT / "memory" / "goals.md")

    # Phase 7 — L3 Rules injection
    rules_path = VAULT / "memory" / "rules.md"
    rules_section = ""
    if rules_path.exists():
        rules_text = rules_path.read_text(encoding="utf-8")
        rule_lines = [l for l in rules_text.splitlines() if l.strip().startswith("- [")]
        if rule_lines:
            rules_section = "## Active Rules (auto-extracted)\n\n" + "\n".join(rule_lines) + "\n\n---\n\n"

    top: list[dict] = []
    try:
        top = vault_db.top_by_score(limit=20) or vault_db.top_by_recency(limit=20)
        rows = "\n".join(
            f"- [{r['title']}]({r['path']})"
            + (f" _(score: {r['score']:.2f})_" if "score" in r else "")
            for r in top
        )
        index_section = f"## Active Notes (top 20 by Ebbinghaus score)\n\n{rows}" if rows else ""
        if not index_section:
            raise ValueError("empty")
    except Exception:
        index_section = "## Vault Index\n\n" + _read(VAULT / "memory" / "index.md")

    # Layer 2: reuse already-fetched top list (no second DB call)
    related_section = ""
    try:
        related_map: dict[str, list[str]] = {}
        for r in top[:5]:
            links = vault_db.find_related(r["path"], limit=3, threshold=0.75)
            if links:
                related_map[r["path"]] = links
        if related_map:
            rel_lines = [
                f"- {Path(p).stem}: {' · '.join(f'[[{l}]]' for l in links)}"
                for p, links in related_map.items()
            ]
            related_section = "\n\n---\n\n## Related Links (semantic)\n\n" + "\n".join(rel_lines)
    except Exception as e:
        print(f"[second-brain] warning: related links failed: {e}", file=sys.stderr)

    return f"{rules_section}## Current Goals\n\n{goals}\n\n---\n\n{index_section}{related_section}"


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

    # Sync into DuckDB (includes embedding) then auto-link
    try:
        with vault_db._connect() as con:
            vault_db.upsert_note(con, VAULT, dest)
        n_links = _inject_related_links(dest, rel)
    except Exception as e:
        print(f"[second-brain] warning: index/link failed for {rel}: {e}", file=sys.stderr)
        n_links = 0

    link_msg = f" ({n_links} related links added)" if n_links else ""
    return f"Created: {rel}{link_msg}"


@mcp.tool()
def search_notes(query: str) -> str:
    """Hybrid semantic + full-text search across all vault notes.

    Uses BM25 + cosine similarity (nomic-embed-text) when embedding server is
    available, falls back to BM25-only, then file scan.

    Args:
        query: Search term — supports natural language and keywords
    """
    try:
        hits = vault_db.hybrid_search(query, limit=20)
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
    emb = vault_db.sync_embeddings(vault=VAULT)
    stats = vault_db.db_stats()
    return (
        f"Synced {count} files → {stats['total_notes']} notes in index.\n"
        f"Embeddings: +{emb['updated']} new (llama-server {'✓' if emb['updated'] or emb['failed'] == 0 else '✗ unavailable'})\n"
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
            lines.append(f"  ~ [{entry['tier']}] {path} (age {entry['age']}d, score {entry.get('score', 0):.2f})")
        elif status == "skipped_high_score":
            lines.append(f"  ⭐ [text] {path} (score {entry['score']:.2f} — kept full text)")
        else:
            lines.append(f"  ✗ {path} — {entry.get('reason', status)}")

    # Phase 7: auto-run L3 rules extraction after sleep (non-blocking)
    if not dry_run and result["processed"] > 0:
        try:
            rules_result = _vs.run_rules_extraction(VAULT)
            if rules_result["total_rules"] > 0:
                lines.append(f"\n📜 Rules extracted: {rules_result['total_rules']} rules from {rules_result['processed']} notes → memory/rules.md")
        except Exception as e:
            print(f"[second-brain] warning: rules extraction failed: {e}", file=sys.stderr)

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


@mcp.tool()
def extract_rules_tool(note_path: str = "") -> str:
    """Extract L3 declarative rules from high-access notes into memory/rules.md.

    Rules are auto-injected at the top of every get_context() call so Claude
    always has the most important project constraints in view.

    Args:
        note_path: Specific note to extract from (e.g. 'decisions/my-note.md').
                   Leave empty to run batch extraction on all eligible notes
                   (access_count >= 5, not extracted in last 90 days).
    """
    if note_path:
        full = (VAULT / note_path).resolve()
        if not full.is_relative_to(VAULT) or not full.exists():
            return f"Note not found: {note_path}"
        rules = _vs.extract_rules_for(note_path, VAULT)
        if not rules:
            return f"No rules extracted from {note_path} (Gemini unavailable or no rules found)"
        _vs._append_rules_to_file(VAULT, note_path, rules)
        return f"Extracted {len(rules)} rules from {note_path}:\n" + "\n".join(f"  {r}" for r in rules)

    result = _vs.run_rules_extraction(VAULT)
    if result["processed"] == 0:
        return "No eligible notes (need access_count >= 5). Try accessing notes first, or pass a specific note_path."
    lines = [f"Extracted {result['total_rules']} rules from {result['processed']} notes → memory/rules.md"]
    for entry in result["log"]:
        lines.append(f"  {entry['path']}: {entry['rules']} rules")
    return "\n".join(lines)


_md_converter = MarkItDown()


def _normalise_source_url(source: str) -> str:
    """Convert known abstract-only URLs to full-text equivalents.

    arxiv /abs/ pages contain only the abstract; /html/ has the full paper.
    """
    import re as _re
    # arxiv: https://arxiv.org/abs/2601.07190 → https://arxiv.org/html/2601.07190v1
    m = _re.match(r"(https?://arxiv\.org)/abs/(\d{4}\.\d+)(v\d+)?$", source)
    if m:
        base, paper_id, ver = m.group(1), m.group(2), m.group(3) or "v1"
        return f"{base}/html/{paper_id}{ver}"
    return source


@mcp.tool()
def save_article(source: str, title: str = "", tags: str = "") -> str:
    """Convert a web article or PDF into a markdown note and save it to 30-resources/.

    Args:
        source: URL of a web article, or absolute path to a local PDF/DOCX file.
        title: Optional title override. If empty, inferred from the source filename or URL.
        tags: Comma-separated tags to add to frontmatter, e.g. 'bioinformatics,clustering'.
    """
    source = _normalise_source_url(source)
    safe = _validate_source(source)
    if safe is None:
        return (
            "Unsupported source. Provide an http/https URL or a path to a "
            f".pdf/.docx/.pptx/.txt/.md file. Got: {source!r}"
        )
    source = safe
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

    # Sync new note into DuckDB (includes embedding computation)
    try:
        with vault_db._connect() as con:
            vault_db.upsert_note(con, VAULT, dest)
    except Exception as e:
        print(f"[second-brain] warning: index failed for {rel}: {e}", file=sys.stderr)

    # Auto-link: find related notes and write into frontmatter
    n_links = _inject_related_links(dest, rel)

    # Trigger figure extraction in background (non-blocking)
    threading.Thread(
        target=_fig.process_article, args=(rel, VAULT), daemon=True
    ).start()

    link_msg = f", {n_links} related links added" if n_links else ""
    return f"Saved: {rel} (figure extraction started in background{link_msg})"


@mcp.tool()
def update_links_tool(note_path: str = "") -> str:
    """Refresh auto-generated related wikilinks in one note or all notes.

    Uses semantic similarity (nomic-embed-text) to find related notes and
    writes them into the frontmatter `related` field.

    Args:
        note_path: Relative path within vault (e.g. 'decisions/my-note.md').
                   Leave empty to update ALL notes that have embeddings.
    """
    if note_path:
        full = (VAULT / note_path).resolve()
        if not full.is_relative_to(VAULT) or not full.exists():
            return f"Note not found: {note_path}"
        n = _inject_related_links(full, note_path)
        return f"Updated: {note_path} — {n} related links written"

    # Batch: update all indexed notes
    with vault_db._connect() as con:
        rows = con.execute(
            "SELECT path FROM notes WHERE embedding IS NOT NULL"
        ).fetchall()

    updated, skipped = 0, 0
    for (rel,) in rows:
        full = (VAULT / rel).resolve()
        if full.exists() and full.is_relative_to(VAULT):
            n = _inject_related_links(full, rel)
            if n:
                updated += 1
            else:
                skipped += 1

    return f"Updated {updated} notes with related links ({skipped} skipped — no matches above threshold)"


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
def consolidate_tool(threshold: float = 0.85, dry_run: bool = True) -> str:
    """Find and consolidate clusters of semantically similar notes.

    Groups notes with cosine similarity >= threshold, then uses Gemini CLI
    to synthesise each cluster into one abstract note in 20-areas/consolidated/.
    Source notes are marked status='consolidated' and deprioritised in context.

    Default dry_run=True — inspect clusters before committing.

    Args:
        threshold: Cosine similarity threshold for clustering (default 0.85)
        dry_run: If True, show clusters without consolidating (default True)
    """
    result = _vs.run_consolidation(VAULT, threshold=threshold, dry_run=dry_run)
    mode = "DRY RUN" if dry_run else "EXECUTED"
    lines = [f"[{mode}] Clusters found: {result['clusters']}, Consolidated: {result['consolidated']}"]
    if result.get("message"):
        lines.append(result["message"])
    for entry in result.get("log", []):
        status = entry["status"]
        cluster = entry.get("cluster", [])
        size = entry.get("size", len(cluster))
        if status == "dry_run":
            stems = [Path(p).stem for p in cluster]
            lines.append(f"  ~ cluster ({size}): {' + '.join(stems)}")
        elif status == "consolidated":
            lines.append(f"  ✓ → {entry['output']}")
        else:
            lines.append(f"  ✗ cluster: {entry.get('reason', status)}")
    return "\n".join(lines)


@mcp.tool()
def prune_archive_tool(min_age_days: int = 365, dry_run: bool = True) -> str:
    """Delete archived originals older than min_age_days that have a snapshot.

    Safe to run: only deletes when a PNG snapshot exists as long-term memory.
    Default dry_run=True — set to False to actually delete.

    Args:
        min_age_days: Minimum age of archived file to consider (default 365)
        dry_run: If True, only report what would be deleted (default True)
    """
    result = _vs.prune_archive(VAULT, min_age_days=min_age_days, dry_run=dry_run)
    mode = "DRY RUN" if dry_run else "EXECUTED"
    lines = [f"[{mode}] Archive prune: {result['deleted']} deleted, {result['skipped']} skipped"]
    for entry in result["log"]:
        icon = {"deleted": "🗑", "dry_run": "📋", "no_snapshot": "🔒", "too_young": "⏳"}.get(entry["status"], "?")
        lines.append(f"  {icon} {entry['path']} ({entry['age']}d) — {entry['status']}")
    return "\n".join(lines)


@mcp.tool()
def read_note_as_image(path: str):
    """Read a note as a PNG snapshot (direct image for VLM agents) or text fallback.

    Returns the PNG image directly so the calling agent (Claude, Gemini, etc.) reads
    it with its own vision model — cheaper and faster than routing through an intermediary.
    Args:
        path: Relative path from vault root
    """
    full_path = (VAULT / path).resolve()
    if not full_path.is_relative_to(VAULT) or not full_path.exists():
        return f"Note not found: {path}"

    with vault_db._connect() as con:
        row = con.execute(
            "SELECT snapshot_path FROM notes WHERE path=?",
            [path]
        ).fetchone()

    if row and row[0]:
        snap_path = Path(row[0])
        if snap_path.exists():
            vault_db.record_access(path)
            return Image(path=snap_path, format="png")

    # No snapshot — return text (capped at 32KB)
    vault_db.record_access(path)
    text = full_path.read_text(encoding="utf-8")
    _MAX_CHARS = 32_000
    excerpt = text[:_MAX_CHARS] + ("\n\n[…truncated]" if len(text) > _MAX_CHARS else "")
    hint = f"run snapshot_note_tool('{path}') to create one"
    return f"[TEXT MODE] ~{len(text)//4} tokens (no snapshot — {hint})\n\n{excerpt}"


if __name__ == "__main__":
    mcp.run()
