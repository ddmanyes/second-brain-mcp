# HOUSEKEEPING.md — Vault 維護紀錄

> 現役筆記整理（尤其 `20-areas/personal/finance/` 的時效性個股報告）的運作機制、
> 已修 bug、以及待辦。操作 SOP 見 [AGENTS.md](AGENTS.md) §C-bis。

## 時效性個股報告的三層整理機制

1. **當日繼承（THESIS carry-over）** — 每份 `*_analysis_*.md` 底部的「投資假設區塊」，
   隔日新報告自動繼承，讓投資判斷跨日延續追蹤。
2. **每檔留最新 + 月度歸檔（`vault_janitor.py`）** — 每個 ticker 只保留最新一份於現役區，
   舊的搬到 `40-archive/finance/YYYYMM/`。
3. **睡眠壓縮（`vault_sleep` / run_sleep.py）** — 老舊、低訪問筆記做語義壓縮 / 整併。

## 2026-07-23 清理紀錄

**背景：** 現役 finance 資料夾爆到 598 檔（523 為 7 月）都沒歸檔。

**根因：** `vault_janitor.py` 的 `_STOCK_ANALYSIS_RE` 舊 pattern 只吃純代號
（`GOOG_analysis_…`），不吃含中文名的新命名（`2890.TW_永豐金_analysis_…`）→ 那類報告
永遠比對失敗、不被歸檔。加上所有 launchd 排程處於 `.disabled`，自動維護根本沒在跑。

**已做：**
- ✅ 修 regex → `^([A-Z0-9\.\-]+?)(?:_.+?)?_analysis_(\d{8})\.md$`（抓前導 ticker、容忍中文名段，
  純代號與中文名變體歸同組）。9 個代表性檔名單元測試通過。
- ✅ 帶正確 `SECOND_BRAIN_PATH` 執行 `--execute`：現役 analysis MD 468→64，歸檔 410 檔進 202607。
- ✅ companion sweep 處理 JSON 快照：101→63（每 ticker 留最新），歸檔 38 檔。
- ✅ vault 根目錄 3 個散落 analysis 檔（GOOGL_20260713、2327.TW_20260702、2359.TW_20260609）歸位。
- ✅ 最終：現役 finance `.md` 598→188（64 分析 + 91 簡報 + 33 其他），`202607/` 歸檔 450 檔，0 碰撞。

## 待辦（Outstanding TODO）

- [x] **每日簡報保留規則（2026-07-23 完成）** — janitor 新增 Task 1b `archive_old_briefings`：
      保留近 `BRIEFING_KEEP_DAYS=14` 天、其餘按月歸檔。已執行：91→25 份現役（範圍 07/09–07/23），
      66 份歸檔（含 1 個 Drive 衝突副本 `..._20260701 2.md`）。
- [x] **JSON 納入 janitor（2026-07-23 完成）** — `archive_old_stock_analyses` regex 加 `\.(md|json)$`，
      按 (ticker, ext) 分組各留最新，`.md` 報告與 `.json` 快照一起歸檔。
- [x] **Task 4 schema 檢查（2026-07-23 完成）** — 與 Task 5/6 一起 gate 在 `_DUCKDB_MAINT_ENABLED`；
      非 DuckDB 後端時略過（避免回過時 DuckDB 資料）。
- [x] **修 script-mode import bug（2026-07-23 完成）** — `from . import vault_db` 改為 lazy 雙模式
      helper `_import_vault_db()`（package 用 relative、bare script 用 top-level，sys.path 已含本檔目錄）。
- [x] **修「修好 import 後暴露的 DuckDB abort」（2026-07-23 完成）** — sync_all 撞 DuckDB PK 重複
      `"AGENTS.md"` → 不可攔截的 C++ FatalException 直接 abort 整個 janitor。根因：janitor 的
      Task 5/6（sleep 候選 + sync）**寫死走 DuckDB**（`~/.second-brain/vault.db`，停在 7/16 的過時
      fallback），完全不理 `SB_DB_BACKEND`；且這兩任務因舊 import bug 從來沒成功跑過。修法：gate 在
      `SB_DB_BACKEND=duckdb`（`_DUCKDB_MAINT_ENABLED`），否則乾淨略過（production 索引由 Postgres
      pg-sync 維護）。janitor 現在 EXIT=0。
- [x] **DuckDB PK 重複 bug 排查（2026-07-23 結案：非程式 bug）** — rebuild `~/.second-brain/vault.db`
      （移除舊 db + abort 遺留的髒 `vault.db.wal`，重跑 `sync_all`）→ 乾淨成功 `synced:1421, embed_failed:0`
      57s，**0 崩潰**；再跑一次 0.7s 仍乾淨。證明 `ON CONFLICT` upsert 正常、可冪等，先前崩潰純粹是
      **前次 abort 留下的損壞 db/WAL 狀態**。舊 db 備份於 `vault.db.bak-20260723`、WAL 備份 `vault.db.wal.bak-20260723`。
      教訓：DuckDB 若被 C++ abort 中斷，殘留 WAL 可能導致下次 replay 撞 PK → 需刪 db+wal 重建。
- [x] **中央主機 launchd 排程已啟用（2026-08-29）** — mac-mini（Tailscale
      `<tailscale-ip>`）是唯一寫入主機；client 維持 HTTP MCP 連線，不啟動本機 server 或維護排程。
      `com.user.vault-janitor` 每日 07:30 以 `--push --execute` 執行，
      `com.user.vault-sleep` 每週日 02:00 執行，最近一次 exit code 均為 0。
- [ ] **（可選）janitor conflict-copy 韌性** — 目前 regex 不吃 Drive 衝突副本（`..._20260701 2.md`、
      `... (1).md`）。已手動清 finance 目錄；如要根治可讓 regex 容忍 ` N` / ` (N)` 後綴或加獨立清理。

## 相關

- 記憶：`feedback-sb-vault-janitor`
- 遷移狀態：mac-mini 已是正式中央主機；client 僅透過 HTTP MCP 存取
