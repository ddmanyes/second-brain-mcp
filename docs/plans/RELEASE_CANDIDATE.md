# 多人 managed intake 本機驗證與發布候選 Runbook

狀態：**0.3.0 包裝與獨立依賴 gate 已通過；正式候選仍待 clean-commit 重建與環境驗收**
日期：2026-09-12  
本文件只描述候選建立、隔離驗證、部署驗收與 rollback。它不授權 commit、merge、push、安裝正式套件、修改正式 DB／service／key、啟動排程或呼叫付費模型。

## 1. 候選來源與邊界

| 項目 | 本機觀察值 | 發布候選證據 |
|---|---|---|
| Second Brain source | `/Users/lab_center/git-repos/second-brain`；分支 `feat/lab-multiuser-private-notes`；基線 HEAD `b701d482014c184ebd0bacbf643694d304ef14e6` | 候選 commit：`[待填]` |
| lcdda-ingest source | `/Users/lab_center/projects/lcdda-ingest`；分支 `feat/exclude-private-notes-from-litnet`；基線 HEAD `47d11d0e49805a04022ef2f00a65afff80a2bea1` | 候選 commit：`[待填]` |
| lcdda runtime/data | `/Users/lab_center/Downloads/lcdda-ingest` | runtime 快照／備份：`[待填]` |
| canonical vault | `/Users/lab_center/lcdda` | 備份與還原演練：`[待填]` |

兩個 source checkout 目前都有大量未提交及未追蹤檔案，基線 HEAD 不包含本輪功能，不能當發布版本。候選必須先由 reviewer 確認精確 diff，再各自形成不可變 commit。建立候選、安裝及執行服務時不得把 source checkout、Downloads runtime 與 canonical vault 混為同一目錄。

## 2. 候選 artifact

正式 merge／push 前先在乾淨、由候選 commit 匯出的暫存目錄完成以下項目：

1. 正式發布分別從已審閱乾淨 commit 建立兩個 wheel。本輪先建立明確列出逐檔 SHA 的 dirty-source 隔離 snapshot，供本機驗證；它不是正式發布 commit。
2. 對每個 wheel 計算 SHA-256，建立只含檔名、版本、候選 commit、SHA-256、Python 版本與建置命令的 manifest。
3. 在全新隔離 venv 安裝兩個 wheel 與 `uv.lock` 的 runtime set，且不得以 `.pth` 或 `PYTHONPATH` 共用 test site。本輪已移除與 `markitdown==0.1.7` metadata 衝突的全域 `magika>=1.0.0` override，將 Python 支援誠實限定為 3.11–3.13，並解析到相容的 `magika==0.6.3`；版本化 venv 的 `pip check` 及本機 Markdown/PDF 轉換已通過。確認 `lcdda-ingest` 不會因 Second Brain 未在 PyPI 而偷偷改變核心 dependency contract。
4. 從不含 source checkout 的工作目錄執行 packaging smoke、module import 與 managed health probe，證明實際載入 wheel 內容。
5. server 與 worker 必須使用同一個 venv/interpreter。記錄 server 算出的 `lcdda_ingest.managed_health.artifact_revision()`，再以 `ManagedSettings.worker_environment()` 呼叫 `verify_worker()`；兩者必須完全相同。
6. 啟動後抽查一個 worker 的 interpreter、已安裝 wheel SHA 與 `LCDDA_ARTIFACT_REVISION`。任一不一致即停止 admission，不得以調整 `PYTHONPATH` 指回 source checkout 規避。

