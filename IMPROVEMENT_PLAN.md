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

## 驗收清單

- [ ] Phase 1：FTS 不再每查重建；新增 index；migration 會 log。`pytest` 綠。
- [ ] Phase 2：刪檔後 sync 會清掉 stale note + 其 figures。新測試綠。
- [ ] Phase 3：知識搜尋/get_context 排除每日金融報告；top_notes 金融用途不受影響。
- [ ] Phase 4：（至少）find_related/get_context 不再重複載入 embedding。
- [ ] Phase 5：清理項目。
- [ ] 全部完成後跑一次 `sync_index()` 重建 DB，並用 `index_stats()` 確認筆數正常。

## 風險備註

- DB 可從 markdown 完全重建（`sync_index`），所以 schema 變更失敗時：刪除 `~/.second-brain/vault.db` 重跑 sync 即可恢復。
- **不要**改 vault 裡的 markdown 內容（那是真實資料）。本計畫只動 `mcp-tools/second-brain/` 下的程式碼與 DB。
- Phase 4.2 風險最高，可獨立評估或略過。
