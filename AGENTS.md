# AGENTS.md — Second Brain Operating Manual for AI Agents

> 本文件供 Claude 等 AI agent 閱讀。接到任何 second-brain vault 相關請求時，先讀本文件確認操作 SOP，再呼叫工具或修改檔案。
>
> **Last updated:** 2026-06-09
>
> **如何取得本文件**：Claude Code 本地工作時自動從 `CLAUDE.md` 引用（需在 `second-brain/` 目錄下啟動）；遠端 MCP 連入時呼叫 `get_agent_instructions()` 工具（回傳本文件完整內容）。
>
> **新增說明時**：依內容類型修改對應文件 → 更新本頁 Last updated 日期

---

## 系統識別

Second Brain 是個人知識庫管理套件，透過 MCP 提供 vault 的讀寫、搜尋、歸檔與維護功能。

- **MCP server**：`server.py`（27 個工具）
- **Vault 資料庫**：`vault_db.py`（語義搜尋、Ebbinghaus 評分）
- **Vault 路徑**：由環境變數 `SECOND_BRAIN_PATH` 控制

---

## 工具清單與呼叫時機

| 使用者說... | 呼叫工具 | 備註 |
| ----------- | -------- | ---- |
| 「現在要做什麼」「有哪些活躍筆記」 | `get_context()` | 每次 session 開始時呼叫，載入目標 + 活躍筆記 |
| 「建立新筆記」「記錄決策」「新增專案頁」 | `new_note(note_type, title)` | note_type 見下方 NOTE_CONFIG |
| 「更新這個筆記」「改寫內容」 | `update_note(path, content)` | 覆寫整個筆記；先 read_note 確認再更新 |
| 「補充進度」「追加內容」 | `append_to_note(path, content)` | 安全追加，不影響現有內容 |
| 「搜尋 xxx 相關筆記」 | `search_notes(query)` | 語義搜尋；精確搜尋加引號 |
| 「分組顯示搜尋結果」 | `search_grouped(query)` | 結果按 type 分組 |
| 「搜尋新聞 / 近期文章」 | `search_news_tool(query, days)` | 預設最近 7 天 |
| 「讀這個筆記」 | `read_note(path)` | path 相對於 vault 根目錄 |
| 「以圖片方式讀取」 | `read_note_as_image(path)` | 用於含圖表的筆記 |
| 「查決策紀錄」 | `get_decisions(project)` | 不帶 project 則回傳全部 |
| 「更新目標」 | `update_goals(new_content)` | 覆寫 memory/goals.md |
| 「儲存這篇文章」 | `save_article(source, title, tags)` | source 為 URL 或本地檔案 |
| 「找相關筆記」 | `find_related_notes(path, limit)` | 語義相似度 threshold 預設 0.7 |
| 「最熱門的筆記」 | `top_notes(by, limit)` | by: "score"/"recency" |
| 「更新索引」「重建語義搜尋」 | `sync_index()` | 新增大量筆記後執行 |
| 「索引狀態」「有幾篇筆記」 | `index_stats()` | |
| 「歸檔舊筆記」 | `vault_sleep(dry_run=True)` | 先 dry_run 確認再執行 |
| 「查看哪些筆記將被歸檔」 | `sleep_status()` | |
| 「找重複筆記」 | `consolidate_tool(dry_run=True)` | 先 dry_run；threshold 預設 0.85 |
| 「清理過期歸檔」 | `prune_archive_tool(dry_run=True)` | 先 dry_run；min_age_days 預設 365 |
| 「擷取筆記規則」 | `extract_rules_tool(note_path)` | 萃取 `- [ ]` 規則條目 |
| 「更新連結」 | `update_links_tool(note_path)` | 重建 wiki link |
| 「擷取圖表」 | `extract_figures_for(note_path)` | 儲存至 figures/ |
| 「搜尋圖表」 | `search_figures(query)` | |
| 「快照這個筆記」 | `snapshot_note_tool(note_path, tier)` | tier: "base"/"detail" |
| 「初始化 vault」「修復目錄結構」 | `init_vault()` | 安全重跑，只建缺少的項目 |
| 「agent 操作說明」（遠端啟動時） | `get_agent_instructions()` | 回傳本文件完整內容 |

---

## NOTE_CONFIG — 筆記類型對應

| note_type | 存放資料夾 | 模板 |
| --------- | ---------- | ---- |
| `decision` / `adr` | `decisions/` | `decision-template.md` |
| `project` | `10-projects/` | `project-template.md` |
| `mcp` | `10-projects/` | `mcp-project-template.md` |
| `research` / `paper` / `finding` | `20-areas/research/` | `research-note-template.md` |
| `coding` / `tool` | `20-areas/coding/` | `note-template.md` |
| `resource` / `reference` | `30-resources/` | `note-template.md` |
| 其他（未知類型） | `00-inbox/` | `note-template.md` |

---

## Frontmatter 規格（各 note_type 必填欄位）

`new_note` 自動填入 `title` 和 `date`。以下欄位若有遺漏，agent 應在建立後補填：

