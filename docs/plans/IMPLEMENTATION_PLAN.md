# lcdda 多人安全、觀測與佇列執行清單

日期：2026-09-12。SB 正本：`10-projects/lcdda-實驗室開放-人員管理系統與個人私有-note-實作計劃書.md` §15–17。歷次執行證據保留在 [execution_trace.md](execution_trace.md)。

## 授權與完成定義

本輪為本機程式實作、合成資料／owned disposable PostgreSQL 驗證、離線候選包與 SB 記錄。未 commit/push/merge、安裝正式套件、改正式 DB/plist、啟動正式排程、發 key 或呼叫付費模型。**本機驗收完成與正式開放是兩個不同狀態。**

Second Brain 基線為 `b701d482014c184ebd0bacbf643694d304ef14e6`，lcdda-ingest 基線為 `47d11d0e49805a04022ef2f00a65afff80a2bea1`。工作分支仍含未提交修改；基線 commit 不包含這些修改。lcdda 的 source 是 projects checkout，Downloads 為既有 runtime，未修改。

## 本機實作狀態

| 工作 | 本機交付與驗收 | 狀態 |
|---|---|---|
| T0 來源／規範 | SB 計畫、repo 規範、source/runtime 分離 | 完成 |
| T1 PG 測試護欄 | 外部 test DSN 拒絕；owned container；非 owner/NOSUPERUSER/NOBYPASSRLS、FORCE RLS startup | 完成 |
| T2 文獻／私有資料 | 共用 uploader/contributor UUID、原子去重、metadata＋單篇 vectors/chunks＋PDF 頁面圖片、B 可查讀且 A private 不外洩 | 程式與隔離 E2E 完成；真模型品質另驗 |
| T3–T5 事件 | 全 45 工具註冊；44 工具可在來源標記結果、無法判讀的分支保留 UNKNOWN；去敏 bounded sink、PG RLS、30/180 日 retention | 完成 |
| T6 報表／效能 | admin 報表與 query window；共用 request deadline/embedding memo；40 分鐘矩陣＋30 分鐘 mixed | 合成 gate 完成；非正式容量承諾 |
| T7 故障／還原 | 真 PG dump/restore、RLS/owner/附件 hash 回驗；崩潰／reconcile、DB lock/pool deadline、取消／撤權 | 完成；5 分鐘混合故障同場驗收通過 |
| P0 私人 fallback | 零結果／故障不掃全 vault；private index/links/graph/figures 隔離；log 去敏 | 完成 |
| P2／P3 匯入 | 真身分橋接、四工具 HTTP、durable fair queue、唯一 supervisor、固定 worker、未知 outcome 不重播 | 本機接線與測試完成 |
| S 資源／觀測 | 有界 admission/body/upload、300s/1GiB worker watchdog、60s PDF 計頁／轉換、heartbeat、disk/readiness/artifact pin、admin operations | 完成；正式監控接線另列 |

PDF 圖片是受限大小的**文件頁面影像**，並非已執行語意圖像抽取、OCR 或 Vision。Vectors/chunks/figures 各自回報 complete/pending/disabled；document 成功不代表所有後處理都完成。Enrichment 預設 off；啟用時僅受控 localhost 模型，無 redirect、proxy、外部 fallback 或自動啟動。

Upload 的 HTTP deadline 回覆 `408/status=unknown` 時，已開始的同步檔案 I/O 仍保留容量直到結束；開始 finalize 的操作可能晚完成，不能宣稱 rollback。Supervisor/HTTP 關機都保留 accepted 工作與未知結果證據。

## 可重現證據

完整數值見 [acceptance-evidence.json](acceptance-evidence.json)、[load-results.json](load-results.json)、[load-mixed30-results.json](load-mixed30-results.json)。

