#!/usr/bin/env python3
"""Second Brain MCP Server — domain-specific tools for the personal knowledge vault. (Trigger Restart 2)"""


import functools
import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, TypedDict
from urllib.parse import urlparse

from markitdown import MarkItDown
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from pydantic import Field

from . import vault_db
from .article_audit import audit_article_records as _audit_article_records
from .vault_db import KNOWLEDGE_EXCLUDE
from . import vault_sleep as _vs
from . import figures as _fig
from . import llm_cli
from . import frontmatter as _fm
from .store import get_store
from .identity import check_admin_permission, check_write_permission, get_current_identity
from .vault_paths import VaultPathError, resolve_in_vault

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


# ---------------------------------------------------------------------------
# Tool boundary — the seam every guarded tool passes through
# ---------------------------------------------------------------------------
#
# The prologue (permission guard + audit record + path-error surfacing) used to be
# hand-copied into each tool body, with the tool's own name repeated as a string
# literal twice per tool. Two things were only ever enforced by discipline: that a
# new write tool remembers the prologue at all, and that the literal matches the
# function. Both failed in practice (extract_rules_tool wrote memory/rules.md with
# no guard and no audit trail for months).
#
# Hoisting the prologue to the decorator makes the tool name un-driftable (taken
# from __name__) and makes "which tools write" a fact derived from code — see
# WRITE_TOOLS below, which tests enumerate instead of hand-copying.

WRITE_TOOLS: dict[str, str] = {}   # tool name → parameter whose value is audited
ADMIN_TOOLS: set[str] = set()      # tool names gated on the admin role


def _audit_target(sig: inspect.Signature, param: str | None, args, kwargs) -> str:
    """Resolve the audit target from the call's bound arguments."""
    if param is None:
        return ""
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    return str(bound.arguments.get(param, ""))


def write_tool(*, target: str | None = None, target_const: str = ""):
    """Register an MCP tool that mutates the vault.

    Owns the entire write-tool prologue — no tool body may hand-roll it:

      1. RBAC write guard (reader blocked or audited per ``SB_RBAC_ENFORCE``);
      2. an immutable audit record, actor taken from the identity contextvar;
      3. ``VaultPathError`` → the caller-visible error string it carries, so
         path validation inside the body can just raise.

    Args:
        target: name of the parameter identifying what is written (e.g. "path").
        target_const: fixed target for tools that always write the same place
            (e.g. update_goals → memory/goals.md). Mutually exclusive with target.

    The tool name comes from ``__name__``, so it can never drift from the function
    it guards, and the tool is registered in ``WRITE_TOOLS``.
    """
    if target and target_const:
        raise TypeError("pass either target or target_const, not both")

    def decorate(fn):
        name = fn.__name__
        sig = inspect.signature(fn)
        if target is not None and target not in sig.parameters:
            raise TypeError(f"{name}: audit target {target!r} is not a parameter")
        WRITE_TOOLS[name] = target or target_const

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if err := check_write_permission(name):
                return err
            _log_write(name, target_const or _audit_target(sig, target, args, kwargs))
            try:
                return fn(*args, **kwargs)
            except VaultPathError as exc:
                return str(exc)

        return mcp.tool()(wrapper)

    return decorate


def admin_tool(*, target: str | None = None, audit: bool = True):
    """Register an MCP tool gated on the admin role.

    Same seam as write_tool, but the guard is always enforced (never audit-only)
    — key management must not be reachable by non-admins even in audit mode.
    ``audit=False`` for read-only admin tools (querying the audit log must not
    itself append to the audit log).
    """
    def decorate(fn):
        name = fn.__name__
        sig = inspect.signature(fn)
        if target is not None and target not in sig.parameters:
            raise TypeError(f"{name}: audit target {target!r} is not a parameter")
        ADMIN_TOOLS.add(name)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if err := check_admin_permission(name):
                return err
            if audit:
                _log_write(name, _audit_target(sig, target, args, kwargs))
            try:
                return fn(*args, **kwargs)
            except VaultPathError as exc:
                return str(exc)

        return mcp.tool()(wrapper)

    return decorate


def _vault_path(rel: str, *, must_exist: bool = True, missing_hint: str = "") -> Path:
    """Resolve a caller-supplied vault-relative path (see vault_paths.py).

    Binds the module-level VAULT at call time so tests can monkeypatch it.
    Raises VaultPathError, which guarded tools surface automatically.
    """
    return resolve_in_vault(VAULT, rel, must_exist=must_exist, missing_hint=missing_hint)


# ── 防止兩個 HTTP server 搶同一個 port：kill 舊的 HTTP 進程 ──────────────────
# 注意：只有長駐的 HTTP transport（遠端 Tailscale server）才需要這個單例保護。
# stdio server（桌面版 Claude、Claude Code）是 per-client、短命、不綁 port，
# 必須能彼此並存，也能與 HTTP server 並存 —— 它們絕不呼叫 _kill_old_server()。
# 因此本函式只在 __main__ 的 HTTP 分支被呼叫，不在 import 時無條件執行。
_PID_FILE = Path(os.environ.get("SB_PID_FILE", str(Path.home() / ".second-brain" / "server.pid")))

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

mcp = FastMCP("second-brain", stateless_http=True)

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

