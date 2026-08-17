# MULTIUSER_PLAN.md — SB 多人協作補強（身分 / 權限 / 稽核 / 中文 FTS）

> **定位**：`MIGRATION_PLAN_POSTGRES.md` 解決了「多機並發」（中央 server + Postgres MVCC）；本 plan 補上「**多人協作**」——身分、角色、唯讀強制、寫入歸屬、金鑰生命週期，外加中文 FTS 精度。
> **建立日期**：2026-06-17
> **狀態（2026-08-17 更新）**：P1–P4 程式碼**已合併**，P5 未做。
> commit：`3e2a769`(P1) / `b18e644`(P2) / `ec8ca6c`(P3) / `e5a58eb`(P4) + R1/R2 修復。
>
> **P4 曾有兩個阻塞缺陷，2026-08-17 已修復**（`auth.py` / `identity.py` / store）：
> - ~~**R1**：`_key_accepted()` 只比對 `SB_API_KEY`/`SB_API_KEYS`，DB 註冊的 key 在 `lookup_fn` 執行前就被 401 擋掉 → `manage_api_key` 走 HTTP 形同虛設。~~
> - ~~**R2**：把 key 同時寫進 env 當 workaround 後，撤銷時 `get_identity_for_key()` 回 `None`，被誤判為「未登記的 env key」而 fallback 成 `role='admin'` → 撤銷等於把成員從 reader 提升為 admin。~~
> - **修法**：`get_identity_for_key()` 改回三態（`Identity` / `KeyState.REVOKED` / `None`=unknown）；
>   `_key_accepted()` + `_resolve_identity()` 合併成 `auth._authenticate()`，DB key 自行認證、
>   `REVOKED` 直接 401 且永不進入 env fallback。另加 `count_active_api_keys()`，讓拿掉共用 env key
>   之後 auth 仍為那些 DB key 保持開啟（原本會靜默整個關閉）。
> - 迴歸測試：`tests/test_auth.py::TestRegisteredKeyRegressions`（三項）+ `test_key_lifecycle.py` 已更新。
>   全套 392 passed / 17 skipped；另對真實 `PostgresStore`(sb_test) 驗證過註冊→認證→撤銷→401。
>
> **仍未啟用**（這是設定問題，不是程式問題）：
> - `SB_RBAC_ENFORCE` 未設 → P2 目前是 audit-only，reader 照樣寫得進去。
> - 各實例 `api_keys` 表 0 筆，實際仍是單一共用 env key = admin。
>
> 完整三層盤點與 review（與 PWA 計畫的 RBAC 重疊、目標實例矛盾、:9106 缺口）見 vault 筆記
> `10-projects/second-brain/phases/實驗室人員開放-三層盤點與計畫-review.md`。
>
> 對應 `MIGRATION_PLAN_POSTGRES.md` 延後項 P5.2（per-key 角色）與 D（雙SB lab ACL/onboarding）。
> **主要適用**：**sb-lab 實例**（多人）。sb-personal 維持單人，多數階段對它是 no-op。
> ⚠️ 注意：**目前並沒有 sb-lab 這個 SB 實例**（`sb_lab` DB 在 `bar-pg`:5433 給 BAR 用）；跑著的是
> sb-personal(9100) / lcdda(9104) / lcdda-harvest(9106)。而 PWA 計畫把 vault 列為「personal + lcdda」，
> 與本文「個人 DB 永不上工作主機」的硬約束衝突——見 review D2。
> **執行順序**：P1 → P2 → P3 →（P4 / P5 可獨立）。每階段可回退。

---

## P0 — 前置 Spike（2026-06-17 已確認）

兩個實作前必解的 🔴 阻塞點，已查證：

### Spike S1：身分傳遞機制

**問題**：`auth.py` 是純 ASGI middleware，FastMCP `@mcp.tool()` 函式預設拿不到 HTTP scope/headers，需確認 contextvar 能否跨 FastMCP 分派傳入。

**查驗方式**：讀 `mcp.server.fastmcp.utilities.func_metadata.FuncMetadata.call_fn_with_arg_validation` 原始碼。

**結論 ✅**：FastMCP 對 sync 工具直接呼叫 `fn(**args)`（無 `run_in_executor`、無 `anyio.to_thread`、無 `create_task`），所以 `contextvars.ContextVar` 在 ASGI middleware 設定後，會在同一 async task 內完整傳遞到 tool handler。

