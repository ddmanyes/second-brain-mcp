<!-- mcp-name: io.github.ddmanyes/mcp-second-brain -->

# second-brain MCP Server

**A self-maintaining personal knowledge base for AI agents — a plain-Markdown vault, powered by MCP.**

[![CI](https://github.com/ddmanyes/second-brain-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ddmanyes/second-brain-mcp/actions/workflows/ci.yml)
[![Python ≥ 3.11](https://img.shields.io/badge/Python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

📖 English · [繁體中文](README.zh.md)

---

A local knowledge base your AI agent can read, write, and **maintain on its own**. Save a paper or note with one command — second-brain converts it to Markdown, OCRs every figure, embeds it for semantic search, and auto-links it to related notes. Notes you stop reading compress themselves over time, so recall stays cheap as the vault grows.

Everything is plain Markdown — sync via Google Drive / iCloud / git, switch agents anytime, **zero lock-in**.

## Highlights

- **One command saves anything** — `save_article(url_or_pdf)` fetches, converts to Markdown, OCRs figures (Claude Vision), embeds, and auto-links.
- **Figure-level search** — `search_figures("UMAP melanocyte")` returns the exact panel across your whole library.
- **Self-organizing** — new notes auto-link to related ones; frequently-read notes extract reusable rules.
- **Memory that forgets like a brain** — Ebbinghaus ranking; stale notes auto-compress (60–90% fewer tokens).
- **Read-only housekeeping audit** — inspect article metadata, links, exact duplicate candidates, inbox age, and source freshness without changing the vault.
- **Session continuity** — `get_context()` reloads goals + top notes + rules at the start of every session.
- **Pluggable backend** — DuckDB (default, offline) or Postgres + pgvector (central, multi-machine). Self-hosted embeddings optional; BM25 fallback when offline.

## Quick Start (Claude Code)

```bash
pip install mcp-second-brain
playwright install chromium

claude mcp add --scope user second-brain \
  --env SECOND_BRAIN_PATH=~/second-brain \
  -- python -m mcp_second_brain
```

The vault directory and templates are created on first run. Then tell your agent `init_vault` to verify.

> ⚠️ PyPI currently lags the source tree. For the newest build — plus Claude Desktop, Windows, and multi-machine / central-server setups — see **[NEW_MACHINE_SETUP.md](NEW_MACHINE_SETUP.md)**.

## Core Tools

| Tool | What it does |
| :--- | :--- |
| `get_context` | Session start — goals + top-ranked notes + auto-rules |
| `save_article` | URL / PDF → Markdown + figures + embeddings |
| `search_notes` / `search_figures` | Hybrid BM25 + semantic search (note text / figure content) |
| `audit_article_records` | Bounded, read-only article housekeeping and social-source freshness report |
| `new_note` / `update_note` / `append_to_note` | Create & edit notes (auto-filed, auto-indexed, auto-linked) |
| `vault_sleep` | Compress old, low-activity notes |
| `get_agent_instructions` | Serve the full filing SOP (AGENTS.md) to remote agents |

Full tool reference (40 tools) lives in **[AGENTS.md](AGENTS.md)**.

Use `search_notes` when you need content, `health_check` when the server or index may be
unhealthy, and `audit_article_records` when you need a housekeeping report. Audit results
never merge, archive, or delete notes automatically.

## How It Works

```text
Any source (paper · PDF · web · note)
        │   save_article · new_note
        ▼
Markdown vault  ──►  index  (DuckDB, or Postgres + pgvector)
  00-inbox/            • BM25 + semantic search
  10-projects/         • figure OCR + vision descriptions
  20-areas/            • auto-wikilinks between related notes
  30-resources/        • Ebbinghaus ranking → weekly auto-compression
  decisions/ memory/
        │
        ▼
Your AI agent queries it — search_notes · search_figures · get_context
```

The **vault is the source of truth**; the index is rebuildable anytime (`sync_index`). Filing conventions live in one operating manual — [AGENTS.md](AGENTS.md) — served to any agent via `get_agent_instructions()`, so every agent files things the same way without being re-taught.

## Vault Structure

```text
vault/
├── 00-inbox/       Unprocessed captures
├── 10-projects/    Active projects
├── 20-areas/       Ongoing research / coding domains
├── 30-resources/   Papers & articles (save_article writes here)
├── 40-archive/     Auto-compressed originals
├── decisions/      Architecture Decision Records
├── memory/         goals.md · rules.md  (injected every session)
└── templates/      Note templates
```

## Documentation

- **[AGENTS.md](AGENTS.md)** — filing SOP, naming conventions, full tool reference (single source of truth)
- **[NEW_MACHINE_SETUP.md](NEW_MACHINE_SETUP.md)** — source install, self-hosting, multi-machine central server, API keys
- **[CONTEXT.md](CONTEXT.md)** — domain model / ubiquitous language

## Design Notes

Inspired by biological memory: the Ebbinghaus forgetting curve (`access_count / ln(age_days)`) for ranking, and sleep-dependent consolidation (weekly LLM compression of low-access notes). Built with [MarkItDown](https://github.com/microsoft/markitdown) · [DuckDB](https://duckdb.org) · [pgvector](https://github.com/pgvector/pgvector) · [FastMCP](https://github.com/jlowin/fastmcp) · [Playwright](https://playwright.dev) · [Claude API](https://docs.anthropic.com).

## License

MIT © 2026 Chan Chi Ru. See [LICENSE](LICENSE).
