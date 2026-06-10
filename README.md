# second-brain MCP Server

<!-- mcp-name: io.github.ddmanyes/mcp-second-brain -->

**A self-maintaining personal knowledge database — powered by MCP, DuckDB, and biological memory models.**

[![CI](https://github.com/ddmanyes/second-brain-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ddmanyes/second-brain-mcp/actions/workflows/ci.yml)
[![Python ≥ 3.11](https://img.shields.io/badge/Python-%E2%89%A53.11-blue)](https://www.python.org/)
[![DuckDB](https://img.shields.io/badge/DuckDB-1.1%2B-yellow)](https://duckdb.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio-green)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

---

> **For anyone who saves more papers, notes, and figures than they could ever re-read.**
> second-brain turns everything you capture into a database that *maintains itself* — auto-linking related notes, compressing what you stop reading, and keeping every figure searchable by its content. What you saved a year ago is still one query away, at a fraction of the token cost.

## Why Does This Exist?

| Problem | Solution |
| :------ | :------- |
| 📄 You save dozens of papers but can never find the right figure | `search_figures("UMAP melanocyte")` — returns the exact panel, across every paper you've saved |
| 📑 arXiv gives you the abstract; you need the full paper | Auto-upgrades `/abs/` → `/html/` — fetches the complete paper with all sections, not just the abstract |
| 🗂 Notes pile up; older ones never get cleaned up | **Vault Sleep**: low-access notes compress automatically every Sunday while you sleep (60–90% token reduction) |
| 🔗 New notes stay isolated; you forget what's connected | **Auto-wikilinks**: every saved note is automatically linked to semantically related notes already in your vault |
| 🔎 Semantic search needs a cloud API or Docker stack | Self-hosted `nomic-embed-text` via llama-server; BM25 fallback when offline |
| 🔒 Every AI memory tool locks you into their format | Pure Markdown vault — sync with Google Drive, iCloud, or git; switch agents anytime |
| 🖼 Figure context is lost when you read a paper | Every figure is downloaded, OCR'd by Claude Vision, and stored in DuckDB — searchable by gene name, p-value, axis label |

---

## The One-Command Demo

```text
save_article("https://arxiv.org/abs/2405.01234")
  ↓
• /abs/ auto-upgraded to /html/ — full paper, not just abstract
• Full text converted to Markdown
• All figures downloaded + OCR'd by Claude Vision
• Semantic embeddings computed
• Auto-linked to related notes already in your vault   ← auto-wikilinks
• Stored in 30-resources/ — queryable immediately

search_figures("UMAP cluster batch correction")
  ↓
• Returns the exact figure from the exact paper
• Works across your entire saved literature library
```

---

## What Makes It Different

```mermaid
flowchart LR
    subgraph input["📥 Any Content Source"]
        A1["arXiv / PubMed paper"]
        A2["Web article / blog"]
        A3["Local PDF / DOCX"]
        A4["Personal note"]
    end

    subgraph core["⚙️ second-brain-mcp"]
        B1["Markdown note<br/>30-resources/"]
        B2["Figure OCR<br/>+ VLM description"]
        B3["Semantic embedding<br/>+ auto-wikilinks"]
        B4["Ebbinghaus score<br/>ranking"]
        B5["PNG snapshots<br/>60–90% token reduction"]
    end

    subgraph query["🔍 Queryable Knowledge"]
        C1["search_figures<br/>'UMAP melanocyte'"]
        C2["search_notes<br/>'batch correction scRNA'"]
        C3["get_context<br/>top-20 relevant notes"]
    end

    input --> core
    B1 --> B2
    B1 --> B3
    B3 --> B4
    B4 --> B5
    B2 --> C1
    B3 --> C2
    B4 --> C3
```

**Eight things most self-hosted memory tools can't do — combined in one:**

| Most memory tools… | second-brain |
| :----------------- | :----------- |
| Save a link or PDF, then leave you to read and tag it | 🔬 **One command builds the database** — `save_article` fetches any URL/PDF, converts to Markdown, downloads & OCRs every figure with Claude Vision, then semantic-indexes it |
| Store the arXiv *abstract* you pasted | 📑 **Full text, not abstracts** — `/abs/` URLs auto-upgrade to `/html/` for the complete paper: methods, results, discussion |
| Leave new notes isolated until you tag them | 🔗 **The knowledge graph builds itself** — every note is auto-linked to semantically related notes already in your vault |
| Cost the same whether a note is read daily or never | 🧠 **Memory that forgets like a brain** — Ebbinghaus score ranks by recency × frequency; stale notes compress while you sleep |
| Search *documents*, not what's inside the figures | 🖼 **Figure-level search across your whole library** — `search_figures("p < 0.001")` returns the exact panel from the exact paper |
| Forget your project decisions between sessions | 📋 **The AI learns your rules** — hot notes auto-extract constraints into `memory/rules.md`, injected at every session start |
| Grow more expensive as the vault grows | 📉 **Token cost shrinks with age** — PNG snapshots replace old text at 60–90% compression; frequently-read papers stay full-fidelity |
| Lock you into their database format | 🔓 **Zero lock-in** — pure Markdown, any MCP agent, sync via any cloud drive or git |

---

## Cross-Session Continuity — Pick Up Where You Left Off

Every project you work on can be resumed in a new session with full context — no re-explaining, no lost progress.

```mermaid
flowchart LR
    A["🟢 Session Start<br/>get_context()"] --> B["AI receives:<br/>• goals.md — current priorities<br/>• Top-20 recent notes<br/>• Extracted rules"]
    B --> C["Work on project<br/>new_note / search / read"]
    C --> D["🔴 Before ending session<br/>update_goals(...)"]
    D --> E["New session<br/>get_context() again"]
    E --> B
```

### How It Works in Practice

**End of session** — tell the agent to save state:

```text
Update goals: currently working on the scRNA batch correction pipeline.
Completed: harmony integration. Blocked on: choosing n_components for PCA.
Next session: start from the PCA parameter sweep in 20-areas/research/harmony-notes.md
```

The agent calls `update_goals()` and optionally `new_note("project", ...)` for detailed progress.

**Start of next session** — just say:

```text
Get context and continue where we left off.
```

The agent calls `get_context()` and immediately sees:

- `goals.md` with the state you saved
- The harmony-notes.md surfaced at the top (recently accessed, high Ebbinghaus score)
- Rules auto-extracted from that note, e.g.:

```text
RULE: use n_components=30 for this dataset — tested 20/30/50, 30 minimises batch effect without losing resolution
RULE: exclude sample CRC_04 — library size outlier confirmed by QC
```

These rules live in `memory/rules.md` and are injected at every `get_context()` call — the AI carries your hard-won decisions forward automatically, without you having to repeat them.

### What Gets Persisted

| What | Where | Always in context? |
| :--- | :---- | :----------------: |
| Current priorities / blocked items | `memory/goals.md` | ✅ every session |
| Project progress notes | `10-projects/` or `20-areas/` | ✅ if recently accessed |
| Decisions and rationale | `decisions/` | via `get_decisions()` |
| Extracted rules from notes | `memory/rules.md` | ✅ every session |
| Saved papers and figures | `30-resources/` | via `search_notes/figures` |

> **This works across any project** — bioinformatics analysis, coding, writing, research. Save state with one sentence at the end of a session; resume instantly at the start of the next.

---

## Consistent Filing — The Operating Manual

Tools alone don't keep a vault tidy — the *agent* has to know **where** each note belongs, **how** to name it, and **what not to touch**. second-brain ships a single operating manual (`AGENTS.md`) that encodes those decisions, so any agent files things the same way every time — no re-explaining your conventions each session.

```text
Agent receives a request
        │
        ▼
get_agent_instructions()   ← returns the full AGENTS.md operating manual
        │
        ▼
Agent now knows, without being told:
  • Filing decision tree   — paper (DOI/author) → 20-areas/research/
                             reference doc      → 30-resources/
                             stock analysis     → finance/  · unsure → 00-inbox/
  • Naming convention      — papers: {YYYY}_{Author}_{ShortTitle}.md
  • Figure rules           — figures/{slug}/fig-NN.png, embedded as ![[…]]
  • Editing discipline     — local edits only, never reorder frontmatter,
                             new notes go through new_note (templated)
```

**Why it matters for efficiency:**

| Without the manual | With `AGENTS.md` |
| :----------------- | :--------------- |
| You re-explain "papers go here, name them like this" every session | The agent reads it once per session and just does it |
| Notes drift into inconsistent folders/names; search degrades | Stable structure → semantic + figure search stays reliable |
| Each agent (local Claude Code, remote MCP, Gemini) behaves differently | One manual, one behaviour — served locally via `CLAUDE.md`, remotely via `get_agent_instructions()` |

> **Single source of truth:** keep all conventions in the one `AGENTS.md` the server serves. Forks in a second copy silently diverge — remote agents then act on stale rules. Edit the canonical file only.

---

## Example Queries

```python
# Resume a project from last session
get_context()  # → goals + recent notes + rules loaded automatically

# Find a specific figure panel across all saved papers
search_figures("p < 0.001 UMAP cluster")

# Semantic search across all notes
search_notes("single cell integration batch correction")

# Decision records for a specific project
get_decisions("MyProject")
```

---

## Memory Architecture — Biological Analogy

| Biological Brain | This System |
| :-------------- | :---------- |
| Hippocampal consolidation during sleep | Vault Sleep: weekly LLM-compression of old low-access notes |
| Ebbinghaus forgetting curve | Score-based ranking: `access_count / ln(age_days)` |
| Visual long-term memory | PNG snapshots — resolution degrades gracefully with age |
| Associative recall | Semantic search + auto-generated `[[wikilinks]]` |
| Sleep-dependent consolidation | launchd cron, runs Sunday 02:00 while you sleep |

---

## Token Efficiency

Memory that gets cheaper over time — unlike flat-file systems where old notes cost the same forever.

```text
Note age →   fresh (0–3 mo)   3–6 months     6–12 months    1 year+
             ──────────────   ──────────     ───────────    ───────
token cost:  ██████████████   ██████         ████           ██
             ~1,000 tokens    ~400 tokens    ~256 tokens    ~100 tokens
                              ▼ 60%          ▼ 74%          ▼ 90%
```

> Tier assigned by **score × age** (adaptive). Frequently-accessed notes stay full-text regardless of age.

---

## Search Performance

Measured on Apple Silicon MacBook (20-rep average, BM25-only mode).

```text
Vault    BM25-only p50          Hybrid BM25+semantic p50
──────   ─────────────────      ────────────────────────
10 n     ████░░░░░   21 ms      ████████████   37 ms
50 n     ██████░░░   25 ms      █████████████  39 ms
100 n    ███████░░   27 ms      ██████████████ 45 ms
```

| Vault Size | BM25 p50 | Hybrid p50 | Recall@1 | Recall@5 | MRR |
| :--------: | :------: | :--------: | :------: | :------: | :-: |
| 10 notes | 21 ms | 37 ms | 30% | 60% | 0.42 |
| 50 notes | 25 ms | 39 ms | 70% | 90% | 0.78 |
| 100 notes | 27 ms | 45 ms | 70% | 80% | 0.73 |

> Hybrid mode adds ~18 ms for embedding lookup. Both modes scale sub-linearly with vault size.
>
> Recall figures at this scale (10–100 notes) carry high sample variance — a single ambiguous query shifts Recall@1 by 10%. Treat them as directional, not as benchmarks against large corpora; the takeaway is that hybrid consistently beats BM25-only on relevance for a fixed query set.

---

## System Architecture

```text
┌─────────────────────────────────────────────────────┐
│                    AI Agent Layer                    │
│         Claude Code · Gemini CLI · Any MCP           │
└──────────────────────┬──────────────────────────────┘
                       │ MCP Protocol (27 tools)
┌──────────────────────▼──────────────────────────────┐
│               Layer 2 — MCP Server                   │
│                    server.py                         │
│  get_context · search_notes · save_article · … (27)  │
└──────┬───────────────┬────────────────┬─────────────┘
       │               │                │
┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
│  vault_sleep│ │  vault_db   │ │  figures    │
│  compress   │ │  DuckDB FTS │ │  PNG snap   │
│  Phase 3–9  │ │  + semantic │ │  OCR · VLM  │
└──────┬──────┘ └──────┬──────┘ └─────────────┘
       │               │
┌──────▼───────────────▼──────────────────────────────┐
│               Layer 0 — Markdown Vault               │
│   00-inbox · 10-projects · 20-areas · 30-resources   │
│   40-archive · decisions · memory · templates        │
│         (syncs via Google Drive / iCloud / git)      │
└─────────────────────────────────────────────────────┘
```

---

## Vault Sleep — Auto-compression Flow

```text
Every Sunday 02:00 (launchd, no interaction needed)
        │
        ▼
 sync_index + embeddings
        │
        ▼  age > 90d AND Ebbinghaus score ≤ 0.5
 ┌──────────────────────────────────────┐
 │         Adaptive Tier Selection      │
 │  score > 1.5  →  text  (keep full)  │  ← frequently-read: never compressed
 │  score > 0.8  →  large  ~400 tokens │
 │  score > 0.3  →  base   ~256 tokens │
 │  otherwise    →  small  ~100 tokens │
 └────────────────┬─────────────────────┘
                  │
  Gemini CLI → Claude CLI → naive   (auto-fallback, no LLM required)
                  │
    compressed → vault  /  original → 40-archive/  /  snapshot → .png
```

---

## MCP Tools (27 total)

| Tool | Description |
| :--- | :---------- |
| `get_context` | Session start: goals + top-20 Ebbinghaus-ranked notes + auto-rules |
| `save_article` | **Fetch URL/PDF → Markdown + auto-extract figures** |
| `search_notes` | Hybrid BM25 + semantic search across all notes |
| `search_figures` | **Search figure OCR text / VLM descriptions** |
| `search_news_tool` | Search cnews morning briefs and financial news by keyword |
| `extract_figures_for` | Manually trigger figure extraction for a saved article |
| `read_note` | Read note + record access (updates Ebbinghaus score) |
| `read_note_as_image` | Return PNG snapshot for token-efficient reading |
| `new_note` | Create note with correct template and folder by type |
| `get_decisions` | List ADR decision records, optionally filtered by project |
| `update_goals` | Update `memory/goals.md` |
| `sync_index` | Rebuild DuckDB index from vault files |
| `index_stats` | Show note counts by type |
| `vault_sleep` | Compress old low-activity notes (dry_run=True by default) |
| `sleep_status` | Show compression candidates without acting |
| `snapshot_note_tool` | Render note to PNG at chosen resolution tier |
| `extract_rules_tool` | Extract L3 rules from frequently-accessed notes |
| `consolidate_tool` | Merge semantically similar notes into one abstract note |
| `update_links_tool` | Refresh auto-generated `[[wikilinks]]` |
| `prune_archive_tool` | Delete archived originals that have a PNG snapshot |
| `find_related_notes` | Find semantically related notes by cosine similarity (finance & knowledge management) |
| `search_grouped` | Hybrid search returning knowledge notes + cnyes morning briefs in one call |
| `top_notes` | Rank notes by Ebbinghaus score or recency — find your most-engaged knowledge nodes |
| `update_note` | Overwrite an existing note with new content (auto-reindexes) |
| `append_to_note` | Append content to end of an existing note — safe, never loses existing text |
| `init_vault` | Create or repair vault directory structure and templates |
| `get_agent_instructions` | Return full AGENTS.md operating manual — call at remote session start to learn vault SOP |

---

## Test Results

```text
tests/test_figures.py      19 passed   (OCR, snapshots, VLM)
tests/test_server.py       25 passed   (MCP tools, path safety)
tests/test_vault_db.py     73 passed   (FTS, semantic search, embeddings)
tests/test_vault_sleep.py  46 passed   (compression, consolidation, rules, prune)
────────────────────────────────────────
163 passed in 7.51s
```

---

## Installation

> ⚠️ **Install from source for now.** The published PyPI package (`mcp-second-brain` 0.1.0)
> lags the current source, and the `python -m mcp_second_brain` entry point in the Quick
> Start below is **not yet packaged** — those `pip install` snippets will land an outdated
> build. Until a fresh release ships, use the **[Development Install (clone)](#development-install-clone)**
> path. Self-hosting across your own machines from a synced source tree? See
> [`NEW_MACHINE_SETUP.md`](NEW_MACHINE_SETUP.md).

### Prerequisites

| Dependency | Required | Notes |
| :--------- | :------: | :---- |
| Python 3.11+ | ✅ | |
| [Playwright](https://playwright.dev/) | ✅ | PNG snapshot rendering |
| [uv](https://docs.astral.sh/uv/) | Dev only | Only needed for `Development Install` path |
| [llama-server](https://github.com/ggerganov/llama.cpp) or [Ollama](https://ollama.com) | Optional | Enables semantic search; BM25 fallback if absent |
| [nomic-embed-text-v1.5.Q8_0.gguf](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF) | Optional | ~300 MB — needed for llama-server path only |
| `ANTHROPIC_API_KEY` | Optional | Better compression quality in vault_sleep; naive fallback if absent |

> **Vault structure is auto-created on first server start** — no manual `mkdir` needed.

---

### macOS / Linux — Quick Start

#### Step 1 — Install

```bash
pip install mcp-second-brain
playwright install chromium
```

#### Step 2 — Register with your AI agent

**Claude Code (CLI) — global, works in any project:**

```bash
claude mcp add --scope user second-brain \
  --env SECOND_BRAIN_PATH=~/second-brain \
  -- python -m mcp_second_brain
```

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "python",
      "args": ["-m", "mcp_second_brain"],
      "env": { "SECOND_BRAIN_PATH": "/Users/yourname/second-brain" }
    }
  }
}
```

#### Step 3 — First run

Start your agent and say:

```text
init_vault
```

The server auto-creates all directories and templates on startup. Call `init_vault` explicitly to verify or repair the structure.

---

### Windows — Quick Start

#### Step 1 — Install Python and the package

```powershell
# Install Python 3.11+ from python.org, or via winget:
winget install Python.Python.3.11

pip install mcp-second-brain
playwright install chromium
```

#### Step 2 — Choose a vault location

```powershell
# Local folder:
$vault = "C:\Users\$env:USERNAME\second-brain"

# Or a cloud-synced folder (Google Drive, OneDrive, etc.):
$vault = "G:\My Drive\second-brain"
```

> The vault directories and templates are **created automatically** when the server first starts. No manual setup needed.

#### Step 3 — Register with your AI agent

**Claude Code (VSCode extension)** — create `.mcp.json` in your vault folder:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "python",
      "args": ["-m", "mcp_second_brain"],
      "env": { "SECOND_BRAIN_PATH": "C:\\Users\\YourName\\second-brain" }
    }
  }
}
```

**Claude Desktop** — add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "python",
      "args": ["-m", "mcp_second_brain"],
      "env": { "SECOND_BRAIN_PATH": "C:\\Users\\YourName\\second-brain" }
    }
  }
}
```

**Gemini CLI / other IDEs** — edit `%USERPROFILE%\.gemini\mcp_config.json` (or IDE-specific config):

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "C:\\Users\\YourName\\.venvs\\mcp-second-brain\\Scripts\\python.exe",
      "args": ["-m", "mcp_second_brain"],
      "env": { "SECOND_BRAIN_PATH": "C:\\Users\\YourName\\second-brain" }
    }
  }
}
```

> **Tip:** Use a dedicated venv at `C:\Users\YourName\.venvs\mcp-second-brain\` (local SSD) rather than a venv on a network drive. Python loads thousands of small files at startup — on a cloud drive this causes 15–30 s delays and `context deadline exceeded` errors.

#### Step 4 — Semantic search (optional, Windows)

Ollama is the easiest path on Windows:

```powershell
winget install Ollama.Ollama
ollama pull nomic-embed-text
```

Then add to the `env` block of your MCP config:

```json
"EMBED_URL": "http://localhost:11434/v1/embeddings",
"EMBED_PORT": "11434"
```

Ollama starts automatically with Windows. No extra configuration needed.

Alternatively, build [llama.cpp](https://github.com/ggerganov/llama.cpp) for Windows and register it as a scheduled task:

```powershell
# Register llama-server as a login-triggered scheduled task:
$exe  = "C:\Users\$env:USERNAME\llama.cpp\build\bin\llama-server.exe"
$model = "C:\Users\$env:USERNAME\nomic-embed-text-v1.5.Q8_0.gguf"
$args = "-m `"$model`" --port 11435 --embedding --pooling mean -np 4 -c 2048 --log-disable"

$action   = New-ScheduledTaskAction -Execute $exe -Argument $args
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "llama-embed" -Action $action -Trigger $trigger `
  -Settings $settings -RunLevel Highest -Force
```

#### Step 5 — Weekly vault maintenance (optional, Windows)

```powershell
# Run vault_sleep every Sunday at 02:00
$pythonExe = "C:\Users\$env:USERNAME\.venvs\mcp-second-brain\Scripts\python.exe"
$serverPy  = "C:\path\to\second-brain-mcp\run_sleep.py"
$vaultPath = "C:\Users\$env:USERNAME\second-brain"

$action  = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$serverPy`"" `
  -WorkingDirectory (Split-Path $serverPy)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 2am
$env_var = [System.Environment]::SetEnvironmentVariable(
  "SECOND_BRAIN_PATH", $vaultPath, "Machine")
Register-ScheduledTask -TaskName "vault-sleep" -Action $action -Trigger $trigger -Force
```

---

### Development Install (clone)

```bash
git clone https://github.com/ddmanyes/second-brain-mcp
cd second-brain-mcp
uv sync
uv run playwright install chromium
```

Register with Claude Code:

```bash
# macOS / Linux
claude mcp add --scope user second-brain \
  --env SECOND_BRAIN_PATH=~/second-brain \
  -- uv run --project /path/to/second-brain-mcp python server.py

# Windows (PowerShell)
claude mcp add --scope user second-brain `
  --env SECOND_BRAIN_PATH="C:\Users\$env:USERNAME\second-brain" `
  -- uv run --project C:\path\to\second-brain-mcp python server.py
```

### Environment Variables

| Variable | Default | Description |
| :------- | :------ | :---------- |
| `SECOND_BRAIN_PATH` | `~/second-brain` | Path to your vault directory |
| `EMBED_URL` | `http://localhost:11435/v1/embeddings` | Embedding server endpoint |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model name |
| `EMBED_PORT` | `11435` | llama-server port (use `11434` for Ollama) |

### Auto-start (macOS, optional)

```bash
# Embedding server — always on, restarts on crash
cp examples/launchd/com.yourname.llama-embed.plist ~/Library/LaunchAgents/
# Edit paths inside the file, then:
launchctl load ~/Library/LaunchAgents/com.yourname.llama-embed.plist

# Weekly vault maintenance — every Sunday 02:00
cp examples/launchd/com.yourname.vault-sleep.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.yourname.vault-sleep.plist
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| :------ | :----------- | :-- |
| `new_note` returns `Error: template not found` | Templates missing from vault | Run `init_vault` — the server copies bundled templates automatically |
| Semantic search silently falls back to BM25 | Embedding server not running | Start Ollama (`ollama serve`) or llama-server; check with `curl localhost:11435/health` (or port 11434 for Ollama) |
| `read_note_as_image` / snapshots fail | Playwright chromium not installed | `pip install playwright && playwright install chromium` |
| `vault_sleep` never compresses anything | No `ANTHROPIC_API_KEY` → naive fallback, or no eligible notes | Export `ANTHROPIC_API_KEY`; only notes >90 days old with Ebbinghaus score ≤ 0.5 are candidates — run `sleep_status` to see them |
| Agent sees no notes / empty results | Index not built | Run `sync_index` once after install (and after bulk file changes) |
| Notes land in the wrong place | `SECOND_BRAIN_PATH` unset or wrong | Set it in your MCP config `env` block; defaults to `~/second-brain` |
| Tools unavailable when working in other project folders | Installed as local config instead of user scope | Re-register with `--scope user`: `claude mcp remove second-brain -s local && claude mcp add --scope user second-brain ...` |
| **Windows:** MCP server times out on connect (`context deadline exceeded`) | Python venv is on a network/cloud drive | Move the venv to local SSD (e.g. `C:\Users\Name\.venvs\`) — cloud drives cause 15–30 s startup delay |
| **Windows:** Semantic search returns no results after `sync_index` | Ollama not running | `ollama serve` in a terminal, or install Ollama with auto-start via winget |

---

## Vault Structure

```text
vault/
├── 00-inbox/          # Unprocessed captures — clear daily
├── 10-projects/       # Active projects
├── 20-areas/
│   ├── research/      # Ongoing research domains
│   ├── coding/        # Dev tools and workflows
│   └── consolidated/  # Auto-merged similar notes (Phase 8)
├── 30-resources/      # ← Papers and articles (save_article writes here)
├── 40-archive/        # Compressed originals (auto-managed by vault_sleep)
├── decisions/         # Architecture Decision Records (ADR format)
├── memory/
│   ├── goals.md       # Current priorities — injected at every session start
│   ├── index.md       # Vault map
│   └── rules.md       # Auto-extracted L3 rules — injected at every session start
└── templates/         # Note templates (note, decision, project, research)
```

---

## Running Tests

```bash
uv run pytest tests/ -v
uv run python benchmark.py --quick --markdown   # search latency + accuracy report
```

---

## References & Acknowledgements

### Papers That Directly Inspired This Project

| Paper | Where Used |
| :---- | :--------- |
| [Do Language Models Need Sleep? Offline Recurrence for Improved Online Inference (2026)](https://arxiv.org/abs/2605.26099) | Phase 3 Vault Sleep — hippocampal replay as batch memory consolidation |
| [Experience Compression Spectrum: Unifying Memory, Skills, and Rules in LLM Agents (2026)](https://arxiv.org/abs/2604.15877) | Phase 9 adaptive tier — score × age dual-axis; addresses the "missing diagonal" in existing systems |
| [DeepSeek-OCR: Contexts Optical Compression (2025)](https://arxiv.org/abs/2510.18234) | Phase 4 PNG tiers — image as compressed medium, 10× compression at 97% fidelity |
| [MemOCR: Layout-Aware Visual Memory for Efficient Long-Horizon Reasoning (2026)](https://arxiv.org/abs/2601.21468) | Phase 4 vision API — Playwright render → VLM reading pipeline |
| [Active Context Compression: Autonomous Memory Management in LLM Agents (2026)](https://arxiv.org/abs/2601.07190) | Phase 3 design comparison — session-level vs. nightly batch consolidation |
| [SimpleMem: Efficient Lifelong Memory for LLM Agents (2026)](https://arxiv.org/abs/2601.02553) | Phase 8 consolidation — 3-stage semantic compression, 30× token reduction |
| [Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers (2026)](https://arxiv.org/abs/2603.07670) | Architecture positioning — mechanisms, evaluation, and frontiers |

### Cognitive Science Foundations

- Ebbinghaus, H. (1885). *Über das Gedächtnis*. — forgetting curve; basis for `access_count / ln(age_days + 1)`
- [Stickgold, R. (2005). *Nature*, 437, 1272–1278.](https://www.nature.com/articles/nature04286) — sleep-dependent memory consolidation

### Built With

[MarkItDown](https://github.com/microsoft/markitdown) · [DuckDB](https://duckdb.org) · [llama.cpp](https://github.com/ggerganov/llama.cpp) · [nomic-embed-text](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF) · [FastMCP](https://github.com/jlowin/fastmcp) · [Playwright](https://playwright.dev) · [Anthropic Claude API](https://docs.anthropic.com)

---

## 新機器安裝教學（個人設定）

> 適用於已有 Google Drive 同步 vault 的情境。Vault 程式碼在 Drive 上，只需在本機建立 venv 和 MCP 設定。

### 前置條件

| 項目 | 說明 |
| :--- | :--- |
| Python 3.11+ | `python3 --version` 確認 |
| Google Drive 桌面版 | Vault 同步到本機 |
| Claude Code（CLI 或 VSCode extension） | MCP client |
| uv（選用） | 加速套件安裝 |

### Step 1 — 確認 vault 路徑

```bash
# Google Drive vault 同步位置
ls ~/Library/CloudStorage/GoogleDrive-*/我的雲端硬碟/PJ_save/second-brain
# 程式碼位置
ls ~/Library/CloudStorage/GoogleDrive-*/我的雲端硬碟/PJ_save/mcp-tools/second-brain/server.py
```

以下指令假設路徑為：

- `SB_CODE` = `~/Library/CloudStorage/GoogleDrive-.../PJ_save/mcp-tools/second-brain`
- `SB_VAULT` = `~/Library/CloudStorage/GoogleDrive-.../PJ_save/second-brain`

### Step 2 — 建立本機 venv

> **重要**：venv 必須建在本機（`~/.venvs/`），不要放在 Google Drive 上。
> Drive 會破壞 symlinks，導致 `bin/python` 指向不存在的路徑。

```bash
# 建立 venv（一次性）
python3 -m venv ~/.venvs/second-brain

# 安裝依賴
~/.venvs/second-brain/bin/pip install -r \
  ~/Library/CloudStorage/GoogleDrive-*/我的雲端硬碟/PJ_save/mcp-tools/second-brain/requirements.txt

# 安裝 Playwright（PNG snapshot 功能需要）
~/.venvs/second-brain/bin/playwright install chromium
```

### Step 3 — 設定 MCP

**Gemini CLI / Antigravity IDE** — 編輯 `~/.gemini/antigravity-ide/mcp_config.json`（或 `~/.gemini/config/mcp_config.json`）：

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/wangchiayi/.venvs/second-brain/bin/python",
      "args": [
        "/Users/wangchiayi/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/mcp-tools/second-brain/server.py"
      ],
      "env": {
        "SECOND_BRAIN_PATH": "/Users/wangchiayi/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/second-brain",
        "PYTHONPATH": "/Users/wangchiayi/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/mcp-tools/second-brain"
      }
    }
  }
}
```

**Claude Code（CLI）** — user scope で登錄：

```bash
# 先刪除舊設定（如果有）
claude mcp remove second-brain --scope user 2>/dev/null

# 重新加入（用本機 venv 的 python）
SB_CODE="$HOME/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/mcp-tools/second-brain"
SB_VAULT="$HOME/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/second-brain"

claude mcp add --scope user second-brain \
  ~/.venvs/second-brain/bin/python \
  "$SB_CODE/server.py" \
  -e SECOND_BRAIN_PATH="$SB_VAULT" \
  -e PYTHONPATH="$SB_CODE"
```

確認設定：

```bash
claude mcp list --scope user | grep second-brain
```

### Step 4 — 語義搜尋（選用）

新機器需要本機的 embedding server。最簡單的方式是 Ollama：

```bash
brew install ollama
ollama pull nomic-embed-text
ollama serve &   # 或設為 background service
```

然後在 MCP 設定加入環境變數：

```bash
# 使用 Ollama（port 11434）
claude mcp add --scope user second-brain \
  ~/.venvs/second-brain/bin/python \
  .../server.py \
  -e SECOND_BRAIN_PATH=... \
  -e EMBED_URL=http://localhost:11434/v1/embeddings \
  -e EMBED_PORT=11434
```

或使用 llama-server（需自行編譯 llama.cpp + 下載 nomic-embed-text-v1.5.Q8_0.gguf）：

```bash
~/llama.cpp/build/bin/llama-server \
  -m ~/nomic-embed-text-v1.5.Q8_0.gguf \
  --port 11435 --embedding --pooling mean -np 4 -c 2048 --log-disable &
```

### Step 5 — 重建索引

```bash
# 在 Claude Code 中呼叫（讓 server 自己跑，不要用外部 python script）
# 說：sync_index
```

> **注意**：不要用外部 `python -c "vault_db.sync_all(...)"` 直接跑，會與 Claude Code 的 MCP server 競爭 DuckDB 排他鎖，導致 `CatalogException: Table does not exist`。
> 應透過 MCP 工具讓 server 內部執行。

### Step 6 — 確認

```bash
# 在 AI agent 中呼叫：
# index_stats
# 應看到當前 vault 筆記數量（每台機器重建後會反映實際筆記數）
```

---

### 備忘：每台機器各自的本機資料

| 資料 | 位置 | 是否同步 |
| :--- | :--- | :------: |
| Vault markdown 筆記 | Google Drive | ✅ 所有機器共享 |
| 程式碼（server.py 等） | Google Drive | ✅ 所有機器共享 |
| Python venv | `~/.venvs/second-brain/` | ❌ 每台機器各自建立 |
| DuckDB index | `~/.second-brain/vault.db` | ❌ 每台機器各自重建（`sync_index`） |
| MCP 設定 | `~/.gemini/antigravity-ide/mcp_config.json`（Gemini CLI）または Claude Code user scope | ❌ 每台機器各自設定 |

---

## Known Issues & Fixes

### WAL corruption (`Failure while replaying WAL file`)

**Cause:** DuckDB write was interrupted mid-transaction (IDE restart, `pkill -9`, or machine sleep).  
**Symptom:** MCP server crashes on startup; every DB operation fails.  
**Fix:**

```bash
rm -f ~/.second-brain/vault.db ~/.second-brain/vault.db.wal
```

Restart the server — it will rebuild a clean DB. Run `sync_index` to re-index your vault.

> **Note:** If running inside a sandboxed IDE (e.g. Antigravity), the agent cannot delete `~/.second-brain/`. Run the command in your local Terminal, or ask Claude Code (VSCode extension) which has full shell access.

---

### vault.db created in the wrong directory

**Cause:** `server.py` was launched with a non-home working directory; DuckDB created `vault.db` relative to cwd.  
**Symptom:** `~/.second-brain/vault.db` is tiny (< 1 MB) but a large `vault.db` exists elsewhere.  
**Fix:**

```bash
# Find the real vault.db
find ~ -name "vault.db" -size +1M 2>/dev/null

# Move it to the correct location
cp /path/to/found/vault.db ~/.second-brain/vault.db
```

---

### IDE reports `no such file or directory` for `.venv/bin/python`

**Root cause:** The venv was created inside the Google Drive folder. Google Drive sync breaks symlinks — `bin/python` points to an absolute path on another machine, which doesn't exist locally.

**Permanent fix — use a local venv (macOS):**

```bash
# Create venv on local machine (once per machine)
python3 -m venv ~/.venvs/second-brain

SB_CODE="$HOME/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/mcp-tools/second-brain"
SB_VAULT="$HOME/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/second-brain"

~/.venvs/second-brain/bin/pip install -r "$SB_CODE/requirements.txt"

# For Gemini CLI / Antigravity — edit ~/.gemini/antigravity-ide/mcp_config.json:
# "command": "/Users/<you>/.venvs/second-brain/bin/python"

# For Claude Code — register with user scope:
claude mcp add --scope user second-brain \
  ~/.venvs/second-brain/bin/python \
  "$SB_CODE/server.py" \
  -e PYTHONPATH="$SB_CODE" \
  -e SECOND_BRAIN_PATH="$SB_VAULT"
```

Source code (`server.py`, etc.) stays on Google Drive and syncs normally. Only the venv lives on the local machine.

> **Do not** create the venv inside the Google Drive folder — it will break on every other machine that syncs it.

---

## Contributing

PRs and Issues welcome. Please open an issue first to discuss significant changes.

---

## License

MIT License — © 2026 Chan Chi Ru. See [LICENSE](LICENSE).
