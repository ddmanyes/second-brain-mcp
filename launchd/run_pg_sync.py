#!/usr/bin/env python3
"""run_pg_sync.py — keep the central Postgres index fresh against the markdown vault.

The central HTTP server only runs a full sync at startup (when the index is empty).
After that, nothing re-syncs Postgres — but the vault_janitor / vault_sleep cron jobs
edit markdown (archiving, status changes). Without a periodic incremental sync the
Postgres index would slowly drift away from the markdown source of truth.

This job runs `sync_incremental` against whatever backend SB_DB_BACKEND selects
(intended: postgres). It is a cheap no-op when nothing changed.

Env (set by the launchd plist):
  SB_DB_BACKEND=postgres
  SB_PG_DSN=postgresql://...:5432/sb_personal
  SECOND_BRAIN_PATH=/path/to/vault
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Make the package importable when launched by launchd from an arbitrary cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    vault = os.environ.get("SECOND_BRAIN_PATH")
    if not vault:
        print("[pg-sync] SECOND_BRAIN_PATH not set", file=sys.stderr)
        return 1

    from mcp_second_brain.store import get_store

    store = get_store()
    backend = type(store).__name__
    t0 = time.time()
    result: dict = {}
    for attempt in range(1, 4):
        try:
            result = store.sync_incremental(Path(vault))
            break
        except OSError as e:
            if attempt < 3:
                print(f"[pg-sync] OSError (attempt {attempt}/3): {e} — retrying in 30s", file=sys.stderr)
                time.sleep(30)
            else:
                print(f"[pg-sync] sync_incremental failed after 3 attempts ({backend}): {e}", file=sys.stderr)
                return 1
        except Exception as e:
            print(f"[pg-sync] sync_incremental failed ({backend}): {e}", file=sys.stderr)
            return 1
    print(f"[pg-sync] {backend} sync_incremental -> {result} in {time.time() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
