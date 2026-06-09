#!/usr/bin/env python3
"""Second Brain MCP Server — domain-specific tools for the personal knowledge vault. (Trigger Restart 2)"""


import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP, Image

import vault_db
from vault_db import KNOWLEDGE_EXCLUDE
import vault_sleep as _vs
import figures as _fig

VAULT = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    Path.home() / "second-brain"
)).expanduser().resolve()

# ── 防止多 server 並存：kill 同腳本的舊進程 ──────────────────────────────────
_PID_FILE = Path.home() / ".second-brain" / "server.pid"

def _kill_old_server() -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                # Wait up to 3s for graceful exit before force-killing
                for _ in range(6):
                    time.sleep(0.5)
                    try:
                        os.kill(old_pid, 0)  # check still alive
                    except ProcessLookupError:
                        break  # exited cleanly
                else:
                    try:
                        os.kill(old_pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    _PID_FILE.write_text(str(os.getpid()))

_kill_old_server()
# ─────────────────────────────────────────────────────────────────────────────

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
    "mcp":       ("10-projects",       "templates/mcp-project-template.md"),
    "resource":  ("30-resources",      "templates/note-template.md"),
    "reference": ("30-resources",      "templates/note-template.md"),
}
_DEFAULT_CONFIG = ("00-inbox", "templates/note-template.md")

# When a note matches a project slug, note_type → subfolder within project
_PROJECT_SUBTYPE_MAP: dict[str, str] = {
    "coding":    "phases",
    "research":  "research",
    "paper":     "research",
    "finding":   "research",
    "resource":  "docs",
    "reference": "docs",
    "tool":      "docs",
}


def _load_project_registry() -> dict[str, str]:
    """Parse PROJECT_REGISTRY.md table → {slug: project_folder}.

    Only includes projects with a dedicated subfolder (e.g. 10-projects/second-brain/).
    Flat projects (overview directly in 10-projects/) are excluded from routing.
    """
    reg_path = VAULT / "10-projects" / "PROJECT_REGISTRY.md"
    if not reg_path.exists():
        return {}
    result: dict[str, str] = {}
    for line in reg_path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]
        if len(parts) < 3:
            continue
        slug = parts[0]
        if slug.startswith("-") or slug.lower() in ("slug", "---"):
            continue
        overview = parts[2]  # e.g. "10-projects/second-brain/overview.md"
        folder = str(Path(overview).parent)
        # Only route projects that have their own subfolder
        if folder != "10-projects":
            result[slug] = folder
    return result


def _detect_project_slug(title: str, tags: str, registry: dict[str, str]) -> str | None:
    """Return matching project slug if title or tags contain a known slug."""
    combined = (title + " " + tags).lower()
    # Prefer longer slugs first to avoid partial matches
    for slug in sorted(registry, key=len, reverse=True):
        if slug.lower() in combined:
            return slug
    return None


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text


_index_lock = threading.Lock()


def _append_to_index(rel: str, label: str, today: str) -> None:
    index_path = VAULT / "memory" / "index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with _index_lock:
        index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
        if f"]({rel})" not in index_text:
            with index_path.open("a", encoding="utf-8") as f:
                f.write(f"\n- [{label}]({rel}) — {today}")


def _safe_yaml(value: str) -> str:
    import json as _json
    return _json.dumps(value.strip())[1:-1]  # JSON-escaped without outer quotes


_TAG_UNSAFE_RE = re.compile(r'[\[\]:{}"\'|>&!*,\n\r]')


def _safe_tag(tag: str) -> str:
    """Strip YAML-unsafe characters from a single tag."""
    return _TAG_UNSAFE_RE.sub("", tag).strip()


_ALLOWED_LOCAL_EXTENSIONS = {".pdf", ".docx", ".pptx", ".txt", ".md"}


_ALLOWED_LOCAL_ROOTS = [
    VAULT,
    Path.home() / "Downloads",
    Path.home() / "Desktop",
]

# NOTE: _is_ssrf_safe() resolves DNS once here, but MarkItDown resolves DNS again
# on the actual HTTP request — DNS rebinding can bypass this check. This is an
# architectural limitation of using MarkItDown as a black-box converter.
def _validate_source(source: str) -> str | None:
    """Return source if safe to pass to MarkItDown, else None.

    - http/https: allowed only when the resolved IP is not private/loopback (SSRF guard).
    - Local paths: allowed only for document extensions and within approved directories.
    - Everything else (file://, ftp://, bare /etc/passwd, etc.): rejected.
    """
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        return source if _fig._is_ssrf_safe(source) else None
    if parsed.scheme in ("", "file") or not parsed.scheme:
        p = Path(source).resolve()
        if p.suffix.lower() not in _ALLOWED_LOCAL_EXTENSIONS:
            return None
        if not p.exists():
            return None
        if not any(p.is_relative_to(root) for root in _ALLOWED_LOCAL_ROOTS):
            return None
        return str(p)
    return None


