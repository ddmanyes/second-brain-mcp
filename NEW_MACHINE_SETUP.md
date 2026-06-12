# Deploy second-brain on a New Machine (Drive source version)

> **Who this is for:** Setting up second-brain on your own additional Mac, running directly from Google Drive–synced source code (not `pip install mcp-second-brain` — that's for public users).
>
> Public installation → [README.md](README.md). Your own multi-machine setup → this guide. Always runs the latest Drive source.

---

## Mental Model: Each machine runs its own local server

Multiple machines do **not** share a single server. Each machine runs its own local server. Four components, clearly separated:

| Component | Location | Why |
| --- | --- | --- |
| Source code `mcp_second_brain/` (package) | **Google Drive** (auto-sync) | Change once, synced everywhere |
| venv | **Local `~/.venvs/second-brain/`** | Drive sync breaks symlinks; macOS blocks executing binaries from cloud folders (`Operation not permitted`) |
| Index DB `~/.second-brain/vault.db` | **Local, auto-created** | Rebuilt from synced vault markdown; DuckDB is single-writer, independent per machine |
| Vault notes (markdown) | **Google Drive** (auto-sync) | Content shared across machines |

> Multiple servers can coexist on the same machine (Desktop stdio + Claude Code stdio), sharing the same local DuckDB. The server is designed with **lock-aware retry — no index corruption**; stdio servers no longer kill each other.

---

## New Machine Bootstrap

Set variables for this machine's Drive path (`/Users/<you>` differs per machine):

```bash
PJ="$HOME/Library/CloudStorage/GoogleDrive-<your-account>/我的雲端硬碟/PJ_save"
SB="$PJ/mcp-tools/second-brain"
VAULT="$PJ/second-brain"
```

### Step 1 — Get the code

Sign in to the same Google Drive account — files sync automatically. Confirm `$SB/mcp_second_brain/server.py` exists.

### Step 2 — Create a local venv (**never inside the Drive folder**)

```bash
python3 -m venv ~/.venvs/second-brain
~/.venvs/second-brain/bin/pip install -r "$SB/requirements.txt"
~/.venvs/second-brain/bin/playwright install chromium   # for PNG snapshot rendering
```

> **PDF conversion dependencies** — `save_article` uses a three-tier pipeline:
>
> 1. **Marker** (ML, best quality) — installed via `requirements.txt` above; ~1.35 GB models auto-downloaded on first PDF save to `~/.cache/datalab/`
> 2. **pdftotext / pdfinfo** (fallback) — install poppler: `brew install poppler`
> 3. **MarkItDown** (last fallback) — already in requirements
>
> Marker models are machine-local and not synced. First PDF save on a new machine triggers automatic download (~1–2 min depending on network).

### Step 3 — Register MCP with Claude

**A. Claude Desktop** — edit `~/Library/Application Support/Claude/claude_desktop_config.json`.
`command` must point to the **local venv**; `args` must use `-m` to launch as a package (**never point directly to `server.py`** — relative imports will fail):

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "/Users/<you>/.venvs/second-brain/bin/python",
      "args": ["-m", "mcp_second_brain.server"],
      "env": {
        "PYTHONPATH": "<PJ>/mcp-tools/second-brain",
        "SECOND_BRAIN_PATH": "<PJ>/second-brain"
      }
    }
  }
}
```

After editing, **⌘Q to fully quit and relaunch** Claude Desktop (MCP config is only read at startup).

**B. Claude Code (CLI)**:

`claude mcp add` does not support the `-m` flag, so edit `~/.claude.json` directly:

```bash
python3 -c "
import json
with open('/Users/<you>/.claude.json', 'r') as f:
    d = json.load(f)
