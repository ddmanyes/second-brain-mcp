# second-brain MCP Server

> A personal knowledge vault that thinks like your brain sleeps — compressing old memories, surfacing relevant ones, and forgetting gracefully.

![Tests](https://img.shields.io/badge/tests-115%20passed-brightgreen)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## What Is This?

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that turns a folder of Markdown files into a searchable, self-maintaining second brain — usable by **Claude Code**, **Gemini CLI**, or any MCP-compatible agent.

Unlike most AI memory systems that just "remember what you said," this one models how biological memory actually works:

| Biological Brain | This System |
|-----------------|-------------|
| Hippocampal consolidation during sleep | Vault Sleep: weekly auto-compression of old notes |
| Ebbinghaus forgetting curve | Score-based context ranking (`access_count / ln(age)`) |
| Visual long-term memory | PNG snapshots (80–92% token reduction) |
| Associative recall | Semantic search + auto-generated wikilinks |
| Sleep-dependent memory consolidation | launchd cron, runs while you sleep |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    AI Agent Layer                    │
│         Claude Code · Gemini CLI · Any MCP           │
└──────────────────────┬──────────────────────────────┘
                       │ MCP Protocol (15 tools)
┌──────────────────────▼──────────────────────────────┐
│               Layer 2 — MCP Server                   │
│                    server.py                         │
│   get_context · search_notes · new_note · …          │
└──────┬───────────────┬────────────────┬─────────────┘
       │               │                │
┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
│  Layer 1    │ │  vault_db   │ │  figures    │
│  vault_sleep│ │  DuckDB FTS │ │  PNG snap   │
│  compress   │ │  + semantic │ │  OCR · VLM  │
└──────┬──────┘ └──────┬──────┘ └─────────────┘
       │               │
┌──────▼───────────────▼──────────────────────────────┐
│               Layer 0 — Markdown Vault               │
│   00-inbox · 10-projects · 20-areas · 30-resources   │
│   40-archive · decisions · memory · templates        │
│              (syncs via Google Drive / iCloud)        │
└─────────────────────────────────────────────────────┘
```

---

## Vault Sleep Flow

```
Every Sunday 02:00 (launchd)
        │
        ▼
┌───────────────┐    age > 90d         ┌──────────────────────────────────┐
│  sync_index   │    score ≤ 0.5       │       Compression Tier           │
│  + embeddings │──── candidates ────▶ │  score > 1.5 → text (keep full) │
└───────────────┘                      │  score > 0.8 → large  (~400 tok) │
                                       │  score > 0.3 → base   (~256 tok) │
                                       │  otherwise   → small  (~100 tok) │
                                       └──────────────┬───────────────────┘
                                                      │
                                       ┌──────────────▼───────────────────┐
                                       │  Gemini CLI → Claude CLI → naive │
                                       │  (compression, auto-fallback)    │
                                       └──────────────┬───────────────────┘
                                                      │
                                         ┌────────────▼──────────┐
                                         │  compressed → vault   │
                                         │  original  → archive  │
                                         │  snapshot  → .png     │
                                         └───────────────────────┘
```

---

## MCP Tools (15 total)

| Tool | Description |
|------|-------------|
| `get_context` | Session start: goals + top-20 notes by Ebbinghaus score + rules |
| `new_note` | Create note with correct template/folder by type |
| `search_notes` | Hybrid BM25 + semantic search |
| `read_note` | Read note + record access (updates Ebbinghaus score) |
| `read_note_as_image` | Return PNG snapshot for token-efficient reading |
| `save_article` | Fetch URL/PDF → Markdown → auto-extract figures |
| `get_decisions` | List ADR decision records |
| `update_goals` | Update `memory/goals.md` |
| `sync_index` | Rebuild DuckDB index from vault files |
| `index_stats` | Show note counts by type |
| `vault_sleep` | Compress old low-activity notes |
| `sleep_status` | Show compression candidates without acting |
| `snapshot_note_tool` | Render note to PNG at chosen resolution tier |
| `extract_figures_for` | Run figure extraction on a saved article |
| `search_figures` | Search figure OCR text / descriptions |
| `extract_rules_tool` | Extract L3 rules from frequently-accessed notes |
| `consolidate_tool` | Merge semantically similar notes into one |
| `update_links_tool` | Refresh auto-generated wikilinks |
| `prune_archive_tool` | Delete archived originals that have a snapshot |

---

## Test Results

### Suite Summary

```
tests/test_figures.py    ···················   19 passed
tests/test_server.py     ·············         13 passed
tests/test_vault_db.py   ·······················
                         ········               33 passed
tests/test_vault_sleep.py ···················
                           ·····················
                           ··········            50 passed
─────────────────────────────────────────────
115 passed in 3.37s
```

### Coverage by Phase

```
Phase 1 — DuckDB FTS indexing          ████████████  100%
Phase 2 — Ebbinghaus score ranking     ████████████  100%
Phase 3 — Vault Sleep compression      ████████████  100%
Phase 4 — PNG snapshot + VLM           ████████████  100%
Phase 5 — Archive prune                ████████████  100%
Phase 6 — Hybrid semantic search       ████████████  100%
Phase 6b— Auto-link (wikilinks)        ████████████  100%
Phase 7 — L3 rules extraction          ████████████  100%
Phase 8 — Cross-note consolidation     ████████████  100%
Phase 9 — Adaptive tier selection      ████████████  100%
Embedding 500-retry logic              ████████████  100%
─────────────────────────────────────────────────────
Total                                  115 / 115
```

### Search Benchmark (20-rep average, BM25-only mode)

> Measured on Apple Silicon MacBook. Hybrid mode adds ~20ms for embedding lookup when `llama-server` is running.

| Vault Size | BM25 p50 | BM25 p95 | Hybrid p50 | Recall@1 | Recall@5 | MRR |
|:----------:|:--------:|:--------:|:----------:|:--------:|:--------:|:---:|
| 10 notes  | 21 ms | 24 ms | 37 ms | 30% | 60% | 0.42 |
| 50 notes  | 25 ms | 29 ms | 39 ms | 70% | 90% | 0.78 |
| 100 notes | 27 ms | 31 ms | 45 ms | 70% | 80% | 0.73 |

> Recall improves significantly with more notes because more real vault content (with known ground truth) is available for matching.

### Token Reduction by Snapshot Tier

```
Original note (full text)  ████████████████████████████████  ~1000 tokens
                                              (baseline)

large tier  (age 90–180d)  ████████████████                   ~400 tokens  ▼ 60%
base  tier  (age 180–365d) ████████                           ~256 tokens  ▼ 74%
small tier  (age 365d+)    ████                               ~100 tokens  ▼ 90%
```

---

## Installation

### Prerequisites

| Dependency | Required | Notes |
|-----------|---------|-------|
| Python 3.11+ | ✅ | |
| [uv](https://docs.astral.sh/uv/) | ✅ | Package manager |
| [Playwright](https://playwright.dev/) | ✅ | PNG snapshot rendering |
| [llama-server](https://github.com/ggerganov/llama.cpp) | Optional | Semantic search (BM25 fallback if absent) |
| [nomic-embed-text-v1.5.Q8_0.gguf](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF) | Optional | Embedding model |
| Gemini CLI | Optional | Better compression quality (naive fallback if absent) |

### Quick Start

```bash
# 1. Clone
git clone https://github.com/yourname/second-brain-mcp
cd second-brain-mcp

# 2. Install dependencies
uv sync
uv run playwright install chromium

# 3. Create your vault
mkdir -p ~/second-brain/{00-inbox,10-projects,20-areas,30-resources,40-archive,decisions,memory,templates}

# 4. Configure MCP
cp mcp_config.example.json mcp_config.json
# Edit mcp_config.json — set SECOND_BRAIN_PATH to your vault

# 5. Register with Claude Code
claude mcp add --scope user second-brain \
  uv run python $(pwd)/server.py

# 6. Build the index
# In Claude Code: "run sync_index"
```

### Environment Variables

| Variable | Default | Description |
|---------|---------|-------------|
| `SECOND_BRAIN_PATH` | `~/second-brain` | Path to your vault directory |
| `EMBED_URL` | `http://localhost:11435/v1/embeddings` | Embedding server endpoint |
| `EMBED_MODEL` | `nomic-embed-text` | Embedding model name |
| `EMBED_PORT` | `11435` | llama-server port |

### Auto-start (macOS, optional)

```bash
# Embedding server — always on
cp examples/launchd/com.yourname.llama-embed.plist ~/Library/LaunchAgents/
# Edit paths, then:
launchctl load ~/Library/LaunchAgents/com.yourname.llama-embed.plist

# Weekly vault maintenance — every Sunday 02:00
cp examples/launchd/com.yourname.vault-sleep.plist ~/Library/LaunchAgents/
# Edit paths, then:
launchctl load ~/Library/LaunchAgents/com.yourname.vault-sleep.plist
```

---

## Vault Structure

```
vault/
├── 00-inbox/          # Unprocessed captures — clear daily
├── 10-projects/       # Active projects
├── 20-areas/
│   ├── research/      # Ongoing research domains
│   ├── coding/        # Dev tools, patterns, workflows
│   └── consolidated/  # Auto-merged similar notes (Phase 8)
├── 30-resources/      # Papers, articles (save_article target)
├── 40-archive/        # Compressed originals (auto-managed)
├── decisions/         # Architecture Decision Records (ADR)
├── memory/
│   ├── goals.md       # Current priorities (injected every session)
│   ├── index.md       # Vault map
│   └── rules.md       # Auto-extracted L3 rules (injected every session)
└── templates/
    ├── note-template.md
    ├── decision-template.md
    ├── project-template.md
    └── research-note-template.md
```

---

## Running Tests

```bash
uv run pytest tests/ -v
```

---

## How It Compares

| Feature | This Project | Mem0 | MemGPT | Obsidian + AI |
|---------|:-----------:|:----:|:------:|:-------------:|
| Pure Markdown (portable) | ✅ | ❌ | ❌ | ✅ |
| Ebbinghaus forgetting curve | ✅ | ❌ | ❌ | ❌ |
| Auto-compression (sleep) | ✅ | ❌ | Partial | ❌ |
| Visual memory (PNG tiers) | ✅ | ❌ | ❌ | ❌ |
| Figure OCR + search | ✅ | ❌ | ❌ | ❌ |
| Agent-agnostic (MCP) | ✅ | ❌ | ❌ | Partial |
| No vendor lock-in | ✅ | ❌ | ❌ | ✅ |
| Self-hosted | ✅ | ❌ | ✅ | ✅ |

---

## License

MIT
