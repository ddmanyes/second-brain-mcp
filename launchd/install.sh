#!/usr/bin/env bash
# launchd/install.sh — generate real plists from templates and load them
#
# Usage (from anywhere):
#   bash /path/to/mcp-tools/second-brain/launchd/install.sh
#
# Environment overrides:
#   SECOND_BRAIN_PATH   vault directory  (default: ~/second-brain)
#   UV_PATH             uv binary path   (default: auto-detected)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$(dirname "$SCRIPT_DIR")"

VAULT_PATH="${SECOND_BRAIN_PATH:-${HOME}/second-brain}"

# Auto-detect uv
UV_BIN="${UV_PATH:-}"
if [ -z "$UV_BIN" ]; then
    for candidate in "${HOME}/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
        if [ -x "$candidate" ]; then UV_BIN="$candidate"; break; fi
    done
fi
if [ -z "$UV_BIN" ]; then
    echo "ERROR: uv not found. Set UV_PATH env var." >&2; exit 1
fi

UV_DIR="$(dirname "$UV_BIN")"
PATH_VAR="/usr/local/bin:/usr/bin:/bin:${UV_DIR}"

echo "second-brain launchd install"
echo "  SERVER_DIR : $SERVER_DIR"
echo "  VAULT_PATH : $VAULT_PATH"
echo "  UV_BIN     : $UV_BIN"
echo ""

AGENTS_DIR="${HOME}/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"

for tmpl in "$SCRIPT_DIR"/*.plist.template; do
    name="$(basename "$tmpl" .template)"
    dest="${AGENTS_DIR}/${name}"

    sed \
        -e "s|{{SERVER_DIR}}|${SERVER_DIR}|g" \
        -e "s|{{VAULT_PATH}}|${VAULT_PATH}|g" \
        -e "s|{{UV_BIN}}|${UV_BIN}|g" \
        -e "s|{{HOME}}|${HOME}|g" \
        -e "s|{{PATH_VAR}}|${PATH_VAR}|g" \
        "$tmpl" > "$dest"

    launchctl unload "$dest" 2>/dev/null || true
    launchctl load "$dest"
    echo "  loaded: $name"
done

echo ""
echo "Done. Verify: launchctl list | grep 'vault\|second-brain'"
