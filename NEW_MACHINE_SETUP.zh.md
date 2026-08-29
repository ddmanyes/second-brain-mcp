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
   lab_center     │   SB_DB_BACKEND=postgres、SB_API_KEY=<key>…
   100.87.59.15   │   + pg-sync（每逢 :00／:30）+ pg-backup（每日）
                  ▼ Tailscale
   client Mac ─────── 連 http://100.87.59.15:9100/mcp + X-API-Key header
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

只是要「用」這個腦的新 Mac。**免 Python、免 venv、免 DuckDB、免建索引、免 llama-server、免 Ollama。**

> embedding 和 LLM（Gemma）都跑在中央 host（100.87.59.15），client 完全不知道也不在乎。

### 連線資訊（直接複製，不需修改）

```
Server URL : http://100.87.59.15:9100/mcp
X-API-Key  : <SB_API_KEY>
```

前提：這台 client 已加入同一個 Tailscale tailnet（連不到 100.87.59.15 = Tailscale 未登入）。

---

### A1 — Claude Code（CLI）

**second-brain（HTTP transport）：**

```bash
claude mcp add --scope user --transport http second-brain \
  "http://100.87.59.15:9100/mcp" \
  --header "X-API-Key: <SB_API_KEY>"
```

**finance-kit（stdio，可選，只在這台機器是 FK 排程主機時需要）：**

```bash
claude mcp add --scope user finance-kit \
  -e "PATH=/Users/lab_center/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  -e "HOME=/Users/lab_center" \
  -- /Users/lab_center/.venvs/finance-kit/bin/python \
     /Users/lab_center/.local/finance-kit/server.py
```

驗證：

```bash
claude mcp list        # 應看到 second-brain ✔ Connected、finance-kit ✔ Connected
```

> ⚠️ **重要陷阱**：`claude mcp add --scope user` 寫入 `~/.claude.json`。
> 若曾手動在 `~/.claude/settings.json` 裡加 `mcpServers` 欄位，**Claude Code 會完全忽略**——
> 必須用上面的 CLI 指令才能生效。

---

### A2 — Claude 桌面版

編輯 `~/Library/Application Support/Claude/claude_desktop_config.json`（⌘Q 重開生效）：

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://100.87.59.15:9100/mcp",
        "--allow-http",
        "--header", "X-API-Key: <SB_API_KEY>"
      ]
    }
  }
}
```

> 桌面版不支援 HTTP transport，透過 `mcp-remote` 橋接（需 Node / npx）。
> npx 找不到時（nvm 安裝），改用絕對路徑 `"command": "/Users/<你的帳號>/.local/bin/npx"`。

---

### A3 — Antigravity

編輯 `~/.gemini/antigravity/mcp_config.json`（若不存在就新建）：

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/<你的帳號>/.local/bin/npx",
      "args": [
        "-y", "mcp-remote",
        "http://100.87.59.15:9100/mcp",
        "--allow-http",
        "--header", "X-API-Key: <SB_API_KEY>"
      ]
    }
  }
}
```

改完重啟 Antigravity 生效。

> `command` 用絕對路徑是因為 GUI app 不讀 `~/.zshrc`，找不到 nvm 的 npx。
> 安裝 Node 步驟：`curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash`，
> 重開終端後 `nvm install --lts`，再 `ln -sf $(which npx) ~/.local/bin/npx`。
>
> ⚠️ **不要**用 `command: python -m mcp_second_brain` 直跑本機 stdio——那會走各機獨立 DuckDB，
> 和中央 Postgres 完全脫節。必須用 mcp-remote 形式才能走單一真相。

---

### A4 — Gemini CLI（`gemini` 指令）

編輯 `~/.gemini/config/mcp_config.json`（若不存在就新建）：

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/<你的帳號>/.local/bin/npx",
      "args": [
        "-y", "mcp-remote",
        "http://100.87.59.15:9100/mcp",
        "--allow-http",
        "--header", "X-API-Key: <SB_API_KEY>"
      ]
    }
  }
}
```

---

### A5 — Windsurf / Cursor（Codeium 系 IDE）

在各 IDE 的 MCP 設定（通常在設定 → MCP Servers 或 `mcp_config.json`）加入同樣的 mcp-remote 設定：

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/<你的帳號>/.local/bin/npx",
      "args": [
        "-y", "mcp-remote",
        "http://100.87.59.15:9100/mcp",
        "--allow-http",
        "--header", "X-API-Key: <SB_API_KEY>"
      ]
    }
  }
}
```

