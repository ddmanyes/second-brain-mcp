# Deploy second-brain on a New Machine (Drive source version)

> **Who this is for:** Running second-brain across your own machines from Google Drive–synced
> source (not `pip install mcp-second-brain` — that's for public users).
>
> Public installation → [README.md](README.md). Your own multi-machine setup → this guide.
>
> **Architecture reference:** [AGENTS.md](AGENTS.md) → "Connection Topology & Write Discipline";
> full plan → [MIGRATION_PLAN_POSTGRES.md](MIGRATION_PLAN_POSTGRES.md).

---

## Mental Model: one central brain, many thin clients

The current setup is **one central HTTP server + Postgres**, not a local server per machine.

```text
            Postgres (sb-pg, Docker)        ← 127.0.0.1:5432 only, never exposed
                  ▲ localhost
   central host ──┤ MCP server :9100 (streamable-http, bound to Tailscale IP)
   (always-on)    │   SB_DB_BACKEND=postgres, SB_API_KEY=…
                  │   + pg-sync (every 30 min) + pg-backup (daily)
                  ▼ Tailscale
   client Macs ───── connect via http://<tailscale-ip>:9100/mcp  +  X-API-Key header
                     (no venv, no DuckDB, no sync — just MCP config)
```

| Component | Where | Notes |
| --- | --- | --- |
| Vault notes (markdown) | **Google Drive** (auto-sync) | Canonical source of truth (L1). Only the **central host** writes. |
| Source code `mcp_second_brain/` | **Google Drive** (auto-sync) | Change once, synced everywhere. |
| Postgres index (L2) | **central host only**, Docker volume on local SSD | Rebuildable from markdown via `sync_all`; **never** put the data dir on Drive. |
| Central MCP server | **central host**, launchd `com.user.second-brain-remote` | streamable-http on the Tailscale IP `:9100`, single instance. |
| Client machines | connect over HTTP | Need **only** an MCP config pointing at the central server + the API key. |

> **Write discipline:** all writes go through the central server (single writer → no Google
> Drive conflict copies). Client-side Obsidian is **read-only** — read synced markdown, but make
> changes via MCP tools, not by typing into Obsidian on a laptop.

---

## A. Client machine setup (the common case — ~2 minutes)

A new Mac that just needs to *use* the brain. **No Python, no venv, no DuckDB, no indexing.**

The central host is the **mac-mini (`lab-center`), Tailscale `100.87.59.15`** — SSH in as
`lab_center` (key auth). It also serves three sibling instances off the same package:

| Port | Instance | Vault / purpose |
| --- | --- | --- |
| 9100 | `second-brain` | the personal vault |
| 9104 | `lcdda` | a second vault, same `mcp_second_brain` package |
| 9106 | `lcdda-harvest` | ingest helper (`~/projects/lcdda-ingest/mcp_server.py`) |
| 9108 | `finance-kit` | separate codebase |

Get the API key from the central host (it lives in `SB_API_KEY` of
`~/Library/LaunchAgents/com.user.second-brain-remote.plist`, and in the existing clients'
configs).

**Claude Code (CLI):**

```bash
claude mcp add --scope user --transport http second-brain \
  "http://100.87.59.15:9100/mcp" \
  --header "X-API-Key: <the-key>"
```

**Claude Desktop** — edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(Desktop reaches HTTP servers through the `mcp-remote` proxy):

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://100.87.59.15:9100/mcp", "--header", "X-API-Key: <the-key>"]
    }
  }
}
```

⌘Q and relaunch Desktop. That's it — the client now reads/writes the central brain over Tailscale.

**Antigravity (and Windsurf / Cursor — Codeium-lineage IDEs)** — edit `~/.gemini/antigravity/mcp_config.json`.
These IDEs' `mcp_config.json` is primarily stdio (`command`/`args`), so use the same `mcp-remote`
proxy to bridge stdio → the central HTTP server and inject `X-API-Key` (needs Node / npx):

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://100.87.59.15:9100/mcp", "--header", "X-API-Key: <the-key>"]
    }
  }
}
```

**Gemini CLI** — `~/.gemini/settings.json`, same `mcp-remote` form under `mcpServers`.

> ⚠️ **Never point any client at a local `python -m mcp_second_brain` stdio server** when the vault
> lives on Drive. Two problems, and the second is the dangerous one:
>
> 1. It lands on a **per-machine DuckDB** — a brain out of sync with the central Postgres.
> 2. It makes that machine a **second writer into the Drive-backed vault**. Concurrent writes to
>    Drive produce a silent lost update — the losing write disappears with *no conflict copy*.
>    All writes must go through the one central server.
>
> This is easy to get wrong because a stale stdio entry can sit in a config for months looking
> harmless (found exactly that in `~/.gemini/settings.json`, 2026-07-31). If you see
> `command: .../python`, `args: ["-m", "mcp_second_brain..."]` in any client config, replace it
> with the `mcp-remote` form above.
>
> Prerequisite: the client is a member of the same Tailscale tailnet (so the
> `100.87.59.15` address is reachable). Without a valid `X-API-Key` the server returns `401`.

**Verify a new client works:**

