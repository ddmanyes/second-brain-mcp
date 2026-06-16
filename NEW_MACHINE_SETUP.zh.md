# 在新電腦部署 second-brain（Drive 原始碼版）

> 適用情境：把 second-brain 跨自己的多台機器跑，**直接用 Google Drive 同步的原始碼**
> （不是 `pip install mcp-second-brain`——那是給公開使用者）。
>
> 公開使用者安裝走 [README.md](README.md)；你自己的多機設定走這份。
>
> **架構參考**：[AGENTS.md](AGENTS.md) 的「Connection Topology & Write Discipline」段；
> 完整計畫見 [MIGRATION_PLAN_POSTGRES.md](MIGRATION_PLAN_POSTGRES.md)。

---

## 心智模型：一個中央活腦，多個輕量 client

現在是**一個中央 HTTP server + Postgres**，不是每台各跑 local server。

```text
            Postgres (sb-pg, Docker)        ← 只綁 127.0.0.1:5432，絕不對外
                  ▲ localhost
   中央 host ─────┤ MCP server :9100（streamable-http，綁 Tailscale IP）
   （always-on）   │   SB_DB_BACKEND=postgres、SB_API_KEY=…
                  │   + pg-sync（每 30 分）+ pg-backup（每日）
                  ▼ Tailscale
   client Mac ─────── 連 http://<tailscale-ip>:9100/mcp + X-API-Key header
                      （免 venv、免 DuckDB、免 sync — 只要 MCP 設定）
```

| 元件 | 位置 | 說明 |
| --- | --- | --- |
| Vault 筆記（markdown） | **Google Drive 同步** | 正本（L1）。**只有中央 host** 寫入。 |
| 程式碼 `mcp_second_brain/` | **Google Drive 同步** | 改一次全機同步。 |
| Postgres 索引（L2） | **僅中央 host**，Docker volume 在本機 SSD | 可由 `sync_all` 從 markdown 重建；**絕不**放 Drive。 |
| 中央 MCP server | **中央 host**，launchd `com.user.second-brain-remote` | streamable-http 綁 Tailscale IP `:9100`，單例。 |
| client 機器 | 走 HTTP 連入 | **只需要** 指向中央 server 的 MCP 設定 + API key。 |

> **寫入紀律**：所有寫入經中央 server（單一寫者 → 不產生 Google Drive 衝突副本）。
> client 端 Obsidian **唯讀** — 讀同步來的 markdown 可以，但要改就用 MCP 工具，別在筆電 Obsidian 直接打字改。

---

## A. Client 機器設定（最常見，約 2 分鐘）

只是要「用」這個腦的新 Mac。**免 Python、免 venv、免 DuckDB、免建索引。**

跟中央 host 拿 API key（存在 `~/Library/LaunchAgents/com.user.second-brain-remote.plist` 的
`SB_API_KEY`，以及現有 client 的設定裡）。

**Claude Code（CLI）：**

```bash
claude mcp add --scope user --transport http second-brain \
  "http://100.81.161.16:9100/mcp" \
  --header "X-API-Key: <金鑰>"
```

**Claude 桌面版** — 編輯 `~/Library/Application Support/Claude/claude_desktop_config.json`
（桌面版透過 `mcp-remote` 代理連 HTTP server）：

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://100.81.161.16:9100/mcp", "--header", "X-API-Key: <金鑰>"]
    }
  }
}
```

⌘Q 重開桌面版即可——這台 client 就能透過 Tailscale 讀寫中央活腦。

> 前提：client 在同一個 Tailscale tailnet（才連得到 `100.81.161.16`）。沒帶有效 `X-API-Key` 會回 `401`。

---

## B. 中央 host 設定（一次性，擁有資料的那台）

只有**一台**扮演此角色（你的 always-on 個人主機）。先設每台不同的路徑：

```bash
PJ="$HOME/Library/CloudStorage/GoogleDrive-<你的帳號>/我的雲端硬碟/PJ_save"
SB="$PJ/mcp-tools/second-brain"
VAULT="$PJ/second-brain"
```

### B1 — Postgres + pgvector（Docker）

```bash
docker run -d --name sb-pg \
  -e POSTGRES_PASSWORD=<pw> \
  -p 127.0.0.1:5432:5432 \
  -v "$HOME/sb-pgdata:/var/lib/postgresql/data" \
  pgvector/pgvector:pg16