---

### 快速驗證（client）

```bash
curl -s http://100.87.59.15:9100/mcp \
  -H "X-API-Key: <SB_API_KEY>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}' \
  | head -c 200
```

成功回傳 `{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":...` 表示 OK。

---

## B. 中央 host（lab_center，100.87.59.15）— 已設定完成

> **這台機器已於 2026-06-26 完成全部設定，通常不需要重做 B 段。**
> 僅在換機或重裝時參考。

只有**一台**扮演此角色（always-on 個人主機）。路徑：

```bash
PJ="$HOME/Library/CloudStorage/GoogleDrive-<you>@gmail.com/我的雲端硬碟/PJ_save"
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

### B2 — Python（pyenv，若系統 Python < 3.11）

若無 Homebrew sudo 權限：

```bash
curl https://pyenv.run | bash   # 或 brew install pyenv
pyenv install 3.12.10
pyenv global 3.12.10
```

建 venv（**不要建在 Drive 目錄裡**）：

```bash
python -m venv ~/.venvs/second-brain
~/.venvs/second-brain/bin/pip install -r "$SB/requirements.txt"
```

> **已知衝突**：`marker-pdf ≤1.10.2` 要求 `anthropic<0.47`，與專案的 `>=0.109.1` 衝突。
> 排除安裝：`pip install -r requirements.txt --constraint <(echo "marker-pdf==0")`，
> 或直接從 requirements 移除 marker-pdf / pymupdf4llm / pymupdf。PDF 仍有 MarkItDown fallback。
>
> **mcp 版本**：mcp 2.x 移除了 fastmcp，必須用 `mcp[cli]>=1.27.2,<2.0.0`。

### B3 — Embedding server

embedding 模型：`nomic-embed-text`，768 維。有兩種方案：

**方案 A：llama-server（推薦，若已有 llama.cpp）**

直接用 llama.cpp 的 llama-server 跑 nomic-embed-text，port 11435，不需要 Ollama：

```bash
# nomic-embed-text gguf（若有 Ollama blob 可直接用，否則自行下載）
NOMIC_GGUF="$HOME/.ollama/models/blobs/sha256-970aa74c0a90ef7482477cf803618e776e173c007bf957f635f1015bfcfef0e6"

/path/to/llama-server \
  --model "$NOMIC_GGUF" \
  --host 127.0.0.1 --port 11435 \
  --embedding --pooling mean --ctx-size 2048 --no-warmup
