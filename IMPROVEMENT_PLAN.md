# Second Brain MCP — 改善實作計畫

> 由 Opus review 產出（2026-06-01），交給 Sonnet 施作。
> 每個任務都標了檔案、行號、改法、驗證方式。**請按 Phase 順序做，每個 Phase 做完先跑測試再進下一個。**

## 背景脈絡（給接手的人）

- 架構：vault（markdown，在 Google Drive 同步）+ MCP server（`mcp-tools/second-brain/`）。
- DB：DuckDB，位於 `~/.second-brain/vault.db`，**本機 only、不同步、可由 `sync_all` 從 markdown 重建**。所以動 schema 不可怕，大不了重建。
- 現況：150 notes（90 是 `stock_analysis` 每日選股報告）、150 筆有 embedding、39 figures。
- 測試：`tests/test_server.py`、`test_vault_db.py`、`test_vault_sleep.py`、`test_figures.py`。
- 執行 server：`uv run --with "mcp[cli]" --with "markitdown[all]" python server.py`
- 跑測試：`uv run pytest`（或專案既有方式）
- 嵌入模型維度 768（`EMBED_DIM`）。

**通則**：改動後一定要 `uv run pytest` 全綠才算完成。每個 Phase 獨立 commit。遵守 PEP8 + type hints（user global rules）。

---

## Phase 1 — 效能與索引（低風險，先做）

### 1.1 修「FTS 索引每次搜尋都重建」🔴

**問題**：`vault_db.py` 的 `_ensure_fts()`（約 L127-134）執行 `PRAGMA create_fts_index(..., overwrite=1)`，而它在 `fts_search()`（L445）與 `search_news()`（L574）**每次查詢都被呼叫**，等於每搜一次就把全文索引砍掉重建。

**改法**：
- `_ensure_fts()` 改為「只在索引不存在時才建立」。判斷方式：先 `try` 跑一個極小的 `match_bm25` probe query，成功就 return；失敗（索引不存在）才建立（這時 overwrite=1 才有意義）。
- 或更簡單：保留 `sync_all()` 結尾的 `_ensure_fts(con)`（L332）作為唯一建立點，把 `fts_search` / `search_news` 裡的 `_ensure_fts` 呼叫移除；若 query 因索引不存在而拋例外，再 fallback 走現有的 LIKE 分支（已經有了）。
- **推薦走後者**（更乾淨）：搜尋路徑不負責建索引。

**驗證**：`fts_search("test")` 連續呼叫多次不應觸發 `create_fts_index`；測試仍綠。

### 1.2 加缺失的索引 🟡

**問題**：`SCHEMA`（L78-80）只索引了 `last_accessed`、`note_date`、`figures.note_path`。但 `note_type` 與 `status` 幾乎每個 query 都拿來 filter。

**改法**：在 `SCHEMA` 的 index 區塊加：
```sql
CREATE INDEX IF NOT EXISTS idx_note_type ON notes(note_type);
CREATE INDEX IF NOT EXISTS idx_status    ON notes(status);
```
（DuckDB 對低基數欄位的 index 效益有限，但無害且語意清楚；保留。）

**驗證**：`sync_index()` 後 `index_stats()` 正常；測試綠。

### 1.3 Migration 失敗要記錄，不要靜默吞掉 🟡

**問題**：`_connect()`（L118-122）migration 的 `try/except: pass` 讓失敗完全隱形。

**改法**：把 `except Exception: pass` 改成 `except Exception as e: print(f"[vault_db] migration skipped: {migration!r} → {e}", file=sys.stderr)`（記得 `import sys`）。注意：**正常情況**（欄位已存在）DuckDB 用的是 `ADD COLUMN IF NOT EXISTS`，本來就不該拋例外，所以這裡 log 出來的才是真問題。

**驗證**：正常 connect 不應印出任何 migration 訊息。

---

## Phase 2 — 資料生命週期正確性（中風險）

### 2.1 sync 要處理刪除/改名（reconcile）🟠

**問題**：`sync_all()`（L323）只 upsert。刪掉/改名 markdown 後，DB 留下 stale row + embedding，搜尋回傳死連結。

**改法**：在 `sync_all()` 掃描時收集所有實際存在的 rel path，掃完後刪除 DB 中 path 不在這個集合裡的 row：
```python
def sync_all(vault: Path) -> int:
    seen: set[str] = set()
    with _connect() as con:
        count = 0
        for md_file in vault.rglob("*.md"):
            if any(p in md_file.parts for p in (".obsidian", ".claude", "templates")):
                continue
            upsert_note(con, vault, md_file)
            seen.add(str(md_file.relative_to(vault)))
            count += 1
        # reconcile: 刪除已不存在的 notes 與其 figures
        if seen:
            placeholders = ",".join("?" * len(seen))
            con.execute(f"DELETE FROM figures WHERE note_path NOT IN ({placeholders})", list(seen))
            con.execute(f"DELETE FROM notes   WHERE path      NOT IN ({placeholders})", list(seen))
        _ensure_fts(con)
    return count
```
**注意**：若 `seen` 可能很大（>幾千），改用 temp table join 而非 IN 清單。目前 150 筆用 IN 沒問題。**保險起見**先確認 `seen` 非空才刪（避免某次掃描異常把整表清空）。