```text
health_check()      → "Vault accessible — N .md files found", "gap 0"
read_note("../../etc/hosts")   → "Error: path must be within the vault."   (containment is live)
```

---

## B. Central host setup (one-time, the machine that owns the data)

Only **one** machine plays this role (your always-on personal host). Set per-machine paths:

```bash
PJ="$HOME/Library/CloudStorage/GoogleDrive-<your-account>/我的雲端硬碟/PJ_save"
SB="$PJ/mcp-tools/second-brain"
VAULT="$PJ/second-brain"
```

### B1 — Postgres + pgvector (Docker)

```bash
docker run -d --name sb-pg \
  -e POSTGRES_PASSWORD=<pw> \
  -p 127.0.0.1:5432:5432 \
  -v "$HOME/sb-pgdata:/var/lib/postgresql/data" \
  pgvector/pgvector:pg16
docker exec sb-pg psql -U postgres -c "CREATE DATABASE sb_personal;"
docker exec -i sb-pg psql -U postgres -d sb_personal < "$SB/mcp_second_brain/store/postgres_schema.sql"
```

- ⚠️ **`-p` must be `127.0.0.1:5432:5432`** (localhost-only). `5432:5432` would expose Postgres on all interfaces.
- ⚠️ **Data dir on local SSD, never on Google Drive** (Drive sync corrupts a Postgres data dir).
- Verify: `docker exec sb-pg psql -U postgres -d sb_personal -c "SELECT extname FROM pg_extension;"` shows `vector` + `pg_trgm`.

### B2 — venv + dependencies (local, **never inside the Drive folder**)

```bash
python3 -m venv ~/.venvs/second-brain
~/.venvs/second-brain/bin/pip install -r "$SB/requirements.txt"   # includes psycopg[binary,pool]
~/.venvs/second-brain/bin/playwright install chromium             # for PNG snapshot rendering
```

> **PDF conversion dependencies** — `save_article` uses a three-tier pipeline:
> 1. **Marker** (ML, best quality) — from `requirements.txt`; ~1.35 GB models auto-download on first PDF save to `~/.cache/datalab/`.
> 2. **pdftotext / pdfinfo** (fallback) — `brew install poppler`.
> 3. **MarkItDown** (last fallback) — already in requirements.

### B3 — Embedding server

`nomic-embed-text` via llama-server on `:11435` (auto-started by the server when present), or
Ollama: `ollama pull nomic-embed-text` and set `EMBED_URL`/`EMBED_PORT`. Embeddings are 768-dim.

### B4 — Central HTTP server (launchd, KeepAlive)

The launchd job `com.user.second-brain-remote` runs the server bound to the Tailscale IP with:

```text
SB_DB_BACKEND=postgres
SB_PG_DSN=postgresql://postgres:<pw>@localhost:5432/sb_personal
SB_API_KEY=<generate a strong key>          # clients must send it as X-API-Key
SECOND_BRAIN_PATH=<VAULT>
```

Start script binds `--host <tailscale-ip> --port 9100 --transport streamable-http`. After
editing the plist's `EnvironmentVariables`, reload it (env changes need a full reload, not just
`kickstart`):

```bash
launchctl bootout  gui/$(id -u)/com.user.second-brain-remote
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.second-brain-remote.plist
```

The log (`/tmp/second-brain-remote.log`) should show `API-key auth ENABLED`.

### B5 — First index build

```bash
SB_DB_BACKEND=postgres SB_PG_DSN=… SECOND_BRAIN_PATH="$VAULT" \
  PYTHONPATH="$SB" ~/.venvs/second-brain/bin/python -c \
  "from mcp_second_brain.store import get_store; from pathlib import Path; import os; \
   print(get_store().sync_all(Path(os.environ['SECOND_BRAIN_PATH'])))"
```

Expect `{'synced': N, 'embed_failed': 0}`. (~900 notes ≈ 35 s once embeddings are fast.)

### B6 — Maintenance schedules (launchd)

| Job | Cadence | Purpose |
| --- | --- | --- |
| `com.user.second-brain-pg-sync` | every 30 min | `sync_incremental` against Postgres so cron-driven markdown edits don't let the index drift (`launchd/run_pg_sync.py`). |
| `com.user.second-brain-pg-backup` | daily 04:00 | `pg_dump \| gzip` of `sb_personal`/`sb_lab` to `PJ_save/backups/`, keep last 7 (`launchd/run_pg_backup.sh`). |
| `com.user.vault-janitor` / `com.user.vault-sleep` | weekly | Vault cleanup + Ebbinghaus compression. ⚠️ still write only DuckDB and bypass the store abstraction; `pg-sync` propagates their markdown edits into Postgres. |

---

## B-bis. Deploying code changes to the central host

**The running server does not import the source tree.** It imports a pip-installed copy in
`~/.venvs/second-brain/lib/python3.12/site-packages/mcp_second_brain`. Editing the source — or
letting Drive sync it — changes nothing until you reinstall. Restarting alone silently keeps
running the old code.

The deploy source on the central host is its **own local git clone**, `~/git-repos/second-brain`
(the `SB_DIR` variable in the start scripts is vestigial and unused).