# Note status vocabulary — the code counterpart of the Frontmatter Spec in AGENTS.md.
# Keep the two in sync; tests/test_note_status.py asserts they agree.
NOTE_STATUS_LIFECYCLE = frozenset({
    "active", "completed", "archived",      # project / general notes
    "proposed", "accepted", "superseded",   # decision / ADR lifecycle
})
# Owned by tooling, not set by hand: consolidate_tool and vault_sleep write these.
# Accepted here so a bulk repair can restore one, but they are not authoring states.
NOTE_STATUS_MANAGED = frozenset({"consolidated", "archive_backup"})
NOTE_STATUS_ALLOWED = NOTE_STATUS_LIFECYCLE | NOTE_STATUS_MANAGED

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
    """Extract up to 10 semantic keywords from content via the LLM CLI.

    Routes through ``llm_cli`` (Claude CLI 主，Gemini 已死備援) since Gemini free
    tier was deprecated. Returns empty list if extraction fails — never raises.
    （函式名保留向後相容，實際後端已非 Gemini。）
    """
    prompt = (
        "從以下文章中提取最多10個繁體中文語義關鍵字（同義詞、概念、主題），"
        "以JSON array格式回傳，例如：[\"關鍵字1\",\"關鍵字2\"]，只輸出JSON array，不要其他文字。\n\n"
        + content[:2000]
    )
    try:
        output = llm_cli.llm_text(prompt, timeout=90)
        if not output:
            return []
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
    _fm.set_fields_in_file(note_path, {
        "semantic_keywords": json.dumps(keywords, ensure_ascii=False),
    })


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
    _fm.set_fields_in_file(note_path, {
        "neighbor_keywords": json.dumps(data["neighbor_keywords"], ensure_ascii=False),
        "cluster_topic": json.dumps(data["cluster_topic"], ensure_ascii=False),
    })


def _maybe_sync(vault: Path) -> None:
    """Sync the vault index at startup if it is empty or the backend deems it stale.

    Empty index → full sync. Otherwise the backend decides staleness itself
    (DuckDB throttles on its DB-file mtime; Postgres relies on its scheduled sync).
    """
    if not _store.has_index():
        _store.sync_all(vault)
        return
    _store.sync_if_stale(vault)


def _inject_related_links(note_path: Path, rel: str) -> int:
    """Find semantically related notes and write them into the frontmatter `related` field.

    Returns count of links added (0 = no embedding server or no matches).
    """
    related = _store.find_related(rel, limit=5, threshold=0.7)
    if not related:
        return 0

    links = ", ".join(f"[[{r.removesuffix('.md')}]]" for r in related)
    _fm.set_fields_in_file(note_path, {"related": f"[{links}]"})
    return len(related)


def _spawn_figure_extract(rel: str) -> None:
    """Fire-and-forget figure extraction for an article, syncing figures into the store."""
    def _worker():
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

    threading.Thread(target=_worker, daemon=True).start()


def after_write(
    dest: Path,
    rel: str,
    *,
    register_label: str | None = None,
    relink: bool = True,
    enrich: str | None = None,
    extract_figures: bool = False,
) -> int:
    """Post-write index + enrichment tail shared by every note write path.

    Invariant: whenever a note's bytes change on disk, the store is re-indexed —
    skipping it silently drops the note from search. Everything else is an optional
    variation the caller opts into:

        register_label   register a NEW note into the project index (label shown there)
        relink           refresh the frontmatter `related` links (returns the count)
        enrich           fire-and-forget semantic-keyword enrichment over this content
        extract_figures  fire-and-forget figure extraction (articles only)

    Never raises: index/relink failures warn to stderr (the write already landed);
    background threads contain their own exceptions. Returns the number of related
    links written (0 on failure or when relink is False).
    """
    if register_label is not None:
        _append_to_index(rel, register_label, date.today().isoformat())
    n_links = 0
    try:
        _store.index_file(VAULT, dest)
        if relink:
            n_links = _inject_related_links(dest, rel)
    except Exception as e:
        print(f"[second-brain] warning: index/link failed for {rel}: {e}", file=sys.stderr)
    if enrich is not None:
        _run_keyword_enrichment_async(dest, enrich)
    if extract_figures:
        _spawn_figure_extract(rel)
    return n_links


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


@write_tool(target="title")
def new_note(note_type: str, title: str, content: str = "", tags: str = "") -> str:
    """Create a new note in the vault using the correct folder and template.

    If the title or tags contain a known project slug (from PROJECT_REGISTRY.md),
    the note is automatically routed into that project's subfolder:
      coding → {project}/phases/, research/paper/finding → {project}/research/,
      resource/reference/tool → {project}/docs/
    decision/adr always go to decisions/; project always goes to 10-projects/.
    A coding-type note whose title slug starts with "fix-" routes to {project}/fixes/
    instead of phases/ — a postmortem isn't an in-flight phase plan.

    Args:
        note_type: Type of note — decision, project, research, coding, resource, or inbox
        title: Human-readable title (will be converted to kebab-case filename)
        content: Optional initial content to append after the template
        tags: Comma-separated tags, e.g. 'evo-prism,architecture'. Added to frontmatter.
    """
    nt = note_type.lower()
    registry = _load_project_registry()
    matched_slug = _detect_project_slug(title, tags, registry)
    slug = _slugify(title)

    if matched_slug and nt in _PROJECT_SUBTYPE_MAP:
        proj_folder = registry[matched_slug]
        subfolder = _PROJECT_SUBTYPE_MAP[nt]
        if subfolder == "phases" and slug.startswith("fix-"):
            # A fix-* note filed under note_type="coding" is a postmortem, not an in-flight
            # phase plan — route it straight to fixes/ so it doesn't need a manual move (and
            # its cross-references updated by hand) afterward. See E5 in
            # litnet-抽取稀疏的定案根因-120-秒-timeout... (2026-08-18).
            subfolder = "fixes"
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

    dest = VAULT / folder / f"{slug}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        return f"Note already exists: {dest.relative_to(VAULT)}"

    dest.write_text(filled, encoding="utf-8")
    rel = str(dest.relative_to(VAULT))
    n_links = after_write(dest, rel, register_label=rel, enrich=filled)

    route_msg = f" [project:{matched_slug}→{folder}]" if matched_slug and nt in _PROJECT_SUBTYPE_MAP else ""
    link_msg = f" ({n_links} related links added)" if n_links else ""
    return f"Created: {rel}{route_msg}{link_msg}"


