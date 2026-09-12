# 執行證據

## 2026-09-12

- T0 completed：兩個 repo 原本乾淨，來源/部署分離；未使用正式 DB。
- P0 in_progress：新增 `test_query_privacy.py`。修復前合成測試 4 failed，證明空結果/錯誤 fallback 暴露另一人的私人路徑及 raw query log。
- 修復後 `tests/test_query_privacy.py tests/test_multiuser_regression.py`：25 passed，1 項既有 ffmpeg 可用性警告。尚未代表完整 gate 通過。
- T1/T3/P2 in_progress：分派三個獨立路徑的子代理。PG 整合測試尚未執行。

## 2026-09-12 續作驗收

- trusted Identity 新增 repr 隱藏的 credential_id（雜湊引用）；IntakeIdentityBridge 每次核对 UUID/role/revocation，無原始 key 持久化。
- 真 HTTP ASGI middleware 合成兩人提交：可信 actor 不串人、raw key 不進 job；真 PG 撤銷後 bridge 拒絕。
- ManagedIntakeFacade：只供 submit/status/cancel/upload_prepare，固定 figures off；未知錯誤去敏，跨人/非 ingest 拒絕；尚未掛正式 HTTP。
- OwnedWorker：等候 lease 與 job PID 一致後才建 worker；提交 checkpoint重查；真子程序交接測試通過；未接正式 factory/supervisor。
- MCP close_queries：停止接單→有界等待worker及外層事件完成→sink關閉；取消/timeout保留capacity，drain race exactly-one事件。
- full SB：`env -u SB_PG_DSN -u SB_PG_TEST_DSN -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u GEMINI_API_KEY -u GOOGLE_API_KEY SB_DB_BACKEND=duckdb DISABLE_EMBEDDING=1 UV_OFFLINE=1 UV_NO_BUILD_ISOLATION=1 TMPDIR=/tmp PYTHONPATH=/Users/lab_center/projects/lcdda-ingest .venv/bin/python -m pytest tests/ -q` → 805 passed, 86 skipped，exit 0；ffmpeg可用性警告1。PG opt-in另跑，不把skip稱通過。
- isolated PG：`env -u SB_PG_DSN -u SB_PG_TEST_DSN SB_DB_BACKEND=duckdb DISABLE_EMBEDDING=1 .venv/bin/python -m pytest --run-postgres tests/test_postgres_multiuser_startup.py tests/test_query_event_postgres.py tests/test_multiuser_rls.py tests/test_postgres_store.py tests/test_note_chunks_sync.py tests/test_hybrid_search_chunks.py -q` → 104 passed，exit 0；既有ffmpeg/pool deprecation警告。新增credential斷言後再跑 test_multiuser_rls.py → 20 passed。
- lcdda（相同去除API key與正式DSN環境）：SB .venv/bin/python -m pytest tests/ --ignore=tests/test_packaging.py -q → 209 passed；`PIP_NO_INDEX=1 TMPDIR=/tmp python3 -m pytest tests/test_packaging.py -q` → 3 passed；兩者exit 0。
- 本輪新增/修改核心檔Ruff及兩repo git diff --check通過；未宣稱repo全檔格式化。
- 尚未commit/push/merge/正式DB migration/安裝/服務重啟/發key；Skill repo的提交授權不延伸至本專案。

### 同輪後續提交與報表整合

- 新增 StagedDocumentRenderer：純文字/真PDF抽文在去除服務環境的子程序，60s timeout、200pages、10MB輸出上限；真子程序timeout kill測試。macOS尚需部署層記憶體限制，不能稱完整sandbox。
- ManagedArticleCommitter：renderer前後重新驗證身分，固定安全frontmatter、upload以verified SHA256去重、index pending不假稱成功、current Identity reset。
- 合成E2E：兩位成員queue→OwnedWorker→RemoteIngestWorker→真轉換子程序→shared helper，共同貢獻同一份文獻；保留首位uploaded_by及兩位contributor。向量/chunks/figures readiness 明確另列。
- 純metadata PostgreSQL index方法：不呼叫embedding或after_write，短pool/statement/lock預算；21項multiuser PG測試通過，含共用row與private-path拒絕。
- admin report CLI：stdin key有界讀取、唯讀pool、active UUID admin限定、summary JSON、pool背景log去敏。15項event PG測試通過，含真readonly報表與撤銷；首輪fixture缺api_keys已補標準schema並重跑。
- 最後整體：SB 824 passed / 88 skipped（PG另opt-in），lcdda 229 non-packaging passed。其後僅測試closure明確綁定與格式調整，focused重跑另附tool輸出；無正式服務變更。
- 尚待：唯一supervisor/HTTP composition正式接線、重啟reconciliation/長時間混合負載、分階段效能及故障還原、部署層資源限制、發版整合。安全元件不等於已正式開放。