| Artifact 證據 | 值 |
|---|---|
| Second Brain wheel / SHA-256 | 中繼 dirty snapshot `mcp_second_brain-0.3.0-py3-none-any.whl` / `21c7a3deb8f77dcfb75cfe814126ecd438f3c1a713ad5d038a390a9111df8874`；後續安全修改已使它過期，不得當正式 artifact |
| lcdda-ingest wheel / SHA-256 | 中繼 dirty snapshot `lcdda_ingest-0.3.0-py3-none-any.whl` / `5a5d9b3681194c0de8573174d3d13750f1fd1f28b9f640fa1cb90b406bb0d1a7`；必須與 clean-commit SB wheel 一起重建驗證 |
| Python / platform | Python 3.12.13 / macOS 26.6.2 arm64；見中繼 manifest |
| server artifact revision | 中繼 probe：`35f4fc1810151e51d40780a910de427dea2fe87770938c664943e10a1c05bda6`；後續安全修改已使它過期 |
| worker artifact revision | 中繼 probe 與 server 相同；clean-commit wheel 仍須重跑 |
| wheel smoke output | 中繼 probe：`wheel_imports=true`、`versions_match=true`、`auth_context_registered=true`、`query_event_migration_packaged=true`、12 個 managed modules 可從新 venv 匯入、`worker_revision_match=true` |
| 獨立 runtime venv | `/Users/lab_center/.venvs/lcdda-0.3.0-20260912`；135 packages；無 `.pth`/source checkout；`pip check` exit 0；本機 Markdown/PDF 轉換通過且未呼叫外部模型 |

## 3. 隔離驗證環境

所有發布前測試使用獨立 tmp workspace、tmp vault、拋棄式 PostgreSQL 與合成 UUID。環境不得包含正式 API key、正式 DSN、正式 vault 路徑或外部模型憑證。網路、付費模型、正式 NAS 及正式服務均保持停用。

測試順序如下；任何一項失敗都停止候選：

1. Second Brain 一般測試（DuckDB、embedding disabled、移除模型與正式 DB 環境變數）。
2. lcdda-ingest 一般測試與離線 wheel packaging 測試。
3. owned disposable PostgreSQL 測試，包括 RLS、身份撤銷、event schema、pool/statement deadline 與 rollback fixture。
4. 真 ASGI composition：raw key 只在 middleware 邊界存在，queue 只持久化 credential hash；兩位 actor 的 submit/status/cancel 不串人，admin 行為另驗。
5. 真 worker subprocess：queue admission → supervisor lease transfer → worker 再驗 credential → shared article commit；取消、撤權、timeout、RSS 超限與 worker crash 均留下可審閱 terminal/interrupted 狀態。
6. 固定速率長負載完成後才填入結果；不得把短跑或單元測試描述為長負載通過。

| Gate | 命令／artifact | exit code | 結果 | reviewer |
|---|---|---:|---|---|
| SB 一般 suite | 見 execution trace 完整環境命令 | 0 | 950 passed / 97 skipped | 主代理 |
| lcdda 一般 suite | SB test interpreter + LC tests | 0 | 311 passed；packaging 另 3 passed | 主代理 |
| 隔離 wheel | 中繼 manifest / wheel-probe.log | 0 | 雙 wheel 版本、auth_context、telemetry migration、managed imports 與 worker revision 通過；dirty snapshot 已被後續安全修改取代 | 主代理 |
| 版本化 venv 依賴 | `versioned-venv-verification.json` / `uv pip check` | 0 | 135 packages 相容；`markitdown 0.1.7` + `magika 0.6.3`；Markdown/PDF 本機轉換通過，無外部模型 | 主代理 |
| disposable PG/RLS | 12 檔 --run-postgres suite | 0 | 125 passed（含 E2E/restore/retention） | 主代理 |
| ASGI composition | managed HTTP/server suite | 0 | 含 311 suite；upload deadline/late-finalize/ops 拒絕 | 主代理 |
| worker/lease/RSS | heartbeat/watchdog/owned-worker suite | 0 | 含 311 suite；真 macOS subprocess 測試 | 主代理 |
| 40 分鐘矩陣負載 | `load-results.json` | 0 | 8 phases，全數符合 ratio/submit/隔離 gate | 主代理 |
| 30 分鐘 mixed soak | `load-mixed30-results.json` | 0 | 36,000 queries / 1,800 ingests，零錯誤/事件遺失/串人 | 主代理 |
| 5 分鐘 mixed faults | `load-fault-results.json` | 0 | 3,000 queries；expected failures、撤銷/recovery、保留 accepted、drain | 主代理 |