@mcp.tool()
def search_notes(query: str) -> str:
    """Hybrid semantic + full-text search across knowledge notes (excludes daily news archives).

    Uses BM25 + cosine similarity (bge-m3, 1024d) when embedding server is
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
def search_snippets(query: str, top_k: int = 8) -> str:
    """Precise localization: return the VERBATIM source sentence from each of the most relevant
    notes, with its citation. The sentence is quoted exactly from the paper, never rewritten —
    ideal for 'what does the literature say about X' or 'a factor's role': jumps to the passage.

    Args:
        query: keyword or phrase, e.g. 'TGF-beta fibrosis', 'hair follicle stem cell niche'.
        top_k: how many notes to pull snippets from (default 8).
    """
    from . import snippets
    _log_write("search_snippets", query)
    try:
        hits = _store.hybrid_search(query, limit=top_k, exclude_types=KNOWLEDGE_EXCLUDE)
    except Exception:
        hits = []
    out, logged = [], []
    for h in hits:
        try:
            full = _vault_path(h["path"])
        except VaultPathError:
            continue
        raw = full.read_text(encoding="utf-8", errors="ignore")
        snip = snippets.best_snippet(raw, h.get("title", ""), query)
        if not snip:
            continue
        fm = raw.split("---")[1] if raw.startswith("---") and "---" in raw[3:] else ""
        dm = re.search(r'^doi:\s*"?(.+?)"?\s*$', fm, re.MULTILINE)
        src = f" · doi:{dm.group(1)}" if dm else ""
        out.append(f"- **{h.get('title','')}** (score {h['score']:.2f}){src}\n"
                   f"  > {snip}\n  [{h['path']}]({h['path']})")
        logged.append(h["path"])
    try:  # eval hook: query log (jsonl) for the future relevance loop
        from datetime import datetime as _dt
        (VAULT / ".query-log.jsonl").open("a", encoding="utf-8").write(
            json.dumps({"ts": _dt.now().isoformat(timespec="seconds"),
                        "query": query, "results": logged}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    if not out:
        return f"No verbatim snippet found for: {query}"
    return f"{len(out)} snippet(s) for '{query}':\n\n" + "\n\n".join(out)


# ── Knowledge graph (C-layer): canonical typed-edge store lives INSIDE the vault
#    (VAULT/.graph/statements.jsonl) so the read-only query tool has a stable home;
#    batch extraction (lcdda-ingest/extract_statements.py) promotes validated
#    statements here.  query_graph is dual-path: typed edges (precision) + snippet
#    full-text recall net (無遺漏), per the C-layer spec.
_GRAPH_SYN = {  # whole-string canonicalisation (applied last)
    "transforming growth factor beta": "tgfb", "transforming growth factor beta 1": "tgfb1",
    "pf": "pulmonary fibrosis", "ckd": "chronic kidney disease",
}
_GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "κ": "kappa"}


def _graph_norm(name: str) -> str:
    """Ground an entity string so variant spellings of the same concept collapse:
    lowercase, Greek→latin, TGF-β/TGFβ/TGF beta→tgfb, British 'signalling'→'signaling',
    then a small whole-string synonym/abbreviation map. Keeps distinct concepts distinct
    (e.g. 'renal fibrosis' ≠ 'liver fibrosis') — only unifies genuine variants."""
    n = (name or "").strip().lower()
    for g, r in _GREEK.items():
        n = n.replace(g, r)
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"tgf[-\s]?beta", "tgfb", n)   # TGF-β / TGFβ / TGF beta → tgfb (incl. tgfb1)
    n = n.replace("signalling", "signaling")
    return _GRAPH_SYN.get(n, n)


def _graph_path() -> Path:
    p = os.environ.get("SB_GRAPH_PATH")
    return Path(p) if p else (VAULT / ".graph" / "statements.jsonl")


def _load_edges() -> list[dict]:
    gp = _graph_path()
    if not gp.exists():
        return []
    edges = []
    for line in gp.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            edges.append(json.loads(line))
        except Exception:
            continue
    return edges


# --- browser-openable graph view (served by the /graph HTTP route) ------------------------
_REL_COLORS = {
    "ACTIVATES": "#3fb950", "PROMOTES": "#2ea043", "CAUSES": "#d29922",
    "INHIBITS": "#f85149", "PREVENTS": "#da3633", "ASSOCIATED_WITH": "#8b949e",
}

# Self-contained interactive force graph. Data is injected at /*__DATA__*/; layout runs
# client-side (no server-side networkx), so it stays vault-agnostic and always current.
_GRAPH_TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LitNet graph</title>
<style>
 html,body{margin:0;height:100%;background:#0e1116;color:#c9d1d9;font:13px system-ui,sans-serif;overflow:hidden}
 #hud,#legend,#readout{position:fixed;z-index:2;background:rgba(20,24,31,.88);padding:8px 10px;border-radius:8px}
 #hud{top:8px;left:8px;max-width:46vw}#hud h1{font-size:13px;margin:0 0 4px}
 #legend{bottom:8px;left:8px}#legend span{display:inline-block;margin-right:10px}
 #readout{top:8px;right:8px;max-width:34vw;max-height:82vh;overflow:auto;display:none}
 #readout h2{font-size:13px;margin:0 0 6px}.rel{margin:2px 0}
 .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}
 canvas{display:block;position:fixed;inset:0;cursor:grab}canvas.drag{cursor:grabbing}
</style></head><body>
<div id="hud"><h1 id="title"></h1><div id="stat"></div>
<div style="margin-top:4px;opacity:.65">滾輪縮放 · 拖曳平移 · 游標移到節點看關係</div></div>
<div id="legend"></div><div id="readout"></div><canvas id="c"></canvas>
<script>
const DATA = /*__DATA__*/;
const N=DATA.nodes,E=DATA.edges,COL=DATA.colors,cv=document.getElementById('c'),ctx=cv.getContext('2d');
let W,H,DPR=devicePixelRatio||1;
function resize(){W=innerWidth;H=innerHeight;cv.width=W*DPR;cv.height=H*DPR;cv.style.width=W+'px';cv.style.height=H+'px';}
resize();addEventListener('resize',resize);
for(const n of N){n.x=W/2+(Math.random()-.5)*Math.min(W,H)*.8;n.y=H/2+(Math.random()-.5)*Math.min(W,H)*.8;n.vx=0;n.vy=0;}
const maxd=Math.max(1,...N.map(n=>n.d));
function R(n){return 3+Math.sqrt(n.d/maxd)*13;}
let cool=1,ticks=N.length>600?70:(N.length>250?140:220),simF=0;
function step(){const k=Math.max(.02,cool);
 for(let i=0;i<N.length;i++){const a=N[i];for(let j=i+1;j<N.length;j++){const b=N[j];
  let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy||.01,d=Math.sqrt(d2),f=1400/d2,ux=dx/d,uy=dy/d;
  a.vx+=ux*f*k;a.vy+=uy*f*k;b.vx-=ux*f*k;b.vy-=uy*f*k;}}
 for(const e of E){const a=N[e.s],b=N[e.t];let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||.01;
  let f=(d-70)*.01*k,ux=dx/d,uy=dy/d;a.vx+=ux*f;a.vy+=uy*f;b.vx-=ux*f;b.vy-=uy*f;}
 for(const n of N){n.vx+=(W/2-n.x)*.0022*k;n.vy+=(H/2-n.y)*.0022*k;n.vx*=.85;n.vy*=.85;n.x+=n.vx;n.y+=n.vy;}
 cool*=.985;}
let view={x:0,y:0,z:1};
function draw(){ctx.setTransform(DPR,0,0,DPR,0,0);ctx.clearRect(0,0,W,H);
 ctx.save();ctx.translate(view.x,view.y);ctx.scale(view.z,view.z);ctx.lineWidth=.7;
 for(const e of E){const a=N[e.s],b=N[e.t];ctx.strokeStyle=(COL[e.r]||'#555')+'99';
  ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();}
 for(const n of N){ctx.beginPath();ctx.arc(n.x,n.y,R(n),0,7);ctx.fillStyle='#58a6ff';ctx.fill();}
 if(view.z>1.3){ctx.fillStyle='#c9d1d9';ctx.font='10px system-ui';
  for(const n of N){if(n.d>=2||view.z>2.2)ctx.fillText(n.l,n.x+R(n)+2,n.y+3);}}
 ctx.restore();}
function fit(){if(!N.length)return;
 const xs=N.map(n=>n.x).sort((a,b)=>a-b),ys=N.map(n=>n.y).sort((a,b)=>a-b);
 const lo=Math.floor(N.length*.05),hi=Math.min(N.length-1,Math.ceil(N.length*.95));// central 90% (ignore outliers)
 const x0=xs[lo],x1=xs[hi],y0=ys[lo],y1=ys[hi],sx=(x1-x0)||1,sy=(y1-y0)||1;
 view.z=Math.max(.15,Math.min(3,Math.min(W/sx,H/sy)*.82));
 view.x=W/2-(x0+x1)/2*view.z;view.y=H/2-(y0+y1)/2*view.z;}
function loop(){if(simF<ticks){step();simF++;if(simF>=ticks)fit();}draw();requestAnimationFrame(loop);}loop();
function esc(s){return(s+'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
let drag=false,px,py;
cv.addEventListener('mousedown',ev=>{drag=true;px=ev.clientX;py=ev.clientY;cv.classList.add('drag');});
addEventListener('mouseup',()=>{drag=false;cv.classList.remove('drag');});
addEventListener('mousemove',ev=>{
 if(drag){view.x+=ev.clientX-px;view.y+=ev.clientY-py;px=ev.clientX;py=ev.clientY;return;}
 const mx=(ev.clientX-view.x)/view.z,my=(ev.clientY-view.y)/view.z;let hit=-1;
 for(let i=N.length-1;i>=0;i--){const n=N[i],dx=mx-n.x,dy=my-n.y;if(dx*dx+dy*dy<=Math.pow(R(n)+3,2)){hit=i;break;}}
 const ro=document.getElementById('readout');
 if(hit<0){ro.style.display='none';return;}
 const n=N[hit],rels=[];
 for(const e of E){if(e.s===hit)rels.push([n.l,e.r,N[e.t].l]);else if(e.t===hit)rels.push([N[e.s].l,e.r,n.l]);}
 ro.style.display='block';
 ro.innerHTML='<h2>'+esc(n.l)+'</h2><div style="opacity:.65">degree '+n.d+'</div>'+
  rels.slice(0,40).map(x=>'<div class="rel"><span class="sw" style="background:'+(COL[x[1]]||'#555')+'"></span>'+
   esc(x[0])+' <b>'+x[1]+'</b> '+esc(x[2])+'</div>').join('');});
cv.addEventListener('wheel',ev=>{ev.preventDefault();const f=ev.deltaY<0?1.1:1/1.1,mx=ev.clientX,my=ev.clientY;
 view.x=mx-(mx-view.x)*f;view.y=my-(my-view.y)*f;view.z*=f;},{passive:false});
document.getElementById('title').textContent='LitNet · '+DATA.meta.title;
document.getElementById('stat').textContent=DATA.meta.n+' nodes · '+DATA.meta.e+' edges';
document.getElementById('legend').innerHTML=Object.entries(COL).map(x=>
 '<span><span class="sw" style="background:'+x[1]+'"></span>'+x[0]+'</span>').join('');
if(!N.length)document.getElementById('stat').textContent='（此範圍沒有機制邊；換個 entity 或先跑 promote_litnet）';
</script></body></html>"""


