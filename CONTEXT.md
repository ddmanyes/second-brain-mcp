# Second Brain — Domain Model (Ubiquitous Language)

> 這份文件釘住本專案的**領域用詞**。架構檢視（improve-architecture）與新程式碼都以此為準。
> 種子文件，非窮舉——遇到磨利的術語就當場補進來。

## 核心名詞

- **Vault（知識庫）**：markdown 檔的集合，是**正本 (source of truth)**。路徑由 `SECOND_BRAIN_PATH` 決定。index 可由 `sync_all` 從 vault 重建。
- **Note（筆記）**：vault 內一份 `.md` 檔。由模板產生，**恆帶 frontmatter**。
- **Frontmatter**：note 開頭的 `---\n … \n---` YAML 區塊。存 title、status、tags、以及 enrichment 寫回的欄位（`semantic_keywords`、`neighbor_keywords`、`cluster_topic`、`related`）。
  - **引號慣例是逐欄位的、無法從值推導**：`semantic_keywords` 是帶引號 list、`tags`/`related` 是裸值 list、`status` 是裸 scalar、`title`/`source` 是帶引號 scalar。
  - **Surgical set（就地設定）**：修改 frontmatter 的既定動作 = 「定位該欄位行 → 取代，或缺則在 block 內附加，或無 block 則建立」，**只動被碰的欄位、保留其餘位元組**（守住 vault 是正本、git diff 最小）。這個手術由 [`frontmatter.py`](mcp_second_brain/frontmatter.py) 的 `set_fields` / `set_fields_in_file` 獨家擁有；值的序列化不屬它，由呼叫端傳已格式化字串。
- **NoteRow（索引投影）**：一則 note 在 index 裡的樣子。由 [`note_row.project_note`](mcp_second_brain/note_row.py) 獨家擁有——frontmatter 解析、`cnyes_archive` 把 tickers 前置到 snippet、embed input 配方、keyword 欄位的多格式容錯、大檔截斷（**hash 覆蓋全檔、投影只讀前 40KB**——2026-09-03 Phase B-0 隨 `embed_text_for` 的 900→32,000 一起調高，見 `note_row.LARGE_FILE_READ_LIMIT` 的註解，否則會是靜默 no-op）。`embed` 與 `validate` 用注入，所以投影可在無 DB、無 embedding server 下測試。
  - **縫切在「怎麼存」，不切在「note 是什麼」**：兩個 store 只負責把 `NoteRow` 綁進自家 SQL。這段知識曾在 DuckDB/Postgres 各抄一遍（~70 行逐行對應），等於讓純領域知識橫跨了 store 這道縫——加一個 frontmatter 欄位要改兩處，漏一處就是**後端間靜默語意分裂**。
- **Store（索引後端）**：可插拔的 index，由 `SB_DB_BACKEND` 選 `postgres`（中央，pgvector+pg_trgm）或 `duckdb`（離線 fallback）。介面在 `store/base.py`。markdown 是正本，store 可重建。
  - **Staleness 由 backend 自答**：「index 是否過時、該不該 sync」是 backend 私有知識——`store.sync_if_stale(vault)`，server 不得認得 `DB_PATH` 這類後端細節（DuckDB 用 DB 檔 mtime 節流；Postgres 靠排程 sync、視 live query 恆新）。
- **Enrichment（充實）**：note 寫入後在背景對它做的增益——semantic keyword 抽取、neighbor keyword、related links、figure 抽取——結果**寫回 frontmatter**（透過 surgical set）。
- **Write tail（寫後尾巴）**：任何 note 的位元組落地後必跑的「index + enrichment」收尾。**不變式**：內容一變 → store 重索引（漏了 note 就從搜尋消失）。其餘（註冊新 note、relink、keyword enrich、figure 抽取）是各寫入路徑自選的變化點。由 `server.after_write(dest, rel, *, register_label, relink, enrich, extract_figures)` 獨家擁有；new_note / save_article / update_note / append_to_note / annotate_figure 皆只剩一行呼叫它。永不 raise（寫已落地，index 失敗只警告）。
- **Figure（圖）**：從 PDF/文章抽出的圖，存 DuckDB figures cache（刻意獨立於可插拔 store）。
- **Vision answer（視覺答覆）**：向 VLM 送一張圖要一份 JSON 的結果。由 [`llm_cli.vision_json`](mcp_second_brain/llm_cli.py) 獨家擁有（Anthropic SDK → CLI 備援、base64/media-type、code-fence 裡撈 JSON、token usage）。**鐵律：`None`（沒得到答覆）與空的 `data`（模型看過且說沒有）是兩件不同的事**——前者必須重試或出聲，後者才可以寫進 negative cache。`analyse_figure` 回 `None`、`_detect_figures_on_page` 丟 `VisionUnavailable`，都是這條線的兩端。文字問答的對應物是 `llm_cli.llm_text` / `llm_image`。
- **Tool boundary（工具邊界）**：MCP tool 的守衛前言所在之處——權限判定、稽核紀錄、路徑錯誤轉成回傳字串。由 [`server.write_tool`](mcp_second_brain/server.py) / `admin_tool` 兩個裝飾器**獨家擁有**；tool body 內不得再手抄任何一段。工具名一律取自 `__name__`，不得以字串字面值重複。
  - **Write tool（寫入 tool）**：任何會改動 vault 位元組或 index 的 tool。「哪些是寫入 tool」是**從程式碼導出的事實**（`server.WRITE_TOOLS` 登記表），不是手抄清單——測試列舉它，不複製它。
  - **Audit target（稽核目標）**：稽核紀錄裡代表「動了什麼」的字串。由裝飾器的 `target=`（參數名）或 `target_const=`（固定目標，如 update_goals → memory/goals.md）指定。
- **Contained path（圍堵路徑）**：呼叫端給的 vault 相對路徑，經 join → resolve → 證明仍落在 VAULT 內（→ 存在性）之後的 `Path`。這道判定由 [`vault_paths.resolve_in_vault`](mcp_second_brain/vault_paths.py) 獨家擁有，失敗一律 `VaultPathError`（其訊息即回給呼叫端的措辭）；server 內用 `_vault_path()` 綁定當前 VAULT。**寫在 CLAUDE.md 的規則靠紀律，寫成模組的規則靠程式碼**——這條 invariant 曾散成 16 份手抄、5 種方言，其中 enrich_neighbor_keywords 漏掉圍堵那一半。

## 架構詞彙（跨層通用）

- **Deep module（深模組）**：小介面藏大量行為。`frontmatter.set_fields` 即一例——一個介面收斂了原本散在 5 檔、4 種不相容手抄的 block 手術。`vault_paths.resolve_in_vault` 是第二例。
- **Seam（縫）**：能在不編輯該處的情況下改變行為的位置。`store/` 是真正的雙 adapter 縫（postgres/duckdb）；**tool boundary** 是另一道縫——縫在 tool 邊界而非 body 內，所以測試能穿過它驅動真 tool（body 內的守衛，測試只能繞過）。
- **介面就是測試面（Interface is the test surface）**：若一個不變式只能靠「讀原始碼確認每個呼叫端都有抄」來保證，它就沒有介面。把它提到一道縫上，測試才寫得出來。RBAC 前言與路徑圍堵都是這樣被提上來的。