長負載報告必須標示 synthetic retrieval/resolver/index，並包含各階段 clients、固定 target rate、query p50/p95/p99、throughput、errors、timeouts、busy、event loss、actor isolation、duplicate/count、queue wait、submit、job run、RSS 與 CPU。它不能產生或暗示 PostgreSQL、模型、NAS 或正式網路指標。

## 4. PostgreSQL 與 RLS 啟動拒絕

Schema 變更只能由另行核准的 migration 身分執行；服務使用的 application role 必須是低權限、`NOSUPERUSER`、`NOBYPASSRLS`，且不可是受保護資料表 owner。服務 DSN 不寫入本文件、wheel、shell history 或測試輸出。

正式啟動前，在 staging DB 留存以下查核結果：

- `notes`、`note_chunks`、`figures` 與 query event 表的 schema、policy、grants 均符合候選 SQL。
- application role 無 schema DDL、DELETE/UPDATE telemetry、BYPASSRLS 或 owner 權限。
- `SB_MULTIUSER=1` 且 `SB_RBAC_ENFORCE=1` 下，`PostgresStore` 只用低權限 role 成功啟動。
- 依序以「RLS 未啟用」、「policy 缺失」、「application role 為 owner」、「具有 BYPASSRLS／superuser」的隔離 fixture 啟動；每一種都必須 fail closed，且錯誤輸出不含 DSN。
- 兩位 member、reader、writer、admin 的 read/write/共享文獻與 private note 可見性符合預期；撤銷 credential 後 intake bridge 與 worker checkpoint 都拒絕。

證據：`[待填]`。

## 5. Local enrichment 與 readiness

`LCDDA_LOCAL_ENRICHMENT` 預設及首波發布都保持 `0`。此時文件 commit 可完成，但 chunks、vectors、figures 必須依實際結果回報 `pending` 或 `disabled`，不得用 document/text index 完成推論其他 readiness 已完成。

後續若另行核准啟用 enrichment，必須同時滿足：

- 設定值明確為 `1`，並提供絕對路徑、可執行的受控 PDF renderer；部署檢查另驗其版本 pin。
- embedding/chunk endpoint 的 scheme 為 `http`，host 僅可為 `localhost`、`127.0.0.1` 或 `::1`；不可有外部 fallback、member 自選 endpoint 或程序自動啟動。
- 用正式預計採用的本機 embedding/chunk 模型建立具答案標註的代表性 corpus，量測 retrieval recall、排序品質、空結果與跨人隔離。
- NAS 上的實際 PDF、page image、chunk/vector 寫入、容量、權限、延遲、故障與恢復均完成演練。

目前真實向量品質、模型選型與 NAS 路徑尚未驗證，所以 enrichment 不得在首波候選開啟。

Readiness 驗收必須證明：workspace/vault 可用空間高於門檻、DB dependency probe 成功、worker artifact 相同、snapshot 未過期；任一失敗時 `/health/ready` 回 503，submit/upload_prepare 拒絕，既有 status/cancel 仍按授權可讀取或處理。

## 6. Queue、telemetry 與維護預覽

啟動前先保存 queue/job/lease/upload/staging 的唯讀清冊與備份位置。驗收至少涵蓋：