| note_type | 必填（模板已含） | 建議補填 | 說明 |
| --------- | --------------- | -------- | ---- |
| `decision` / `adr` | `title`, `date`, `type: decision`, `status` | `tags` | status 值：`proposed` → `accepted` → `superseded` |
| `project` / `mcp` | `title`, `date`, `type: project`, `status` | `tags` | status 值：`active` / `completed` / `archived` |
| `research` / `paper` | `title`, `date`, `type: research`, `status` | `source`, `tags` | source 填原始 URL 或 DOI |
| `coding` / `tool` | `title`, `date`, `type: note`, `status` | `tags` | |
| `resource` / `reference` | `title`, `date`, `type: resource`, `status` | `source`, `tags` | |
| stock_analysis（finance-kit 寫入） | `title`, `date`, `type: stock_analysis` | `ticker`, `close`, `rsi`, `composite_score` | 缺少這四欄則 00_Finance_MOC.md 的 Dataview 無法抓取 |

**通用規則：**

- `status` 只用：`active` / `completed` / `archived` / `proposed` / `accepted`
- `tags` 用小寫 kebab-case，如 `[mcp, ai-agent, finance]`
- `related` 用 `[[wikilink]]` 格式，工具會自動注入語義相關連結

---

## 論文 / 文獻筆記命名規範

從 PDF / 外部文獻建立研究筆記時，**不要沿用 `new_note` 自動產生的 kebab-case 全標題**（太長、缺年份/作者難排序溯源），改用：

```text
{YYYY}_{FirstAuthorLastName}_{ShortTitle}.md
```

- `YYYY`：線上發表年份
- `FirstAuthorLastName`：第一作者姓氏，英文 PascalCase
- `ShortTitle`：系統名稱或關鍵詞，PascalCase（**非論文全標題**）
- 存放：`20-areas/research/`

範例：`2026_Gottweis_CoScientist.md`、`2026_Ghareeb_Robin.md`、`2026_Aygun_ERA.md`

---

## 圖片附件規則

所有圖片集中在 vault root 的 `figures/`，**不散落在筆記旁**。
**務必用可見目錄 `figures/`，不要用隱藏的 `.figures/`** — Obsidian 不索引隱藏目錄，圖片會顯示「無法找到」。

| 情境 | 路徑 |
| ---- | ---- |
| 論文圖表 | `figures/{paper-short-title-kebab}/fig-{NN}.png` |
| 專案截圖 | `figures/{project-slug}/fig-{NN}.png` |
| 其他 / 暫存 | `figures/misc/` |

- `fig-NN` 從 `fig-00` 起，依 PDF 頁碼或圖號遞增
- 從本地 PDF 擷取：`pdftoppm -r 150 -png -f {start} -l {end} input.pdf figures/{slug}/fig`
- 嵌入語法：`![[figures/{slug}/fig-00.png]]`
- **`extract_figures_for` 只對 `save_article` 建立的筆記有效**（需 source URL）；本地 PDF 一律用上面的 `pdftoppm`

---

## 標準作業流程（SOP）

### A. 筆記建立請求

```text
1. 判斷 note_type（見 NOTE_CONFIG 表）
2. 呼叫 new_note(note_type, title, content, tags)
3. 工具自動套用模板、寫入正確資料夾、加入索引、注入語義連結
4. 模板只自動替換 {{title}} 和 {{date}}；其餘佔位符需補填
5. 對照上方 frontmatter 規格確認必填欄位是否齊全
```

### B. 搜尋 / 查詢請求

```text
1. 模糊語義搜尋 → search_notes(query)
2. 分組顯示    → search_grouped(query)
3. 新聞 / 文章 → search_news_tool(query, days)
4. 決策紀錄   → get_decisions(project)
5. 讀取原文   → read_note(path)
```

### C. Vault 維護（定期或按需）

```text
1. 確認歸檔候選     → sleep_status()
2. 執行歸檔（試跑） → vault_sleep(dry_run=True)
3. 確認無誤後正式執行 → vault_sleep(dry_run=False)
4. 找重複（試跑）   → consolidate_tool(threshold=0.85, dry_run=True)
5. 清理過期歸檔     → prune_archive_tool(dry_run=True) → (dry_run=False)
```

### D. 修改程式碼或新增功能

```text
1. 先讀 CLAUDE.md 確認架構與安全規範
2. 主要工具邏輯在 server.py
3. 語義搜尋 / 評分在 vault_db.py
4. 修改後跑測試（若有）：.venv/bin/python -m pytest tests/ -q
```

### E. 更新現有筆記請求

`new_note` 不覆蓋現有檔案。更新現有筆記用 second-brain 自身工具：

```text
追加內容（安全，不影響現有內容）：
  append_to_note(path, content)

覆寫整個筆記（大幅改寫時）：
  1. read_note(path)                   ← 先確認現有結構
  2. update_note(path, new_content)    ← 覆寫（自動更新索引 + 語義連結）
```

**判斷建立 vs 更新：**