def _extract_semantic_keywords_via_gemini(content: str) -> list[str]:
    """Call Gemini CLI to extract up to 10 semantic keywords from content.

    Returns empty list if CLI unavailable or extraction fails — never raises.
    """
    gemini_cli = shutil.which("gemini")
    if not gemini_cli:
        return []
    prompt = (
        "從以下文章中提取最多10個繁體中文語義關鍵字（同義詞、概念、主題），"
        "以JSON array格式回傳，例如：[\"關鍵字1\",\"關鍵字2\"]，只輸出JSON array，不要其他文字。\n\n"
        + content[:2000]
    )
    try:
        env = os.environ.copy()
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "false"
        result = subprocess.run(
            [gemini_cli, "-"],
            input=prompt,
            capture_output=True, text=True, timeout=60, env=env,
            cwd=str(Path.home()),
        )
        output = result.stdout.strip()
        m = re.search(r"\[.*?\]", output, re.DOTALL)
        if m:
            keywords = json.loads(m.group())
            return [str(k) for k in keywords if k][:10]
        # Fallback: comma-separated plain text
        return [s.strip() for s in output.split(",") if s.strip()][:10]
    except Exception as e:
        print(f"[second-brain] semantic keyword extraction failed: {e}", file=sys.stderr)
        return []


def _inject_semantic_keywords(note_path: Path, keywords: list[str]) -> None:
    """Write semantic_keywords into the frontmatter of an existing note file."""
    text = note_path.read_text(encoding="utf-8")
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not fm_match:
        return
    fm_text = fm_match.group(1)
    kw_line = f"semantic_keywords: {json.dumps(keywords, ensure_ascii=False)}"
    if "semantic_keywords:" in fm_text:
        fm_text = re.sub(r"^semantic_keywords:.*$", kw_line, fm_text, flags=re.MULTILINE)
    else:
        fm_text += f"\n{kw_line}"
    new_text = f"---\n{fm_text}\n---\n\n" + text[fm_match.end():]
    if new_text != text:
        note_path.write_text(new_text, encoding="utf-8")


def _run_keyword_enrichment_async(dest: Path, content: str) -> None:
    """Fire-and-forget: extract semantic keywords via Gemini and re-index in background thread.

    Returns immediately — never blocks the caller.
    """
    def _worker():
        try:
            sk = _extract_semantic_keywords_via_gemini(content)
            if sk:
                _inject_semantic_keywords(dest, sk)
                with vault_db._connect() as con:
                    vault_db.upsert_note(con, VAULT, dest)
        except Exception as e:
            print(f"[second-brain] background keyword enrichment failed for {dest.name}: {e}", file=sys.stderr)

    threading.Thread(target=_worker, daemon=True).start()


def _inject_neighbor_keywords(note_path: Path, data: dict) -> None:
    """Write neighbor_keywords and cluster_topic into the frontmatter of a note file."""
    text = note_path.read_text(encoding="utf-8")
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not fm_match:
        return
    fm_text = fm_match.group(1)
    nk_line = f"neighbor_keywords: {json.dumps(data['neighbor_keywords'], ensure_ascii=False)}"
    ct_line = f"cluster_topic: {json.dumps(data['cluster_topic'], ensure_ascii=False)}"
    for field, line in (("neighbor_keywords:", nk_line), ("cluster_topic:", ct_line)):
        if field in fm_text:
            fm_text = re.sub(f"^{field}.*$", line, fm_text, flags=re.MULTILINE)
        else:
            fm_text += f"\n{line}"
    new_text = f"---\n{fm_text}\n---\n\n" + text[fm_match.end():]
    if new_text != text:
        note_path.write_text(new_text, encoding="utf-8")


def _maybe_sync(vault: Path) -> None:
    """Sync vault index at startup if DB is stale or missing.

    Throttled to 30 minutes — skips if DB was updated recently and vault has no
    newer markdown files. Non-blocking on first run (no DB yet → full sync).
    """
    db_path = vault_db.DB_PATH
    if not db_path.exists():
        vault_db.sync_all(vault)
        return
    db_mtime = db_path.stat().st_mtime
    if time.time() - db_mtime > 1800:
        try:
            latest_md = max(
                (f.stat().st_mtime for f in vault.rglob("*.md")), default=0
            )
        except Exception:
            return
        if latest_md > db_mtime:
            vault_db.sync_incremental(vault)


