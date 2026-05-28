#!/usr/bin/env bash
# Vault Sleep runner — triggered by launchd every Sunday 02:00
# Uses Gemini CLI with second-brain-tools MCP to compress old notes.

set -euo pipefail

LOG_DIR="$HOME/.second-brain/logs"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/vault_sleep_$(date +%Y%m%d_%H%M%S).log"

echo "[$(date)] Starting vault sleep..." | tee "$LOGFILE"

gemini \
  --yolo \
  --allowed-mcp-server-names second-brain-tools \
  -p "Check sleep_status first. If there are any candidates, run vault_sleep. Report how many notes were compressed and any errors. Be concise." \
  2>&1 | tee -a "$LOGFILE"

echo "[$(date)] Done." | tee -a "$LOGFILE"

# Keep only last 10 logs
ls -t "$LOG_DIR"/vault_sleep_*.log 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
