#!/bin/bash
# start-remote.sh — 啟動 second-brain MCP server（streamable-http，綁 Tailscale IP）
#
# 用法：
#   手動測試：  bash start-remote.sh
#   launchd：   ProgramArguments = ["/bin/bash", "<此檔完整路徑>"]
#
# 環境變數（可覆蓋）：
#   SECOND_BRAIN_REMOTE_PORT   HTTP 監聽 port（預設 9100）
#   TAILSCALE_CLI              tailscale 指令完整路徑（自動偵測，通常不需設）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${SECOND_BRAIN_REMOTE_PORT:-9100}"

# ── 偵測 tailscale CLI ────────────────────────────────────────────────────────
# macOS App Store 版：/Applications/Tailscale.app/Contents/MacOS/Tailscale
# Homebrew 版：       /opt/homebrew/bin/tailscale  或  /usr/local/bin/tailscale
# 環境變數覆蓋：      export TAILSCALE_CLI=/your/path/tailscale
_TAILSCALE="${TAILSCALE_CLI:-}"
if [ -z "$_TAILSCALE" ]; then
  for candidate in \
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale" \
    "/opt/homebrew/bin/tailscale" \
    "/usr/local/bin/tailscale"; do
    if [ -x "$candidate" ]; then
      _TAILSCALE="$candidate"
      break
    fi
  done
fi

if [ -z "$_TAILSCALE" ]; then
  echo "[second-brain] ERROR: tailscale CLI not found. Set TAILSCALE_CLI env var." >&2
  exit 1
fi

# ── 取得 Tailscale IP ─────────────────────────────────────────────────────────
TAILSCALE_IP=$("$_TAILSCALE" ip -4 2>/dev/null || true)
if [ -z "$TAILSCALE_IP" ]; then
  echo "[second-brain] ERROR: Tailscale not connected (no IPv4 address)" >&2
  exit 1
fi

# ── 偵測 uv ──────────────────────────────────────────────────────────────────
_UV="${UV_PATH:-}"
if [ -z "$_UV" ]; then
  for candidate in \
    "${HOME}/.local/bin/uv" \
    "/opt/homebrew/bin/uv" \
    "/usr/local/bin/uv"; do
    if [ -x "$candidate" ]; then
      _UV="$candidate"
      break
    fi
  done
fi

if [ -z "$_UV" ]; then
  echo "[second-brain] ERROR: uv not found. Set UV_PATH env var." >&2
  exit 1
fi

echo "[second-brain] Binding to Tailscale IP ${TAILSCALE_IP}:${PORT}" >&2

exec "$_UV" run \
  --with "mcp[cli]" \
  --with "markitdown[all]" \
  python "${SCRIPT_DIR}/server.py" \
  --transport streamable-http \
  --host "${TAILSCALE_IP}" \
  --port "${PORT}"
