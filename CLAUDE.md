# Second Brain MCP Server

> **本地 Claude Code**：操作 SOP 見 [AGENTS.md](AGENTS.md)，vault 目錄結構與工具清單在同一文件。
> **遠端 MCP 連入**：呼叫 `get_agent_instructions()` 工具可取得 AGENTS.md 完整內容。
> **CLAUDE.md 位置**：`second-brain/CLAUDE.md`（由 Claude Code 在此目錄啟動時自動載入）。

## 執行（開發）

`uv run --with "mcp[cli]" --with "markitdown[all]" python -m mcp_second_brain`

## 部署（改完碼要做的事）

⚠️ **線上服務跑的是「安裝好的 wheel」，不是這個 checkout**（`lcdda-start-remote.sh` 明寫
"The live service imports the installed wheel"）。**只改 repo + 重啟服務不會生效**：

```bash
~/.venvs/second-brain/bin/pip install --force-reinstall --no-deps ~/git-repos/second-brain
launchctl kickstart -k gui/501/com.user.second-brain-remote
launchctl kickstart -k gui/501/com.user.lcdda-remote
```

- 三個 server（sb `:9100` / lcdda `:9104` / lcdda-harvest `:9106`）**共用同一個 venv**，
  一次 `pip install` 全部改到——要嘛全部重啟，要嘛新舊碼並存。
- **`AGENTS.md` 也是 wheel 的一部分**（`pyproject.toml` 的 `force-include`），所以改手冊
  同樣要重新打包才會送到遠端 agent。`tests/test_wheel_packaging.py` 會驗產物內容。
- 這個落差曾讓 `get_agent_instructions()` 靜默回傳 placeholder 數個月都沒被發現，
  見 CHANGELOG「wheel never packaged AGENTS.md」。

## 架構（中央活腦）

- **Index backend 可插拔**：`store/`，由 `SB_DB_BACKEND` 選 `postgres`（中央，pgvector+pg_trgm，多機並發）或 `duckdb`（預設/離線 fallback）。markdown 是正本，index 可由 `sync_all` 重建。
- **正式拓樸**：單一中央 HTTP server（launchd `com.user.second-brain-remote`，`streamable-http` 綁 Tailscale IP `:9100`，`SB_DB_BACKEND=postgres`）+ Docker Postgres（`sb-pg`，僅 `127.0.0.1:5432`）。
- **連線**：client 走 `http://<tailscale-ip>:9100/mcp`，帶 header `X-API-Key`（server 設 `SB_API_KEY`/`SB_API_KEYS` 時強制）。Postgres 永不對外，只 server 同機 localhost 連。
- **排程**：`second-brain-pg-sync`（`StartCalendarInterval` 每逢 `:00`、`:30` incremental，防脫節）、`second-brain-pg-backup`（每日 pg_dump）。
- **寫入紀律**：所有寫入經中央 server（單一寫者）；client 端 Obsidian 唯讀，避免 Drive 衝突副本。
- **測試**：Postgres 測試對 `sb_test`（fixture 會 `DELETE` 全表，**絕不可**指向 `sb_personal`/`sb_lab`，`test_postgres_store.py` 有硬 guard）。
- 詳見 [MIGRATION_PLAN_POSTGRES.md](MIGRATION_PLAN_POSTGRES.md) 與 [AGENTS.md](AGENTS.md) 的 Connection Topology 段。

## 安全

- **呼叫端給的路徑一律走 `_vault_path()`（→ `vault_paths.resolve_in_vault`）**，不要手抄 `.resolve().is_relative_to(VAULT)`。手抄過一次就會漏第二次（2026-07-31 修掉的 `enrich_neighbor_keywords_tool` 即是）。
- **會寫入的 tool 一律掛 `@write_tool(...)`，admin tool 掛 `@admin_tool(...)`**，不要在 body 內手抄權限檢查或 `_log_write`。裝飾器同時登記進 `WRITE_TOOLS`，`tests/test_permissions.py` 會列舉它並實際驅動每個 tool。
- YAML frontmatter 的 title/source 用 `json.dumps(value.strip())[1:-1]` 做正確 escaping（不是 `.replace('"', "'")`）
- `save_article` 的 source 必須過 `_validate_source()` — 只允許 http/https（SSRF 過濾）或白名單副檔名的本地檔案
- 圖片下載前必須過 `_is_ssrf_safe()` — 封鎖 loopback / RFC-1918 / 169.254

## Vault 路徑

由環境變數 `SECOND_BRAIN_PATH` 控制，預設 `~/second-brain`
