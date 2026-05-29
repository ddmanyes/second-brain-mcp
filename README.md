# second-brain MCP Server

> Turn any URL, PDF, or note into a searchable knowledge database — with figure OCR, semantic search, and memory that compresses itself while you sleep.

![Tests](https://img.shields.io/badge/tests-115%20passed-brightgreen)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## The Core Idea

Most AI memory tools just "remember what you said." This system does something different: it builds a **searchable database from any content** — especially scientific literature — and models how biological memory actually works.

```
save_article("https://arxiv.org/abs/2405.01234")
  ↓
• Full paper → Markdown (auto-converted)
• All figures downloaded + OCR'd by Claude Vision
• Semantic embeddings computed
• Auto-linked to related notes in your vault

search_figures("UMAP cluster melanocyte")
  ↓
• Returns exact figure + caption from the paper
• Works across every paper you've ever saved
```

**One command to save a paper. One query to find a figure — across your entire literature library.**

---

## What Makes It Different

```mermaid
quadrantChart
    title Portability vs. Automation
    x-axis "Low Portability" --> "High Portability"
    y-axis "Low Automation" --> "High Automation"
    quadrant-1 Best of Both
    quadrant-2 Powerful but locked
    quadrant-3 Manual work
    quadrant-4 Portable but manual
    second-brain-mcp: [0.88, 0.92]
    Mem0: [0.18, 0.72]
    MemGPT: [0.22, 0.78]
    Obsidian + Smart Connections: [0.82, 0.32]
    Zotero + AI: [0.58, 0.28]
    Plain Markdown + Claude: [0.92, 0.12]
```

| Capability | **This Project** | Obsidian + Smart Connections | Zotero + AI | Mem0 / MemGPT |
| --------- | :-: | :-: | :-: | :-: |
| Save any URL / PDF as Markdown | ✅ | ❌ | Partial | ❌ |
| Figure extraction + OCR search | ✅ | ❌ | ❌ | ❌ |
| Semantic search (self-hosted) | ✅ | ✅ | ❌ | ✅ (cloud) |
| Memory auto-compression (sleep) | ✅ | ❌ | ❌ | ❌ |
| Ebbinghaus forgetting curve | ✅ | ❌ | ❌ | ❌ |
| Visual memory (PNG tiers) | ✅ | ❌ | ❌ | ❌ |
| Pure Markdown — no vendor lock-in | ✅ | ✅ | ❌ | ❌ |
| Works with any MCP agent | ✅ | ❌ | ❌ | ❌ |
| 100% self-hosted | ✅ | ✅ | ✅ | ❌ |

---

## Scientific Literature Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│  Input: any URL · arXiv · PubMed · PDF · blog · docs page       │
└──────────────────────────┬──────────────────────────────────────┘
                           │  save_article("https://...")
                           ▼
              ┌────────────────────────┐
              │  MarkItDown converter  │  ← handles HTML, PDF, DOCX
              │  arXiv /abs → /html   │  ← auto full-text upgrade
              └────────────┬───────────┘
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ┌─────────────┐ ┌────────────┐ ┌─────────────────┐
    │  Markdown   │ │  Figures   │ │  Semantic index  │
    │  30-resources│ │  OCR + VLM │ │  nomic-embed    │
    │  .md file   │ │  DuckDB    │ │  auto-wikilinks  │
    └─────────────┘ └────────────┘ └─────────────────┘
           │               │
           └───────────────┴──────────────────────────┐
                                                      ▼
                                        search_figures("TYRP1")
                                        search_notes("scRNA-seq harmony")
                                        → returns ranked results with
                                          figure previews + paper context
```

### Example Queries After Saving Papers

```python
# Find a specific figure across all saved papers
search_figures("p < 0.001 UMAP cluster")

# Find papers about a method
search_notes("single cell integration batch correction")

# Find decision records for your own project
get_decisions("Evo_PRISM")

# Ask Claude: "summarise everything I know about melanocyte markers"
# → Claude calls search_notes + read_note automatically
```

---

## Memory Architecture — Biological Analogy

| Biological Brain | This System |
| --------------- | ----------- |
| Hippocampal consolidation during sleep | Vault Sleep: weekly LLM-compression of old notes |
| Ebbinghaus forgetting curve | Score-based ranking: `access_count / ln(age_days)` |
| Visual long-term memory | PNG snapshots — resolution degrades gracefully with age |
| Associative recall | Semantic search + auto-generated `[[wikilinks]]` |
| Sleep-dependent consolidation | launchd cron, runs Sunday 02:00 while you sleep |

---

## Token Efficiency

Memory that gets cheaper over time — unlike flat-file systems where old notes cost the same as new ones.

```mermaid
xychart-beta
    title "Tokens to Represent a Note (by age)"
    x-axis ["Full text (any age)", "Large tier (>3 months)", "Base tier (>6 months)", "Small tier (>1 year)"]
    y-axis "Token cost" 0 --> 1100
    bar [1000, 400, 256, 100]
```

```mermaid
xychart-beta
    title "Cumulative Token Savings — 100-note Vault Over 2 Years"
    x-axis ["Month 3", "Month 6", "Month 12", "Month 18", "Month 24"]
    y-axis "Tokens saved (k)" 0 --> 80
    bar [0, 14, 37, 58, 74]
```

> Tier is selected by **score × age** (Phase 9 adaptive). High-access notes stay full-text regardless of age.

---

## Search Performance

```mermaid
xychart-beta
    title "Search Latency p50 (ms) — Apple Silicon MacBook"
    x-axis ["10 notes", "50 notes", "100 notes"]
    y-axis "Latency (ms)" 0 --> 55
    line [21, 25, 27]
    line [37, 39, 45]
```

> Line 1: BM25-only · Line 2: Hybrid (BM25 + semantic)  
> Hybrid adds ~18 ms for embedding lookup. Both scale sub-linearly with vault size.

| Vault Size | BM25 p50 | Hybrid p50 | Recall@1 | Recall@5 | MRR |
| :--------: | :------: | :--------: | :------: | :------: | :-: |
| 10 notes  | 21 ms | 37 ms | 30% | 60% | 0.42 |
| 50 notes  | 25 ms | 39 ms | 70% | 90% | 0.78 |
| 100 notes | 27 ms | 45 ms | 70% | 80% | 0.73 |

---

## System Architecture

```
┌─────────────────────────────────────────────────────┐
│                    AI Agent Layer                    │
│         Claude Code · Gemini CLI · Any MCP           │
└──────────────────────┬──────────────────────────────┘
                       │ MCP Protocol (19 tools)
┌──────────────────────▼──────────────────────────────┐
│               Layer 2 — MCP Server                   │
│                    server.py                         │
│   get_context · search_notes · save_article · …      │
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

```
Every Sunday 02:00 (launchd, no interaction needed)
        │
        ▼
 sync_index + embeddings
        │
        ▼  age > 90d AND score ≤ 0.5
 ┌──────────────────────────────────┐
 │       Adaptive Tier Selection    │
 │  score > 1.5  →  text (no comp) │  ← frequently-read papers: keep full
 │  score > 0.8  →  large  400 tok │
 │  score > 0.3  →  base   256 tok │
 │  otherwise    →  small  100 tok │
 └──────────────┬───────────────────┘
                │
 Gemini CLI → Claude CLI → naive   (auto-fallback, no LLM required)
                │
   compressed → vault / original → archive / snapshot → .png
```

---

## MCP Tools (19 total)

| Tool | Description |
| ---- | ----------- |
| `get_context` | Session start: goals + top-20 Ebbinghaus-ranked notes + auto-rules |
| `save_article` | **Fetch URL/PDF → Markdown + auto-extract figures** |
| `search_notes` | Hybrid BM25 + semantic search across all notes |
| `search_figures` | **Search figure OCR text / VLM descriptions** |
| `extract_figures_for` | Manually trigger figure extraction for a saved article |
| `read_note` | Read note + record access (updates Ebbinghaus score) |
| `read_note_as_image` | Return PNG snapshot for token-efficient reading |
| `new_note` | Create note with correct template/folder by type |
| `get_decisions` | List ADR decision records, optionally filtered by project |
| `update_goals` | Update `memory/goals.md` |
| `sync_index` | Rebuild DuckDB index from vault files |
| `index_stats` | Show note counts by type |
| `vault_sleep` | Compress old low-activity notes (dry_run by default) |
| `sleep_status` | Show compression candidates without acting |
| `snapshot_note_tool` | Render note to PNG at chosen resolution tier |
| `extract_rules_tool` | Extract L3 rules from frequently-accessed notes |
| `consolidate_tool` | Merge semantically similar notes into one |
| `update_links_tool` | Refresh auto-generated wikilinks |
| `prune_archive_tool` | Delete archived originals that have a PNG snapshot |

---

## Test Results

```
tests/test_figures.py     ···················   19 passed
tests/test_server.py      ·············         13 passed
tests/test_vault_db.py    ·······················
                          ········               33 passed
tests/test_vault_sleep.py ···················
                          ·····················
                          ··········            50 passed
──────────────────────────────────────────────────────
115 passed in 3.37s
```

```mermaid
xychart-beta
    title "Test Coverage by Phase"
    x-axis ["FTS Index", "Ebbinghaus", "Vault Sleep", "PNG Snap", "Archive", "Semantic", "Auto-Link", "L3 Rules", "Consolidate", "Adaptive Tier"]
    y-axis "Tests passing (%)" 0 --> 110
    bar [100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
```

---

## Installation

### Prerequisites

| Dependency | Required | Notes |
| --------- | ------- | ----- |
| Python 3.11+ | ✅ | |
| [uv](https://docs.astral.sh/uv/) | ✅ | Package manager |
| [Playwright](https://playwright.dev/) | ✅ | PNG snapshot rendering |
| [llama-server](https://github.com/ggerganov/llama.cpp) | Optional | Semantic search; BM25 fallback if absent |
| [nomic-embed-text-v1.5.Q8_0.gguf](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF) | Optional | Embedding model (~300 MB) |
| Gemini CLI or `ANTHROPIC_API_KEY` | Optional | Better LLM compression; naive fallback if absent |

### Quick Start

```bash
# 1. Clone
git clone https://github.com/yourname/second-brain-mcp
cd second-brain-mcp

# 2. Install
uv sync
uv run playwright install chromium

# 3. Create vault structure
mkdir -p ~/second-brain/{00-inbox,10-projects,20-areas,30-resources,40-archive,decisions,memory,templates}

# 4. Configure MCP
cp mcp_config.example.json mcp_config.json
# Edit: set SECOND_BRAIN_PATH to your vault path

# 5. Register with Claude Code
claude mcp add --scope user second-brain \
  uv run python $(pwd)/server.py

# 6. Index your vault
# In Claude Code: tell the agent to run sync_index
```

### Environment Variables

| Variable | Default | Description |
| ------- | ------- | ----------- |
| `SECOND_BRAIN_PATH` | `~/second-brain` | Path to your vault |
| `EMBED_URL` | `http://localhost:11435/v1/embeddings` | Embedding endpoint |
| `EMBED_MODEL` | `nomic-embed-text` | Model name |
| `EMBED_PORT` | `11435` | llama-server port |

### Auto-start (macOS)

```bash
# Embedding server (always-on)
cp examples/launchd/com.yourname.llama-embed.plist ~/Library/LaunchAgents/
# edit paths, then:
launchctl load ~/Library/LaunchAgents/com.yourname.llama-embed.plist

# Weekly vault maintenance (Sunday 02:00)
cp examples/launchd/com.yourname.vault-sleep.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.yourname.vault-sleep.plist
```

---

## Vault Structure

```text
vault/
├── 00-inbox/          # Unprocessed captures
├── 10-projects/       # Active projects
├── 20-areas/
│   ├── research/      # Ongoing research domains
│   ├── coding/        # Dev tools and workflows
│   └── consolidated/  # Auto-merged similar notes
├── 30-resources/      # ← Papers and articles live here (save_article target)
├── 40-archive/        # Compressed originals (auto-managed)
├── decisions/         # Architecture Decision Records (ADR format)
├── memory/
│   ├── goals.md       # Current priorities — injected every session
│   ├── index.md       # Vault map
│   └── rules.md       # Auto-extracted L3 rules — injected every session
└── templates/
```

---

## Running Tests

```bash
uv run pytest tests/ -v
uv run python benchmark.py --quick --markdown   # search latency + accuracy
```

---

## License

MIT