def _inject_related_links(note_path: Path, rel: str) -> int:
    """Find semantically related notes and write them into the frontmatter `related` field.

    Returns count of links added (0 = no embedding server or no matches).
    """
    related = vault_db.find_related(rel, limit=5, threshold=0.7)
    if not related:
        return 0

    links = ", ".join(f"[[{r.removesuffix('.md')}]]" for r in related)
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
        top = (vault_db.top_by_score(limit=20, exclude_types=KNOWLEDGE_EXCLUDE)
               or vault_db.top_by_recency(limit=20, exclude_types=KNOWLEDGE_EXCLUDE))
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

    # Layer 2: load embedding cache once, reuse across all find_related calls
    related_section = ""
    try:
        emb_cache = vault_db.load_embedding_cache()
        related_map: dict[str, list[str]] = {}
        for r in top[:5]:
            links = vault_db.find_related(r["path"], limit=3, threshold=0.75, _embedding_cache=emb_cache)
            if links:
                related_map[r["path"]] = links
        if related_map:
            rel_lines = [
                "- {}: {}".format(Path(p).stem, " · ".join("[[{}]]".format(l.removesuffix(".md")) for l in links))
                for p, links in related_map.items()
            ]
            related_section = "\n\n---\n\n## Related Links (semantic)\n\n" + "\n".join(rel_lines)
    except Exception as e:
        print(f"[second-brain] warning: related links failed: {e}", file=sys.stderr)
        related_section = "\n\n---\n\n## Related Links (semantic)\n\n⚠️ *embedding server offline — related links unavailable*"

    return f"{rules_section}## Current Goals\n\n{goals}\n\n---\n\n{index_section}{related_section}"


@mcp.tool()
def new_note(note_type: str, title: str, content: str = "", tags: str = "") -> str:
    """Create a new note in the vault using the correct folder and template.

    If the title or tags contain a known project slug (from PROJECT_REGISTRY.md),
    the note is automatically routed into that project's subfolder:
      coding → {project}/phases/, research/paper/finding → {project}/research/,
      resource/reference/tool → {project}/docs/
    decision/adr always go to decisions/; project always goes to 10-projects/.

    Args:
        note_type: Type of note — decision, project, research, coding, resource, or inbox
        title: Human-readable title (will be converted to kebab-case filename)
        content: Optional initial content to append after the template
        tags: Comma-separated tags, e.g. 'evo-prism,architecture'. Added to frontmatter.
    """
    nt = note_type.lower()
    registry = _load_project_registry()
    matched_slug = _detect_project_slug(title, tags, registry)

    if matched_slug and nt in _PROJECT_SUBTYPE_MAP:
        proj_folder = registry[matched_slug]
        subfolder = _PROJECT_SUBTYPE_MAP[nt]
        folder = f"{proj_folder}/{subfolder}"
        _, tmpl_rel = NOTE_CONFIG.get(nt, _DEFAULT_CONFIG)
    else:
        folder, tmpl_rel = NOTE_CONFIG.get(nt, _DEFAULT_CONFIG)

    tmpl_path = VAULT / tmpl_rel

    if not tmpl_path.exists():
        return f"Error: template not found: {tmpl_rel}"

    today = date.today().isoformat()
    filled = tmpl_path.read_text(encoding="utf-8").replace("{{title}}", title).replace("{{date}}", today)
    if tags:
        tag_list = f"[{', '.join(_safe_tag(t) for t in tags.split(',') if _safe_tag(t))}]"
        filled = filled.replace("tags: []", f"tags: {tag_list}", 1)
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

    # Index immediately, then enrich keywords in background (avoids blocking on Gemini CLI)
    try:
        with vault_db._connect() as con:
            vault_db.upsert_note(con, VAULT, dest)
        n_links = _inject_related_links(dest, rel)
    except Exception as e:
        print(f"[second-brain] warning: index/link failed for {rel}: {e}", file=sys.stderr)
        n_links = 0

    _run_keyword_enrichment_async(dest, filled)

    route_msg = f" [project:{matched_slug}→{folder}]" if matched_slug and nt in _PROJECT_SUBTYPE_MAP else ""
    link_msg = f" ({n_links} related links added)" if n_links else ""
    return f"Created: {rel}{route_msg}{link_msg}"


