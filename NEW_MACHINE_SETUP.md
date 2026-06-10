# 在新電腦部署 second-brain（Drive 原始碼版）

> 適用情境：把 second-brain 架到你自己的另一台 Mac，**直接跑 Google Drive 同步的原始碼**
> （不是 `pip install mcp-second-brain`——那是給公開使用者）。
>
> 公開使用者的安裝走 [README.md](README.md)；你自己的多台機器走這份，永遠跑最新的 Drive 原始碼。

---

## 心智模型：每台各跑自己的本機 server

多台電腦**不是連到同一個 server**，而是每台各自跑本機 server。四樣東西分工：

| 元件 | 位置 | 原因 |
| --- | --- | --- |
| 程式碼 `mcp_second_brain/`（package） | **Google Drive 同步**（自動） | 改一次全機同步 |
| venv | **本機 `~/.venvs/second-brain/`** | Drive 同步會壞 symlink；macOS 不允許執行雲端資料夾內的執行檔（`Operation not permitted`） |
| 索引 DB `~/.second-brain/vault.db` | **本機自動建** | 從同步的 vault markdown 重建；DuckDB 單寫者，各機獨立 |
| vault 筆記（markdown） | **Google Drive 同步** | 內容跨機共享 |

> 同機可並存多個 server（桌面版 stdio + Claude Code stdio），共用本機同一個
> DuckDB，已設計為 **lock-aware 退避重試、不會互砸索引**；stdio server 也**不再互殺**。

---

## 新機 bootstrap

先設變數代表這台電腦的 Drive 路徑（`/Users/<你>` 每台可能不同）：

```bash
PJ="$HOME/Library/CloudStorage/GoogleDrive-<你的帳號>/我的雲端硬碟/PJ_save"
SB="$PJ/mcp-tools/second-brain"
VAULT="$PJ/second-brain"
```

### Step 1 — 取得程式碼

新機登入同一 Google Drive 即自動同步；確認 `$SB/mcp_second_brain/server.py` 存在。

### Step 2 — 建立本機 venv（**不要建在 Drive 目錄裡**）

```bash
python3 -m venv ~/.venvs/second-brain
~/.venvs/second-brain/bin/pip install -r "$SB/requirements.txt"
~/.venvs/second-brain/bin/playwright install chromium   # PNG 快照渲染用
```

### Step 3 — 註冊 MCP 給本機 Claude

**A. Claude 桌面版** — 編輯 `~/Library/Application Support/Claude/claude_desktop_config.json`，
`command` 一定指向**本機 venv**，`args` 指向 Drive 同步的 `mcp_second_brain/server.py`：

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/<你>/.venvs/second-brain/bin/python",
      "args": ["<PJ>/mcp-tools/second-brain/mcp_second_brain/server.py"],
      "env": {
        "PYTHONPATH": "<PJ>/mcp-tools/second-brain",
        "SECOND_BRAIN_PATH": "<PJ>/second-brain"
      }
    }
  }
}
```

改完 **⌘Q 完全結束並重開桌面版**（MCP config 只在啟動時讀取）。

**B. Claude Code（CLI）**：

```bash
claude mcp add --scope user second-brain \
  ~/.venvs/second-brain/bin/python "$SB/mcp_second_brain/server.py" \
  -e PYTHONPATH="$SB" \
  -e SECOND_BRAIN_PATH="$VAULT"
```

### Step 4 — 首次建索引

啟動 agent 後說 `init_vault`（建/修目錄與模板），再跑 `sync_index` 建立本機索引 DB。
之後每次大量改檔再 `sync_index` 一次即可。

> **不要**用外部 `python -c "vault_db.sync_all(...)"` 直接跑 — 會與 Claude Code 的
> MCP server 競爭 DuckDB 排他鎖，導致 `CatalogException: Table does not exist`。
> 一律透過 MCP 工具（說 `sync_index`）讓 server 內部執行。

### Step 5 —（選用）語意搜尋

不裝也能用（自動 fallback BM25）。要語意搜尋就跑 Ollama：

```bash
brew install ollama 2>/dev/null || true
ollama pull nomic-embed-text
# 然後在上面 MCP config 的 env 加：
#   "EMBED_URL": "http://localhost:11434/v1/embeddings", "EMBED_PORT": "11434"
```

### Step 6 —（選用）每週自動維護

```bash
SECOND_BRAIN_PATH="$VAULT" bash "$SB/launchd/install.sh"
```

`install.sh` 會用本機 `~/.venvs/second-brain/bin/python` 產生 plist 並載入，
每週日 02:00 跑 `launchd/run_sleep.py`（索引 → embedding → 壓縮舊筆記 → 萃取規則）。

---

## 每台機器各自的本機資料

| 資料 | 位置 | 是否同步 |
| --- | --- | :---: |
| Vault markdown 筆記 | Google Drive | ✅ 所有機器共享 |
| 程式碼（`mcp_second_brain/`） | Google Drive | ✅ 所有機器共享 |
| Python venv | `~/.venvs/second-brain/` | ❌ 每台各自建立 |
| DuckDB index | `~/.second-brain/vault.db` | ❌ 每台各自重建（`sync_index`） |
| MCP 設定 | 桌面版 config / Claude Code user scope | ❌ 每台各自設定 |

---

## 多機注意事項

- **不要兩台同時編輯同一筆 vault 筆記** → Google Drive 會生 `xxx (1).md` 衝突檔。等同步完再換機操作。
- **索引 DB 不跨機共享**（各機 `~/.second-brain/vault.db` 各自從同步的 markdown 重建）——這是刻意設計，不要把 DB 放進 Drive。
- **不需要 HTTP 遠端 server**。自己有同步 Drive + venv 的 Mac 用本機 server 即可。
  「不想裝環境的裝置零安裝連入」那套（Tailscale）已停用，細節留存於 [REMOTE_SETUP.md](REMOTE_SETUP.md)。

---

## 疑難排解

| 症狀 | 原因 | 解法 |
| --- | --- | --- |
| 桌面版 `Operation not permitted` / `Server disconnected` | `command` 還指向 Drive 內的 `.venv/bin/python` | 改成本機 `~/.venvs/second-brain/bin/python` |
| 連上但 0.5 秒掉線 | 舊版互殺機制（已修正） | 確認跑的是含修正的 Drive 原始碼（`_kill_old_server` 只在 HTTP 分支） |
| agent 看不到筆記 / 空結果 | 索引未建 | 跑一次 `sync_index` |
| 語意搜尋默默退回 BM25 | embedding server 沒開 | 啟動 Ollama / llama-server |
| 快照 `read_note_as_image` 失敗 | playwright chromium 沒裝 | `~/.venvs/second-brain/bin/playwright install chromium` |
| `Failure while replaying WAL file`（DB 損毀） | DuckDB 寫入中途被打斷（IDE 重啟、`pkill -9`、機器睡眠） | `rm -f ~/.second-brain/vault.db ~/.second-brain/vault.db.wal` 後重啟，再 `sync_index` |
| `~/.second-brain/vault.db` 很小但別處有大的 | server 啟動時 cwd 不是 home，DuckDB 把 DB 建在 cwd | `find ~ -name vault.db -size +1M`，把找到的搬回 `~/.second-brain/vault.db` |