def _render_graph_html(entity: str = "") -> str:
    """Build a self-contained interactive force-graph HTML from the live edge store.

    Reused by the /graph HTTP route (Tailscale-only). vault-agnostic — reads the same
    SB_GRAPH_PATH edge store as query_graph, so the view is always current (no static file).
    entity: optional filter — keep only edges whose subject/object grounds to it.
    """
    q = _graph_norm(entity) if entity.strip() else ""
    idx: dict[str, int] = {}
    nodes: list[dict] = []

    def _nid(label: str) -> int:
        k = _graph_norm(label)
        if k not in idx:
            idx[k] = len(nodes)
            nodes.append({"id": len(nodes), "l": label, "d": 0})
        return idx[k]

    jedges: list[dict] = []
    for e in _load_edges():
        s, o, r = e.get("subject", ""), e.get("object", ""), e.get("relation", "")
        if not (s and o and r):
            continue
        if q and q not in _graph_norm(s) and q not in _graph_norm(o):
            continue
        si, oi = _nid(s), _nid(o)
        nodes[si]["d"] += 1
        nodes[oi]["d"] += 1
        jedges.append({"s": si, "t": oi, "r": r})

    meta = {"title": (f"filter: {entity}" if entity.strip() else "full graph"),
            "n": len(nodes), "e": len(jedges)}
    data = {"nodes": nodes, "edges": jedges, "meta": meta, "colors": _REL_COLORS}
    return _GRAPH_TEMPLATE.replace("/*__DATA__*/", json.dumps(data, ensure_ascii=False))