docker exec sb-pg psql -U postgres -c "CREATE DATABASE sb_personal;"
docker exec -i sb-pg psql -U postgres -d sb_personal < "$SB/mcp_second_brain/store/postgres_schema.sql"
```

- ⚠️ **`-p` 必須是 `127.0.0.1:5432:5432`**（只綁 localhost）。`5432:5432` 會對所有介面開放。
- ⚠️ **data dir 在本機 SSD，絕不放 Google Drive**（Drive 同步會毀掉 Postgres data dir）。
- 驗證：`docker exec sb-pg psql -U postgres -d sb_personal -c "SELECT extname FROM pg_extension;"` 應有 `vector` + `pg_trgm`。

### B2 — venv + 依賴（本機，**不要建在 Drive 目錄裡**）

```bash
python3 -m venv ~/.venvs/second-brain
~/.venvs/second-brain/bin/pip install -r "$SB/requirements.txt"   # 含 psycopg[binary,pool]
~/.venvs/second-brain/bin/playwright install chromium             # PNG 快照渲染用
```

> **PDF 轉換依賴** — `save_article` 三層管線：
> 1. **Marker**（ML，品質最佳）— 來自 `requirements.txt`；~1.35 GB 模型首次存 PDF 時自動下載到 `~/.cache/datalab/`。
> 2. **pdftotext / pdfinfo**（fallback）— `brew install poppler`。
> 3. **MarkItDown**（最後 fallback）— 已在 requirements。

### B3 — Embedding server

`nomic-embed-text` 經 llama-server 跑 `:11435`（server 偵測到會自動啟動），或 Ollama：
`ollama pull nomic-embed-text` 並設 `EMBED_URL`/`EMBED_PORT`。embedding 為 768 維。

### B4 — 中央 HTTP server（launchd，KeepAlive）

launchd job `com.user.second-brain-remote` 把 server 綁到 Tailscale IP，環境：

```text
SB_DB_BACKEND=postgres
SB_PG_DSN=postgresql://postgres:<pw>@localhost:5432/sb_personal
SB_API_KEY=<產生一把強金鑰>          # client 要以 X-API-Key 帶上
SECOND_BRAIN_PATH=<VAULT>
```

start script 綁 `--host <tailscale-ip> --port 9100 --transport streamable-http`。改完 plist 的
`EnvironmentVariables` 後要**完整重載**（env 變更不能只 `kickstart`）：

```bash
launchctl bootout  gui/$(id -u)/com.user.second-brain-remote
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.second-brain-remote.plist
```

log（`/tmp/second-brain-remote.log`）應出現 `API-key auth ENABLED`。

### B5 — 首次建索引

```bash
SB_DB_BACKEND=postgres SB_PG_DSN=… SECOND_BRAIN_PATH="$VAULT" \
  PYTHONPATH="$SB" ~/.venvs/second-brain/bin/python -c \
  "from mcp_second_brain.store import get_store; from pathlib import Path; import os; \
   print(get_store().sync_all(Path(os.environ['SECOND_BRAIN_PATH'])))"
```

預期 `{'synced': N, 'embed_failed': 0}`。（~900 篇 ≈ 35 秒，embedding 修正後很快。）

### B6 — 維護排程（launchd）

| Job | 頻率 | 用途 |
| --- | --- | --- |
| `com.user.second-brain-pg-sync` | 每 30 分 | 對 Postgres 跑 `sync_incremental`，撿回 cron 改的 markdown，防索引脫節（`launchd/run_pg_sync.py`）。 |
| `com.user.second-brain-pg-backup` | 每日 04:00 | `pg_dump \| gzip` `sb_personal`/`sb_lab` 到 `PJ_save/backups/`，保留近 7 份（`launchd/run_pg_backup.sh`）。 |
| `com.user.vault-janitor` / `com.user.vault-sleep` | 每週 | vault 清理 + Ebbinghaus 壓縮。⚠️ 仍只寫 DuckDB、繞過 store 抽象層；其 markdown 變更由 `pg-sync` 撿進 Postgres。 |

---

## C. 離線 fallback（無網路 / Tailscale 斷線）

保留 DuckDB backend 供離線唯讀。完全沒連線時，跑**本機 stdio** server 並設
`SB_DB_BACKEND=duckdb`（從同步的 markdown 經 `sync_index` 重建 `~/.second-brain/vault.db`）。
回線後對 Postgres 再跑一次 `sync_all` 對帳。這是刻意的降級模式，非常態。

---

## 每台機器資料總覽

| 資料 | 位置 | 共享？ |
| --- | --- | :---: |
| Vault markdown 筆記 | Google Drive | ✅ 所有機器 |
| 程式碼（`mcp_second_brain/`） | Google Drive | ✅ 所有機器 |
| Postgres 索引 | 中央 host（Docker，本機 SSD） | ❌ 僅中央 — 唯一的線上索引 |
| Python venv / Docker / embedding | 中央 host | ❌ 僅 host（client 都不用） |
| MCP 設定 + API key | 各 client | ❌ 每台各自 |

---

## 疑難排解

| 症狀 | 原因 | 解法 |
| --- | --- | --- |
| Client `401 unauthorized` | 缺 / 錯 `X-API-Key` | 補 `--header "X-API-Key: <金鑰>"`（Code）或 `mcp-remote` 的 `--header` 參數（桌面版）。 |
| Client 完全連不上 | 不在 tailnet，或中央 server / Tailscale 掛了 | `tailscale status`；在 host 查 `pgrep -f streamable-http` 與 `/tmp/second-brain-remote.log`。 |
| server log 設了金鑰卻顯示 `API-key auth DISABLED` | `kickstart` 不會重載 plist env | 改用 `launchctl bootout` + `bootstrap`（見 B4）。 |
| host 設定後查詢空結果 | 索引未建 | 跑 B5 的 `sync_all`。 |
| `sync_all` 龜速 / embed server 一堆 HTTP 500 | embed 截斷未對齊 token batch（已修） | 確認跑的是最新 Drive 原始碼（`_call_embed_api` 有 256 字 tier、deterministic 500 不退避）。 |
| Postgres 索引與 markdown 脫節 | `pg-sync` job 沒載入 | `launchctl list \| grep pg-sync`；bootstrap `com.user.second-brain-pg-sync`。 |
| 跑 pytest 清空了正式索引 | 舊測試預設 DSN 指向 `sb_personal`（已修） | 測試現預設 `sb_test` 且有硬 guard；`SB_PG_TEST_DSN` 絕不可指向 `sb_personal`/`sb_lab`。 |
| 快照 `read_note_as_image` 失敗（host） | playwright chromium 沒裝 | `~/.venvs/second-brain/bin/playwright install chromium`。 |
| 桌面版 `Operation not permitted`（若跑本機 stdio host） | `command` 指向 Drive 內 `.venv/bin/python` | 改成本機 `~/.venvs/second-brain/bin/python`。 |
