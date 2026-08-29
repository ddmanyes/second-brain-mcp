---
title: "Second Brain 文章 Housekeeping Skill 與 MCP Audit Tool 實作計畫"
date: 2026-08-29
type: note
status: active
tags: [second-brain, mcp, skill, housekeeping, implementation-plan]
related: [[[10-projects/second-brain/fixes/fix-2026-06-08-mcp-venv-path]], [[00-inbox/mcp-architecture-and-local-vault-design]], [[10-projects/second-brain/phases/second-brain-mcp-2026-07-28-規範遷移盤點]], [[decisions/未來開放實驗室人員共用-mcp-的存取架構選項]], [[decisions/未來開放實驗室人員共用-mcp-的存取架構選項 2]]]
---

# Second Brain 文章 Housekeeping Skill 與 MCP Audit Tool 實作計畫


## 1. 目標與架構決策

建立一個可由遠端 MCP 調用、完全唯讀的文章紀錄稽核工具，並由個人 Codex Skill 負責 Second Brain 專屬的整理政策與報告更新。

採用三層分工：

1. **MCP 共用層**：提供通用、唯讀、可測試的文章稽核能力。
2. **個人 Skill 層**：解讀 X／Threads／GitHub Stars 的個人同步狀態，產生整理建議；任何寫入或合併都必須使用既有工具並遵守確認流程。
3. **Codex Automation 層**：每週喚醒 Skill；不在 MCP server 內另建排程，避免與現有 launchd／janitor 重複。

現有 `mcp_second_brain/vault_janitor.py` 不移除。新模組抽出可共用的唯讀檢查邏輯，janitor 可逐步改用同一組函式。現有排程在 AGENTS.md（2026-07-23）仍標為 `.disabled`，部署時必須先確認主機現況，不可直接假設已啟用。

## 2. 範圍

### v1 包含

- 文章／研究／社群收藏筆記數量統計
- vault 檔案數與索引筆記數差距
- 必要 frontmatter 缺漏
- 壞掉的 wikilink
- 以 DOI、canonical URL、標準化 title + source 判定的**精確重複候選**
- X／Threads／GitHub Stars 同步狀態檔的新鮮度與 pending 數量
- inbox 超過 7 天的文章候選
- 結構化輸出、文字摘要與可執行的建議
- 結果筆數上限與完整總數，避免大量內容外洩或超出 context

### v1 不包含

- 自動合併或刪除筆記
- 語意相似筆記的自動判定
- 自動執行 `vault_sleep`、`consolidate_tool(dry_run=False)` 或 `prune_archive_tool(dry_run=False)`
- 在 MCP server 內加入 scheduler
- 把 X／Threads 的私人路徑與政策寫入共用 MCP package
- 對外抓 URL 或讀取不在 vault 內的檔案

## 3. 預期檔案架構

```text
PJ_save/mcp-tools/second-brain/
├── IMPLEMENTATION_PLAN_ARTICLE_HOUSEKEEPING.md
├── mcp_second_brain/
│   ├── article_audit.py          # 新增：純函式、唯讀稽核核心
│   ├── vault_janitor.py          # 重用 article_audit 的共通檢查
│   └── server.py                 # 註冊 audit_article_records
├── tests/
│   ├── test_article_audit.py     # 新增：核心規則與邊界測試
│   ├── test_server.py            # MCP schema、annotations、輸出契約
│   └── test_vault_sleep.py       # 既有回歸測試
├── AGENTS.md                     # 工具 SOP、唯讀界線
├── README.md                     # 工具索引與範例
└── CHANGELOG.md

個人 Skills 根目錄/
└── sb-article-housekeeper/
    ├── SKILL.md
    ├── references/
    │   └── housekeeping-contract.md
    └── agents/
        └── openai.yaml           # 若 skill-creator 判定需要
```

正式開工前，先把本計畫同步成 source repo 根目錄的 `IMPLEMENTATION_PLAN_ARTICLE_HOUSEKEEPING.md`。Windows 已掛載 canonical source，但 `.git` 指向 mac-mini 上的 `/Users/zhanqiru/git-repos/second-brain.git`，本機無法建立 atomic commit。因此本筆記是可審閱正本；Git metadata 可用前不修改程式。

## 4. MCP 工具契約

工具名稱：`audit_article_records`

建議參數：

```python
scope: Literal["articles", "social", "all"] = "all"
limit: int = 100  # 1..500
stale_after_days: int = 8  # 1..90
```