**方案**：
- 新增 `mcp_second_brain/identity.py`：`Identity` dataclass（`user_id`, `role`）+ `_current ContextVar` + `set_identity()` / `get_current_identity()` / `hash_key()`。
- `APIKeyMiddleware.__call__` 驗 key 後呼叫 optional `lookup_fn(key) → Identity | None`，設 `set_identity(identity)`，再轉發請求。

### Spike S2：P2「統一 call_tool 出口」修正

**問題**：原 P2 描述「在 `call_tool` 統一出口檢查」對 SB **錯誤**——SB 用 FastMCP `@mcp.tool()` 裝飾器（server.py ~35 個），無低階 `call_tool` 攔截點。

**修正方案**：改為在每個 write 工具函式開頭呼叫 `_check_write_permission()` helper（從 `get_current_identity()` 讀取，`role == 'reader'` 時拋 `PermissionError` → 回傳 403 訊息）；或先做 audit-only（記錄不擋）灰度。P2 描述已在下方對應修改。

---

## 0. 現況盤點（已查證 2026-06-17）

| 面向 | 現況 | 檔案/證據 |
|---|---|---|
| 認證 | 多把 key（`SB_API_KEY` + `SB_API_KEYS` 逗號分隔），無 key→401 | `auth.py` |
| 身分/角色 | **無**——任一有效 key = 完整存取，匿名共享密鑰 | `auth.py` `_key_accepted` 只比對 key 集合 |
| 唯讀強制 | **無**——無法限制成員只讀 | 同上 |
| 寫入歸屬/稽核 | **無** `created_by`/`modified_by`/audit | `postgres_store.py` grep 無 actor 欄 |
| 連線池 | psycopg `ConnectionPool` min 1 / **max 10** | `postgres_store.py:65` |
| 寫入模型 | 全寫入經中央 server 單一 funnel；client Obsidian 唯讀 | `AGENTS.md` |
| 中文 FTS | `pg_trgm`（trigram，較粗，不斷詞） | `MIGRATION_PLAN_POSTGRES.md` §中文 FTS 陷阱 |

**判斷**：並發正確性已由 Postgres MVCC 解決，**補強重點在「權限層」非「再加並發」**。別把力氣花錯地方。

## 1. 硬約束（不可違反）

- Postgres 只綁 `127.0.0.1`，永不對外（沿用既有）。
- **個人理財/個人 DB 永不上工作主機**；多人僅針對 sb-lab 實例。
- markdown 是正本，DB 是可重建索引——權限層**不得**改變這條。
- 既有測試（190+）全程保綠；測試一律對 `sb_test`。

---

## P1 — per-key 身分 + 角色（RBAC 地基）⭐

把匿名共享 key 變成「key → {user_id, role}」。

- **角色**：`reader` / `writer` / `admin`（admin 含金鑰管理）。
- **儲存**：Postgres 新增 `api_keys(key_hash, user_id, role, created_at, revoked_at)`；**存 key 的 hash 不存明文**（多人安全前提）。
- **auth.py 擴充**：驗 key 後解析 → identity，掛進 ASGI request scope 供下游取用；保留純 env key 為 admin/back-compat。
- **DoD**：無效 key→401；有效 key 解析出 `(user_id, role)` 並可在 tool handler 取得。
- **驗證 V1**：單元測試 key→identity 對映；撤銷的 key（`revoked_at` 非空）→401；明文不入庫（檢查欄位為 hash）。
- **安全檢查**：確認 key 比對為**常數時間**（`hmac.compare_digest`），非 `==`。

## P2 — 授權：唯讀強制 ⭐（多人最重要的安全項）

依角色擋寫入。

> ⚠️ **P0 S2 修正**：FastMCP 無低階 `call_tool` 攔截點；改用 `_check_write_permission()` helper，在每個 write 工具函式開頭呼叫。

- **工具分類**（server.py ~35 個工具）：
  - **write**：`new_note`、`update_note`、`save_article`、`append_to_note`、`mark_note_status`、`vault_sleep`、`consolidate_tool`、`update_links_tool`、`expand_semantic_keywords_tool`、`enrich_neighbor_keywords_tool`、`prune_archive_tool`、`snapshot_note_tool`、`annotate_figure`
  - **read**：`search_notes`、`read_note`、`get_context`、`find_related`、`search_figures`、`read_figure`、`extract_figures_for`、`get_agent_instructions`、`index_stats`、`search_news_tool`、`extract_rules_tool`