@mcp.tool()
def search_notes(query: str) -> str:
    """Hybrid semantic + full-text search across knowledge notes (excludes daily news archives).

    Uses BM25 + cosine similarity (nomic-embed-text) when embedding server is
    available, falls back to BM25-only, then file scan.
    To search news specifically, use search_news_tool.

    Args:
        query: Search term — supports natural language and keywords
    """
    try:
        hits = vault_db.hybrid_search(query, limit=20, exclude_types=KNOWLEDGE_EXCLUDE)
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
def search_news_tool(query: str, days: int = 7) -> str:
    """Search recent cnyes daily news archives.

    Only searches cnyes_archive notes within the last N days.
    Use search_notes for knowledge base search.

    Args:
        query: Stock ticker, keyword, or company name (e.g. '2317', 'TSMC', 'AI')
        days:  How many days back to search (default 7)
    """
    try:
        hits = vault_db.search_news(query, days=days, limit=20)
    except Exception:
        hits = []

    if not hits:
        return f"No news found matching '{query}' in the last {days} days."

    lines = [f"Found {len(hits)} news note(s) for '{query}' (last {days}d):\n"]
    for h in hits:
        lines.append(f"- [{h['title']}]({h['path']}) ({h['date']})")
    return "\n".join(lines)


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
def update_note(path: str, content: str) -> str:
    """Overwrite an existing note with new content.

    Use when rewriting or restructuring a note. For adding content without
    losing existing text, use append_to_note instead.

    Args:
        path: Relative path from vault root, e.g. 'decisions/my-decision.md'
        content: Full new content to write (replaces the entire file)
    """
    full_path = (VAULT / path).resolve()
    if not full_path.is_relative_to(VAULT):
        return "Error: path must be within the vault."
    if not full_path.exists():
        return f"Note not found: {path}. Use new_note to create it."
    full_path.write_text(content, encoding="utf-8")
    try:
        with vault_db._connect() as con:
            vault_db.upsert_note(con, VAULT, full_path)
        n_links = _inject_related_links(full_path, path)
    except Exception as e:
        print(f"[second-brain] warning: index/link failed for {path}: {e}", file=sys.stderr)
        n_links = 0
    link_msg = f" ({n_links} related links refreshed)" if n_links else ""
    return f"Updated: {path}{link_msg}"


@mcp.tool()
def append_to_note(path: str, content: str) -> str:
    """Append content to the end of an existing note.

    Safer than update_note — existing text is never lost.
    Use for adding progress updates, new findings, or extra sections.

    Args:
        path: Relative path from vault root, e.g. '10-projects/my-project.md'
        content: Text to append (added after a blank line at end of file)
    """
    full_path = (VAULT / path).resolve()
    if not full_path.is_relative_to(VAULT):
        return "Error: path must be within the vault."
    if not full_path.exists():
        return f"Note not found: {path}. Use new_note to create it."
    existing = full_path.read_text(encoding="utf-8")
    separator = "\n" if existing.endswith("\n") else "\n\n"
    full_path.write_text(existing + separator + content, encoding="utf-8")
    try:
        with vault_db._connect() as con:
            vault_db.upsert_note(con, VAULT, full_path)
        _inject_related_links(full_path, path)
    except Exception as e:
        print(f"[second-brain] warning: index/link failed for {path}: {e}", file=sys.stderr)
    return f"Appended to: {path}"


@mcp.tool()
def mark_note_status(path: str, status: str) -> str:
    """Update the frontmatter status field of a note and sync to DB.

    Use this to track note lifecycle without rewriting the whole file.

    Args:
        path: Relative path from vault root, e.g. '30-resources/my-note.md'
        status: One of: active | archived | consolidated | archive_backup
    """
    allowed = {"active", "archived", "consolidated", "archive_backup"}
    if status not in allowed:
        return f"Invalid status {status!r}. Choose from: {', '.join(sorted(allowed))}"

    full_path = (VAULT / path).resolve()
    if not full_path.is_relative_to(VAULT):
        return "Error: path must be within the vault."
    if not full_path.exists():
        return f"Note not found: {path}"

    text = full_path.read_text(encoding="utf-8")
    if re.search(r"^status\s*:", text, re.MULTILINE):
        updated = re.sub(r"(?m)^(status\s*:).*", rf"\1 {status}", text)
    else:
        updated = re.sub(r"(^---\n)", rf"\1status: {status}\n", text, count=1)
    full_path.write_text(updated, encoding="utf-8")

    try:
        with vault_db._connect() as con:
            con.execute("UPDATE notes SET status = ? WHERE path = ?", [status, path])
    except Exception as e:
        print(f"[second-brain] warning: DB status update failed for {path}: {e}", file=sys.stderr)

    return f"Status updated to '{status}': {path}"


