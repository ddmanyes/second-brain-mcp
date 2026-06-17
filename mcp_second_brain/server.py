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

from . import vault_db
from .vault_db import KNOWLEDGE_EXCLUDE
from . import vault_sleep as _vs
from . import figures as _fig
from .store import get_store
from .identity import check_admin_permission, check_write_permission, get_current_identity

VAULT = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    Path.home() / "second-brain"
)).expanduser().resolve()

_store = get_store()  # DuckDBStore or PostgresStore, selected by SB_DB_BACKEND env var


def _log_write(tool: str, target: str = "") -> None:
    """Append an immutable audit record for a write-tool invocation (MULTIUSER_PLAN P3).

    Reads actor from the per-request identity contextvar; falls back to 'unknown'
    for unauthenticated stdio/dev usage.  Never raises — audit failures must not
    interrupt actual writes.
    """
    identity = get_current_identity()
    actor = identity.user_id if identity else "unknown"
    try:
        _store.append_audit_log(actor, tool, target)
    except Exception:
        pass


# ── 防止兩個 HTTP server 搶同一個 port：kill 舊的 HTTP 進程 ──────────────────
# 注意：只有長駐的 HTTP transport（遠端 Tailscale server）才需要這個單例保護。
# stdio server（桌面版 Claude、Claude Code）是 per-client、短命、不綁 port，
# 必須能彼此並存，也能與 HTTP server 並存 —— 它們絕不呼叫 _kill_old_server()。
# 因此本函式只在 __main__ 的 HTTP 分支被呼叫，不在 import 時無條件執行。
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