d['mcpServers']['second-brain'] = {
    'type': 'stdio',
    'command': '$HOME/.venvs/second-brain/bin/python',
    'args': ['-m', 'mcp_second_brain.server'],
    'env': {
        'PYTHONPATH': '$SB',
        'SECOND_BRAIN_PATH': '$VAULT'
    }
}
with open('/Users/<you>/.claude.json', 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
print('Done')
"
```

### Step 4 — Build the index for the first time

Start the agent and say `init_vault` (creates/repairs directories and templates), then run `sync_index` to build the local index DB. Re-run `sync_index` after bulk file changes.

> **Do not** run `python -c "vault_db.sync_all(...)"` directly — it competes with Claude Code's MCP server for DuckDB's exclusive write lock, causing `CatalogException: Table does not exist`.
> Always sync via the MCP tool (`sync_index`) so the server executes it internally.

### Step 5 — (Optional) Semantic search

Works without this (falls back to BM25 automatically). For semantic search, install Ollama:

```bash
brew install ollama 2>/dev/null || true
ollama pull nomic-embed-text
# Then add to the env block in your MCP config above:
#   "EMBED_URL": "http://localhost:11434/v1/embeddings", "EMBED_PORT": "11434"
```

### Step 6 — (Optional) Weekly automated maintenance

```bash
SECOND_BRAIN_PATH="$VAULT" bash "$SB/launchd/install.sh"
```

`install.sh` generates and loads a plist using the local `~/.venvs/second-brain/bin/python`. Runs every Sunday at 02:00: index → embedding → compress old notes → extract rules.

---

## Per-machine Local Data

| Data | Location | Synced? |
| --- | --- | :---: |
| Vault markdown notes | Google Drive | ✅ Shared across all machines |
| Source code (`mcp_second_brain/`) | Google Drive | ✅ Shared across all machines |
| Python venv | `~/.venvs/second-brain/` | ❌ Created separately per machine |
| DuckDB index | `~/.second-brain/vault.db` | ❌ Rebuilt per machine via `sync_index` |
| MCP config | Desktop config / Claude Code user scope | ❌ Configured separately per machine |

---

## Multi-machine Notes

- **Never edit the same vault note on two machines simultaneously** → Google Drive creates `xxx (1).md` conflict files. Wait for sync to complete before switching machines.
- **The index DB is not shared across machines** (`~/.second-brain/vault.db` is rebuilt from synced markdown on each machine) — this is intentional. Do not put the DB in Drive.
- **No HTTP remote server needed.** If you have Drive synced and a local venv, use the local server. The Tailscale remote access setup has been retired.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Desktop `Operation not permitted` / `Server disconnected` | `command` still points to `.venv/bin/python` inside Drive | Change to local `~/.venvs/second-brain/bin/python` |
| `ImportError: attempted relative import with no known parent package` | `args` points directly to `server.py` (script mode cannot resolve relative imports) | Use `["-m", "mcp_second_brain.server"]` — Desktop: edit JSON directly; Claude Code: use the Python script to edit `.claude.json` (`claude mcp add` does not support `-m`) |
| Connected but drops after 0.5 s | Old mutual-kill mechanism (fixed in current code) | Confirm you're running the fixed Drive source (`_kill_old_server` only exists in the HTTP branch) |
| Agent can't see notes / empty results | Index not built | Run `sync_index` once |
| Semantic search silently falls back to BM25 | Embedding server not running | Start Ollama / llama-server |
| `read_note_as_image` snapshot fails | playwright chromium not installed | `~/.venvs/second-brain/bin/playwright install chromium` |
| `Failure while replaying WAL file` (DB corrupted) | DuckDB write interrupted (IDE restart, `pkill -9`, sleep) | `rm -f ~/.second-brain/vault.db ~/.second-brain/vault.db.wal`, restart server, then `sync_index` |
| `~/.second-brain/vault.db` is tiny but a larger one exists elsewhere | Server started with cwd ≠ home; DuckDB created the DB in cwd | `find ~ -name vault.db -size +1M`, move the found file to `~/.second-brain/vault.db` |