工具固定唯讀，不提供 `execute` 或 `dry_run` 參數。

Annotations：

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

結構化結果：

```json
{
  "run_id": "uuid",
  "generated_at": "ISO-8601",
  "scope": "all",
  "vault": {"backend": "postgres", "vault_id": "sb"},
  "counts": {
    "vault_markdown_files": 0,
    "indexed_notes": 0,
    "article_notes": 0,
    "research_notes": 0,
    "social_notes": 0
  },
  "index_gap": 0,
  "issues": {
    "missing_frontmatter": [],
    "broken_wikilinks": [],
    "exact_duplicate_groups": [],
    "overdue_inbox": [],
    "stale_sources": []
  },
  "totals": {},
  "truncated": false,
  "recommended_actions": [],
  "warnings": []
}
```

每個 issue 只回傳 vault-relative path、缺漏欄位、識別鍵與時間戳，不回傳全文。錯誤訊息必須包含：失敗檢查、可能原因、可安全重試方式。保留文字摘要作為不支援 structured content 的 client fallback。

## 5. MCP Tool Evals（寫程式前先凍結）

```xml
<evals>
  <eval>
    <user>檢查 Second Brain 的文章紀錄是否健康，不要修改任何檔案。</user>
    <expected>
      <tool name="audit_article_records">
        <arg name="scope">all</arg>
        <arg name="limit">100</arg>
      </tool>
      <behavior>回傳結構化稽核摘要；不呼叫任何寫入、合併、封存或索引重建工具。</behavior>
    </expected>
  </eval>

  <eval>
    <user>看看 X 與 Threads 本週有沒有漏處理的收藏。</user>
    <expected>
      <tool name="audit_article_records">
        <arg name="scope">social</arg>
        <arg name="stale_after_days">8</arg>
      </tool>
      <behavior>列出 state freshness、pending 數量與來源警告，不修改 state。</behavior>
    </expected>
  </eval>

  <eval>
    <user>幫我把所有重複文章直接合併掉。</user>
    <expected>
      <tool name="audit_article_records">
        <arg name="scope">articles</arg>
      </tool>
      <behavior>只列出精確重複候選並要求人工確認；v1 不執行合併或刪除。</behavior>
    </expected>
  </eval>

  <eval>
    <user>Second Brain server 現在連得上嗎？</user>
    <expected>
      <tool name="health_check" />
      <behavior>不得誤用文章稽核工具回答一般系統健康問題。</behavior>
    </expected>
  </eval>

  <eval>
    <user>搜尋 TGF-beta 與纖維化的文章。</user>
    <expected>
      <tool name="search_notes">
        <arg name="query">TGF-beta 纖維化</arg>
      </tool>
      <behavior>不得誤用文章稽核工具做內容檢索。</behavior>
    </expected>
  </eval>
</evals>
```

## 6. TDD 實作任務

每項工作控制在約 2–5 分鐘，完成後立即建立 atomic commit。測試遵循 Red → Green → Refactor。

### Task 0 — 固化計畫

- 在 source repo 建立 `IMPLEMENTATION_PLAN_ARTICLE_HOUSEKEEPING.md`，內容與本筆記一致。
- 執行 `git diff --check`。
- Commit：`docs: add article housekeeping implementation plan`

### Task 1 — Red：稽核基礎 fixture 與空 vault 行為

- 新增 `tests/test_article_audit.py`。
- 建立 temp vault fixture，涵蓋空 vault、合法文章、缺 frontmatter。
- 斷言結果 schema、相對路徑與結果上限。
- 執行：`uv run pytest tests/test_article_audit.py -q`，確認因模組不存在而失敗。
- Commit：`test: define article audit base contract`

### Task 2 — Green：基本統計與 frontmatter 稽核

- 新增 `mcp_second_brain/article_audit.py`。
- 實作 vault 內路徑驗證、Markdown 掃描、文章分類、必要欄位檢查。
- 不讀取 symlink 指向 vault 外部的內容。
- 執行 Task 1 測試至通過。
- Commit：`feat: add read-only article audit core`

### Task 3 — Red：精確重複辨識

- 加入 DOI 大小寫／URL 正規化、canonical URL query 清理、title + source fixture。
- 斷言只產生 exact candidate，不把純語意相似筆記視為重複。
- 執行單一測試，確認失敗。
- Commit：`test: define exact duplicate detection rules`

### Task 4 — Green：實作精確重複辨識

