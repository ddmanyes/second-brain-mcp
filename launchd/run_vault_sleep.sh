#!/usr/bin/env bash
# run_vault_sleep.sh — Legacy shell wrapper (kept for reference).
# Preferred runner: run_sleep.py (called by launchd, no LLM orchestration needed).
#
# This script starts the embedding server, calls Gemini CLI to orchestrate
# vault maintenance via MCP tools, then stops the server.
# Gemini CLI must be installed and authenticated for this to work.

set -euo pipefail

LLAMA_SERVER="${LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}"
EMBED_MODEL="${EMBED_MODEL:-$HOME/nomic-embed-text-v1.5.Q8_0.gguf}"
EMBED_PORT="${EMBED_PORT:-11435}"
LOG_DIR="$HOME/.second-brain/logs"
mkdir -p "$LOG_DIR"
LOGFILE="$LOG_DIR/vault_sleep_$(date +%Y%m%d_%H%M%S).log"

echo "[$(date)] Starting vault sleep..." | tee "$LOGFILE"

# --- Start embedding server (if model exists and server not already running) ---
EMBED_PID=""
if [[ -f "$EMBED_MODEL" ]] && ! curl -sf "http://localhost:${EMBED_PORT}/health" >/dev/null 2>&1; then
  echo "[$(date)] Starting embedding server..." | tee -a "$LOGFILE"
  "$LLAMA_SERVER" \
    -m "$EMBED_MODEL" \
    --port "$EMBED_PORT" \
    --embedding --pooling mean -np 4 -c 2048 --log-disable \
    > "$LOG_DIR/llama_embed.log" 2>&1 &
  EMBED_PID=$!
  for i in $(seq 1 15); do
    curl -sf "http://localhost:${EMBED_PORT}/health" >/dev/null 2>&1 && break
    sleep 1
  done
  echo "[$(date)] Embedding server ready (PID $EMBED_PID)" | tee -a "$LOGFILE"
fi

# --- Run vault sleep via Gemini CLI ---
gemini \
  --yolo \
  --allowed-mcp-server-names second-brain \
  -p "First run sync_index to backfill any missing embeddings. Then check sleep_status and run vault_sleep if there are candidates. Report how many notes were compressed and how many embeddings were updated. Be concise." \
  2>&1 | tee -a "$LOGFILE"

# --- Stop embedding server if we started it ---
if [[ -n "$EMBED_PID" ]]; then
  echo "[$(date)] Stopping embedding server (PID $EMBED_PID)..." | tee -a "$LOGFILE"
  kill "$EMBED_PID" 2>/dev/null || true
fi

echo "[$(date)] Done." | tee -a "$LOGFILE"

# Keep only last 10 logs
ls -t "$LOG_DIR"/vault_sleep_*.log 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
