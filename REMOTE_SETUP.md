# Second Brain MCP — 跨平台 & 遠端設定指南

> 本文件說明：
>
> 1. **跨平台（Mac + Windows）本機直連設定** — 兩台電腦各自用 user-scope MCP，vault/.mcp.json 保持空白不干擾
> 2. **遠端 Tailscale 存取** — 讓遠端 Mac 透過加密隧道連入主機

---

## 跨平台本機設定（Mac + Windows）

### 設計原則

`vault/.mcp.json` 跟著 Google Drive 同步，**不能放機器特定路徑**。  
每台電腦改用 `--scope user` 設定，存在本機 `~/.claude/`（不同步）。

### Mac（已設定，確認用）

```bash
# 確認目前設定
claude mcp list
```

若需要重新設定（路徑依實際 Google Drive 掛載點）：

```bash
claude mcp add --scope user second-brain \
  "/Users/zhanqiru/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/mcp-tools/second-brain/.venv/bin/python" \
  "/Users/zhanqiru/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/mcp-tools/second-brain/server.py" \
  --env SECOND_BRAIN_PATH="/Users/zhanqiru/Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/second-brain"
```

### Windows（PowerShell，初次設定）

前置條件：已建立 venv 並安裝依賴：

```powershell
# 建立 venv（只需執行一次）
python -m venv C:\Users\User\.venvs\mcp-second-brain
C:\Users\User\.venvs\mcp-second-brain\Scripts\pip install -r "G:\我的雲端硬碟\PJ_save\mcp-tools\second-brain\requirements.txt"
```

設定 user-scope MCP：

```powershell
claude mcp add --scope user second-brain `
  --env SECOND_BRAIN_PATH="G:\我的雲端硬碟\PJ_save\second-brain" `
  -- "C:\Users\User\.venvs\mcp-second-brain\Scripts\python.exe" `
  "G:\我的雲端硬碟\PJ_save\mcp-tools\second-brain\server.py"
```

確認：

```powershell
claude mcp list
```

### vault/.mcp.json 說明

此檔案已設為空設定（`"mcpServers": {}`），跟著 Google Drive 同步但**不含任何路徑**。  
各機器的實際設定存放在本機 `~/.claude/`，互不干擾。

---

---

## 架構

```
遠端 Mac（client）
  Antigravity IDE / Claude Code
    ↓ HTTP MCP（url: http://100.x.x.x:9100/mcp）
      ── Tailscale 加密隧道 ──
    ↓
主機 Mac（server）
  second-brain/server.py --transport streamable-http
  vault（Google Drive 同步）
  DuckDB（~/.second-brain/vault.db，本機 only）
```

---

## 主機端設定（有 vault 的 Mac）

### Step 1：安裝前置套件

```bash
# Tailscale（App Store 或 Homebrew）
brew install --cask tailscale          # Homebrew 方式
# 或從 App Store 安裝 Tailscale，登入同一帳號

# uv（Python 套件管理）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 驗證
tailscale ip -4          # 應回傳 100.x.x.x
# macOS App Store 版需用完整路徑：
/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4
```

### Step 2：Clone / 複製 repo

```bash
# Google Drive 同步後路徑自動出現，或手動 clone：
git clone https://github.com/ddmanyes/second-brain-mcp.git
cd second-brain-mcp
uv sync   # 建立 .venv，安裝所有依賴
```

### Step 3：設定環境變數（plist 已包含，手動測試需要）

```bash
export SECOND_BRAIN_PATH="~/path/to/second-brain-vault"
export SECOND_BRAIN_REMOTE_PORT="9100"   # 可選，預設 9100
```

### Step 4：手動測試啟動

```bash
bash start-remote.sh
# 預期輸出：
# [second-brain] Binding to Tailscale IP 100.x.x.x:9100
# [second-brain] Starting streamable-http on 100.x.x.x:9100
```

從另一台機器測試（需先完成 Step 5）：
```bash
curl http://100.x.x.x:9100/mcp
```

### Step 5：安裝 launchd（開機常駐）

**複製 plist 並修改路徑：**

```bash
cp com.user.second-brain-remote.plist ~/Library/LaunchAgents/
```

**需要修改 plist 裡的三個路徑（換機器必改）：**

| 欄位 | 說明 | 範例 |
| --- | --- | --- |
| `ProgramArguments[1]` | `start-remote.sh` 完整路徑 | `/Users/newuser/path/to/start-remote.sh` |
| `WorkingDirectory` | second-brain 程式碼目錄 | `/Users/newuser/path/to/second-brain-mcp` |
| `SECOND_BRAIN_PATH` | vault markdown 目錄 | `/Users/newuser/path/to/vault` |

**載入並啟動：**

```bash
# 載入
launchctl load ~/Library/LaunchAgents/com.user.second-brain-remote.plist