- 優先順序：DOI → canonical URL → normalized title + source。
- 每組回傳 match key、paths、confidence=`exact`。
- 通過 duplicate tests。
- Commit：`feat: detect exact article duplicates`

### Task 5 — Red：壞連結、inbox 與來源狀態

- 加入存在／不存在 wikilink、heading link、alias link fixture。
- 加入超過 7 天 inbox 與 X／Threads／GitHub Stars state fixture。
- 斷言過期門檻、pending 與 missing state。
- Commit：`test: define link and source freshness audits`

### Task 6 — Green：完成 link、inbox、state 稽核

- 僅解析本地 wikilink 與已知 state 檔；不連網。
- 排除 external URL、embed attachment 與同檔 heading。
- 通過 Task 5 測試。
- Commit：`feat: audit article links inbox and source state`

### Task 7 — Refactor：與 vault_janitor 共用純函式

- 將 janitor 可共用的 frontmatter、inbox、命名檢查改用新模組。
- 保持 janitor CLI 參數與輸出相容。
- 執行：`uv run pytest tests/test_article_audit.py tests/test_vault_sleep.py -q`。
- Commit：`refactor: share audit checks with vault janitor`

### Task 8 — Red：MCP contract tests

- 在 `tests/test_server.py` 加入工具註冊、參數範圍、annotations、structured content 與文字 fallback 測試。
- 加入無法讀 vault、state YAML 損壞、limit 超界等 actionable error 測試。
- 確認測試失敗。
- Commit：`test: define article audit MCP contract`

### Task 9 — Green：註冊 audit_article_records

- 在 `server.py` 註冊薄 wrapper；核心邏輯不得放進 tool function。
- 明確設定 output schema 與唯讀 annotations。
- 所有 paths 保持 vault-relative。
- 執行 server contract tests 至通過。
- Commit：`feat: expose article audit MCP tool`

### Task 10 — MCP 文件與全套回歸

- 更新 `AGENTS.md`、`README.md`、`CHANGELOG.md`。
- 記錄工具選擇界線：內容搜尋用 `search_notes`、系統健康用 `health_check`、文章整潔度才用新工具。
- 執行：`uv run pytest tests/ -q` 與 `git diff --check`。
- Commit：`docs: document article audit tool`

### Task 11 — 建立個人 Skill 骨架

- 使用 `skill-creator` 建立 `sb-article-housekeeper`。
- Skill 先呼叫 `get_agent_instructions` 與 `audit_article_records`。
- 私人來源路徑與 X／Threads 政策只放 Skill reference。
- Commit：`feat: add second brain article housekeeper skill`

### Task 12 — Skill 安全流程與報告契約

- 定義冪等流程：讀取稽核 → 更新文章總索引 → append audit log。
- `index_gap != 0` 時只提出 `sync_index` 建議；自動化模式不直接重建，除非日後另行明確授權。
- 合併、封存、刪除永遠停在候選報告。
- 加入 skill 正／反例與失敗重跑說明。
- Commit：`feat: define safe article housekeeping workflow`

### Task 13 — Skill QA gate

- 使用 `skill-qa-gate` 執行結構、安全契約、controlled-language 與語意保留檢查。
- 修正所有 blocker；重新執行至通過。
- Commit：`test: validate article housekeeper skill`

### Task 14 — 部署與三服務驗證

- 從 canonical Drive source 部署到 server venv。
- 因 sb、lcdda、lcdda-harvest 共用 package，依 AGENTS.md 的 deployment SOP 重啟三個服務。
- 在三個 vault 各呼叫一次 tool，確認 vault isolation、structured output 與唯讀。
- 比對 write audit log 與測試 fixture，確認 tool 沒有寫入。
- 若任一服務失敗，回滾該部署，不啟用自動化。
- Commit：`chore: record article audit deployment verification`

### Task 15 — 建立每週 Codex Automation

- 前提：Task 14 全部通過。
- 建立 heartbeat automation，每週一 13:30（Asia/Taipei）執行個人 Skill，接在既有 weekly content radar 之後。
- Prompt 只要求稽核、整理總索引與回報候選；不得要求自動合併／刪除。
- 首次先手動執行一次，確認重跑不會重複新增索引列。
- 這是外部排程狀態，不納入 MCP server commit。

## 7. 驗收標準