**驗證**：新增測試 `test_sync_removes_deleted_notes`：建 2 個 note → sync → 刪 1 個檔 → sync → DB 只剩 1 筆，且該 note 的 figures 也被清掉。

### 2.2 figures 與 notes 的 orphan 清理

由 2.1 的 reconcile 一併解決（DELETE figures WHERE note_path NOT IN seen）。確認 `test_figures.py` 仍綠。

---

## Phase 3 — 知識搜尋訊號（中風險，影響使用體驗）

### 3.1 知識搜尋排除每日金融報告 🟠

**問題**：90/150 是 `stock_analysis`，但 `search_notes`（server.py L226）與 `get_context()` 的 top-20 只排除 `cnyes_archive`，導致知識訊號被每日選股報告淹沒。

**改法**：
- 定義一組「非知識類」type 常數，集中管理。在 `vault_db.py` 頂部加：
  ```python
  NEWS_TYPES = ["cnyes_archive"]
  FINANCE_DAILY_TYPES = ["stock_analysis", "daily_briefing", "market_calendar", "dashboard"]
  KNOWLEDGE_EXCLUDE = NEWS_TYPES + FINANCE_DAILY_TYPES
  ```
- `server.py` 的 `search_notes`：`exclude_types=KNOWLEDGE_EXCLUDE`（import 進來）。
- `hybrid_search_grouped`（L654）的 knowledge 組同樣用 `KNOWLEDGE_EXCLUDE`。
- `get_context()`（server.py L132-140）的 `top_by_score` / `top_by_recency`：加一個 `exclude_types` 參數讓 top 排名也排除每日報告（需在 `vault_db.top_by_score` / `top_by_recency` 加 optional filter，預設不排除以免破壞 `top_notes` 工具的金融用途）。

**注意**：`top_notes` 工具（server.py L820）的金融用途**要保留**能看到 stock_analysis（找最常研究的標的）。所以排除只用在 `get_context` 與 `search_notes`，不要改 `top_notes` 預設行為 —— 用參數控制，別寫死。

**驗證**：`search_notes("transformer")` 不應回傳 stock_analysis；`top_notes(by="score")` 仍可看到金融標的。新增測試覆蓋兩種情境。

---

## Phase 4 — 向量計算下推 SQL（較大風險，最後做，可選）

### 4.1 embedding 改用 DuckDB 原生 array + array_cosine_similarity

**問題**：`semantic_search`（L478）與 `find_related`（L659）把全部 embedding 讀進 Python 算 cosine；`get_context` 一次載入要做 5 次全表掃描。

**改法（謹慎，分兩步）**：
1. 先**不改 schema**，只把 `find_related` 內重複的兩次 `_connect()` 合併成一次、並在 `get_context` 內把已抓的 embedding 重用（避免 N 次重載）。這步低風險、先做。
2. 進階（可選）：schema 的 `embedding BLOB` 改為 `FLOAT[768]`，存取改用 DuckDB list，查詢用 `array_cosine_similarity(embedding, ?::FLOAT[768])` 在 SQL 內排序。需要：
   - 改 `_vec_to_blob`/`_blob_to_vec` → 直接存 list；
   - migration 無法原地改型別，需 `sync_index()` 重建（DB 可重建，OK）；
   - 驗證維度一律 768，不足/超過要拒絕。

**注意**：第 2 步動到儲存格式，務必先確認 DuckDB 版本支援 `FLOAT[N]` 與 `array_cosine_similarity`（requirements 是 `duckdb>=1.1.0`，支援）。做完務必 `sync_index()` 全量重建並驗證搜尋結果與舊版一致。**若時間有限，只做第 1 步即可。**

---

## Phase 5 — 清理（低優先，順手做）

- `.mcp.json`：目前是 Windows 路徑（`C:\...`、`G:\`），在 macOS 無法用。確認真正生效的 config（user scope）後，把這個檔案改成跨平台或刪除，避免誤導。
- `EMBED_DIM = 768` 定義了沒用到 → 在 Phase 4.2 拿來做維度驗證；否則刪掉。
- `.gitignore` 補上 `dist/`、`__pycache__/`、`.pytest_cache/`（若還沒）。
- type 詞彙表：`NOTE_CONFIG`（server.py L27）與實際 DB type（`tech_note`/`dashboard`/`market_calendar`）對不上，未來可統一，但**這次先不動**（會牽動既有 note 分類）。

---

## Phase 11 — 遠端存取（Tailscale + streamable-http）

> 目標：讓第二台 Mac 透過 Tailscale 私有網路使用同一個 second-brain MCP server。

### 11.1 server.py 加 transport / host / port 參數 🟡

**改動**：`server.py` 最末 `if __name__ == "__main__"` 區塊。

```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", default="stdio",
                        choices=["stdio", "streamable-http", "sse"])
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--host", default="")   # 空字串 = FastMCP 預設 (127.0.0.1)
    args = parser.parse_args()

    bootstrap_log = _bootstrap_vault(VAULT)
    if bootstrap_log:
        print("[second-brain] Bootstrap:", ", ".join(bootstrap_log), file=sys.stderr)

    if args.transport == "stdio":
        mcp.run()
    else:
        kwargs: dict = {"transport": args.transport, "port": args.port}
        if args.host:
            kwargs["host"] = args.host
        mcp.run(**kwargs)
