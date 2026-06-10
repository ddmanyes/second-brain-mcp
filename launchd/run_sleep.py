#!/usr/bin/env python3
"""
run_sleep.py — Standalone vault maintenance runner.

Called by launchd every Sunday 02:00.
No LLM orchestration: calls Python functions directly.
Compression backend: Gemini CLI → Claude CLI → naive (auto-fallback in vault_sleep.py).

Requires: embedding server already running (see examples/launchd/llama-embed.plist).
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_second_brain import vault_db
from mcp_second_brain import vault_sleep as _vs

VAULT = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    Path.home() / "second-brain",
)).expanduser().resolve()

LOG_DIR = Path.home() / ".second-brain" / "logs"


def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"vault_sleep_{stamp}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Prune: keep last 10 logs
    for old in sorted(LOG_DIR.glob("vault_sleep_*.log"))[:-10]:
        old.unlink(missing_ok=True)

    return logging.getLogger(__name__)


def main() -> None:
    log = _setup_logging()
    log.info("vault sleep — vault: %s", VAULT)

    if not VAULT.exists():
        log.error("Vault not found: %s", VAULT)
        sys.exit(1)

    # Step 1: index
    log.info("[1/4] Syncing index...")
    n = vault_db.sync_all(VAULT)
    synced = n["synced"] if isinstance(n, dict) else n
    log.info("      %d files indexed", synced)

    # Step 2: embeddings (llama-server expected to be up via launchd)
    log.info("[2/4] Syncing embeddings...")
    emb = vault_db.sync_embeddings(vault=VAULT)
    log.info("      +%d new  %d failed", emb["updated"], emb["failed"])

    # Step 3: compress old low-activity notes
    log.info("[3/4] Running vault sleep (compression)...")
    result = _vs.run_sleep(VAULT)
    log.info(
        "      candidates=%d  compressed=%d  skipped=%d  errors=%d",
        result["candidates"], result["processed"], result["skipped"], result["errors"],
    )
    for entry in result.get("log", []):
        status = entry["status"]
        if status == "compressed":
            snap = "📷 " if entry.get("snapshot") else ""
            log.info("  ✓ %s[%s] %s (age %dd)", snap, entry["tier"], entry["path"], entry["age"])
        elif status == "skipped_high_score":
            log.info("  ⭐ kept full text: %s (score %.2f)", entry["path"], entry["score"])
        elif status == "error":
            log.warning("  ✗ %s — %s", entry["path"], entry.get("reason", "unknown"))

    # Step 4: L3 rules extraction (only if something was compressed)
    if result["processed"] > 0:
        log.info("[4/4] Extracting L3 rules...")
        rules = _vs.run_rules_extraction(VAULT)
        log.info("      %d rules from %d notes → memory/rules.md", rules["total_rules"], rules["processed"])
    else:
        log.info("[4/4] No compression — skipping rules extraction.")

    log.info("Done.")


if __name__ == "__main__":
    main()