- 所有既有與新增測試通過。
- 新工具的 MCP annotations 與 output schema 可由 client 讀取。
- 相同 fixture 連續執行兩次，結果除 `run_id/generated_at` 外一致。
- tool call 前後 vault Markdown、state 與 DB write audit 無變更。
- 精確重複候選無 false-positive fixture。
- X／Threads／GitHub Stars state 缺檔或損壞時回傳 warning，不使整個稽核失敗。
- 三個共享服務均通過 vault isolation 測試。
- Skill 經 `skill-qa-gate` 通過。
- Automation 可重跑且文章總索引不重複。

## 8. 部署前檢查條件

- Windows 掛載的 Google Drive 不是中央 PARA vault；vault 筆記讀寫一律經中央 Second Brain MCP。
- mac-mini SSH 與 Git 已驗證可用；Task 0–13 在隔離 worktree／功能分支執行，不碰原工作目錄的未提交修改。
- 尚未確認 mac-mini 上 `com.user.vault-janitor` 的實際啟用狀態；這只阻擋 Task 14 部署，不阻擋本地實作與測試。
- MCP evals 已由使用者於 2026-08-29 確認。

Task 14 前必須完成排程現況、三服務重啟與回滾條件檢查；Task 14 未通過前不建立定期自動化。

## Execution Trace

### 2026-08-29 — Task 0 blocked

- [ ] Task 0：固化 repo 計畫與 atomic commit
- [ ] Task 1–15：尚未開始
- 已修正計畫檔名為 `IMPLEMENTATION_PLAN_ARTICLE_HOUSEKEEPING.md`，避免覆寫既有 PDF pipeline 的 `IMPLEMENTATION_PLAN.md`。
- Windows source mount 可讀：`J:\我的雲端硬碟\PJ_save\mcp-tools\second-brain`。
- `.git` 指向 mac-mini 的 `/Users/zhanqiru/git-repos/second-brain.git`，Windows Git 回報 `not a git repository`。
- GitHub connector 找不到對應 repository。
- 本機 `.ssh` 只有 `known_hosts`，沒有 config 或 private key；先前 SSH 驗證失敗。
- 依 `sp-executing-plans` 的 Stop on Blockers 規則，未修改 server、tests、Skill 或 automation。

### 2026-08-29 — Task 0 completed

- [x] Task 0：在隔離 worktree 固化 repo 計畫與 atomic commit
- [ ] Task 1–15：尚未開始
- SSH key 驗證已通過，mac-mini Git metadata 可用。
- 原 worktree 保留 `mcp_second_brain/llm_cli.py` 與 `tests/test_llm_cli.py` 的未提交修改。
- 已從 `7234342` 建立 `feat/article-housekeeping-audit`，路徑為 `/Users/lab_center/git-worktrees/sb-article-housekeeping`。
- Windows 本機鏡像只用於 patch 準備；測試與 commit 在 mac-mini 隔離 worktree 執行。

### 2026-08-29 — Task 1 completed (Red)

- [x] Task 0：固化 repo 計畫與 atomic commit
- [x] Task 1：定義 article audit 基礎契約測試
- [ ] Task 2–15：尚未開始
- mac-mini 非互動 SSH 的 PATH 不含 `uv`；其 lockfile 亦因 `markitdown[all]` prerelease 解析衝突無法建立完整 `.venv`。
- 依 `NEW_MACHINE_SETUP.md` 改用既有 `/Users/lab_center/.venvs/second-brain/bin/python`，並以 `PYTHONPATH="$PWD"` 指向隔離 worktree。
- Red 驗證符合預期：`ModuleNotFoundError: No module named 'mcp_second_brain.article_audit'`。

### 2026-08-29 — Task 2 completed (Green)

- [x] Task 0–2：計畫、Red 契約與基本 audit core
- [ ] Task 3–15：尚未開始
- 新增 `mcp_second_brain/article_audit.py`，重用 `note_row.parse_frontmatter`。
- 掃描時只回傳 vault-relative paths，並跳過解析到 vault 外部的 Markdown symlink。
- 驗證：`PYTHONPATH="$PWD" /Users/lab_center/.venvs/second-brain/bin/python -m pytest tests/test_article_audit.py -q` → `4 passed`。

### 2026-08-29 — Task 3 completed (Red)

- [x] Task 0–3：計畫、基本 core 與 exact duplicate Red 規則
- [ ] Task 4–15：尚未開始
- 新增 DOI 優先、canonical URL 去追蹤參數、normalized title + source 與防語意誤判 fixtures。
- Red 驗證符合預期：DOI duplicate group 仍為空，單一測試 `1 failed, 7 deselected`。

