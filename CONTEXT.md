# Second Brain — Domain Model (Ubiquitous Language)

> 這份文件釘住本專案的**領域用詞**。架構檢視（improve-architecture）與新程式碼都以此為準。
> 種子文件，非窮舉——遇到磨利的術語就當場補進來。

## 核心名詞

- **Vault（知識庫）**：markdown 檔的集合，是**正本 (source of truth)**。路徑由 `SECOND_BRAIN_PATH` 決定。index 可由 `sync_all` 從 vault 重建。
- **Note（筆記）**：vault 內一份 `.md` 檔。由模板產生，**恆帶 frontmatter**。
- **Frontmatter**：note 開頭的 `---\n … \n---` YAML 區塊。存 title、status、tags、以及 enrichment 寫回的欄位（`semantic_keywords`、`neighbor_keywords`、`cluster_topic`、`related`）。
  - **引號慣例是逐欄位的、無法從值推導**：`semantic_keywords` 是帶引號 list、`tags`/`related` 是裸值 list、`status` 是裸 scalar、`title`/`source` 是帶引號 scalar。
  - **Surgical set（就地設定）**：修改 frontmatter 的既定動作 = 「定位該欄位行 → 取代，或缺則在 block 內附加，或無 block 則建立」，**只動被碰的欄位、保留其餘位元組**（守住 vault 是正本、git diff 最小）。這個手術由 [`frontmatter.py`](mcp_second_brain/frontmatter.py) 的 `set_fields` / `set_fields_in_file` 獨家擁有；值的序列化不屬它，由呼叫端傳已格式化字串。
- **Store（索引後端）**：可插拔的 index，由 `SB_DB_BACKEND` 選 `postgres`（中央，pgvector+pg_trgm）或 `duckdb`（離線 fallback）。介面在 `store/base.py`。markdown 是正本，store 可重建。
  - **Staleness 由 backend 自答**：「index 是否過時、該不該 sync」是 backend 私有知識——`store.sync_if_stale(vault)`，server 不得認得 `DB_PATH` 這類後端細節（DuckDB 用 DB 檔 mtime 節流；Postgres 靠排程 sync、視 live query 恆新）。
- **Enrichment（充實）**：note 寫入後在背景對它做的增益——semantic keyword 抽取、neighbor keyword、related links、figure 抽取——結果**寫回 frontmatter**（透過 surgical set）。
- **Write tail（寫後尾巴）**：任何 note 的位元組落地後必跑的「index + enrichment」收尾。**不變式**：內容一變 → store 重索引（漏了 note 就從搜尋消失）。其餘（註冊新 note、relink、keyword enrich、figure 抽取）是各寫入路徑自選的變化點。由 `server.after_write(dest, rel, *, register_label, relink, enrich, extract_figures)` 獨家擁有；new_note / save_article / update_note / append_to_note / annotate_figure 皆只剩一行呼叫它。永不 raise（寫已落地，index 失敗只警告）。
- **Figure（圖）**：從 PDF/文章抽出的圖，存 DuckDB figures cache（刻意獨立於可插拔 store）。

## 架構詞彙（跨層通用）

- **Deep module（深模組）**：小介面藏大量行為。`frontmatter.set_fields` 即一例——一個介面收斂了原本散在 5 檔、4 種不相容手抄的 block 手術。
- **Seam（縫）**：能在不編輯該處的情況下改變行為的位置。`store/` 是真正的雙 adapter 縫（postgres/duckdb）。
