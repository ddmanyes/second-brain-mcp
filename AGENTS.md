# AGENTS.md — Second Brain Operating Manual for AI Agents

> Read this document before making any vault changes. It defines tool usage, note types, security rules, and standard operating procedures.
>
> **How to get this document**: When connected via MCP, call `get_agent_instructions()`. When working locally with Claude Code in the `second-brain/` directory, it is auto-loaded via `CLAUDE.md`.
>
> **When adding documentation**: modify the relevant section here, then update the Last updated date.
>
> **Last updated:** 2026-08-30

---

## System Overview

Second Brain is a personal knowledge management server that exposes vault read/write, search, archiving, and maintenance via MCP.

- **MCP server**: `server.py` (39 tools — see Tool Reference; keep this count in sync when adding/removing tools)
- **Index backend**: pluggable `VaultStore` (`store/`), selected by `SB_DB_BACKEND`:
  - `postgres` (central brain) — `store/postgres_store.py`, Postgres 16 + pgvector + pg_trgm, connection-pooled, multi-machine concurrent read/write via MVCC.
  - `duckdb` (default / offline fallback) — `store/duckdb_store.py` wrapping `vault_db.py`.
- **Vault path**: controlled by environment variable `SECOND_BRAIN_PATH`
- **Source of truth**: markdown is canonical (L1); the index (Postgres or DuckDB) is a
  rebuildable L2 — `sync_all` regenerates it from markdown. Switching backends never
  touches markdown.

---

## Connection Topology & Write Discipline

The production setup is **one central HTTP server + Postgres**; clients never touch
Postgres directly.

- **Central host**: the mac-mini `lab-center`, Tailscale `100.87.59.15` (SSH as `lab_center`).
- **Central server**: launchd `com.user.second-brain-remote`, `streamable-http` bound to
  the Tailscale IP `:9100`, `SB_DB_BACKEND=postgres`. Single instance (PID guard).
- **Sibling instances on the same host and the same package**: `lcdda` (`:9104`, a second
  vault) and `lcdda-harvest` (`:9106`). They import the same `mcp_second_brain` from the same
  venv, so **one `pip install` changes all three** — deploying means restarting all three, or
  new and old code run side by side. (`finance-kit` on `:9108` is a separate codebase.)
- **Postgres**: Docker `sb-pg`, bound to `127.0.0.1:5432` only — **never** exposed off-host.
- **Clients connect via MCP over HTTP**: `http://100.87.59.15:9100/mcp` with header
  `X-API-Key: <key>` (auth is enforced when `SB_API_KEY`/`SB_API_KEYS` is set on the server).
  Setup per client type is in [NEW_MACHINE_SETUP.md](NEW_MACHINE_SETUP.md) §A.
- **Index freshness**: `com.user.second-brain-pg-sync` runs `sync_incremental` against
  Postgres at minute `:00` and `:30` via `StartCalendarInterval`, so cron-driven markdown
  edits do not let the index drift. Keep the source plist template on calendar triggers;
  `StartInterval=1800` was observed to stall on the central host.
- **Backups**: `com.user.second-brain-pg-backup` runs `pg_dump` daily (markdown is still
  the real backup; this only speeds recovery).

**Write discipline (avoid Google Drive conflict copies):**
- All writes go through the **central server** (single writer on the host).
- Client-side Obsidian is **read-only** — read synced markdown, but edit via MCP tools,
  not by typing into Obsidian on a laptop (two machines editing one file → Drive conflict
  copy → pollutes the source and index).
- **Never configure a local stdio `python -m mcp_second_brain` client against the Drive
  vault.** It makes that machine a second writer; concurrent Drive writes lose one side
  **with no conflict copy**. A stale stdio entry can sit unnoticed in a client config for
  months — if you see `args: ["-m", "mcp_second_brain..."]` anywhere, replace it with the
  `mcp-remote` HTTP form.
- Offline fallback: with no network, set `SB_DB_BACKEND=duckdb` + stdio for local
  read-only use; reconcile via `sync_all` when back online.

---

## Tool Reference