- **強制點**：`_check_write_permission()` helper — 讀 `get_current_identity()`，`role == 'reader'` 時拋 `PermissionError`（回傳 "403: read-only access" 字串，不中斷 server）。
- **灰度策略**：先 audit-only（只 log，不擋），確認矩陣正確後再開強制（對應 `§風險與回退`）。
- **DoD**：reader key 無法觸發任何 write 工具；writer/admin 可。
- **驗證 V2**：`(role × tool)` 矩陣測試，逐格驗 allow/deny 與預期一致（這是本案最關鍵的回歸測試）。

## P3 — 寫入歸屬 / 稽核

- **欄位**：寫入路徑帶入 identity → 記 `modified_by` + `modified_at`（notes 既有 `last_accessed` 可比照加欄）；或獨立 `audit_log(ts, user_id, tool, target, action)`。
- **DoD**：每次 write 留下 actor + 時間，可查詢；無歸屬的 write 視為缺陷。
- **驗證 V3**：經某 user key 寫一筆 → audit/欄位有正確 actor；偽造/缺 identity 的寫入被擋或標 `unknown`。

## P4 — 金鑰生命週期 + pool 調校（維運）

- **per-key 撤銷/輪替**：admin 工具或 SQL 設 `revoked_at`，立即失效，不影響他人。
- **key→人對映文件**：記錄哪把 key 給誰（不含明文）。
- **pool**：依實際人數調 `max_size`（>10 時）；加每 key 速率限制（選做）。
- **DoD**：撤銷單一 key 後該人即 401、他人不受影響；pool 不被人數打爆。
- **驗證 V4**：撤銷測試 + N 並發連線壓測不耗盡 pool。

## P5 — 中文 FTS 精度（zhparser / pg_jieba，獨立、可選）

承 `MIGRATION_PLAN_POSTGRES.md` 結論：`pg_trgm` 對中文較粗。多人都靠搜尋時才值得修。

- **做法**：裝 `zhparser` 或 `pg_jieba` → 中文走斷詞 tsvector，英文續用既有；或與 pg_trgm 混合。
- **DoD**：中文關鍵字 recall 明顯優於 pg_trgm baseline。
- **驗證 V5**：固定一組中文 query，比 pg_trgm baseline 的 hit@k；同時量與向量結果的 top-K 重疊不退化（沿用 migration 的 93% 對照法）。

---

## 風險與回退

- **權限誤擋**：P2 上線後若 writer 被誤判 reader 會擋掉正常寫入 → 先以 `audit-only`（記錄但不擋）灰度一輪，確認矩陣正確再開強制。
- **金鑰外洩**：key 存 hash + 常數時間比對 + 可單獨撤銷；明文僅發放時出現一次。
- **回退**：未設角色時預設 `admin`（back-compat），等同現況；P5 可 `ER`…（不適用）→ 改回 pg_trgm 即復原。
- **正本安全**：全程不動 markdown 與 DB 的「正本=檔案」關係。

## 執行順序與 Sonnet 鐵則

```
P1 per-key 身分+角色（地基）
  → P2 唯讀強制（先 audit-only 灰度 → 再強制）
  → P3 寫入歸屬/稽核
  → P4 金鑰生命週期 + pool
  → P5 中文 FTS zhparser（獨立、可選）
```

> **給執行者（Sonnet）的鐵則**：① 每階段一個 commit；② 全程 190+ 測試保綠、測試對 `sb_test`；③ key 一律存 hash + `hmac.compare_digest`，**禁止**明文入庫或 `==` 比對；④ P2 先 audit-only 灰度再開強制，避免誤擋寫入；⑤ 不改 markdown 正本關係、Postgres 維持 localhost-only；⑥ 多人僅針對 sb-lab，勿動個人 DB 上工作主機的硬約束。

## 相關
- 並發基建（前置，已上線）：`MIGRATION_PLAN_POSTGRES.md`（含 D 雙SB 段、P5.2 per-key 角色延後項）。
- ADR：`../../second-brain/decisions/multi-machine-central-brain-architecture.md`。
