# {{PROJECT_NAME}} — 本地主機完整安裝指南

> 從零在新 Mac 上安裝 {{PROJECT_NAME}}。
> 若只是要**遠端連入**現有主機，見 `REMOTE_SETUP.md`。

---

## 前置條件

```bash
# Python 3.11+
python3 --version

# uv
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version

# 確認 Google Drive 已同步
ls "$HOME/Library/CloudStorage/GoogleDrive-{{GOOGLE_ACCOUNT}}/我的雲端硬碟/PJ_save/mcp-tools/{{SLUG}}/"
```

---

## Step 1：建立虛擬環境與安裝依賴

```bash
cd "$HOME/Library/CloudStorage/GoogleDrive-{{GOOGLE_ACCOUNT}}/我的雲端硬碟/PJ_save/mcp-tools/{{SLUG}}"

uv venv
uv pip install -r requirements.txt

# 驗證
.venv/bin/python -c "import mcp; print('OK')"
```

---

## Step 2：設定環境變數

在 `~/.zshrc` 加入：

```bash
# [必要] Second Brain vault 根目錄
export SECOND_BRAIN_PATH="$HOME/Library/CloudStorage/GoogleDrive-{{GOOGLE_ACCOUNT}}/我的雲端硬碟/PJ_save/second-brain"

# [專案必要] ___
export _____="your_value"

# [選用] ___
# export _____="your_value"
```

```bash
source ~/.zshrc
echo $SECOND_BRAIN_PATH   # 驗證
```

---

## Step 3：本地 MCP 註冊

```bash
PROJDIR="$HOME/Library/CloudStorage/GoogleDrive-{{GOOGLE_ACCOUNT}}/我的雲端硬碟/PJ_save/mcp-tools/{{SLUG}}"

claude mcp add --scope user {{SLUG}} \
  -- "$PROJDIR/.venv/bin/python" "$PROJDIR/server.py"

# 驗證
claude mcp list
```

---

## Step 4：快速驗證

```bash
PROJDIR="$HOME/Library/CloudStorage/GoogleDrive-{{GOOGLE_ACCOUNT}}/我的雲端硬碟/PJ_save/mcp-tools/{{SLUG}}"

# 測試套件
.venv/bin/python -m pytest tests/ -q --tb=short

# 手動功能驗證
# .venv/bin/python _____.py _____
```

---

## Step 5：安裝排程（可選）

```bash
PROJDIR="$HOME/Library/CloudStorage/GoogleDrive-{{GOOGLE_ACCOUNT}}/我的雲端硬碟/PJ_save/mcp-tools/{{SLUG}}"

# 複製 plist
cp "$PROJDIR/launchd/"*.plist ~/Library/LaunchAgents/

# 修改路徑後載入
# launchctl load ~/Library/LaunchAgents/com.user.{{SLUG}}-*.plist
```

---

## Step 6：遠端服務（可選）

見 `REMOTE_SETUP.md`。

---

## 快速 Sanity Check

```bash
PROJDIR="$HOME/Library/CloudStorage/GoogleDrive-{{GOOGLE_ACCOUNT}}/我的雲端硬碟/PJ_save/mcp-tools/{{SLUG}}"

echo "=== 依賴 ===" && "$PROJDIR/.venv/bin/python" -c "import mcp; print('OK')"
echo "=== 環境變數 ===" && echo "SB_PATH=$SECOND_BRAIN_PATH"
echo "=== MCP 註冊 ===" && claude mcp list | grep {{SLUG}}
echo "=== Vault 可寫 ===" && touch "$SECOND_BRAIN_PATH/.write_test" && rm "$SECOND_BRAIN_PATH/.write_test" && echo "OK"
```

---

## 本機資訊（安裝後填寫）

| 項目 | 值 |
| --- | --- |
| 用戶 | {{USERNAME}} |
| 專案路徑 | `~/Library/CloudStorage/GoogleDrive-.../PJ_save/mcp-tools/{{SLUG}}` |
| vault 路徑 | `~/Library/CloudStorage/GoogleDrive-.../PJ_save/second-brain` |
| uv 路徑 | `~/.local/bin/uv` |
| Tailscale IP | — |
| 遠端 port | — |

---

## 相關文件

- `REMOTE_SETUP.md` — 遠端主機設定
- `AGENTS.md` — AI agent 操作 SOP
- `CLAUDE.md` — 專案架構概覽