@mcp.tool()
def sync_index() -> str:
    """Rebuild the DuckDB index by scanning all vault markdown files.
    Run this after adding notes manually, or when setting up on a new machine.
    """
    result = vault_db.sync_all(VAULT)
    emb = vault_db.sync_embeddings(vault=VAULT)
    stats = vault_db.db_stats()
    embed_warn = f" ⚠️ {result['embed_failed']} notes missing embedding" if result["embed_failed"] else ""
    return (
        f"Synced {result['synced']} files → {stats['total_notes']} notes in index.{embed_warn}\n"
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

    Thresholds are read from vault/.sleep-config.json (per-folder):
    - cnyes_archive: 7 days
    - finance: 30 days
    - everything else: 90 days
    Notes with Ebbinghaus score > 0.5 are skipped.

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
            tier = _vs._tier_for_profile(c.get("score", 0.0), c["age_days"])
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


@mcp.tool()
def expand_semantic_keywords_tool(note_path: str = "", force: bool = False) -> str:
    """Batch-extract or refresh semantic_keywords for notes using Gemini CLI.

    Writes extracted keywords into each note's frontmatter and rebuilds FTS index.
    Skips notes that already have semantic_keywords unless force=True.

    Args:
        note_path: Specific vault-relative path to process (e.g. 'decisions/my-note.md').
                   Leave empty to process all indexed notes missing keywords.
        force:     If True, overwrite existing semantic_keywords (default False).

    Returns:
        Summary dict: {"processed": N, "skipped": M, "failed": K}
    """
    gemini_cli = shutil.which("gemini")
    if not gemini_cli:
        return "Gemini CLI not found — install with `npm install -g @google/generative-ai`"

    if note_path:
        paths = [note_path]
    else:
        with vault_db._connect() as con:
            if force:
                rows = con.execute("SELECT path FROM notes").fetchall()
            else:
                rows = con.execute(
                    "SELECT path FROM notes WHERE semantic_keywords IS NULL"
                ).fetchall()
        paths = [r[0] for r in rows]

    processed, skipped, failed = 0, 0, 0
    for rel in paths:
        full = (VAULT / rel).resolve()
        if not full.exists() or not full.is_relative_to(VAULT):
            failed += 1
            continue
        try:
            content = full.read_text(encoding="utf-8")
            # In single note mode, also check frontmatter (DB may be stale) when force=False
            if not force and note_path:
                fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
                if fm_match and "semantic_keywords:" in fm_match.group(1):
                    skipped += 1
                    continue
            sk = _extract_semantic_keywords_via_gemini(content)
            if sk:
                _inject_semantic_keywords(full, sk)
                with vault_db._connect() as con:
                    vault_db.upsert_note(con, VAULT, full)
                processed += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[second-brain] expand_semantic_keywords failed for {rel}: {e}", file=sys.stderr)
            failed += 1

    # Rebuild FTS index to incorporate new semantic_keywords
    if processed > 0:
        try:
            with vault_db._connect() as con:
                vault_db._ensure_fts(con)
        except Exception as e:
            print(f"[second-brain] FTS rebuild failed: {e}", file=sys.stderr)

    return str({"processed": processed, "skipped": skipped, "failed": failed})


@mcp.tool()
def enrich_neighbor_keywords_tool(note_path: str = "", force: bool = False) -> str:
    """Enrich notes with neighbor_keywords and cluster_topic derived from embedding similarity.

    Computes cosine similarity between all notes' embeddings, finds top-5 neighbors per note,
    and writes high-frequency words from neighbors back into each note's frontmatter.
    No API or model calls — pure local computation from vault.db embeddings.

    Args:
        note_path: Relative path to a single note (e.g. "10-projects/foo.md").
                   Empty string = process all notes without neighbor_keywords.
        force:     If True, overwrite existing neighbor_keywords. Default: skip existing.
    Returns:
        JSON-like string with {"enriched": N, "skipped": M, "no_neighbors": K}.
    """
    enriched = skipped = no_neighbors = 0
    try:
        all_data = vault_db.compute_neighbor_keywords()
    except Exception as e:
        return str({"error": f"compute_neighbor_keywords failed: {e}"})

    targets: list[str]
    if note_path:
        targets = [note_path]
    else:
        # Batch: only notes that have no neighbor_keywords yet (unless force)
        with vault_db._connect() as con:
            if force:
                rows = con.execute("SELECT path FROM notes WHERE embedding IS NOT NULL").fetchall()
            else:
                rows = con.execute(
                    "SELECT path FROM notes WHERE embedding IS NOT NULL AND neighbor_keywords IS NULL"
                ).fetchall()
        targets = [r[0] for r in rows]

    for path in targets:
        full = VAULT / path
        if not full.exists():
            skipped += 1
            continue
        data = all_data.get(path)
        if not data:
            no_neighbors += 1
            continue
        # In single-note mode, respect force flag via frontmatter check
        if not force and note_path:
            try:
                content = full.read_text(encoding="utf-8")
                fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
                if fm_match and "neighbor_keywords:" in fm_match.group(1):
                    skipped += 1
                    continue
            except Exception:
                pass
        try:
            _inject_neighbor_keywords(full, data)
            with vault_db._connect() as con:
                vault_db.upsert_note(con, VAULT, full)
            enriched += 1
        except Exception as e:
            print(f"[second-brain] enrich_neighbor_keywords failed for {path}: {e}", file=sys.stderr)
            skipped += 1

    if enriched > 0:
        try:
            with vault_db._connect() as con:
                vault_db._ensure_fts(con)
        except Exception as e:
            print(f"[second-brain] FTS rebuild failed: {e}", file=sys.stderr)

    return str({"enriched": enriched, "skipped": skipped, "no_neighbors": no_neighbors})


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

    tag_list = f"[{', '.join(_safe_tag(t) for t in tags.split(',') if _safe_tag(t))}]" if tags else "[]"
    frontmatter = (
        f'---\ntitle: "{_safe_yaml(title)}"\ndate: {today}\ntype: resource\n'
        f'status: active\ntags: {tag_list}\nsource: "{_safe_yaml(source)}"\n---\n\n'
    )
    dest.write_text(frontmatter + body, encoding="utf-8")

    rel = f"30-resources/{slug}.md"
    _append_to_index(rel, title, today)

    # Index immediately, then enrich keywords in background (avoids blocking on Gemini CLI)
    try:
        with vault_db._connect() as con:
            vault_db.upsert_note(con, VAULT, dest)
    except Exception as e:
        print(f"[second-brain] warning: index failed for {rel}: {e}", file=sys.stderr)

    _run_keyword_enrichment_async(dest, body)

    # Auto-link: find related notes and write into frontmatter
    n_links = _inject_related_links(dest, rel)

    # Trigger figure extraction in background (non-blocking)
    def _bg_extract():
        try:
            _fig.process_article(rel, VAULT)
        except Exception as e:
            print(f"[second-brain] figure extraction failed for {rel}: {e}", file=sys.stderr)

    threading.Thread(target=_bg_extract, daemon=True).start()

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
        return result.get("error") or f"Rendering failed for: {note_path}"

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
        snap_path = Path(row[0]).resolve()
        snap_root = (VAULT / ".snapshots").resolve()
        if snap_path.exists() and snap_path.is_relative_to(snap_root):
            vault_db.record_access(path)
            return Image(path=snap_path, format="png")

    # No snapshot — return text (capped at 32KB)
    vault_db.record_access(path)
    text = full_path.read_text(encoding="utf-8")
    _MAX_CHARS = 32_000
    excerpt = text[:_MAX_CHARS] + ("\n\n[…truncated]" if len(text) > _MAX_CHARS else "")
    hint = f"run snapshot_note_tool('{path}') to create one"
    return f"[TEXT MODE] ~{len(text)//4} tokens (no snapshot — {hint})\n\n{excerpt}"


@mcp.tool()
def find_related_notes(path: str, limit: int = 5, threshold: float = 0.7) -> str:
    """Find semantically related notes for a given note (by vault-relative path).

    Uses cosine similarity on stored embeddings. Useful for:
    - Finance: from a stock report, find related morning briefs / sector notes
    - Knowledge: after writing a note, discover overlapping existing notes

    Args:
        path:      Vault-relative path, e.g. "20-areas/personal/finance/NVDA_analysis_20260601.md"
        limit:     Max results to return (default 5)
        threshold: Minimum cosine similarity 0–1 (default 0.7)

    Returns:
        Markdown list of related note paths and titles, or a message if no embeddings found.
    """
    from vault_db import find_related, _connect
    related = find_related(path, limit=limit, threshold=threshold)
    if not related:
        return (
            f"No related notes found for `{path}` "
            f"(threshold={threshold}, embeddings may not be synced — try sync_index first)."
        )
    lines = [f"## Related notes for `{path}`\n"]
    with _connect() as con:
        for stem in related:
            row = con.execute(
                "SELECT title, note_type FROM notes WHERE path = ? OR path = ?",
                [stem, stem + ".md"],
            ).fetchone()
            title = row[0] if row else stem.split("/")[-1]
            ntype = row[1] if row else ""
            tag = f" `{ntype}`" if ntype else ""
            lines.append(f"- [[{stem.removesuffix('.md')}]] — {title}{tag}")
    return "\n".join(lines)


@mcp.tool()
def search_grouped(query: str, limit: int = 10) -> str:
    """Hybrid search that returns results split into two groups in one call:
    - **knowledge**: permanent notes, project notes, literature (excludes cnyes_archive)
    - **news**: cnyes morning briefs from the last 7 days

    Useful for finance research (get stock report + morning brief context together)
    and general knowledge work (see both deep notes and recent news at once).

    Args:
        query: Search terms, e.g. "NVDA" or "transformer architecture"
        limit: Max results per group (default 10)

    Returns:
        Markdown with two sections: Knowledge and News.
    """
    from vault_db import hybrid_search_grouped
    groups = hybrid_search_grouped(query, limit=limit)
    lines = [f"## Search: `{query}`\n"]

    knowledge = groups.get("knowledge", [])
    lines.append(f"### Knowledge ({len(knowledge)} results)\n")
    if knowledge:
        for r in knowledge:
            score = f"{r.get('score', 0):.2f}"
            lines.append(f"- [[{r['path'].removesuffix('.md')}]] — {r['title']} `{r.get('type', '')}` (score: {score})")
    else:
        lines.append("*No knowledge notes found.*")

    news = groups.get("news", [])
    lines.append(f"\n### Morning Briefs / News ({len(news)} results)\n")
    if news:
        for r in news:
            lines.append(f"- [[{r['path'].removesuffix('.md')}]] — {r['title']} `{r.get('date', '')}`")
    else:
        lines.append("*No recent morning briefs found.*")

    return "\n".join(lines)


@mcp.tool()
def top_notes(by: str = "score", limit: int = 20) -> str:
    """Return your most important notes ranked by engagement.

    Two ranking modes:
    - **score** (default): Ebbinghaus decay score = access_count / time_decay.
      High score = frequently accessed AND recently accessed. Best for finding
      your core knowledge nodes and most-researched stocks.
    - **recency**: Last accessed time. Best for resuming recent work.

    Use cases:
    - Finance: find your most-researched tickers (= notes with highest score)
    - Knowledge: find Evergreen note candidates (high score = worth refining)
    - Weekly review: top 20 notes you've engaged with most this week

    Args:
        by:    "score" or "recency" (default "score")
        limit: Number of notes to return (default 20)

    Returns:
        Ranked Markdown table of notes.
    """
    from vault_db import top_by_score, top_by_recency
    by_lower = by.strip().lower()
    if by_lower not in ("score", "recency"):
        return "❌ `by` must be 'score' or 'recency'"

    results = top_by_score(limit=limit) if by_lower == "score" else top_by_recency(limit=limit)
    if not results:
        return "No notes found in index — try sync_index first."

    label = "Ebbinghaus Score" if by_lower == "score" else "Last Accessed"
    lines = [f"## Top {limit} Notes by {label}\n"]
    lines.append(f"| # | Title | Type | {label} |")
    lines.append("|---|-------|------|---------|")
    for i, r in enumerate(results, 1):
        val = r.get("score", r.get("last_accessed", "—"))
        path_stem = r["path"].removesuffix(".md")
        lines.append(f"| {i} | [[{path_stem}]] {r['title']} | `{r.get('type', '')}` | {val} |")
    return "\n".join(lines)


def _bootstrap_vault(vault: Path) -> list[str]:
    """Ensure vault has required directories and default templates.

    Safe to re-run: only creates missing items, never overwrites existing files.
    Returns list of actions taken (empty if vault was already complete).
    """
    actions: list[str] = []

    for folder in ("00-inbox", "10-projects", "20-areas", "30-resources",
                   "40-archive", "decisions", "memory", "templates"):
        d = vault / folder
        if (d.exists() or d.is_symlink()) and not d.is_dir():
            d.unlink()  # remove any non-directory obstacle (file, symlink, junction)
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            actions.append(f"Created directory: {folder}/")

    bundled = Path(__file__).parent / "templates"
    if bundled.is_dir():
        for tmpl in bundled.glob("*.md"):
            dest = vault / "templates" / tmpl.name
            if not dest.exists():
                dest.write_text(tmpl.read_text(encoding="utf-8"), encoding="utf-8")
                actions.append(f"Created template: templates/{tmpl.name}")

    goals = vault / "memory" / "goals.md"
    if not goals.exists():
        goals.write_text(
            f"---\ntitle: Current Goals & Priorities\ndate: {date.today().isoformat()}\n"
            "type: memory\nstatus: active\ntags: [memory, goals]\n---\n\n"
            "# Current Goals\n\n## In Progress\n\n- [ ] \n\n"
            "---\n*Update this file when priorities shift.*\n",
            encoding="utf-8",
        )
        actions.append("Created memory/goals.md")

    return actions


@mcp.tool()
def init_vault() -> str:
    """Initialize or repair vault directory structure and default templates.

    Safe to re-run: only creates missing items, never overwrites existing files.
    Call this after cloning the repo or setting up on a new machine.
    """
    actions = _bootstrap_vault(VAULT)
    if actions:
        return "Vault initialized:\n" + "\n".join(f"  + {a}" for a in actions)
    return "Vault already complete — nothing to create."


@mcp.tool()
def get_agent_instructions() -> str:
    """Return the full AGENTS.md operating manual for AI agents.

    Call this at the start of a remote session (when AGENTS.md cannot be read
    from the filesystem) to learn vault structure, tool SOP, and hard constraints.

    Returns:
        str: Full contents of AGENTS.md
    """
    def _read(filename: str) -> str:
        path = Path(__file__).parent / filename
        if not path.exists():
            return f"⚠️ 找不到 {filename}"
        return path.read_text(encoding="utf-8")

    return _read("AGENTS.md")


@mcp.tool()
def health_check() -> str:
    """Diagnose second-brain system health.

    Checks: DB connectivity, note count vs vault files, WAL file size,
    duplicate server processes, embedding server, and vault accessibility.
    Returns a plain-text report with OK / WARN / ERROR per item.
    """
    import urllib.request
    lines: list[str] = ["## second-brain health check\n"]
    ok = "OK  "
    warn = "WARN"
    err = "ERR "

    # 1. Vault accessible
    try:
        md_count = sum(
            1 for f in VAULT.rglob("*.md")
            if not any(p in f.parts for p in (".obsidian", ".claude", "templates"))
        )
        lines.append(f"[{ok}] Vault accessible — {md_count} .md files found")
    except Exception as e:
        lines.append(f"[{err}] Vault not accessible: {e}")
        md_count = 0

    # 2. DB connectivity + note count
    db_path = Path.home() / ".second-brain" / "vault.db"
    try:
        import vault_db
        stats = vault_db.db_stats()
        db_count = stats.get("total_notes", 0)
        gap = md_count - db_count
        if gap > 20:
            lines.append(f"[{warn}] DB has {db_count} notes, vault has {md_count} — gap {gap} (run sync_index)")
        else:
            lines.append(f"[{ok}] DB has {db_count} notes (vault {md_count}, gap {gap})")
    except Exception as e:
        lines.append(f"[{err}] DB not connectable: {e}")

    # 3. WAL file size
    wal = db_path.with_name(db_path.name + ".wal")
    if wal.exists():
        size_mb = wal.stat().st_size / 1024 / 1024
        if size_mb > 10:
            lines.append(f"[{warn}] WAL file is {size_mb:.1f} MB (large — possible checkpoint lag)")
        else:
            lines.append(f"[{ok}] WAL file {size_mb:.1f} MB")
    else:
        lines.append(f"[{ok}] No WAL file (clean)")

    # 4. Duplicate server processes
    try:
        result = subprocess.run(
            ["pgrep", "-f", "second-brain/server.py"],
            capture_output=True, text=True
        )
        pids = [p for p in result.stdout.strip().splitlines() if p]
        if len(pids) == 0:
            lines.append(f"[{warn}] No server process found via pgrep (process may be mis-named)")
        elif len(pids) > 1:
            lines.append(f"[{warn}] {len(pids)} server processes running (PIDs: {', '.join(pids)}) — PID file may not have cleaned up")
        else:
            lines.append(f"[{ok}] 1 server process running (PID {pids[0]})")
    except Exception as e:
        lines.append(f"[{warn}] Cannot check server process count: {e}")

    # 5. Embedding server
    try:
        import vault_db as _vdb
        url = _vdb.EMBED_URL.replace("/v1/embeddings", "/health")
        with urllib.request.urlopen(url, timeout=2) as resp:
            lines.append(f"[{ok}] Embedding server reachable ({url})")
    except Exception:
        lines.append(f"[{warn}] Embedding server offline — semantic search falls back to BM25")

    lines.append("\nRun `sync_index` to rebuild index. Run `rm ~/.second-brain/vault.db*` in Terminal to reset DB.")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Second Brain MCP server")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "streamable-http", "sse"],
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--port", type=int, default=9100, help="HTTP port (default: 9100)")
    parser.add_argument(
        "--host",
        default="",
        help="Bind host for HTTP transport. Empty = FastMCP default (127.0.0.1). "
             "Use Tailscale IP for remote access; never use 0.0.0.0.",
    )
    args = parser.parse_args()

    bootstrap_log = _bootstrap_vault(VAULT)
    if bootstrap_log:
        print("[second-brain] Bootstrap:", ", ".join(bootstrap_log), file=sys.stderr)

    try:
        threading.Thread(target=_maybe_sync, args=(VAULT,), daemon=True).start()
    except Exception as _e:
        print(f"[second-brain] _maybe_sync failed (non-fatal): {_e}", file=sys.stderr)

    if args.transport == "stdio":
        mcp.run()
    else:
        # host/port are FastMCP constructor settings; update before run
        mcp.settings.port = args.port
        if args.host:
            mcp.settings.host = args.host
            if mcp.settings.transport_security:
                hosts = [args.host, f"{args.host}:*"]
                try:
                    import socket
                    hostname, _, _ = socket.gethostbyaddr(args.host)
                    if hostname:
                        hosts.extend([hostname, f"{hostname}:*"])
                except Exception:
                    pass
                mcp.settings.transport_security.allowed_hosts.extend(hosts)
                mcp.settings.transport_security.allowed_origins.extend(
                    [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
                )
        print(
            f"[second-brain] Starting {args.transport} on "
            f"{mcp.settings.host}:{mcp.settings.port}",
            file=sys.stderr,
        )
        mcp.run(transport=args.transport)