| 情境 | 做法 |
| ---- | ---- |
| 筆記不存在 | `new_note` |
| 追加進度 / 補充內容 | `append_to_note` |
| 大幅改寫、修正 frontmatter | `read_note` → `update_note` |
| 同一主題想保留歷史版本 | `snapshot_note_tool` 先快照，再 `update_note` |
| 更新 goals | `update_goals`（專用工具） |

---

## 檔案輸出規則

| 輸出類型 | 路徑 | 命名 |
| -------- | ---- | ---- |
| 新筆記 | 依 NOTE_CONFIG 對應資料夾 | `{slug}.md`（自動產生） |
| 決策紀錄 | `decisions/` | `{slug}.md` |
| 儲存文章（save_article） | `30-resources/` | `{slug}.md` |
| 歸檔筆記 | `40-archive/` | 原名稱不變 |
| 圖表檔案 | `figures/{slug}/` | `fig-{NN}.png`（見圖片附件規則） |

---

## 歸檔決策樹（論文 vs 參考資料）

無已知專案的外部內容，依「是否有明確學術來源」分流：

| 條件 | 存放位置 | 命名 |
| ---- | -------- | ---- |
| 有 DOI / 期刊名 / 明確第一作者 | `20-areas/research/` | `{YYYY}_{Author}_{ShortTitle}.md` |
| 無以上（官方 doc、教學、工具文件） | `30-resources/` | `{kebab-slug}.md` |
| 個股分析 | `20-areas/personal/finance/` | `{TICKER}_analysis_{YYYYMMDD}.md` |
| 不確定 | `00-inbox/` | `{YYYY-MM-DD}-{topic}.md`（最多停留 7 天） |

專案類筆記存檔前先查 `10-projects/PROJECT_REGISTRY.md` 確認 slug。

---

## 編輯紀律

1. **局部更新，不整檔 rewrite** — 改既有筆記只動需要改的段落；`append_to_note` 優先，大幅改寫才 `read_note` → `update_note`
2. **不動無關處** — 不重排 frontmatter key 順序、不改沒要改的欄位、不 reflow 既有表格與段落
3. **建檔走工具** — 新筆記一律經 `new_note`（套 template），禁止繞過工具直接寫入
4. **命名查 registry** — project / coding 類存檔前先查 `PROJECT_REGISTRY.md` 確認 slug

---

## 分析限制（硬規則）

1. **路徑安全**：`read_note` / `update_note` / `append_to_note` 均使用 `.resolve().is_relative_to(VAULT)` 防止路徑遍歷攻擊
2. **SSRF 防護**：`save_article` 的 source 必須通過 `_validate_source()` — 只允許 http/https 或白名單副檔名；圖片下載必須通過 `_is_ssrf_safe()` — 封鎖 loopback / RFC-1918 / 169.254
3. **破壞性操作先 dry_run**：`vault_sleep`、`consolidate_tool`、`prune_archive_tool` 必須先以 `dry_run=True` 確認範圍再正式執行
4. **不覆蓋現有筆記（new_note）**：`new_note` 若檔案已存在回傳 `"Note already exists"`，不做任何寫入；改用 `update_note`（覆寫）或 `append_to_note`（追加）
5. **YAML frontmatter 安全**：title/source 用 `json.dumps(value.strip())[1:-1]` 做 escaping，不得用 `.replace('"', "'")`

---

## Session 啟動清單

新對話開始時（依需要）：

1. 呼叫 `get_context()` — 載入目標與活躍筆記，建立當前 session 的 context
2. 若需搜尋歷史 → `search_notes()` 或 `get_decisions()`
3. 若是開發 / 修改程式 → 讀 `CLAUDE.md` 確認安全規範
4. 若遠端連線 → 確認 Tailscale IP + port 9100，見 `REMOTE_SETUP.md`

---

## Vault 目錄結構

```text
second-brain/
├── 00-inbox/          # 未分類新筆記（定期清理）
├── 10-projects/       # 專案頁（含 MCP 專案文件）
├── 20-areas/
│   ├── coding/        # 技術筆記、工具評測
│   ├── research/      # 研究論文、研究筆記
│   └── personal/
│       └── finance/   # 財務分析（與 finance-kit 整合）
├── 30-resources/      # 參考資料
├── 40-archive/        # 歸檔舊筆記
├── decisions/         # 決策紀錄（ADR）
├── memory/
│   ├── goals.md       # 當前目標（get_context 載入）
│   ├── rules.md       # 活躍規則（get_context 注入）
│   └── index.md       # Vault 索引備份
└── templates/         # 筆記模板
```

---

## 相關文件

- [`CLAUDE.md`](CLAUDE.md) — 安全規範、執行指令、環境變數
- [`README.md`](README.md) — 系統功能概覽、工具索引
- [`REMOTE_SETUP.md`](REMOTE_SETUP.md) — 遠端 MCP 接入設定（Tailscale port 9100）
- finance-kit [`AGENTS.md`](../finance-kit/AGENTS.md) — 財務分析模組操作手冊