@mcp.custom_route("/graph", methods=["GET"])
async def _graph_route(request):  # Tailscale-only (auth-exempt); read-only visualization
    from starlette.responses import HTMLResponse
    try:
        html = _render_graph_html(request.query_params.get("entity", ""))
    except Exception as ex:  # never 500 the browser — show the error inline
        html = f"<pre>graph render error: {ex}</pre>"
    return HTMLResponse(html)


@mcp.tool()
def query_graph(entity: str, mode: str = "both", top_k: int = 12) -> str:
    """Dual-path knowledge-graph query for an entity (gene / factor / phenotype / …).

    Path 1 — structured typed edges (ACTIVATES / INHIBITS / PROMOTES / CAUSES /
    PREVENTS / ASSOCIATED_WITH) mentioning the entity, aggregated ACROSS papers with
    verbatim evidence + source note.  Cross-paper agreement = stronger (shown as ×N).
    Path 2 — full-text verbatim snippet recall net (same engine as search_snippets),
    so a relation the edge extractor missed is still surfaced (goal: 無遺漏).

    Args:
        entity: e.g. 'TXNDC5', 'TGF-beta', 'pulmonary fibrosis'.
        mode:  'edges' | 'snippets' | 'both' (default 'both').
        top_k: snippet notes to pull for the recall net (default 12).
    """
    q = _graph_norm(entity)
    parts: list[str] = []

    # ---- Path 1: structured edges ----
    if mode in ("edges", "both"):
        matched = []
        for e in _load_edges():
            # re-ground from raw strings so grounding improvements apply without re-extraction
            e["_sn"], e["_on"] = _graph_norm(e.get("subject", "")), _graph_norm(e.get("object", ""))
            if q in e["_sn"] or q in e["_on"]:
                matched.append(e)
        agg: dict = {}
        for e in matched:  # aggregate identical (subj, relation, obj) across papers
            key = (e["_sn"], e.get("relation", ""), e["_on"])
            g = agg.setdefault(key, {"subject": e.get("subject", ""),
                                     "relation": e.get("relation", ""),
                                     "object": e.get("object", ""), "notes": {}})
            g["notes"].setdefault(e.get("note", ""), e.get("evidence", ""))
        rows = sorted(agg.values(), key=lambda g: (-len(g["notes"]), g["relation"]))
        if rows:
            lines = [f"### Structured edges for '{entity}' "
                     f"({len(rows)} relations from {len(matched)} raw statements)"]
            for g in rows:
                tag = f"  ×{len(g['notes'])} papers" if len(g["notes"]) > 1 else ""
                lines.append(f"- **{g['subject']} →{g['relation']}→ {g['object']}**{tag}")
                for n, ev in g["notes"].items():
                    lines.append(f"    - _{n}_: “{ev}”")
            parts.append("\n".join(lines))
        elif mode == "edges":
            parts.append(f"### Structured edges for '{entity}'\n"
                         "(none — graph empty or entity absent; run extract_statements.py)")

    # ---- Path 2: snippet full-text recall net ----
    if mode in ("snippets", "both"):
        from . import snippets
        try:
            hits = _store.hybrid_search(entity, limit=top_k, exclude_types=KNOWLEDGE_EXCLUDE)
        except Exception:
            hits = []
        snips = []
        for h in hits:
            try:
                full = _vault_path(h["path"])
            except VaultPathError:
                continue
            snip = snippets.best_snippet(full.read_text(encoding="utf-8", errors="ignore"),
                                         h.get("title", ""), entity)
            if snip:
                snips.append(f"- **{h.get('title', '')}**\n  > {snip}\n  [{h['path']}]({h['path']})")
        if snips:
            parts.append(f"### Full-text recall net ({len(snips)} notes)\n" + "\n".join(snips))

    # browser-openable interactive graph (Tailscale-only /graph route; no key on the tailnet).
    # Only advertise when bound to a real remote host (HTTP transport), not stdio/loopback.
    if mode in ("edges", "both") and mcp.settings.host not in ("", "127.0.0.1", "localhost"):
        from urllib.parse import quote
        parts.append(f"🌐 互動圖（瀏覽器開，tailnet 內免 key）："
                     f"http://{mcp.settings.host}:{mcp.settings.port}/graph?entity={quote(entity)}")

    try:  # eval hook: query log for the future relevance loop
        from datetime import datetime as _dt
        (VAULT / ".query-log.jsonl").open("a", encoding="utf-8").write(
            json.dumps({"ts": _dt.now().isoformat(timespec="seconds"),
                        "tool": "query_graph", "query": entity, "mode": mode},
                       ensure_ascii=False) + "\n")
    except Exception:
        pass

    return "\n\n".join(parts) if parts else f"No graph data or snippets for: {entity}"