# （刻意不在此處呼叫 _kill_old_server()；見上方說明，改在 HTTP 分支才呼叫）
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
    # Replace punctuation with a space (not "") so adjacent words don't merge:
    # "SOP（~/.claude 設定）" → "sop-claude-設定", not "sopclaude-設定".
    text = re.sub(r"[^\w\s-]", " ", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")


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
    Path("/Volumes/KINGSTON"),
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
                _store.index_file(VAULT, dest)
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
    """Sync vault index at startup if the index is empty or stale.

    DuckDB backend: throttled to 30 min by DB file mtime.
    Postgres backend: full sync on first use (has_index() False), then skips.
    """
    if not _store.has_index():
        _store.sync_all(vault)
        return
    # DuckDB-only throttle: skip if DB file was written within the last 30 min
    db_path = vault_db.DB_PATH
    if not db_path.exists():
        return  # Postgres backend — index already populated, skip
    db_mtime = db_path.stat().st_mtime
    if time.time() - db_mtime > 1800:
        try:
            latest_md = max(
                (f.stat().st_mtime for f in vault.rglob("*.md")), default=0
            )
        except Exception:
            return
        if latest_md > db_mtime:
            _store.sync_incremental(vault)


def _inject_related_links(note_path: Path, rel: str) -> int:
    """Find semantically related notes and write them into the frontmatter `related` field.

    Returns count of links added (0 = no embedding server or no matches).
    """
    related = _store.find_related(rel, limit=5, threshold=0.7)
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
        top = (_store.top_by_score(limit=20, exclude_types=KNOWLEDGE_EXCLUDE)
               or _store.top_by_recency(limit=20, exclude_types=KNOWLEDGE_EXCLUDE))
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
        emb_cache = _store.load_embedding_cache()
        related_map: dict[str, list[str]] = {}
        for r in top[:5]:
            links = _store.find_related(r["path"], limit=3, threshold=0.75, _embedding_cache=emb_cache)
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
    if err := check_write_permission("new_note"): return err
    _log_write("new_note", title)
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
        _store.index_file(VAULT, dest)
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
        hits = _store.hybrid_search(query, limit=20, exclude_types=KNOWLEDGE_EXCLUDE)
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
        hits = _store.search_news(query, days=days, limit=20)
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
    if err := check_write_permission("update_goals"): return err
    _log_write("update_goals", "memory/goals.md")
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
        _store.record_access(path)
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
    if err := check_write_permission("update_note"): return err
    _log_write("update_note", path)
    full_path = (VAULT / path).resolve()
    if not full_path.is_relative_to(VAULT):
        return "Error: path must be within the vault."
    if not full_path.exists():
        return f"Note not found: {path}. Use new_note to create it."
    full_path.write_text(content, encoding="utf-8")
    try:
        _store.index_file(VAULT, full_path)
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
    if err := check_write_permission("append_to_note"): return err
    _log_write("append_to_note", path)
    full_path = (VAULT / path).resolve()
    if not full_path.is_relative_to(VAULT):
        return "Error: path must be within the vault."
    if not full_path.exists():
        return f"Note not found: {path}. Use new_note to create it."
    existing = full_path.read_text(encoding="utf-8")
    separator = "\n" if existing.endswith("\n") else "\n\n"
    full_path.write_text(existing + separator + content, encoding="utf-8")
    try:
        _store.index_file(VAULT, full_path)
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
    if err := check_write_permission("mark_note_status"): return err
    _log_write("mark_note_status", path)
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
        _store.set_note_status(path, status)
    except Exception as e:
        print(f"[second-brain] warning: DB status update failed for {path}: {e}", file=sys.stderr)

    return f"Status updated to '{status}': {path}"


@mcp.tool()
def sync_index() -> str:
    """Rebuild the DuckDB index by scanning all vault markdown files.
    Run this after adding notes manually, or when setting up on a new machine.
    """
    result = _store.sync_all(VAULT)
    emb = _store.sync_embeddings(vault=VAULT)
    stats = _store.db_stats()
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
        stats = _store.db_stats()
        lines = [f"Total: {stats['total_notes']} notes", f"DB: {stats['db_path']}", ""]
        lines += [f"  {t}: {c}" for t, c in stats["by_type"].items()]
        
        fig_count = stats.get("figures")
        if fig_count is not None:
            lines.append(f"\nFigures in DB: {fig_count}")

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
    if err := check_write_permission("vault_sleep"): return err
    _log_write("vault_sleep", "")
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
    candidates = _store.sleep_candidates()

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
    if err := check_write_permission("expand_semantic_keywords_tool"): return err
    _log_write("expand_semantic_keywords_tool", note_path)
    gemini_cli = shutil.which("gemini")
    if not gemini_cli:
        return "Gemini CLI not found — install with `npm install -g @google/generative-ai`"

    if note_path:
        paths = [note_path]
    else:
        paths = _store.get_paths_for_semantic_keywords(force)

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
                _store.index_file(VAULT, full)
                processed += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[second-brain] expand_semantic_keywords failed for {rel}: {e}", file=sys.stderr)
            failed += 1

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
    if err := check_write_permission("enrich_neighbor_keywords_tool"): return err
    _log_write("enrich_neighbor_keywords_tool", note_path)
    enriched = skipped = no_neighbors = 0
    try:
        all_data = _store.compute_neighbor_keywords()
    except Exception as e:
        return str({"error": f"compute_neighbor_keywords failed: {e}"})

    targets: list[str]
    if note_path:
        targets = [note_path]
    else:
        # Batch: only notes that have no neighbor_keywords yet (unless force)
        targets = _store.get_paths_for_neighbor_keywords(force)

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
            _store.index_file(VAULT, full)
            enriched += 1
        except Exception as e:
            print(f"[second-brain] enrich_neighbor_keywords failed for {path}: {e}", file=sys.stderr)
            skipped += 1

    return str({"enriched": enriched, "skipped": skipped, "no_neighbors": no_neighbors})


_md_converter = MarkItDown()


_marker_converter = None  # lazy singleton — loaded on first PDF conversion
_marker_lock = threading.Lock()


def _get_marker_converter():
    global _marker_converter
    if _marker_converter is not None:  # fast path — no lock needed once initialised
        return _marker_converter if _marker_converter else None
    with _marker_lock:
        if _marker_converter is None:  # re-check under lock
            try:
                from marker.converters.pdf import PdfConverter
                from marker.models import create_model_dict
                print("[second-brain] loading Marker models (first PDF)…", file=sys.stderr)
                _marker_converter = PdfConverter(artifact_dict=create_model_dict())
                print("[second-brain] Marker ready", file=sys.stderr)
            except Exception as e:
                print(f"[second-brain] Marker unavailable: {e}", file=sys.stderr)
                _marker_converter = False  # sentinel: don't retry
    return _marker_converter if _marker_converter else None


def _extract_pdf_body(path: str) -> str:
    """Extract text from a PDF.
    Priority: pymupdf4llm (multi-column + reading order) → Marker (scanned PDFs)
    → pdftotext -layout → MarkItDown."""
    # primary: pymupdf4llm — clean GitHub-flavoured Markdown, detects columns,
    # tables and reading order; far less whitespace noise than pdftotext.
    try:
        import pymupdf4llm
        result = pymupdf4llm.to_markdown(path)
        if result and len(result.strip()) > 100:
            return result.strip()
    except Exception as e:
        print(f"[second-brain] pymupdf4llm failed: {e}", file=sys.stderr)
    converter = _get_marker_converter()
    if converter is not None:
        try:
            return converter(path).markdown.strip()
        except Exception as e:
            print(f"[second-brain] Marker conversion failed: {e}", file=sys.stderr)
    # fallback 1: pdftotext -layout
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # fallback 2: MarkItDown
    return _md_converter.convert(path).text_content.strip()


def _extract_pdf_title(path: str) -> str:
    """Read the PDF title from document metadata using pdfinfo (poppler).
    Returns empty string if unavailable or title looks like a fragment."""
    try:
        result = subprocess.run(
            ["pdfinfo", path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("Title:"):
                    t = line[6:].strip()
                    if len(t) < 4:
                        return ""
                    if any(kw in t.lower() for kw in ("author", "contributed equally", "copyright")):
                        return ""
                    return t
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


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
def save_article(
    source: str,
    title: str = "",
    tags: str = "",
    dest_folder: str = "30-resources",
    filename: str = "",
) -> str:
    """Convert a web article or PDF into a markdown note and save it to the vault.

    Args:
        source: URL of a web article, or absolute path to a local PDF/DOCX file.
        title: Optional title override. If empty, inferred from the source filename or URL.
        tags: Comma-separated tags to add to frontmatter, e.g. 'bioinformatics,clustering'.
        dest_folder: Vault-relative folder to save into. Defaults to '30-resources'.
                     Use '20-areas/research' for academic papers with DOI/journal.
        filename: Filename stem (without .md). If empty, auto-generated from title as kebab-slug.
                  Use 'YYYY_Author_ShortTitle' format for research papers, e.g. '2024_Bakr_ARID1A'.
    """
    if err := check_write_permission("save_article"): return err
    _log_write("save_article", source)
    source = _normalise_source_url(source)
    safe = _validate_source(source)
    if safe is None:
        return (
            "Unsupported source. Provide an http/https URL or a path to a "
            f".pdf/.docx/.pptx/.txt/.md file. Got: {source!r}"
        )
    source = safe
    is_pdf = source.lower().endswith(".pdf") and not source.startswith("http")
    try:
        body = _extract_pdf_body(source) if is_pdf else _md_converter.convert(source).text_content.strip()
    except Exception as e:
        return f"Conversion failed: {e}"

    if not title:
        if is_pdf:
            title = _extract_pdf_title(source)
        if not title:
            h1 = re.search(r'^#\s+(.+)', body, re.MULTILINE)
            if h1:
                title = h1.group(1).strip()
            else:
                parsed = urlparse(source)
                stem = Path(parsed.path).stem if parsed.path else "article"
                title = stem.replace("-", " ").replace("_", " ").title()

    today = date.today().isoformat()

    # Validate dest_folder: must resolve inside vault (prevent path traversal)
    folder = (VAULT / dest_folder).resolve()
    if not folder.is_relative_to(VAULT.resolve()):
        return f"dest_folder is outside vault: {dest_folder!r}"

    # Sanitize filename: strip, forbid path separators and traversal sequences
    filename = filename.strip()
    if filename:
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return f"filename contains invalid characters: {filename!r}"
    stem = filename if filename else _slugify(title)
    if not stem:
        return "Cannot determine a valid filename. Provide a title or filename."

    folder.mkdir(parents=True, exist_ok=True)
    dest = (folder / f"{stem}.md").resolve()
    if not dest.is_relative_to(VAULT.resolve()):
        return f"Resolved path escapes vault: {dest}"
    rel = str(dest.relative_to(VAULT.resolve()))

    if dest.exists():
        return f"Already saved: {rel}"

    # note_type follows folder path components (exact match, not startswith prefix)
    folder_parts = Path(dest_folder).parts
    note_type = (
        "research"
        if len(folder_parts) >= 2 and folder_parts[0] == "20-areas" and folder_parts[1] == "research"
        else "resource"
    )
    tag_list = f"[{', '.join(_safe_tag(t) for t in tags.split(',') if _safe_tag(t))}]" if tags else "[]"
    frontmatter = (
        f'---\ntitle: "{_safe_yaml(title)}"\ndate: {today}\ntype: {note_type}\n'
        f'status: active\ntags: {tag_list}\nsource: "{_safe_yaml(source)}"\n---\n\n'
    )
    dest.write_text(frontmatter + body, encoding="utf-8")

    _append_to_index(rel, title, today)

    # Index immediately, then enrich keywords in background (avoids blocking on Gemini CLI)
    try:
        _store.index_file(VAULT, dest)
    except Exception as e:
        print(f"[second-brain] warning: index failed for {rel}: {e}", file=sys.stderr)

    _run_keyword_enrichment_async(dest, body)

    # Auto-link: find related notes and write into frontmatter
    n_links = _inject_related_links(dest, rel)

    # Trigger figure extraction in background (non-blocking)
    def _bg_extract():
        try:
            _fig.process_article(rel, VAULT)
            # Sync figures written to DuckDB cache into the primary store (postgres when SB_DB_BACKEND=postgres)
            for fig in vault_db.get_figures_for_note(rel):
                try:
                    _store.upsert_figure(**fig)
                except Exception as fe:
                    print(f"[second-brain] figure sync to store failed: {fe}", file=sys.stderr)
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
    if err := check_write_permission("update_links_tool"): return err
    _log_write("update_links_tool", note_path)
    if note_path:
        full = (VAULT / note_path).resolve()
        if not full.is_relative_to(VAULT) or not full.exists():
            return f"Note not found: {note_path}"
        n = _inject_related_links(full, note_path)
        return f"Updated: {note_path} — {n} related links written"

    # Batch: update all indexed notes
    paths_with_emb = _store.get_paths_with_embeddings()

    updated, skipped = 0, 0
    for rel in paths_with_emb:
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
    if err := check_write_permission("extract_figures_for"): return err
    _log_write("extract_figures_for", note_path)
    full = (VAULT / note_path).resolve()
    if not full.is_relative_to(VAULT) or not full.exists():
        return f"Note not found: {note_path}"
    result = _fig.process_article(note_path, VAULT)
    for fig in vault_db.get_figures_for_note(note_path):
        try:
            _store.upsert_figure(**fig)
        except Exception as fe:
            print(f"[second-brain] figure sync to store failed: {fe}", file=sys.stderr)
    return result


@mcp.tool()
def search_figures(query: str) -> str:
    """Search figures by OCR text or semantic description across all saved articles.

    Args:
        query: Search term, e.g. 'UMAP', 'TYRP1', 'cluster', 'p < 0.001'
    """
    hits = _store.search_figures(query, limit=10)
    if not hits:
        return f"No figures found matching: {query}"
    lines = [
        f"Found {len(hits)} figure(s) matching '{query}':",
        "(text proxy below — only call read_figure if the text can't answer you)\n",
    ]
    for h in hits:
        lines.append(f"**{h['note_path']}** (fig {h['fig_index']})")
        if h.get("caption"):
            lines.append(f"  caption: {h['caption']}")
        if h["description"]:
            lines.append(f"  → {h['description']}")
        if h["ocr_text"]:
            snippet = h["ocr_text"][:120].replace("\n", " ")
            lines.append(f"  OCR: {snippet}")
        # Load ladder: nudge toward the single-figure thumbnail, not full-page render.
        lines.append(f'  → need the image: read_figure("{h["note_path"]}", {h["fig_index"]})')
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def snapshot_note_tool(note_path: str, tier: str = "base") -> str:
    """Render a markdown note to PNG snapshot for token-efficient storage.

    Args:
        note_path: Relative path within vault, e.g. 'decisions/my-note.md'
        tier: Resolution tier — 'large' (400 tokens), 'base' (256), 'small' (100)
    """
    if err := check_write_permission("snapshot_note_tool"): return err
    _log_write("snapshot_note_tool", note_path)
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
    if err := check_write_permission("consolidate_tool"): return err
    _log_write("consolidate_tool", "")
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
    if err := check_write_permission("prune_archive_tool"): return err
    _log_write("prune_archive_tool", "")
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

    _snap = _store.get_snapshot_path(path)
    if _snap:
        snap_path = Path(_snap).resolve()
        snap_root = (VAULT / ".snapshots").resolve()
        if snap_path.exists() and snap_path.is_relative_to(snap_root):
            _store.record_access(path)
            return Image(path=snap_path, format="png")

    # No snapshot — return text (capped at 32KB)
    _store.record_access(path)
    text = full_path.read_text(encoding="utf-8")
    _MAX_CHARS = 32_000
    excerpt = text[:_MAX_CHARS] + ("\n\n[…truncated]" if len(text) > _MAX_CHARS else "")
    hint = f"run snapshot_note_tool('{path}') to create one"
    return f"[TEXT MODE] ~{len(text)//4} tokens (no snapshot — {hint})\n\n{excerpt}"


@mcp.tool()
def read_figure(note_path: str, fig_index: int):
    """Load ONE extracted figure as a down-scaled image (the cheap rung of the
    recall ladder — between text search and rendering a whole page).

    Use this only when search_figures' text proxy (caption + OCR + description)
    can't answer the question. Returns a thumbnail (long edge ~768px, ~256-400
    tokens) rather than the full-resolution image or the whole page.

    Args:
        note_path: Vault-relative path of the source note, e.g. '20-areas/research/paper.md'
        fig_index: 0-based figure index as shown by search_figures
    """
    row = _store.get_figure(note_path, fig_index)
    if not row:
        return f"No figure #{fig_index} for {note_path} (try search_figures first)"

    src = Path(row["local_path"]).resolve()
    fig_root = (VAULT / "figures").resolve()
    if not (src.exists() and src.is_relative_to(fig_root)):
        return (
            f"Figure file missing or outside vault for {note_path} #{fig_index}. "
            f"caption: {row.get('caption') or '—'}; description: {row.get('description') or '—'}"
        )

    thumb = _fig.make_figure_thumbnail(src, note_path, fig_index)
    img = Image(path=thumb or src, format="png")

    # 5.8.4 — surface any prior read-time insight so next time text alone suffices.
    insight_rel = _figure_insight_rel(note_path, fig_index)
    insight_file = (VAULT / insight_rel).resolve()
    if insight_file.exists() and insight_file.is_relative_to(VAULT.resolve()):
        body = insight_file.read_text(encoding="utf-8")
        return [img, f"📝 Prior insights ([[{insight_rel.removesuffix('.md')}]]):\n\n{body}"]
    return img


# ---------------------------------------------------------------------------
# Figure insight write-back (Phase 5.8) — atomic vault notes, no DuckDB mirror
# ---------------------------------------------------------------------------

def _figure_insight_rel(note_path: str, fig_index: int) -> str:
    """Vault-relative path of the atomic insight note for one figure."""
    paper_slug = _fig._figure_slug(note_path)
    return f"20-areas/research/figure-insights/{paper_slug}--fig{fig_index:02d}.md"


def _add_figure_insight_backlink(note_path: str, fig_index: int, insight_rel: str) -> None:
    """Add an idempotent forward link in the paper note's figure section."""
    paper = (VAULT / note_path).resolve()
    if not (paper.exists() and paper.is_relative_to(VAULT.resolve())):
        return
    link = f"→ insights fig {fig_index:02d}: [[{insight_rel.removesuffix('.md')}]]"
    content = paper.read_text(encoding="utf-8")
    if link in content:
        return
    if "## Figure Insights" in content:
        content = content.replace("## Figure Insights\n", f"## Figure Insights\n{link}\n", 1)
    else:
        content = content.rstrip() + f"\n\n## Figure Insights\n{link}\n"
    paper.write_text(content, encoding="utf-8")


@mcp.tool()
def annotate_figure(note_path: str, fig_index: int, insight: str) -> str:
    """Save a read-time insight about a figure as an atomic vault note.

    Use AFTER you have loaded a figure (read_figure) and reasoned out something
    worth keeping — e.g. a specific value, trend, or conclusion. The insight is
    stored as a short standalone note (fully within the search index window) that
    backlinks the paper, so next time the question can be answered from text alone
    without re-loading the image. Insights for the same figure are appended.

    Store STRUCTURED facts ('panel C: IC50 = 2.3 µM') over prose — they cache better.

    Args:
        note_path: Vault-relative path of the source paper note
        fig_index: 0-based figure index (as shown by search_figures / read_figure)
        insight: The fact/observation to remember about this figure
    """
    if err := check_write_permission("annotate_figure"): return err
    _log_write("annotate_figure", note_path)
    insight = insight.strip()
    if not insight:
        return "Empty insight — nothing saved."

    rel = _figure_insight_rel(note_path, fig_index)
    dest = (VAULT / rel).resolve()
    if not dest.is_relative_to(VAULT.resolve()):
        return f"Resolved path escapes vault: {rel}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    paper_link = note_path.removesuffix(".md")
    if dest.exists():
        with open(dest, "a", encoding="utf-8") as f:
            f.write(f"- {today}: {insight}\n")
        action = "Appended insight to"
    else:
        paper_slug = _fig._figure_slug(note_path)
        title = f"{paper_slug} — figure {fig_index} insights"
        frontmatter = (
            f'---\ntitle: "{_safe_yaml(title)}"\ndate: {today}\n'
            f'type: figure-insight\nstatus: active\ntags: []\n'
            f'source_note: "{_safe_yaml(note_path)}"\nfig_index: {fig_index}\n---\n\n'
        )
        body = (
            f"Read-time insights for figure {fig_index} of [[{paper_link}]].\n\n"
            f"- {today}: {insight}\n"
        )
        dest.write_text(frontmatter + body, encoding="utf-8")
        _append_to_index(rel, title, today)
        action = "Created insight note"

    # Index so search_notes finds it (content_hash change triggers embedding/FTS).
    try:
        _store.index_file(VAULT, dest)
    except Exception as e:
        print(f"[second-brain] warning: index failed for {rel}: {e}", file=sys.stderr)

    _add_figure_insight_backlink(note_path, fig_index, rel)
    return f"{action}: {rel}"


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
    from .vault_db import find_related, _connect
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
    from .vault_db import hybrid_search_grouped
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
    from .vault_db import top_by_score, top_by_recency
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
    if err := check_write_permission("init_vault"): return err
    _log_write("init_vault", "")
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
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    base = Path(__file__).parent / "AGENTS.md"
    result = _read(base) if base.exists() else "⚠️ 找不到 AGENTS.md"

    # Append personal rules from vault AGENTS.md if present.
    # The vault file is intentionally not in the public repo — it stays on Drive
    # and is only visible to the vault owner's own agents.
    vault_agents = VAULT / "AGENTS.md"
    if vault_agents.exists():
        personal = _read(vault_agents).strip()
        result = result.rstrip() + "\n\n---\n\n" + personal

    return result


@mcp.tool()
def manage_api_key(
    action: str,
    raw_key: str = "",
    user_id: str = "",
    role: str = "reader",
) -> str:
    """Manage API keys for multi-user access (admin only).

    action: "register" | "revoke" | "list"
    raw_key: the plaintext API key (register/revoke). Never stored; only its SHA-256 hash is persisted.
    user_id: human-readable owner label (register/list filter).
    role: "reader" | "writer" | "admin" (register only, default "reader").

    Returns a plain-text summary of the operation.
    """
    if err := check_admin_permission("manage_api_key"):
        return err

    from .identity import hash_key, VALID_ROLES

    if action == "register":
        if not raw_key:
            return "Error: raw_key is required for register"
        if not user_id:
            return "Error: user_id is required for register"
        if role not in VALID_ROLES:
            return f"Error: role must be one of {sorted(VALID_ROLES)}"
        kh = hash_key(raw_key)
        try:
            _store.register_api_key(kh, user_id, role)
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception:
            return f"Error: failed to register key (hash_prefix={kh[:8]}) — check server logs"
        return f"Registered key for '{user_id}' (role={role}, hash_prefix={kh[:8]})"

    if action == "revoke":
        if not raw_key:
            return "Error: raw_key is required for revoke"
        kh = hash_key(raw_key)
        revoked = _store.revoke_api_key(kh)
        if revoked:
            return f"Revoked key (hash_prefix={kh[:8]})"
        return f"Key not found or already revoked (hash_prefix={kh[:8]})"

    if action == "list":
        rows = _store.list_api_keys(user_id=user_id or None)
        if not rows:
            return "No API keys found."
        lines = ["prefix   | user_id                       | role    | created_at          | revoked_at"]
        lines.append("-" * 95)
        for r in rows:
            revoked = r["revoked_at"] or "active"
            lines.append(
                f"{r['key_hash_prefix']:<8} | {r['user_id']:<29} | {r['role']:<7} | "
                f"{r['created_at'][:19]} | {revoked[:19]}"
            )
        return "\n".join(lines)

    return f"Error: unknown action '{action}'. Use register | revoke | list"


@mcp.tool()
def query_audit_log(
    user_id: str = "",
    tool_name: str = "",
    limit: int = 50,
) -> str:
    """Query the write-action audit log (admin only).

    user_id: filter by actor (optional).
    tool_name: filter by tool (optional).
    limit: max rows to return (default 50).
    """
    if err := check_admin_permission("query_audit_log"):
        return err

    rows = _store.query_audit_log(
        user_id=user_id or None,
        tool=tool_name or None,
        limit=limit,
    )
    if not rows:
        return "No audit records found."
    lines = ["ts                   | user_id                       | tool                  | target"]
    lines.append("-" * 100)
    for r in rows:
        lines.append(
            f"{str(r['ts'])[:19]} | {r['user_id']:<29} | {r['tool']:<21} | {r['target']}"
        )
    return "\n".join(lines)


@mcp.tool()
def health_check() -> str:
    """Diagnose second-brain system health.

    Checks: DB connectivity, note count vs vault files, WAL file size,
    duplicate server processes, embedding server, and vault accessibility.
    Returns a plain-text report with OK / WARN / ERROR per item.
    """
    import urllib.request
    lines: list[str] = ["## second-brain health check\n"]

    # Test PDF libraries
    pdf_libs = ['fitz', 'pypdf', 'pdfplumber', 'PyPDF2', 'pdfminer', 'pdfminer.high_level']
    pdf_res = []
    for lib in pdf_libs:
        try:
            parts = lib.split('.')
            mod = __import__(parts[0])
            for part in parts[1:]:
                mod = getattr(mod, part)
            pdf_res.append(f"{lib}: available")
        except Exception as e:
            pdf_res.append(f"{lib}: unavailable ({e})")
    lines.append("PDF libraries check:")
    lines.append("\n".join("  - " + r for r in pdf_res))
    lines.append("")

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
        stats = _store.db_stats()
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
        from . import vault_db as _vdb
        url = _vdb.EMBED_URL.replace("/v1/embeddings", "/health")
        with urllib.request.urlopen(url, timeout=2) as resp:
            lines.append(f"[{ok}] Embedding server reachable ({url})")
    except Exception:
        lines.append(f"[{warn}] Embedding server offline — semantic search falls back to BM25")

    lines.append("\nRun `sync_index` to rebuild index. Run `rm ~/.second-brain/vault.db*` in Terminal to reset DB.")
    return "\n".join(lines)


def main() -> None:
    """CLI entry point for `python -m mcp_second_brain` and `second-brain` script."""
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
        # stdio：per-client 短命程序，不殺任何人、不寫 PID，與其他 server 並存
        mcp.run()
    else:
        # HTTP：長駐單例，先殺掉舊的 HTTP server 以釋放 port，再啟動
        _kill_old_server()
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
        _run_http_with_auth(args.transport)


def _run_http_with_auth(transport: str) -> None:
    """Run the HTTP transport like FastMCP.run, but install API-key auth middleware first.

    Replicates run_streamable_http_async / run_sse_async (build app → uvicorn serve),
    adding maybe_add_api_key_auth(app) in between so a valid key is required when
    SB_API_KEY / SB_API_KEYS is set (opt-in; no-op otherwise).
    """
    import anyio
    import uvicorn

    from .auth import maybe_add_api_key_auth
    from .identity import hash_key as _hash_key

    app = mcp.sse_app() if transport == "sse" else mcp.streamable_http_app()
    # auth.py passes the raw key; store expects SHA-256 hash — wrap here
    n_keys = maybe_add_api_key_auth(
        app,
        lookup_fn=lambda raw: _store.get_identity_for_key(_hash_key(raw)),
    )
    if n_keys:
        print(f"[second-brain] API-key auth ENABLED ({n_keys} key(s))", file=sys.stderr)
    else:
        print(
            "[second-brain] API-key auth DISABLED (set SB_API_KEY to enable) "
            "— relying on Tailscale membership only",
            file=sys.stderr,
        )

    async def _serve() -> None:
        config = uvicorn.Config(
            app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=mcp.settings.log_level.lower(),
        )
        await uvicorn.Server(config).serve()

    anyio.run(_serve)


if __name__ == "__main__":
    main()