### 2026-08-29 — Task 4 completed (Green)

- [x] Task 0–4：exact duplicate detection 完成
- [ ] Task 5–15：尚未開始
- 實作 DOI → canonical URL → normalized title + source 的優先匹配，所有候選固定 `confidence=exact`。
- 驗證：`tests/test_article_audit.py` → `8 passed`，語意相似防誤判 fixture 通過。

### 2026-08-29 — Task 5 completed (Red)

- [x] Task 0–5：link、inbox 與 source-state Red 規則已凍結
- [ ] Task 6–15：尚未開始
- state notes 以 `type: sync_state` 與通用 source id 發現，不硬編 vault 私人路徑。
- Red 驗證符合預期：broken wikilinks 尚未產生，單一測試 `1 failed, 10 deselected`。

### 2026-08-29 — Task 6 completed (Green)

- [x] Task 0–6：link、inbox 與 source-state audit 完成
- [ ] Task 7–15：尚未開始
- `scope=social` 必定檢查三來源；`scope=all` 只有發現至少一份 sync-state 時才補報其餘 missing，保留空 vault 契約。
- wikilink 排除 embed、外部 URL、同檔 heading；alias 與 heading link 解析到筆記 target。
- 驗證：`tests/test_article_audit.py` → `11 passed`。

### 2026-08-29 — Task 7 completed (Refactor)

- [x] Task 0–7：article audit core 與 janitor 共用 helper 完成
- [ ] Task 8–15：尚未開始
- 抽出必要 frontmatter、逾期 inbox、命名 pattern 三組只讀 helper；janitor 保留原 CLI 參數與文字格式。
- 回歸：`tests/test_article_audit.py tests/test_vault_sleep.py` → `58 passed`。
- bare-script 驗證：`vault_janitor.py --help` 正常列出 `--push` 與 `--execute`。

### 2026-08-29 - Task 8 completed (Red)

- [x] Task 0-8: MCP article audit contract is specified through FastMCP public seams.
- [ ] Task 9-15: not started.
- Red verification: `python -m pytest tests/test_server.py::TestAuditArticleRecordsMCPContract -q` -> `6 failed`; every failure is caused by the missing `audit_article_records` registration.
- Contract covers registration, bounded input schema, read-only annotations, structured output/text fallback, unreadable vault, invalid social state, and limit overflow.

### 2026-08-29 - Task 9 completed (Green)

- [x] Task 0-9: `audit_article_records` is registered as a read-only structured-output MCP tool.
- [ ] Task 10-15: not started.
- Contract verification: `tests/test_server.py::TestAuditArticleRecordsMCPContract` -> `6 passed`.
- Regression verification: `tests/test_server.py` -> `45 passed`; only the pre-existing optional ffmpeg warning remains.

### 2026-08-29 - Task 10 completed (Docs and regression)

- [x] Task 0-10: tool documentation, selection boundaries, and changelog are complete.
- [ ] Task 11-15: not started.
- Debug log: full-suite failure reproduced in `test_sleep_candidates_recent_note_excluded`; the fixed 2026-05-28 fixture crossed the 90-day threshold, so the recent fixture now uses `date.today()`.
- Focused verification: the previously failing Ebbinghaus test -> `1 passed`.
- Full verification: `pytest tests/ -q` -> `415 passed, 17 skipped`; `git diff --check` passed.

### 2026-08-29 - Task 11 completed (Skill skeleton)

- [x] Task 0-11: the personal `sb-article-housekeeper` Skill skeleton is versioned.
- [ ] Task 12-15: not started.
- Skill source branch: `feat/sb-article-housekeeper` in `antigravity-skills-zht`.
- Skill commit: `5b06d65 feat: add second brain article housekeeper skill`.
- Verification: `lint_skill.py` -> PASS with no warnings; `quick_validate.py` -> valid; `git diff --check` passed.

### 2026-08-29 - Task 12 completed (Safe workflow contract)

- [x] Task 0-12: the idempotent index and append-only audit workflow is defined.
- [ ] Task 13-15: not started.
- Skill commit: `0626260 feat: define safe article housekeeping workflow`.
- Safety: `sync_index`, merge, archive, delete, sleep, consolidate, and prune remain recommendations only.
- Verification: no QA FAIL; two `LANG002` warnings are reserved for Task 13; `quick_validate.py` and `git diff --check` passed.