# --- litnet_answer: retrieve (query_graph) → synthesize (Claude) → cited note --------------
_LA_ENT_PROMPT = (
    "從下面這個生醫研究問題，抽出要在知識圖譜查詢的核心實體（基因/蛋白/因子/細胞型/表型/疾病），"
    "最多 3 個，用英文正規名、逗號分隔，只輸出實體、不要其他字。\n\n問題："
)
_LA_MIDDLE = {
    "auto": "  - 依問題類型自行選最合適的中段結構（機制清單 / 比較表 / 分組列舉 / 方法對照）。",
    "mechanism": "  - 中段用 `## 機制與證據`：逐條 bullet，每條一個機制關係 + 來源（+可選逐字證據）。",
    "compare": "  - 中段用 `## 比較`：Markdown 表格，列＝被比較對象、欄＝面向（方法/樣本/機制/結論），格內掛來源。",
    "list": "  - 中段用 `## 清單`：把符合的因子/機制分組或排序列出，每項掛來源論文。",
    "methods": "  - 中段用 `## 如何證明`：逐研究列實驗手法（敲除/模型/assay）+ 來源。",
}
_LA_PROMPT = """你是生醫文獻綜合者。以下是從 LitNet 知識圖譜檢索到的**接地素材**（結構化機制邊 ×N 跨論文 + 逐字證據 + 全文片段），回答使用者的問題，寫成一則**帶引用的綜合筆記**。

鐵律：
1) 只用素材裡出現的事實；**不得加入素材沒有的內容**，不確定就不寫。
2) 每條主張後**標來源論文**（素材裡的 note 名，如 2020_Lee_...）；跨論文用 ×N 標佐證強度。
3) 這是 AI 綜合、非一手文獻——語氣客觀、不誇大。
4) 誠實缺口：明講素材**沒涵蓋**的面向。

輸出格式（繁體中文 Markdown）：
- 固定開頭 `## 核心答案`（2–3 句直接回答問題）。
- **中段依問題調整**：
{middle_hint}
- 固定結尾三段：`## 跨論文佐證強度`（×N 強 / 單篇待補）、`## 誠實缺口`、`## 來源清單`（逐列出現過的論文 note）。
不要加格式外的段落。

---
問題：{question}

檢索素材：
{grounding}
"""