## 2026-09-12 HTTP／supervisor 接線驗收

- 新增 opt-in managed_server／managed_worker：明確環境設定，固定 worker argv，protocol=1，僅繼承白名單環境。settings mapping 只解析設定，不啟用程序全域 SB visibility；open_store 仍要求實際 multiuser context。
- ManagedSupervisor：唯一鎖、逐次 dispatch、自己啟動的 child reap／300s deadline，restart 保留活躍 job、死 worker interrupted，不自動重播未知結果。停止 supervisor 保留 running worker；重啟後不接管外部 PID 的強制 kill，故完整跨重啟 deadline 仍待部署設計。
- 成員 HTTP 僅四工具；強制 key lookup、owner-bound 雙憑證 upload、120s upload deadline、有限 thread limiter、FastMCP/supervisor lifespan。真 composition 測試確認 raw key→hash→可信 Identity→queue，原始 key 不持久化，shutdown close store。
- 身分 PG lookup 新增 pool 0.5s、statement 1s、lock 0.5s budget。
- 主代理驗證：SB 一般 suite 824 passed/88 skipped；獨立 PG multiuser/event/startup 66 passed（隔離 harness）；lcdda non-packaging 253 passed；離線 packaging 3 passed。composition 測試格式修正後重跑通過。
- 仍待長時間混合負載、完整向量/圖片處理、資源限制與故障還原，以及發版部署整合。未更動正式 DB／服务／排程、未發 key、未 commit/push。

## 2026-09-12 最終本機驗收與候選（SB §21 已 append/read-back）