```

launchd plist 範例：`~/Library/LaunchAgents/com.llama-server-embed.plist`（RunAtLoad + KeepAlive）。

plist 需設：
```
EMBED_URL=http://localhost:11435/v1/embeddings
EMBED_MODEL=nomic-embed-text
```

> ⚠️ 若同機另有 llama-server 跑 LLM（如 Gemma）在 port 11434，embedding 必須用不同 port（11435），否則衝突。

**方案 B：Ollama（較簡單，沒有自己 llama.cpp 的情況）**

```bash
curl -L https://ollama.com/download/Ollama-darwin.zip -o /tmp/Ollama.zip
ditto -xk /tmp/Ollama.zip /Applications/
/Applications/Ollama.app/Contents/MacOS/ollama serve &
ollama pull nomic-embed-text
```

plist 需設：
```
EMBED_URL=http://localhost:11434/v1/embeddings
EMBED_MODEL=nomic-embed-text
```

> ⚠️ 若同機有其他服務也綁 11434，Ollama 會衝突 → 改用方案 A。

### B4 — 中央 HTTP server（launchd，KeepAlive）

Start script `~/.local/bin/second-brain-start-remote.sh`：
- 從 macOS Keychain 讀 `ANTHROPIC_API_KEY`（`security find-generic-password -a $USER -s ANTHROPIC_API_KEY -w`）
- 偵測 Tailscale IP：`/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4`
- 啟動：`python -m mcp_second_brain --transport streamable-http --host <tailscale-ip> --port 9100`

launchd plist `~/Library/LaunchAgents/com.user.second-brain-remote.plist` 環境：

```text
SB_DB_BACKEND=postgres
SB_PG_DSN=postgresql://postgres:<pw>@localhost:5432/sb_personal
SB_API_KEY=<強金鑰>          # client 以 X-API-Key 帶上
SECOND_BRAIN_PATH=<VAULT>
EMBED_URL=http://localhost:11435/v1/embeddings   # 方案A: llama-server；方案B(Ollama)改為 11434
EMBED_MODEL=nomic-embed-text
```

⚠️ ANTHROPIC_API_KEY **不寫進 plist**（安全），由 start script 從 Keychain 讀取：

```bash
security add-generic-password -a $USER -s ANTHROPIC_API_KEY -w "sk-ant-..."
```

改完 plist 後完整重載（env 變更不能只 `kickstart`）：

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

預期 `{'synced': N, 'embed_failed': 0}`。若 embed_failed > 0：先跑 `sync_all` 再跑 `store.sync_embeddings(vault)`。

### B6 — 維護排程（launchd）

| Job | 頻率 | 用途 |
| --- | --- | --- |
| `com.user.second-brain-pg-sync` | 每逢 :00／:30 | 對 Postgres 跑 `sync_incremental`，撿回 cron 改的 markdown，防索引脫節。使用 `StartCalendarInterval`；實測 `StartInterval=1800` 會停滯。 |
| `com.user.second-brain-pg-backup` | 每日 04:00 | `pg_dump \| gzip` 到 `PJ_save/backups/second-brain-pg/`，保留近 7 份。 |
| `com.user.vault-janitor` | 每日 07:30 | 只在中央主機以 `--push --execute` 歸檔過期財經紀錄；pg-sync 會把 Markdown 變更同步到 Postgres。 |
| `com.user.vault-sleep` | 每週日 02:00 | 只在中央主機執行 Ebbinghaus 維護；DuckDB 專用工作仍只在 DuckDB backend 啟用。 |

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
| Python venv / Docker / Postgres | 中央 host | ❌ 僅 host（client 都不用） |
| llama-server / Ollama（embedding + LLM） | 中央 host | ❌ 僅 host — embedding 在 host 算完再回傳，client 不需安裝任何 AI runtime |
| MCP 設定 + API key | 各 client | ❌ 每台各自 |

---

## 疑難排解

| 症狀 | 原因 | 解法 |
| --- | --- | --- |
| Client `401 unauthorized` | 缺 / 錯 `X-API-Key` | 確認 header 值與 `SB_API_KEY` plist 值完全相同（見 A 段連線資訊）。 |
| Client 完全連不上 | 不在 tailnet，或中央 server / Tailscale 掛了 | `tailscale status`；在 host 查 `pgrep -f streamable-http` 與 `/tmp/second-brain-remote.log`。 |
| server log 設了金鑰卻顯示 `API-key auth DISABLED` | `kickstart` 不會重載 plist env | 改用 `launchctl bootout` + `bootstrap`（見 B4）。 |
| host 設定後查詢空結果 | 索引未建 | 跑 B5 的 `sync_all`。 |
| `sync_all` embed_failed 不歸零 | embeddings 沒有初次補填 | `sync_all` 建索引後，另跑 `store.sync_embeddings(vault)` 補 NULL embeddings。 |
| `sync_all` 龜速 / embed HTTP 500 | embed 截斷未對齊 token batch（已修） | 確認跑最新 Drive 原始碼（`_call_embed_api` 有 256 字 tier、deterministic 500 不退避）。 |
| Postgres 索引與 markdown 脫節 | `pg-sync` job 沒載入 | `launchctl list \| grep pg-sync`；bootstrap `com.user.second-brain-pg-sync`。 |
| Resource deadlock OSError（host sync） | Google Drive streaming 未快取的檔案 | 非 fatal — 背景執行緒錯誤，server 繼續跑；`sync_embeddings` 會補回成功載入的筆記。 |
| `mcp 2.x` ImportError fastmcp | 裝到 mcp 2.0 pre-release | `pip install "mcp[cli]>=1.27.2,<2.0.0"` 覆蓋。 |
| Claude Code `claude mcp list` 看不到 second-brain | 誤把 MCP 寫進 `~/.claude/settings.json` | 用 `claude mcp add --scope user` 寫入 `~/.claude.json`（見 A1 ⚠️ 說明）。 |
| 跑 pytest 清空了正式索引 | 舊測試 DSN 指向 `sb_personal`（已修） | 測試現預設 `sb_test`，`SB_PG_TEST_DSN` 絕不可指向 `sb_personal`。 |
| 快照 `read_note_as_image` 失敗（host） | playwright chromium 沒裝 | `~/.venvs/second-brain/bin/playwright install chromium`。 |