```bash
ssh lab_center@100.87.59.15
cd ~/git-repos/second-brain
git log --oneline -3          # ← compare against your machine FIRST, see the warning below
~/.venvs/second-brain/bin/python -m pip install --no-deps --force-reinstall "$PWD"

# Restart ALL THREE — they share the same package (see the table in section A)
U=$(id -u)
for J in second-brain-remote lcdda-remote lcdda-harvest; do
  launchctl kickstart -k gui/$U/com.user.$J
done
```

Verify the new code is actually live (not just running):

```bash
~/.venvs/second-brain/bin/python -c \
  "import mcp_second_brain.server as s; print(len(s.WRITE_TOOLS))"   # 0 = stale, 17 = current
```

> ⚠️ **The central host's clone can hold commits your laptop doesn't.** On 2026-07-31 it had an
> auth fix that existed nowhere else — deploying over it would have silently reverted it. Always
> diff the two histories before pushing. To move commits between them (the host has no GitHub
> credentials, so GitHub can't be the intermediary):
>
> ```bash
> # on your machine — pull the host's work in first
> git fetch ssh://lab_center@100.87.59.15/Users/lab_center/git-repos/second-brain master
> git cherry-pick <their-commit>            # then run the tests
> # push yours over — a checked-out branch can't be pushed to directly
> git push ssh://lab_center@.../second-brain master:refs/heads/incoming
> ssh lab_center@... 'cd ~/git-repos/second-brain && git merge --ff-only incoming'
> ```

### Running the Postgres test suite

`tests/test_postgres_store.py` needs a live Postgres, so it only runs on the central host:

```bash
ssh lab_center@100.87.59.15
# one-time: the schema (incl. CREATE EXTENSION vector/pg_trgm) is applied automatically
docker exec sb-pg psql -U postgres -c "CREATE DATABASE sb_test;"

cd ~/git-repos/second-brain
# same credentials as the live DSN, only the database name differs
export SB_PG_TEST_DSN="$(python3 -c 'import plistlib,pathlib,re;print(re.sub(r"/sb_personal$","/sb_test",plistlib.loads((pathlib.Path.home()/"Library/LaunchAgents/com.user.second-brain-remote.plist").read_bytes())["EnvironmentVariables"]["SB_PG_DSN"]))')"
~/.venvs/second-brain/bin/python -m pytest tests -q
```

> ⚠️ Two traps. The fixture opens with `DELETE FROM notes` — the DSN **must** end in `/sb_test`
> (there is a hard guard against `sb_personal` / `sb_lab`, but check anyway). And the default DSN
> baked into the test file uses the password `postgres`, which is wrong here: it times out after
> 30 s and every test reports **skipped**. *17 skipped is not 17 passed.*

---

## C. Offline fallback (no network / Tailscale down)

The DuckDB backend is retained for offline read-only use. With no connectivity, run a **local
stdio** server with `SB_DB_BACKEND=duckdb` (rebuilds `~/.second-brain/vault.db` from synced
markdown via `sync_index`). Reconcile by re-running `sync_all` against Postgres once back online.
This is a deliberate degraded mode, not the normal path.

---

## Per-machine Data Summary

| Data | Location | Shared? |
| --- | --- | :---: |
| Vault markdown notes | Google Drive | ✅ all machines |
| Source code (`mcp_second_brain/`) | Google Drive | ✅ all machines |
| Postgres index | central host (Docker, local SSD) | ❌ central only — the single live index |
| Python venv / Docker / embedding | central host | ❌ host only (clients need none) |
| MCP config + API key | each client | ❌ per machine |

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Client `401 unauthorized` | missing/wrong `X-API-Key` | Add `--header "X-API-Key: <key>"` (Code) or the `--header` arg to `mcp-remote` (Desktop). |
| Client can't connect at all | not on the tailnet, or central server / Tailscale down | `tailscale status`; from the host check `pgrep -f streamable-http` and `/tmp/second-brain-remote.log`. |
| Server log says `API-key auth DISABLED` after setting the key | `kickstart` doesn't reload plist env | `launchctl bootout` + `bootstrap` (see B4). |
| Empty results right after host setup | index not built | Run the B5 `sync_all`. |
| `sync_all` crawls / many HTTP 500 from embed server | embed truncation vs token batch (fixed) | Ensure current Drive source (`_call_embed_api` has the 256-char tier + no backoff on deterministic 500). |
| Postgres index drifting from markdown | `pg-sync` job not loaded | `launchctl list \| grep pg-sync`; bootstrap `com.user.second-brain-pg-sync`. |
| Running pytest wiped the live index | old test default DSN pointed at `sb_personal` (fixed) | Tests now default to `sb_test` with a hard guard; never point `SB_PG_TEST_DSN` at `sb_personal`/`sb_lab`. |
| `read_note_as_image` snapshot fails (host) | playwright chromium not installed | `~/.venvs/second-brain/bin/playwright install chromium`. |
| Desktop `Operation not permitted` (if running a local stdio host) | `command` points to `.venv/bin/python` inside Drive | Use local `~/.venvs/second-brain/bin/python`. |