```

**重點**：`--host` 預設空字串（不傳給 FastMCP），讓它綁 `127.0.0.1`。
用 Tailscale 啟動時需明確傳入 Tailscale IP：`--host $(tailscale ip -4)`。
**不要用 `0.0.0.0`**（見資安備註）。

**驗證**：

```bash
# 本機 stdio 模式（不受影響）
uv run python server.py

# 遠端 http 模式
uv run python server.py --transport streamable-http --host $(tailscale ip -4) --port 9100
curl http://$(tailscale ip -4):9100/mcp  # 應返回 MCP endpoint 描述
```

### 11.2 launchd plist（持久化遠端模式）🟡

檔案：`~/Library/LaunchAgents/com.user.second-brain-remote.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>       <string>com.user.second-brain-remote</string>
  <key>RunAtLoad</key>   <true/>
  <key>KeepAlive</key>   <true/>
  <key>WorkingDirectory</key>
  <string>/path/to/mcp-tools/second-brain</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/uv</string>
    <string>run</string>
    <string>--with</string><string>mcp[cli]</string>
    <string>--with</string><string>markitdown[all]</string>
    <string>python</string><string>server.py</string>
    <string>--transport</string><string>streamable-http</string>
    <string>--port</string><string>9100</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SECOND_BRAIN_PATH</key>
    <string>/path/to/second-brain</string>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StandardOutPath</key>  <string>/tmp/second-brain-remote.log</string>
  <key>StandardErrorPath</key><string>/tmp/second-brain-remote.log</string>
</dict>
</plist>
```

> ⚠️ `--host` 不要寫死進 plist，Tailscale IP 可能在 Tailscale 重裝後改變。
> 改用啟動 wrapper script，讓它動態取 `$(tailscale ip -4)`。

### 11.3 遠端 Mac 的 Claude Code 設定 🟢

```bash
claude mcp add --scope user second-brain-remote \
  --transport http \
  http://100.x.x.x:9100/mcp
```

---

### 資安備註（Phase 11 專用）

| 風險 | 等級 | 現況 / 說明 |
| --- | --- | --- |
| 路徑遍歷 | ✅ 已防護 | `is_relative_to(VAULT)` 所有 read/write 都有 |
| SSRF | ✅ 已防護 | `_is_ssrf_safe()` + `save_article` URL 白名單 |
| 無認證 | ⚠️ Tailscale 緩解 | **Tailscale = 裝置層認證**，只有同帳號裝置可連；不開放 public internet |
| 破壞性工具 | ⚠️ 設計緩解 | `vault_sleep` / `prune_archive_tool` 預設 `dry_run=True`，需明確傳 `False` 才真正執行 |
| 個資曝露 | ⚠️ Tailscale 緩解 | vault 含個人財務/決策筆記；Tailscale 限制只有你的設備能讀 |
| 0.0.0.0 綁定 | 🔴 **禁止** | 不要 bind 到所有介面；只綁 Tailscale IP（`100.x.x.x`）或 loopback |
| 無 rate limit | ℹ️ 低風險 | 兩台 Mac 個人使用，不需要 |
| 無 audit log | ℹ️ 可接受 | 如有需要，可在 FastMCP middleware 加 access log |

**Tailscale ACL（選做，更嚴謹）**：在 Tailscale admin console 設定，只允許特定 device tag 能訪問 port 9100，其餘設備即使在同一個 Tailnet 也無法連線。

---

## 驗收清單

- [ ] Phase 1：FTS 不再每查重建；新增 index；migration 會 log。`pytest` 綠。
- [ ] Phase 2：刪檔後 sync 會清掉 stale note + 其 figures。新測試綠。
- [ ] Phase 3：知識搜尋/get_context 排除每日金融報告；top_notes 金融用途不受影響。
- [ ] Phase 4：（至少）find_related/get_context 不再重複載入 embedding。
- [ ] Phase 5：清理項目。
- [ ] Phase 11：`server.py` 支援 `--transport streamable-http --host --port`；本機 stdio 不受影響；遠端 Mac 測試 `get_context()` 回應正常。
- [ ] 全部完成後跑一次 `sync_index()` 重建 DB，並用 `index_stats()` 確認筆數正常。

## 風險備註

- DB 可從 markdown 完全重建（`sync_index`），所以 schema 變更失敗時：刪除 `~/.second-brain/vault.db` 重跑 sync 即可恢復。
- **不要**改 vault 裡的 markdown 內容（那是真實資料）。本計畫只動 `mcp-tools/second-brain/` 下的程式碼與 DB。
- Phase 4.2 風險最高，可獨立評估或略過。
- Phase 11 安全前提：Tailscale 帳號安全 = MCP server 安全。啟用 Tailscale 2FA。