- Queue 保持既定全域 `capacity=50`、每 owner `per_owner=5`，公平 dispatch；不得為通過負載測試而提高上限或刪除歷史。
- Supervisor 只啟動固定的 `python -m lcdda_ingest.managed_worker`，環境使用 allowlist；worker 等待並核對自身 PID lease，commit 前再次驗 credential/cancel。
- `succeeded`、`duplicate`、`failed`、`cancelled`、`needs_review` 與 `interrupted` 不互相假冒；未知 outcome 不自動重播。
- Readiness 逐項呈現 `document`、`text_index`、`chunks`、`vectors`、`figures`，pending 不得顯示為 complete。
- Telemetry 明確 opt in；只寫固定 allowlist 欄位與 UUID，不含 query、path、header、結果、例外文字或 raw key。檢查 sink accepted/written/failed/overflow/rejected/dropped/pending 與 callback failure。
- Event retention 先跑 `python -m mcp_second_brain.query_event_maintenance --key-stdin` 的獨立 maintenance DSN 唯讀 preview；DELETE 維護需另外的 privileged maintenance 身分與核准，不得交給 application role。
- Staging 維護先跑 `python -m lcdda_ingest.managed_maintenance --key-stdin`（preview）。人工核對結果只含 job ID、count、固定 code，且只命中超過 30 天、PID 不活、無 matching lease、無 active upload reservation 的 terminal job。symlink、scan/file/byte 超限與不明狀態全部保留。
- Queue archive 也先 `archive_terminal(..., apply=False)`；staging cleanup 與 job archive 的 apply 分別取得核准，不新增 scheduler。背景 log rotation 由部署 runbook 配置並驗證，不由應用程式暗中啟用。

維護 preview 證據：`[待填]`。首波部署不得執行 maintenance apply。

## 7. 精確部署驗收（需另行核准後執行）

1. 凍結兩個候選 commit、wheel manifest 與測試證據；reviewer 簽核精確 diff。
2. 備份 runtime 設定、queue/jobs/uploads/staging、DB schema/grants/policies 與 canonical vault；記錄可讀取的還原位置和校驗值。
3. 停止舊服務的 admission，等待既有請求有界 drain。若 worker 尚在執行，保留 job、lease、staging 與 upload，不強制重播或清理。
4. 由 migration gate 套用已審閱 SQL；以低權限 application role 執行正向及負向 RLS 啟動檢查。
5. 將兩個已核 SHA-256 的 wheel 安裝到新的版本化 venv。不要原地覆寫舊 venv，也不要從 source checkout 啟動。
6. 以明確設定啟用 `LCDDA_MANAGED_INTAKE=1`、`SB_MULTIUSER=1`、`SB_RBAC_ENFORCE=1`、絕對 workspace/vault 路徑；`LCDDA_LOCAL_ENRICHMENT=0`。secret 由受控 secret store 注入，不載入 legacy plist。
7. 先在未接收流量下執行 worker artifact、DB、storage 與 readiness probe。失敗即 rollback。
8. 服務先綁定部署核准的 loopback endpoint；外部入口、TLS、proxy、firewall、body limit 與 request timeout 必須有獨立審閱設定。不可因預設值存在就視為已保護。
9. 使用隔離 staging credential 進行：live/ready、身份拒絕、owner status/cancel、撤銷、dry-run URL、受控 upload hash、queue capacity/fairness、lease handoff、shared attribution及 readiness smoke。不可使用正式研究資料。
10. 驗證 raw key/DSN/query 未出現在 job、event、log、response 或 staging；確認 telemetry loss counters 與 callback failures 為可接受值。
11. 做一次受控 service restart；確認 queued 保留、live external PID 不被誤殺、dead worker 標 interrupted、未知 commit outcome 不重播。
12. 觀察期結束後再由人決定是否開放正式 member、發 key 或啟用維護 apply；這些都是新的 gate。

部署時間、執行人、核准人與逐步輸出：`[待填]`。

## 8. Rollback

觸發條件包括：RLS startup check 失敗、artifact mismatch、readiness 持續 503、身份串用、raw secret/query 洩漏、queue/lease 不一致、未知 commit outcome、event loss 超標、worker deadline/RSS guard 失效，或任何未分類資料寫入。

Rollback 步驟：

