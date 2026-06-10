# AGENTS.md — Second Brain Operating Manual for AI Agents

> Read this document before making any vault changes. It defines tool usage, note types, security rules, and standard operating procedures.
>
> **How to get this document**: When connected via MCP, call `get_agent_instructions()`. When working locally with Claude Code in the `second-brain/` directory, it is auto-loaded via `CLAUDE.md`.
>
> **When adding documentation**: modify the relevant section here, then update the Last updated date.
>
> **Last updated:** 2026-06-10

---

## System Overview

Second Brain is a personal knowledge management server that exposes vault read/write, search, archiving, and maintenance via MCP.

- **MCP server**: `server.py` (27 tools)
- **Vector database**: `vault_db.py` (semantic search, Ebbinghaus scoring)
- **Vault path**: controlled by environment variable `SECOND_BRAIN_PATH`

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
| "Search figures" | `search_figures(query)` | |
| "Snapshot this note" | `snapshot_note_tool(note_path, tier)` | tier: "base" or "detail" |
| "Initialize vault / fix directory structure" | `init_vault()` | Safe to re-run, only creates missing items |
| "Agent instructions" (remote session start) | `get_agent_instructions()` | Returns this document |

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

### C. Vault maintenance (periodic or on demand)

```text
1. Check archive candidates   → sleep_status()
2. Dry-run archive            → vault_sleep(dry_run=True)
3. Execute archive            → vault_sleep(dry_run=False)
4. Find duplicates (dry-run)  → consolidate_tool(threshold=0.85, dry_run=True)
5. Clean old archive          → prune_archive_tool(dry_run=True) → (dry_run=False)
```

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