# 確認狀態
launchctl list | grep second-brain-remote
# 正常：PID 不為 -，exit code 為 0

# 看 log
tail -f /tmp/second-brain-remote.log
```

**常用管理指令：**

```bash
# 停止
launchctl unload ~/Library/LaunchAgents/com.user.second-brain-remote.plist

# 重啟（改完 plist 後）
launchctl unload ~/Library/LaunchAgents/com.user.second-brain-remote.plist
launchctl load   ~/Library/LaunchAgents/com.user.second-brain-remote.plist

# 手動觸發（不用 unload/load）
launchctl kickstart gui/$(id -u)/com.user.second-brain-remote
```

---

## 客戶端設定（遠端 Mac）

### Antigravity IDE

編輯 `~/.gemini/antigravity-ide/mcp_config.json`：

```json
{
  "mcpServers": {
    "second-brain": {
      "url": "http://100.x.x.x:9100/mcp"
    }
  }
}
```

`100.x.x.x` 換成主機的 Tailscale IP（主機執行 `tailscale ip -4` 取得）。

### Claude Code CLI

```bash
claude mcp add --scope user second-brain \
  --transport http \
  http://100.x.x.x:9100/mcp
```

---

## Tailscale 路徑對照

| 安裝方式 | CLI 路徑 |
| --- | --- |
| App Store（macOS） | `/Applications/Tailscale.app/Contents/MacOS/Tailscale` |
| Homebrew（Apple Silicon） | `/opt/homebrew/bin/tailscale` |
| Homebrew（Intel Mac） | `/usr/local/bin/tailscale` |

`start-remote.sh` 會依序自動偵測上述路徑，通常不需手動設定。
若全部找不到，設環境變數覆蓋：

```bash
export TAILSCALE_CLI=/your/custom/path/tailscale
```

---

## 故障排查

| 症狀 | 原因 | 解法 |
| --- | --- | --- |
| log 顯示 `Tailscale not connected` | Tailscale 未登入或 VPN 未啟動 | 開啟 Tailscale app 並登入 |
| log 顯示 `tailscale CLI not found` | 找不到 tailscale binary | 設 `TAILSCALE_CLI` 環境變數 |
| log 顯示 `uv not found` | uv 不在已知路徑 | 設 `UV_PATH` 環境變數 |
| 客戶端連線被拒 | 防火牆或 Tailscale 未連 | 確認兩端都在同一 Tailnet |
| launchd exit code -1 | plist 路徑錯誤 | 檢查三個路徑是否正確 |
| port 9100 被佔用 | 其他服務佔用 | 改 `SECOND_BRAIN_REMOTE_PORT` |

---

## 目前主機資訊（2026-06-02）

| 項目 | 值 |
| --- | --- |
| 主機 | zhanqiru 的 Mac |
| Tailscale IP | 100.81.161.16 |
| Port | 9100 |
| uv 路徑 | `/Users/zhanqiru/.local/bin/uv` |
| Tailscale CLI | `/Applications/Tailscale.app/Contents/MacOS/Tailscale` |
| Vault 路徑 | `~/Library/CloudStorage/GoogleDrive-.../PJ_save/second-brain` |
| server.py 路徑 | `~/Library/CloudStorage/GoogleDrive-.../PJ_save/mcp-tools/second-brain/` |

---

## 資安備註

- **不要開放 `0.0.0.0`**：`start-remote.sh` 只綁 Tailscale IP，避免區域網路上其他設備直連
- **Tailscale 2FA**：帳號安全 = MCP server 安全，建議啟用
- **Tailscale ACL（選做）**：在 [Tailscale admin console](https://login.tailscale.com/admin/acls) 設 tag，限制只有特定設備能訪問 port 9100
- vault 含個人財務/決策筆記，不要把 Tailscale 帳號分享給不信任的人
