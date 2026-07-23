# ADR 0001 — 不從 server.py 抽出獨立的 domain layer

- 狀態：Accepted
- 日期：2026-07-23
- 脈絡來源：架構檢視（improve-architecture）候選 C

## 決策

**不**把 `server.py`（~2300 行、38 個 `@mcp.tool`）的 domain 邏輯下沉成一層獨立的、與 MCP 解耦的 API；`server.py` 維持「MCP adapter 與 vault domain 邏輯同層」的現狀。

## 理由

架構檢視曾提出把 server 拆成「薄 MCP adapter + vault domain layer」，前提是 domain 邏輯**被 MCP 以外的第二個消費者重用**。實地驗證所有潛在消費者後，此前提不成立：

| 檔案 | 與 server domain 邏輯的關係 |
|---|---|
| `__main__.py` | 只取 `main()`——啟動入口，非 domain 消費 |
| `llm_cli.py` | server **依賴的下層**工具（LLM subprocess wrapper），非消費者 |
| `benchmark.py` | 直接 import `vault_db`（更低層），不碰 tool 邏輯 |
| `mcp_remote_bridge_pure.py` | 純 HTTP proxy（網路轉發），不在行程內呼叫 domain |

設計鐵律：**一個 adapter 是假想的縫，兩個 adapter 才是真的縫。** MCP 是唯一 in-process 消費者，抽 domain layer 等於為不存在的第二消費者預先建縫——speculative、違反 YAGNI。

**Deletion test**：純機械式的檔案切分（把 tool 依領域分到多檔）並不會讓任何模組變深——複雜度只是「搬家」到新檔而非「消失」，卻要付 2300+ 行的高風險 churn 與零功能收益。

此外，server.py 內真正的**重複**（frontmatter 手術、note 寫後儀式）已在同一輪檢視由候選 A（`frontmatter.set_fields`）與候選 B（`after_write`）抽出並收斂。剩下的「長」是 38 個 tool 的體量——那是 MCP surface 的本質尺寸，不是設計缺陷。

## 何時重開此決策

出現**真正的第二個 in-process 消費者**時（而非假設它會出現），例如：

- 一個非 MCP 的 CLI 或 HTTP 入口需要在行程內呼叫同一批 vault 操作；
- 測試需要繞過 FastMCP 直接驅動 domain 邏輯，且現有「monkeypatch `server` module 級函式」的方式已明顯不敷使用。

屆時再抽出 domain layer，縫才是真的。

## 參考

- 已落地的相關深化：`mcp_second_brain/frontmatter.py`（候選 A）、`server.after_write`（候選 B）、`store.sync_if_stale`（候選 D，store 抽象補縫）。
- 領域詞彙見 [CONTEXT.md](../../CONTEXT.md)。