| User says… | Call | Notes |
| ---------- | ---- | ----- |
| "What's active / what should I work on" | `get_context()` | Call at session start to load goals + active notes |
| "Create a new note / log a decision / new project" | `new_note(note_type, title)` | note_type — see NOTE_CONFIG below |
| "Update this note / rewrite content" | `update_note(path, content)` | Overwrites entire note; read first to confirm structure |
| "Add to / append progress" | `append_to_note(path, content)` | Safe append, preserves existing content |
| "Search for X" | `search_notes(query)` | Semantic search; wrap in quotes for exact match |
| "Show grouped search results" | `search_grouped(query)` | Results grouped by note type |
| "Search news / recent articles" | `search_news_tool(query, days)` | Default: last 7 days |
| "Audit article housekeeping" | `audit_article_records(scope, limit, stale_after_days)` | Read-only, bounded report for metadata, links, exact duplicate candidates, overdue inbox, and social-source state |
| "Read this note" | `read_note(path)` | path relative to vault root |
| "Read as image" | `read_note_as_image(path)` | For notes with charts/figures |
| "Show decision log" | `get_decisions(project)` | Omit project for all decisions |
| "Update goals" | `update_goals(new_content)` | Overwrites memory/goals.md |
| "Save this article" | `save_article(source, title, tags)` | source: URL or local file |
| "Find related notes" | `find_related_notes(path, limit)` | Semantic similarity, threshold 0.7 |
| "Top notes" | `top_notes(by, limit)` | by: "score" or "recency" |
| "Rebuild index / update semantic search" | `sync_index()` | Run after bulk note changes |
| "Index stats / how many notes" | `index_stats()` | |
| "Archive old notes" | `vault_sleep(dry_run=True)` | Always dry_run first |
| "Which notes will be archived" | `sleep_status()` | |
| "Find duplicates" | `consolidate_tool(dry_run=True)` | threshold default 0.85 |
| "Clean up old archive" | `prune_archive_tool(dry_run=True)` | min_age_days default 365 |
| "Extract rules from note" | `extract_rules_tool(note_path)` | Extracts `- [ ]` rule items |
| "Update links" | `update_links_tool(note_path)` | Rebuilds wiki links |
| "Extract figures" | `extract_figures_for(note_path)` | Saves to figures/ |
| "Search figures" | `search_figures(query)` | Text proxy (caption+OCR+description) — usually answers without loading pixels |
| "Show me figure N" | `read_figure(note_path, fig_index)` | Loads ONE figure thumbnail (~256-400 tok); use only when text isn't enough |
| "Remember this about figure N" | `annotate_figure(note_path, fig_index, insight)` | Saves a read-time insight as an atomic note so next time text answers (no re-load) |
| "Snapshot this note" | `snapshot_note_tool(note_path, tier)` | tier: "base" or "detail" |
| "Initialize vault / fix directory structure" | `init_vault()` | Safe to re-run, only creates missing items |
| "Agent instructions" (remote session start) | `get_agent_instructions()` | Returns this document (base + personal layer) |
| "Set / change note status" | `mark_note_status(path, status)` | Updates frontmatter status + syncs DB |
| "Exact source sentence / precise citation" | `search_snippets(query)` | Returns VERBATIM source sentences, not summaries |
| "Answer a literature question (cited)" | `litnet_answer(question)` | Retrieve → synthesize → fixed-format cited note |
| "Graph query for a gene / factor / entity" | `query_graph(entity)` | Dual-path knowledge-graph lookup |
| "Refresh semantic keywords" | `expand_semantic_keywords_tool()` | Batch (re)extract `semantic_keywords` via Gemini CLI |
| "Enrich neighbor keywords / cluster topic" | `enrich_neighbor_keywords_tool()` | Derives `neighbor_keywords` + `cluster_topic` from embeddings |
| "System health check" | `health_check()` | DB / index / vault / embedding-server diagnostics |
| "Manage remote API keys" | `manage_api_key(action, …)` | create / list / revoke `X-API-Key` (admin) |
| "Show audit log" | `query_audit_log(user_id, tool_name, …)` | Multi-user tool-call audit trail (admin) |

---

## NOTE_CONFIG — Note Types

| note_type | Folder | Template |
| --------- | ------ | -------- |
| `decision` / `adr` | `decisions/` | `decision-template.md` |
| `project` | `10-projects/` | `project-template.md` |
| `mcp` | `10-projects/` | `mcp-project-template.md` |
| `research` / `paper` / `finding` | `20-areas/research/` | `research-note-template.md` |
| `coding` / `tool` | `20-areas/coding/` | `note-template.md` |
| `resource` / `reference` | `30-resources/` | `note-template.md` |
| other (unknown type) | `00-inbox/` | `note-template.md` |

---

## Frontmatter Spec

`new_note` auto-fills `title` and `date`. Fill in any missing fields after creation:

| note_type | Required (template) | Recommended | Notes |
| --------- | ------------------- | ----------- | ----- |
| `decision` / `adr` | `title`, `date`, `type: decision`, `status` | `tags` | status: `proposed` → `accepted` → `superseded` |
| `project` / `mcp` | `title`, `date`, `type: project`, `status` | `tags` | status: `active` / `completed` / `archived` |
| `research` / `paper` | `title`, `date`, `type: research`, `status` | `source`, `tags` | source: original URL or DOI |
| `coding` / `tool` | `title`, `date`, `type: note`, `status` | `tags` | |
| `resource` / `reference` | `title`, `date`, `type: resource`, `status` | `source`, `tags` | |

