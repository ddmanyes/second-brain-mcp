<!-- mcp-name: io.github.ddmanyes/mcp-second-brain -->

# second-brain MCP Server

**給 AI agent 用的自維護個人知識庫——純 Markdown vault，以 MCP 驅動。**

[![CI](https://github.com/ddmanyes/second-brain-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ddmanyes/second-brain-mcp/actions/workflows/ci.yml)
[![Python ≥ 3.11](https://img.shields.io/badge/Python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

[English](README.md) · 📖 繁體中文

---

一個你的 AI agent 能讀、能寫、還能**自己維護**的本地知識庫。一行指令存下一篇論文或筆記——second-brain 會把它轉成 Markdown、對每張圖做 OCR、算語義向量以供搜尋，並自動連結到相關筆記。你不再讀的舊筆記會隨時間**自動壓縮**，所以 vault 再大，回想成本依然低。

全部都是純 Markdown——用 Google Drive / iCloud / git 同步、隨時換 agent、**零綁定**。

## 亮點

- **一行存下任何東西**——`save_article(url_或_pdf)` 抓取、轉 Markdown、對圖做 OCR（Claude Vision）、算向量、自動連結。
- **圖級搜尋**——`search_figures("UMAP melanocyte")` 跨整個文獻庫回傳「那一張」panel。
- **自我組織**——新筆記自動連到相關筆記；常讀的筆記自動萃取可重用規則。
- **像大腦一樣會遺忘**——Ebbinghaus 排序；冷門筆記自動壓縮（省 60–90% token）。
- **唯讀文章整理稽核**——檢查文章 metadata、連結、精確重複候選、inbox 時效與來源新鮮度，不改動 vault。
- **跨 session 連續性**——每個 session 開頭 `get_context()` 自動載入 goals + 熱門筆記 + rules。
- **可插拔後端**——DuckDB（預設、離線）或 Postgres + pgvector（中央、多機）。自架 embedding 選用；離線時 BM25 fallback。

## 快速開始（Claude Code）

```bash
pip install mcp-second-brain
playwright install chromium

claude mcp add --scope user second-brain \
  --env SECOND_BRAIN_PATH=~/second-brain \
  -- python -m mcp_second_brain
```

vault 目錄與模板會在首次啟動時自動建立。之後叫 agent 執行 `init_vault` 驗證即可。

> ⚠️ PyPI 版本目前落後於原始碼。要最新版——以及 Claude Desktop、Windows、多機／中央 server 設定——請見 **[NEW_MACHINE_SETUP.md](NEW_MACHINE_SETUP.md)**。

## 核心工具

| 工具 | 用途 |
| :--- | :--- |
| `get_context` | Session 開頭——goals + 排序後的熱門筆記 + 自動 rules |
| `save_article` | URL / PDF → Markdown + 圖片 + 向量 |
| `search_notes` / `search_figures` | 混合 BM25 + 語義搜尋（筆記內文 / 圖片內容） |
| `audit_article_records` | 有界、唯讀的文章整理與社群來源新鮮度報告 |
| `new_note` / `update_note` / `append_to_note` | 建立與編輯筆記（自動歸檔、索引、連結） |
| `vault_sleep` | 壓縮老舊、低活躍度的筆記 |
| `get_agent_instructions` | 把完整歸檔 SOP（AGENTS.md）提供給遠端 agent |

完整工具清單（40 個）見 **[AGENTS.md](AGENTS.md)**。

需要內容時使用 `search_notes`；server 或索引可能異常時使用 `health_check`；
需要文章整理報告時使用 `audit_article_records`。稽核結果不會自動合併、歸檔或刪除筆記。

## 運作方式

```text
任何來源（論文 · PDF · 網頁 · 筆記）
        │   save_article · new_note
        ▼
Markdown vault  ──►  索引（DuckDB，或 Postgres + pgvector）
  00-inbox/            • BM25 + 語義搜尋
  10-projects/         • 圖片 OCR + 視覺描述
  20-areas/            • 相關筆記自動 wikilink
  30-resources/        • Ebbinghaus 排序 → 每週自動壓縮
  decisions/ memory/
        │
        ▼
你的 AI agent 查詢它——search_notes · search_figures · get_context
```

**vault 是正本**；索引隨時可重建（`sync_index`）。歸檔慣例集中在單一操作手冊 [AGENTS.md](AGENTS.md)，透過 `get_agent_instructions()` 提供給任何 agent——每個 agent 都照同一套規則歸檔，不必每次重教。

## Vault 結構

```text
vault/
├── 00-inbox/       未處理的捕捉
├── 10-projects/    進行中的專案
├── 20-areas/       持續的研究 / 開發領域
├── 30-resources/   論文與文章（save_article 寫這裡）
├── 40-archive/     自動壓縮的原文
├── decisions/      架構決策紀錄（ADR）
├── memory/         goals.md · rules.md（每個 session 注入）
└── templates/      筆記模板
```

## 文件

- **[AGENTS.md](AGENTS.md)**——歸檔 SOP、命名慣例、完整工具參考（單一真相來源）
- **[NEW_MACHINE_SETUP.md](NEW_MACHINE_SETUP.md)**——原始碼安裝、自架、多機中央 server、API key
- **[CONTEXT.md](CONTEXT.md)**——領域模型 / 統一語彙

## 設計理念

靈感來自生物記憶：以 Ebbinghaus 遺忘曲線（`access_count / ln(age_days)`）排序，以睡眠依賴的鞏固（每週用 LLM 壓縮低存取筆記）節省成本。技術棧：[MarkItDown](https://github.com/microsoft/markitdown) · [DuckDB](https://duckdb.org) · [pgvector](https://github.com/pgvector/pgvector) · [FastMCP](https://github.com/jlowin/fastmcp) · [Playwright](https://playwright.dev) · [Claude API](https://docs.anthropic.com)。

## 授權

MIT © 2026 Chan Chi Ru。見 [LICENSE](LICENSE)。