1. 立即停止新 admission；保留 health/status 的診斷證據。不要刪 queue、job result、lease、upload、staging 或 vault 檔案。
2. 有界停止新服務。對仍在執行的 worker 記錄 PID/job ID；讓其自身 deadline/RSS watchdog 處理，不把不明結果改成成功或重新排隊。
3. 切回上一個版本化 venv／已驗 SHA-256 wheel 與上一份設定快照；再次確認不會由 source checkout 或錯誤 `PYTHONPATH` 載入模組。
4. DB schema 只按事前審閱的 backward-compatibility 結論處理。沒有已演練 down migration 時不得 DROP table/policy/column；先保持 managed service disabled，再由 privileged DB gate 決定修復。
5. 不回收或重發 key；若 credential 安全性受影響，另走 revoke/rotate gate。不要把 env admin fallback 當作復原捷徑。
6. 以舊版執行 artifact、低權限 RLS、storage/readiness 與唯讀 queue reconciliation。只有證據一致才恢復 admission。
7. 對新版本留下的 `interrupted`、`needs_review`、index pending、staging 與 upload reservation逐筆人工判定；先 preview，禁止批次刪除來製造綠燈。

Rollback 演練證據與 RTO/RPO：`[待填]`。

## 9. 尚未補齊的部署項目

- 舊 0.2.0 證據已過期。0.3.0 中繼 dirty snapshot 已證明 packaging/import 路徑，但後續安全修改使其不再對應目前 source；仍須由審閱後 clean commit 重建正式候選。
- 獨立版本化 venv 已依 `uv.lock` 安裝 135 個 runtime packages 與雙 wheel，`pip check`、Markdown/PDF 本機轉換及 import smoke 均通過。Git 歷史顯示舊 override 是為 Python 3.14 的 wheel 缺口而加；本版改為不宣告支援 Python 3.14，避免用 override 製造 metadata 不一致。
- 40 分鐘矩陣與 30 分鐘 mixed 已完成合成 gate；正式 PG/HTTP/模型/NAS 壓测仍待部署環境驗收。
- 正式 application role、RLS migration、grants/policies 與 rollback SQL 尚未在 staging 演練。
- 正式 runtime/NAS 的路徑 ownership、ACL、free-space 門檻、備份、恢復、I/O latency 與 page-image 容量尚未驗證。
- 真實 embedding/chunk 模型、版本 pin、品質門檻與代表性 corpus 尚未決定或驗證。
- Enrichment 的 localhost endpoint 約束在 job 執行時檢查；readiness 尚未在 admission 前驗證模型 endpoint／模型版本。PDF renderer 設定層已驗 executable，版本 pin 仍待部署。
- 程式已有 body/admission/upload 限制、worker heartbeat、admin operations 與無副作用告警評估器；正式 service definition、proxy/TLS/firewall、port ownership、log rotation 與監控閾值/接線仍需部署審閱。
- Query-event privileged retention 與 staging cleanup CLI 已本機驗證；queue archive 仍走明確 API preview/apply。正式值班身份、backup 狀態 adapter 與沿用 scheduler 的接線待核准，不新增臨時 scheduler。
- DB schema 的 backward compatibility 與無 down migration 時的 rollback 決策尚未簽核。

上述項目全部有可重現證據並取得對應 gate 核准前，本候選維持不可發布。

## 本輪候選存放與證據界線

舊 0.2.0 候選：`/Users/lab_center/projects/lab-multiuser-candidates/2026-09-12-local-01/manifest.json`，只保留為歷史證據。0.3.0 中繼 dirty snapshot：`/Users/lab_center/projects/lab-multiuser-releases/2026-09-12-local-02/manifest.json`，包含 Second Brain 71 個、lcdda-ingest 72 個 release source 檔 SHA 與雙 wheel SHA；同目錄的 `versioned-venv-verification.json` 保留獨立 venv import 成功及 `pip check` 失敗證據。它們都不可取代 clean-commit 正式候選重建。

來源 diff 的閱讀入口是本計畫與 `execution_trace.md`；實際數據彙整於 `acceptance-evidence.json`。這些結果不會解除正式 merge/push、RLS migration、服務切換、名單及 key 開通的既有核准階段。正式 RPO/RTO 與模型搜尋品質必須用目標資料/儲存做演練，不能把小型拋棄式 restore 時間當正式承諾。