**Universal rules:**

- `status`: only `active` / `completed` / `archived` / `proposed` / `accepted`
- `tags`: lowercase kebab-case, e.g. `[mcp, ai-agent]`
- `related`: use `[[wikilink]]` format — auto-injected by semantic link tool

---

## Figures

All figures live under vault root `figures/`, **not scattered next to notes**.
**Always use the visible `figures/` directory — Obsidian does not index hidden directories.**

| Scenario | Path | Created by |
| -------- | ---- | ---------- |
| Auto-extracted from saved article | `figures/{note-filename-kebab}/fig-{NN}.png` | `extract_figures_for` (automatic) |
| Manual from local PDF | `figures/{slug}/fig-{NN}.png` | manual `pdftoppm` |
| Project screenshots | `figures/{project-slug}/fig-{NN}.png` | manual |
| Miscellaneous | `figures/misc/` | manual |

- `fig-NN` starts at `fig-00`, increments by PDF page or figure number
- Extract from local PDF: `pdftoppm -r 150 -png -f {start} -l {end} input.pdf figures/{slug}/fig`
- Embed: `![[figures/{slug}/fig-00.png]]`
- `extract_figures_for` only works on notes created by `save_article` (requires a source URL); use `pdftoppm` for local PDFs

### Recall ladder — cheap → expensive (don't skip rungs)

When answering a question that *might* need a figure, climb only as far as needed:

1. `search_notes` / `search_figures` — **pure text** (semantic + caption + OCR + description). Cheapest; usually enough to answer "what does figure X show / what's the value".
2. `read_note` — full clean markdown body, when you need surrounding text.
3. `read_figure(note_path, fig_index)` — **one** figure thumbnail (~256-400 tok). Only when the text proxy can't answer (a specific visual detail).
4. `read_note_as_image(path)` — whole-page render (highest cost). Last resort, layout/visual-only cases.

> Rule of thumb: **if text can answer it, do not load pixels.** Captions and OCR are stored precisely so the expensive rungs are rarely needed. Never reflexively `read_note_as_image` an article just to inspect one chart — use `read_figure`.

---

## Standard Operating Procedures

### A. Create a note

```text
1. Determine note_type (see NOTE_CONFIG)
2. Call new_note(note_type, title, content, tags)
3. Tool auto-applies template, writes to correct folder, indexes, injects semantic links
4. Template only auto-fills {{title}} and {{date}}; fill remaining placeholders manually
5. Verify required frontmatter fields against the spec above
```

### B. Search / query

```text
1. Fuzzy semantic search  → search_notes(query)
2. Grouped display        → search_grouped(query)
3. News / articles        → search_news_tool(query, days)
4. Decision log           → get_decisions(project)
5. Read full note         → read_note(path)
```

**Tool boundary:** use `search_notes` to retrieve content, `health_check` to diagnose
server/index/runtime health, and `audit_article_records` only for article-record
housekeeping. The audit is read-only: duplicate groups and recommended actions always
require human confirmation before merge, archive, or deletion.

### C. Vault maintenance (periodic or on demand)

```text
1. Check archive candidates   → sleep_status()
2. Dry-run archive            → vault_sleep(dry_run=True)
3. Execute archive            → vault_sleep(dry_run=False)
4. Find duplicates (dry-run)  → consolidate_tool(threshold=0.85, dry_run=True)
5. Clean old archive          → prune_archive_tool(dry_run=True) → (dry_run=False)
```

### C-bis. Finance report housekeeping (`vault_janitor.py` CLI)

Daily stock reports pile up in `20-areas/personal/finance/`. The janitor keeps the
**newest analysis per ticker** and moves older ones to `40-archive/finance/YYYYMM/`.
It is a **CLI script**, not an MCP tool. See [HOUSEKEEPING.md](HOUSEKEEPING.md) for the
full record and outstanding TODOs.

```bash
# MUST export the vault path, else it scans nonexistent ~/second-brain and falsely
# reports "無需 archive". Default = dry-run; pass --execute to actually move.
export SECOND_BRAIN_PATH="<abs path to vault>"
python mcp_second_brain/vault_janitor.py            # dry-run
python mcp_second_brain/vault_janitor.py --execute  # move files
```

- **Filename regex** groups by leading ticker token; tolerates an optional human-name
  segment (`2890.TW_永豐金_analysis_YYYYMMDD.md`) so all variants share one bucket.
- **Companion retention:** `*_analysis_*.json` snapshots follow the same newest-per-ticker
  rule as Markdown analyses; `00_Daily_Briefing_*.md` keeps the newest 14 days.