- SB 一般 suite：950 passed、97 skipped；PG opt-in 另跑，不把 skip 當通過。
- 真隔離 PG 綜合 suite：125 passed，含 shared PDF queue E2E、圖片隱私重跑、保留期限 CLI、還原與 deadline。與一般 suite 有重疊，不相加。
- lcdda 一般 suite：311 passed；離線 packaging：3 passed。
- 1/5/10/20 clients 固定 20 query/s，每個 query-only/mixed phase 各 300 秒；mixed/query-only p95 比值依序 1.300/1.029/0.973/0.984，全低於 1.5；submit p95 22–24ms。
- 30 分鐘 mixed：36,000 queries、1,800 ingests、1,799 duplicates；query p95 1.581ms、submit p95 79.992ms；errors/timeouts/event loss/actor isolation failures 均 0。
- Telemetry A/B 各 200 warmup＋200 samples，p95 overhead 0.030ms，小於 20ms 門檻。
- 5 分鐘混合故障：3,000 queries，含慢 query、DB adapter unavailable、sink failure、queued credential 撤銷與 dead worker reconciliation；1 passed，所有 accepted jobs 保留、零跨人結果、slots/sink 有界排空。見 [load-fault-results.json](load-fault-results.json)。故障為 in-process 合成注入；真 PG/HTTP/worker restart 由獨立測試覆蓋。
- 長跑使用的 10 個核心 source/harness SHA-256 在結束後均讀回一致。
- 以上負載使用真 MCP wrapper/dispatcher/sink/queue/workspace/shared commit，retrieval/resolver/index 為合成替身；沒有量測正式 PostgreSQL、HTTP/auth、模型、NAS 或真下載效能。真 PG、ASGI、子程序與還原由另列測試驗證。
- 新／修改範圍 Ruff、lcdda 全套 Ruff 與兩 repo diff whitespace 通過。SB 全 repo 仍有舊 lint 債；未擴修無關檔案。既有 ffmpeg discovery 與 pool deprecation warning 保留。

## 發布候選與操作入口

候選 manifest：`/Users/lab_center/projects/lab-multiuser-candidates/2026-09-12-local-01/manifest.json`。兩個 wheel 在新隔離 venv 離線安裝，從 wheel 載入，server/worker artifact 相同。第三方 dependencies 從已驗 test site 重用，**未宣稱獨立重建了 dependency lock**。候選為 dirty source 的明確 SHA snapshot，非已發布 commit。

管理員操作（先 preview，key 由有界 stdin，DSN 不放 argv）：

- `python -m mcp_second_brain.query_event_report --key-stdin`
- `python -m mcp_second_brain.query_event_maintenance --key-stdin`；需獨立 maintenance DSN，`--apply` 才有界 retention。
- `python -m mcp_second_brain.article_maintenance --key-stdin`；`--apply` 才修 pending text index。
- `python -m lcdda_ingest.managed_maintenance --key-stdin`；`--apply` 只清合資格舊 terminal staging，不等於 queue archive。
- managed `/health/operations` 僅 admin，顯示快取 aggregate queue age/count、worker heartbeat、版本與 storage；不含 job/owner/source 清單。
- `operational_alerts` 是無副作用評估器：queue/p95/timeout/heartbeat/storage/event loss/backup 採持續異常、單次恢復；閾值必須明確提供，missing backup/model/NAS 證據保留 unknown。尚未啟用正式排程或外部通知。

## 正式發布前仍需驗收

依 [RELEASE_CANDIDATE.md](RELEASE_CANDIDATE.md) 審閱確切 diff／候選、dependency lock、RLS migration 與相容回退，再決定 merge/push／正式切換。需在部署環境測實際模型品質、NAS/backup/restore、連線數與 p95，才能決定人數、SLO、RPO/RTO。正式服務/proxy/log rotation/監控與備份狀態 adapter 仍需部署接線；backup unknown 不得視為成功。

正式名單、角色、roster apply、key 發放與多人切換尚未執行。本文沒有把這些核准階段勾成已完成。
