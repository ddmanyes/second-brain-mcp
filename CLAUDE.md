# Second Brain MCP Server

## 執行
`uv run --with "mcp[cli]" --with "markitdown[all]" python server.py`

## 安全
- `read_note` 必須用 `.resolve().is_relative_to(VAULT)` 防路徑遍歷
- YAML frontmatter 的 title/source 欄位要先 `.replace('"', "'")`

## Vault 路徑
由環境變數 `SECOND_BRAIN_PATH` 控制，預設 Google Drive 路徑