def _la_synth(prompt: str, model: str) -> str:
    """Claude synthesis; key from env else Keychain (-s ANTHROPIC_API_KEY), same as figures.py."""
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        key = subprocess.run(["security", "find-generic-password", "-s", "ANTHROPIC_API_KEY", "-w"],
                             capture_output=True, text=True).stdout.strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
    msg = anthropic.Anthropic().messages.create(
        model=model, max_tokens=4096, stream=False,   # synthesis can be long; avoid mid-note cutoff
        messages=[{"role": "user", "content": prompt}])
    try:  # token ledger: litnet_answer is the LitNet Claude-cost hotspot (grounding[:24000] + synth)
        from datetime import datetime as _dt
        u = getattr(msg, "usage", None)
        (VAULT / ".litnet-token-log.jsonl").open("a", encoding="utf-8").write(json.dumps(
            {"ts": _dt.now().isoformat(timespec="seconds"), "tool": "litnet_answer", "model": model,
             "input_tokens": getattr(u, "input_tokens", None),
             "output_tokens": getattr(u, "output_tokens", None),
             "prompt_chars": len(prompt)}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


@mcp.tool()
def litnet_answer(question: str, entity: str = "", fmt: str = "auto",
                  save: bool = False, model: str = "claude-sonnet-5") -> str:
    """Answer a literature question as a fixed-format, CITED synthesis note (retrieve → synthesize).

    Retrieves grounded material via query_graph (LitNet 正本 edges + full-text net), then Claude
    writes a cited summary. GENERATED prose (not raw literature): every claim is anchored to a
    source note in the retrieval; tagged type: synthesis; a synthesis is NEVER re-extracted into
    the 正本 (saved outside 20-areas/research/).

    Args:
        entity: comma-separated entities to look up; if empty, extracted from the question.
        fmt: middle-section shape — auto | mechanism | compare | list | methods (auto = model picks).
        save: if true, write the note into VAULT/20-areas/syntheses/ (else just return it).
        model: Claude synthesis model.
    """
    ents = [e.strip() for e in entity.split(",") if e.strip()]
    if not ents:
        raw = llm_cli.llm_text(_LA_ENT_PROMPT + question, timeout=60) or ""
        ents = [e.strip() for e in re.split(r"[,，、\n]", raw) if e.strip()][:3] or [question]
    # per-entity: keep only entities that actually returned material (one empty entity must not
    # abort a multi-entity query where others have data).
    blocks = []
    for e in ents:
        out = query_graph(e, "both", 10)
        if out and "No graph data" not in out:
            blocks.append(f"# 查詢實體：{e}\n{out}")
    grounding = "\n\n".join(blocks)
    if not grounding.strip():
        return f"no LitNet material for: {ents}"

    prompt = _LA_PROMPT.format(middle_hint=_LA_MIDDLE.get(fmt, _LA_MIDDLE["auto"]),
                               question=question, grounding=grounding[:24000])
    body = _la_synth(prompt, model)
    # note names look like 2020_Lee_Title; no \b — they're often wrapped in markdown italics (_..._)
    srcs = sorted(set(re.findall(r"\d{4}_[A-Z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*", grounding)))
    today = date.today().isoformat()
    ent_list = ", ".join('"' + _safe_yaml(e) + '"' for e in ents)  # entities are free-text → escape
    note = (
        "---\n" f'title: "{_safe_yaml(question)}"\n' "type: synthesis\ngenerated: true\n"
        "evidence_origin: synthesis\n" f"date: {today}\nmodel: {model}\n"
        f"entities: [{ent_list}]\nsources: [{', '.join(srcs)}]\n"
        "tags: [litnet, synthesis]\n---\n\n"
        f"# {question}\n\n"
        "> ⚠️ 本筆記為 AI 綜合（synthesis）自 LitNet 正本邊 + 全文檢索；**非原始文獻**。"
        "事實錨在來源論文，勿當一手來源、勿回灌 LitNet 正本。\n\n"
        + body + "\n"
    )
    if save:
        d = VAULT / "20-areas" / "syntheses"       # OUTSIDE 20-areas/research (never re-extracted)
        d.mkdir(parents=True, exist_ok=True)
        slug = _slugify(question)[:60].strip("-") or "litnet-answer"  # unify with vault-wide naming
        (d / f"{today}-{slug}.md").write_text(note, encoding="utf-8")
    return note


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


@write_tool(target_const="memory/goals.md")
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
    try:
        full_path = _vault_path(path)
    except VaultPathError as exc:
        return str(exc)
    try:
        _store.record_access(path)
    except Exception:
        pass  # access tracking is best-effort
    return full_path.read_text(encoding="utf-8")


@write_tool(target="path")
def update_note(path: str, content: str) -> str:
    """Overwrite an existing note with new content.

    Use when rewriting or restructuring a note. For adding content without
    losing existing text, use append_to_note instead.

    Args:
        path: Relative path from vault root, e.g. 'decisions/my-decision.md'
        content: Full new content to write (replaces the entire file)
    """
    full_path = _vault_path(path, missing_hint=". Use new_note to create it.")
    full_path.write_text(content, encoding="utf-8")
    n_links = after_write(full_path, path)
    link_msg = f" ({n_links} related links refreshed)" if n_links else ""
    return f"Updated: {path}{link_msg}"


@write_tool(target="path")
def append_to_note(path: str, content: str) -> str:
    """Append content to the end of an existing note.

    Safer than update_note — existing text is never lost.
    Use for adding progress updates, new findings, or extra sections.

    Args:
        path: Relative path from vault root, e.g. '10-projects/my-project.md'
        content: Text to append (added after a blank line at end of file)
    """
    full_path = _vault_path(path, missing_hint=". Use new_note to create it.")
    existing = full_path.read_text(encoding="utf-8")
    separator = "\n" if existing.endswith("\n") else "\n\n"
    full_path.write_text(existing + separator + content, encoding="utf-8")
    after_write(full_path, path)
    return f"Appended to: {path}"


@write_tool(target="path")
def mark_note_status(path: str, status: str) -> str:
    """Update the frontmatter status field of a note and sync to DB.

    Use this to track note lifecycle without rewriting the whole file — including
    a decision note's proposed → accepted → superseded progression.

    Args:
        path: Relative path from vault root, e.g. '30-resources/my-note.md'
        status: active | completed | archived (general / project notes),
            proposed | accepted | superseded (decision / ADR),
            or consolidated | archive_backup (normally written by
            consolidate_tool / vault_sleep, accepted here for repairs).
    """
    if status not in NOTE_STATUS_ALLOWED:
        return (
            f"Invalid status {status!r}. Lifecycle: "
            f"{', '.join(sorted(NOTE_STATUS_LIFECYCLE))}. "
            f"Tool-managed: {', '.join(sorted(NOTE_STATUS_MANAGED))}."
        )

    full_path = _vault_path(path)
    _fm.set_fields_in_file(full_path, {"status": status})

    try:
        _store.set_note_status(path, status)
    except Exception as e:
        print(f"[second-brain] warning: DB status update failed for {path}: {e}", file=sys.stderr)

    return f"Status updated to '{status}': {path}"


class AuditVaultInfo(TypedDict):
    backend: str
    vault_id: str


class AuditCounts(TypedDict):
    vault_markdown_files: int
    indexed_notes: int
    article_notes: int
    research_notes: int
    social_notes: int


class AuditArticleRecordsResult(TypedDict):
    run_id: str
    generated_at: str
    scope: str
    vault: AuditVaultInfo
    counts: AuditCounts
    index_gap: int
    issues: dict[str, list[dict[str, object]]]
    totals: dict[str, int]
    truncated: bool
    recommended_actions: list[str]
    warnings: list[str]


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=True,
)
def audit_article_records(
    scope: Literal["articles", "social", "all"] = "all",
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    stale_after_days: Annotated[int, Field(ge=1, le=90)] = 8,
) -> AuditArticleRecordsResult:
    """Audit article records and social-source state without changing the vault.

    Args:
        scope: Audit article notes, social-source state, or both.
        limit: Maximum results returned per issue category (1..500).
        stale_after_days: Age at which source state is considered stale (1..90).
    """
    try:
        stats = _store.db_stats()
    except Exception:
        stats = {}

    backend = str(
        stats.get("backend") or os.environ.get("SB_DB_BACKEND", "duckdb")
    ).lower()
    return _audit_article_records(
        VAULT,
        scope=scope,
        limit=limit,
        stale_after_days=stale_after_days,
        indexed_notes=stats.get("total_notes"),
        vault_backend=backend,
        vault_id=VAULT.name,
    )


@mcp.tool()
def sync_index() -> str:
    """Rebuild the DuckDB index by scanning all vault markdown files.
    Run this after adding notes manually, or when setting up on a new machine.
    """
    result = _store.sync_all(VAULT)
    emb = _store.sync_embeddings(vault=VAULT)
    chunks = _store.sync_chunks(VAULT)
    stats = _store.db_stats()
    embed_warn = f" ⚠️ {result['embed_failed']} notes missing embedding" if result["embed_failed"] else ""
    chunk_line = f"\nChunks: +{chunks['updated']} notes backfilled" if chunks["updated"] else ""
    return (
        f"Synced {result['synced']} files → {stats['total_notes']} notes in index.{embed_warn}\n"
        f"Embeddings: +{emb['updated']} new (llama-server {'✓' if emb['updated'] or emb['failed'] == 0 else '✗ unavailable'})"
        f"{chunk_line}\n"
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


@write_tool()
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


@write_tool(target="note_path")   # writes memory/rules.md — was unguarded until 2026-07-31
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
        _vault_path(note_path)
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


@write_tool(target="note_path")
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
        paths = _store.get_paths_for_semantic_keywords(force)

    processed, skipped, failed = 0, 0, 0
    for rel in paths:
        try:
            full = _vault_path(rel)
        except VaultPathError:
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


@write_tool(target="note_path")
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
        try:
            full = _vault_path(path)
        except VaultPathError:
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


@write_tool(target="source")
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

    # Both the folder and the final file must resolve inside the vault; the folder
    # does not exist yet on a first save, hence must_exist=False.
    folder = _vault_path(dest_folder, must_exist=False)

    # Sanitize filename: strip, forbid path separators and traversal sequences
    filename = filename.strip()
    if filename:
        if "/" in filename or "\\" in filename or filename.startswith("."):
            return f"filename contains invalid characters: {filename!r}"
    stem = filename if filename else _slugify(title)
    if not stem:
        return "Cannot determine a valid filename. Provide a title or filename."

    folder.mkdir(parents=True, exist_ok=True)
    rel = str((folder / f"{stem}.md").relative_to(VAULT.resolve()))
    dest = _vault_path(rel, must_exist=False)

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

    n_links = after_write(
        dest, rel, register_label=title, enrich=body, extract_figures=True
    )

    link_msg = f", {n_links} related links added" if n_links else ""
    return f"Saved: {rel} (figure extraction started in background{link_msg})"


@write_tool(target="note_path")
def update_links_tool(note_path: str = "") -> str:
    """Refresh auto-generated related wikilinks in one note or all notes.

    Uses semantic similarity (bge-m3, 1024d) to find related notes and
    writes them into the frontmatter `related` field.

    Args:
        note_path: Relative path within vault (e.g. 'decisions/my-note.md').
                   Leave empty to update ALL notes that have embeddings.
    """
    if note_path:
        full = _vault_path(note_path)
        n = _inject_related_links(full, note_path)
        return f"Updated: {note_path} — {n} related links written"

    # Batch: update all indexed notes
    paths_with_emb = _store.get_paths_with_embeddings()

    updated, skipped = 0, 0
    for rel in paths_with_emb:
        try:
            full = _vault_path(rel)
        except VaultPathError:
            continue
        n = _inject_related_links(full, rel)
        if n:
            updated += 1
        else:
            skipped += 1

    return f"Updated {updated} notes with related links ({skipped} skipped — no matches above threshold)"


@write_tool(target="note_path")
def extract_figures_for(note_path: str) -> str:
    """Manually trigger figure extraction for a saved article.

    Args:
        note_path: Relative path within vault, e.g. '30-resources/my-article.md'
    """
    _vault_path(note_path)
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


def _snapshot_note_in_worker(note_path: str, tier: str) -> dict:
    """Run Playwright's sync API outside FastMCP's asyncio event loop."""
    with ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="snapshot-note",
    ) as executor:
        future = executor.submit(_fig.snapshot_note, note_path, VAULT, tier)
        return future.result()


@write_tool(target="note_path")
def snapshot_note_tool(note_path: str, tier: str = "base") -> str:
    """Render a markdown note to PNG snapshot for token-efficient storage.

    Args:
        note_path: Relative path within vault, e.g. 'decisions/my-note.md'
        tier: Resolution tier — 'large' (400 tokens), 'base' (256), 'small' (100)
    """
    full = _vault_path(note_path)
    result = _snapshot_note_in_worker(note_path, tier)
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


@write_tool()
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


@write_tool()
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
    try:
        full_path = _vault_path(path)
    except VaultPathError as exc:
        return str(exc)

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
    try:
        insight_file = _vault_path(insight_rel)
    except VaultPathError:
        return img
    body = insight_file.read_text(encoding="utf-8")
    return [img, f"📝 Prior insights ([[{insight_rel.removesuffix('.md')}]]):\n\n{body}"]


# ---------------------------------------------------------------------------
# Figure insight write-back (Phase 5.8) — atomic vault notes, no DuckDB mirror
# ---------------------------------------------------------------------------

def _figure_insight_rel(note_path: str, fig_index: int) -> str:
    """Vault-relative path of the atomic insight note for one figure."""
    paper_slug = _fig._figure_slug(note_path)
    return f"20-areas/research/figure-insights/{paper_slug}--fig{fig_index:02d}.md"


def _add_figure_insight_backlink(note_path: str, fig_index: int, insight_rel: str) -> None:
    """Add an idempotent forward link in the paper note's figure section."""
    try:
        paper = _vault_path(note_path)
    except VaultPathError:
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


@write_tool(target="note_path")
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
    insight = insight.strip()
    if not insight:
        return "Empty insight — nothing saved."

    rel = _figure_insight_rel(note_path, fig_index)
    dest = _vault_path(rel, must_exist=False)
    dest.parent.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    paper_link = note_path.removesuffix(".md")
    register_label = None
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
        register_label = title
        action = "Created insight note"

    # Index so search_notes finds it (content_hash change triggers embedding/FTS).
    after_write(dest, rel, register_label=register_label, relink=False)

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


@write_tool()
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
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    # Canonical base manual lives at the repo root — one level ABOVE this package
    # (mcp-tools/second-brain/AGENTS.md), not inside mcp_second_brain/. Probe known
    # locations so a layout change surfaces loudly instead of silently shipping the
    # "not found" placeholder to remote agents.
    _here = Path(__file__).resolve()
    _candidates = [
        _here.parent.parent / "AGENTS.md",  # repo root (canonical)
        _here.parent / "AGENTS.md",         # packaged copy, if ever added
    ]
    base = next((p for p in _candidates if p.exists()), None)
    result = _read(base) if base else "⚠️ 找不到 AGENTS.md"

    # Append personal rules from vault AGENTS.md if present.
    # The vault file is intentionally not in the public repo — it stays on Drive
    # and is only visible to the vault owner's own agents.
    vault_agents = VAULT / "AGENTS.md"
    if vault_agents.exists():
        personal = _read(vault_agents).strip()
        result = result.rstrip() + "\n\n---\n\n" + personal

    return result


@admin_tool(target="action")
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


@admin_tool(audit=False)   # reading the audit log must not append to it
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
    try:
        _db_keys = _store.count_active_api_keys()
    except Exception as exc:  # store without api_keys support, or DB unreachable
        print(f"[second-brain] could not count DB API keys: {exc}", file=sys.stderr)
        _db_keys = 0
    n_keys = maybe_add_api_key_auth(
        app,
        lookup_fn=lambda raw: _store.get_identity_for_key(_hash_key(raw)),
        exempt_paths={"/graph"},  # read-only browser viz; Tailscale-only (see AskUserQuestion)
        db_key_count=_db_keys,
    )
    if n_keys:
        print(
            f"[second-brain] API-key auth ENABLED ({n_keys} key(s): "
            f"{n_keys - _db_keys} env, {_db_keys} registered)",
            file=sys.stderr,
        )
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
