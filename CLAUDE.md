# Second Brain MCP Server

> **本地 Claude Code**：操作 SOP 見 [AGENTS.md](AGENTS.md)，vault 目錄結構與工具清單在同一文件。
> **遠端 MCP 連入**：呼叫 `get_agent_instructions()` 工具可取得 AGENTS.md 完整內容。
> **CLAUDE.md 位置**：`second-brain/CLAUDE.md`（由 Claude Code 在此目錄啟動時自動載入）。

## 執行

`uv run --with "mcp[cli]" --with "markitdown[all]" python server.py`

## 安全

- `read_note` 必須用 `.resolve().is_relative_to(VAULT)` 防路徑遍歷
- YAML frontmatter 的 title/source 用 `json.dumps(value.strip())[1:-1]` 做正確 escaping（不是 `.replace('"', "'")`）
- `save_article` 的 source 必須過 `_validate_source()` — 只允許 http/https（SSRF 過濾）或白名單副檔名的本地檔案
- 圖片下載前必須過 `_is_ssrf_safe()` — 封鎖 loopback / RFC-1918 / 169.254

## Vault 路徑

由環境變數 `SECOND_BRAIN_PATH` 控制，預設 `~/second-brain`
