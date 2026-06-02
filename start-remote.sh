#!/bin/bash
# start-remote.sh — 啟動 second-brain MCP server（streamable-http，綁 Tailscale IP）
# 用法：
#   手動測試：  bash start-remote.sh
#   launchd：   ProgramArguments 填此 script 路徑，KeepAlive=true

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${SECOND_BRAIN_REMOTE_PORT:-9100}"

TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || true)
if [ -z "$TAILSCALE_IP" ]; then
  echo "[second-brain] ERROR: Tailscale not connected or tailscale CLI not found" >&2
  exit 1
fi

echo "[second-brain] Binding to Tailscale IP ${TAILSCALE_IP}:${PORT}" >&2

exec uv run \
  --with "mcp[cli]" \
  --with "markitdown[all]" \
  python "${SCRIPT_DIR}/server.py" \
  --transport streamable-http \
  --host "${TAILSCALE_IP}" \
  --port "${PORT}"