- T2b：單篇 vector/chunk CAS + public page images；真 PG 的 A queue→PDF extraction→metadata→model stub→pdftoppm→B search/read/figure E2E，private note 不可見。Private figure row 在任何寫檔前與 transaction 內兩次拒絕；public metadata rerun 完整清理/讀回。
- Auth offload 4 slots/2s、key512界線、multiuser不接受env fallback；HTTP body limit、query/DB/模型剩餘deadline與request embedding memo。已存 private path/exception diagnostics 在多人 PG 路徑去敏；local model HTTP 拒redirect/proxy、body有界。
- Managed HTTP upload 真 blocked begin/finalize regression：deadline及時回408 unknown、slot由tracked task持有到cleanup、已開始finalize可晚完成且不abort已完成ticket。2s shutdown drain失敗才arm own-process guard。Operations endpoint僅admin並用獨立1slot。
- Worker watchdog自身300s/1GiB，涵蓋registered子程序group；PDF計頁/轉圖共用60s期限、macOS RSS與CPU/FD/output caps。新每秒heartbeat在獨立目錄，失去ownership停止；read最多4KiB、驗UUID/PID/time，非進度或授權承諾。
- 單篇pending-index reconcile、staging terminal cleanup、query retention CLI皆有preview/explicit apply、bounded stdin、freshadmin與去敏輸出；retention僅獨立maintenanceDSN且真PG試驗應用role不能DELETE。
- ManagedOperations cache10s，admin讀queue counts/age、heartbeat、storage、requiredrevision；QueryWindow max1000/300s只timing/timeout。OperationalAlertEvaluator需明確threshold、3bad觸發/3good恢復；eventloss恢復要求written有新進度，backup缺證據為unknown。沒有新增正式scheduler或外部通知。
- SB final general：沿用上述完整去除DB/模型憑證的環境，`PYTHONPATH=/Users/lab_center/projects/lcdda-ingest .venv/bin/python -m pytest tests/ -q` → 950 passed、97 skipped，10.63s，exit0。後加mixed-fault案例為獨立opt-in；一般suite未把它列為通過。
- SB final PG：相同隔離環境，以owned harness執行 `tests/test_multiuser_rls.py tests/test_postgres_multiuser_startup.py tests/test_query_event_postgres.py tests/test_query_event_maintenance.py tests/test_multiuser_restore.py tests/test_query_budget_postgres.py tests/test_article_enrichment_postgres.py tests/test_article_page_images.py tests/test_managed_article_end_to_end.py tests/test_postgres_store.py tests/test_note_chunks_sync.py tests/test_hybrid_search_chunks.py --run-postgres -q` → 125 passed／25.86s／exit0。16warnings為既有ffmpeg與fixturepoolopen deprecation。測試數與general重疊，不相加。
- LC final general：同前環境、SB test interpreter＋`PYTHONPATH=/Users/lab_center/git-repos/second-brain`，`pytest tests/ --ignore=tests/test_packaging.py -q` → 311 passed／6.92s／exit0；系統python離線packaging另3passed。
- 長矩陣：8×300秒，clients1/5/10/20每個query-only/mixed；20query/s；mixed/query-onlyp95=1.300/1.029/0.973/0.984，submitp95=23.266/24.039/23.041/22.438ms。全部零errors/timeouts/eventloss/actorviolations。Telemetry各200warmup+200samples，p95 overhead0.030ms。
- 30分鐘mixed：36000queries、1800ingests、1799duplicates，queryp95=1.581ms，submitp95=79.992ms，零errors/timeouts/eventloss/actorviolations。受測10core/harness SHA讀回一致，結果JSON留存。全程syntheticretrieval/resolver/index，非正式PG/model/NAS/HTTP容量。
- 最後5分鐘faultsoak：`SB_MANAGED_FAULT_SOAK=1 ... pytest tests/test_mixed_fault_soak.py -q` → 1passed／300.27s／exit0。3000queries、210accepted全部可追蹤、30timeouts/30unavailable/30revokedcancel/30deadPIDreconcile；刻意29writerfailures有計數，2971written，零overflow、全部drain。故障為inprocessadapter/PID0注入；真process/PG/HTTP故障另有tests。無跨人結果，詳load-fault-results.json。
- 曾出現query-budget PG混跑6個FakePool fixture不接受timeout/GUC額外參數；修正fake與精確斷言後相關32項及最終125項通過。未將該次失敗稱通過。最初未固定測試source hashes的長跑被主動中止(exit130)，其後完整40+30分鐘結果才作正式本機證據。
- Changed/new SB67個Python檔Ruff、LC整套Ruff、兩repo `git diff --check`通過；SB舊全repo lint債未擴修。最後純測試排版後focused3passed/1skip（5mincase無optin），不需重跑已通過300秒同邏輯。
- 候選目錄 `/Users/lab_center/projects/lab-multiuser-candidates/2026-09-12-local-01/`：source snapshot135檔、双wheel、manifest、build logs與新venv。源碼與安裝wheel/worker revision一致：`1dae360fb44cf116cc2dc19e06d39efdc732f998672077d7929ae90a5038fc75`。SB wheel SHA `18968faa613116a6391e3ba8ee89090a08356b7c1280ddb4adc571e489d4ec10`；LC wheel SHA `32812da7db3cd7ff52cbb81b4918011b93a3684adfb0c2963a2656dcbb702d4b`。第三方依賴重用已驗testsite，非完整獨立dependencyrebuild；dirty-source snapshot非發布commit。
- SB計畫新增§21並讀回確認；本機實作/驗收交付與正式release gate分開。未commit/push/merge、改正式DB/服務/plist/NAS/排程、匯入名單或發key，未呼叫付費模型。後續正式dependencylock、migration/安全rollback、模型品質/NAS/backup/SLO/RPO/RTO與memberonboarding依RELEASE_CANDIDATE.md。


## 2026-09-12 lcdda-first release refresh

Version 0.3.0 source refreshed with canonical auth_context, explicit query-event migration, and multiuser paid-synthesis gate. Current source validation: 1076 passed, 2 skipped with --run-postgres; additional paid-synthesis gate 10 passed. lcdda source suite 314 passed. Dependency metadata corrected to Python 3.11–3.13 and compatible magika 0.6.3; independent versioned environment pip check and local Markdown/PDF conversions passed. These are build/preflight results; formal cutover remains a separate step.