- **Active central-host schedules:** `com.user.vault-janitor` runs daily at 07:30 with
  `--push --execute`; `com.user.vault-sleep` runs Sunday at 02:00. Run these jobs only on
  the central host to preserve the single-writer discipline.
- **DuckDB upkeep is opt-in:** Tasks 5–6 (sleep candidates + `sync_all`) touch the local
  DuckDB fallback index and run **only** when `SB_DB_BACKEND=duckdb`. In the Postgres-central
  setup they are skipped (pg-sync owns freshness); this avoids an uncatchable DuckDB C++ abort.
  The bare-script `import` bug is fixed (lazy dual-mode `_import_vault_db()`).

### D. Code changes

```text
1. Read CLAUDE.md for architecture and security rules
2. Core tool logic: server.py
3. Semantic search / scoring: vault_db.py
4. After changes, run tests: pytest tests/ -q
```

### E. Update an existing note

`new_note` does not overwrite existing files. To update:

```text
Append (safe, preserves existing content):
  append_to_note(path, content)

Overwrite (full rewrite):
  1. read_note(path)                 ← verify current structure
  2. update_note(path, new_content)  ← overwrites (re-indexes + updates links)
```

**Create vs update:**

| Situation | Action |
| --------- | ------ |
| Note does not exist | `new_note` |
| Appending progress / supplement | `append_to_note` |
| Major rewrite, fixing frontmatter | `read_note` → `update_note` |
| Preserve history before rewrite | `snapshot_note_tool` first, then `update_note` |
| Update goals | `update_goals` (dedicated tool) |

---

## File Output Rules

| Output type | Path | Naming |
| ----------- | ---- | ------ |
| New note | Folder per NOTE_CONFIG | `{slug}.md` (auto-generated) |
| Decision log | `decisions/` | `{slug}.md` |
| Saved article | `30-resources/` | `{slug}.md` |
| Archived note | `40-archive/` | original name preserved |
| Figures | `figures/{slug}/` | `fig-{NN}.png` |

---

## Security Rules (Hard Limits)

1. **Path safety**: `read_note` / `update_note` / `append_to_note` use `.resolve().is_relative_to(VAULT)` to prevent path traversal
2. **SSRF protection**: `save_article` source must pass `_validate_source()` — only http/https or whitelisted extensions; image downloads must pass `_is_ssrf_safe()` — blocks loopback / RFC-1918 / 169.254
3. **Destructive ops require dry_run first**: `vault_sleep`, `consolidate_tool`, `prune_archive_tool` must be called with `dry_run=True` before executing
4. **`new_note` never overwrites**: returns `"Note already exists"` if file exists; use `update_note` or `append_to_note` instead
5. **YAML frontmatter escaping**: title/source use `json.dumps(value.strip())[1:-1]`, never `.replace('"', "'")`

---

## Session Start Checklist

At the start of a new conversation (as needed):

1. Call `get_context()` — loads goals and active notes, establishes session context
2. For history search → `search_notes()` or `get_decisions()`
3. For code changes → read `CLAUDE.md` for security rules

---

## Vault Directory Structure

```text
second-brain/
├── 00-inbox/          # Unsorted new notes (clear periodically)
├── 10-projects/       # Project pages
├── 20-areas/
│   ├── coding/        # Tech notes, tool evaluations
│   ├── research/      # Research papers, findings
│   └── personal/      # Personal areas (finance, health, etc.)
├── 30-resources/      # Reference material
├── 40-archive/        # Archived notes
├── decisions/         # Decision logs (ADR)
├── figures/           # All image attachments (visible, Obsidian-indexed)
├── memory/
│   ├── goals.md       # Current goals (loaded by get_context)
│   ├── rules.md       # Active rules (injected by get_context)
│   └── index.md       # Vault index backup
└── templates/         # Note templates
```

---

## Related Files

- [`CLAUDE.md`](CLAUDE.md) — Security rules, run commands, environment variables
- [`README.md`](README.md) — Feature overview, tool index
- [`NEW_MACHINE_SETUP.md`](NEW_MACHINE_SETUP.md) — Multi-machine deployment guide (Drive source code model)
- [`HOUSEKEEPING.md`](HOUSEKEEPING.md) — Vault maintenance record, `vault_janitor.py` mechanism + TODOs (see §C-bis)
- [`MIGRATION_PLAN_POSTGRES.md`](MIGRATION_PLAN_POSTGRES.md) — DuckDB → Postgres central-brain migration
- [`MULTIUSER_PLAN.md`](MULTIUSER_PLAN.md) — Multi-user / dual-vault (personal + lab) design
- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — PDF figure/text pipeline plan
