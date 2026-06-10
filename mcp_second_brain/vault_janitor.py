"""
vault_janitor.py — Daily vault maintenance (non-destructive by default).

Tasks:
  1. Archive old individual stock analyses (keep newest per ticker)
  2. Warn about inbox notes overdue > 7 days (Telegram push)
  3. Detect naming violations in 20-areas/coding/ (Telegram push)
  4. Report vault_sleep candidates (print only, never auto-compress)

Run:
  python vault_janitor.py          # dry-run, print only
  python vault_janitor.py --push   # also send Telegram report
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import requests

sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VAULT = Path(
    os.environ.get("SECOND_BRAIN_PATH", "~/second-brain")
).expanduser()

STOCK_ANALYSIS_DIR = VAULT / "20-areas" / "personal" / "finance"
ARCHIVE_BASE = VAULT / "40-archive" / "finance"
INBOX_DIR = VAULT / "00-inbox"
CODING_DIR = VAULT / "20-areas" / "coding"

INBOX_OVERDUE_DAYS = 7
NAMING_VIOLATION_PATTERNS = [
    r"^stock-analyzer-",  # old slug
]


# ---------------------------------------------------------------------------
# Telegram helper
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("⚠️  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skipping push")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            print(f"⚠️  Telegram push HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"⚠️  Telegram push failed: {e}")


# ---------------------------------------------------------------------------
# Task 1: Archive old stock analyses
# ---------------------------------------------------------------------------

_STOCK_ANALYSIS_RE = re.compile(
    r"^([A-Z0-9\.\-]+)_analysis_(\d{8})\.md$", re.IGNORECASE
)


def archive_old_stock_analyses(dry_run: bool = True) -> list[str]:
    """Keep newest analysis per ticker; move older ones to 40-archive/finance/YYYYMM/."""
    by_ticker: dict[str, list[tuple[str, Path]]] = defaultdict(list)

    for f in STOCK_ANALYSIS_DIR.glob("*.md"):
        m = _STOCK_ANALYSIS_RE.match(f.name)
        if m:
            ticker, date_str = m.group(1).upper(), m.group(2)
            by_ticker[ticker].append((date_str, f))

    archived: list[str] = []
    for ticker, entries in by_ticker.items():
        if len(entries) <= 1:
            continue
        entries.sort(key=lambda x: x[0], reverse=True)  # newest first
        for date_str, path in entries[1:]:  # skip newest
            ym = date_str[:6]  # YYYYMM
            dest_dir = ARCHIVE_BASE / ym
            dest = dest_dir / path.name
            if dry_run:
                archived.append(f"[dry-run] would archive: {path.name} → 40-archive/finance/{ym}/")
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    archived.append(f"⚠️ skip (already exists): {dest.name}")
                    continue
                shutil.move(str(path), str(dest))
                archived.append(f"archived: {path.name} → 40-archive/finance/{ym}/")
    return archived


# ---------------------------------------------------------------------------
# Task 2: Inbox overdue warnings
# ---------------------------------------------------------------------------

def check_inbox_overdue() -> list[str]:
    """Return inbox notes older than INBOX_OVERDUE_DAYS days."""
    cutoff = datetime.now() - timedelta(days=INBOX_OVERDUE_DAYS)
    overdue: list[str] = []
    for f in INBOX_DIR.glob("*.md"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            age = (datetime.now() - mtime).days
            overdue.append(f"{f.name}  ({age}d)")
    return sorted(overdue)


# ---------------------------------------------------------------------------
# Task 3: Naming violation detection
# ---------------------------------------------------------------------------

def check_naming_violations() -> list[str]:
    """Detect files in 20-areas/coding/ that match old naming patterns."""
    violations: list[str] = []
    for f in CODING_DIR.glob("*.md"):
        for pattern in NAMING_VIOLATION_PATTERNS:
            if re.match(pattern, f.name):
                violations.append(f"{f.name}  (matches: {pattern})")
    return violations


# ---------------------------------------------------------------------------
# Task 4: Schema violation report
# ---------------------------------------------------------------------------

def check_schema_violations(top_n: int = 10) -> list[dict]:
    """Query DuckDB for notes with recorded violations."""
    try:
        db_path = Path.home() / ".second-brain" / "vault.db"
        if not db_path.exists():
            return []
        con = duckdb.connect(str(db_path))
        rows = con.execute(
            "SELECT path, violations FROM notes WHERE violations IS NOT NULL ORDER BY path LIMIT ?",
            [top_n],
        ).fetchall()
        con.close()
        return [{"path": r[0], "violations": r[1]} for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


# ---------------------------------------------------------------------------
# Task 5: Sleep candidates report
# ---------------------------------------------------------------------------

def get_sleep_candidates(top_n: int = 5) -> list[dict]:
    """Return top sleep candidates from vault_db (read-only, never compresses)."""
    try:
        from . import vault_db
        return vault_db.sleep_candidates(min_age_days=90, max_score=0.5)[:top_n]
    except Exception as e:
        return [{"error": str(e)}]


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(dry_run: bool = True) -> str:
    today = date.today().isoformat()
    lines = [f"🧹 <b>Vault Janitor — {today}</b>"]

    # Task 1
    archived = archive_old_stock_analyses(dry_run=dry_run)
    if archived:
        lines.append(f"\n📦 <b>個股舊版 archive（{'dry-run' if dry_run else '已執行'}）</b>")
        lines.extend(f"  • {a}" for a in archived)
    else:
        lines.append("\n📦 個股舊版：無需 archive")

    # Task 2
    overdue = check_inbox_overdue()
    if overdue:
        lines.append(f"\n⏰ <b>Inbox 逾期（>{INBOX_OVERDUE_DAYS}d）</b>")
        lines.extend(f"  • {o}" for o in overdue)
    else:
        lines.append("\n⏰ Inbox：無逾期筆記")

    # Task 3
    violations = check_naming_violations()
    if violations:
        lines.append("\n⚠️ <b>命名違規（20-areas/coding/）</b>")
        lines.extend(f"  • {v}" for v in violations)
    else:
        lines.append("\n✅ 命名規範：無違規")

    # Task 4: Schema violations
    schema_viols = check_schema_violations()
    if schema_viols and "error" not in schema_viols[0]:
        lines.append(f"\n⚠️ <b>Schema 違規（{len(schema_viols)} 筆）</b>")
        for v in schema_viols:
            lines.append(f"  • {v['path']} — {v['violations']}")
    elif schema_viols and "error" in schema_viols[0]:
        lines.append(f"\n⚠️ Schema 違規查詢失敗：{schema_viols[0]['error']}")
    else:
        lines.append("\n✅ Schema 違規：無")

    # Task 5: Sleep candidates
    candidates = get_sleep_candidates()
    if candidates and "error" not in candidates[0]:
        lines.append(f"\n💤 <b>Sleep 候選（top {len(candidates)}，請人工決定是否執行 vault_sleep）</b>")
        for c in candidates:
            lines.append(f"  • {Path(c['path']).name}  score={c['score']}  age={c['age_days']}d")
    elif candidates and "error" in candidates[0]:
        lines.append(f"\n💤 Sleep 候選：無法取得（{candidates[0]['error']}）")
    else:
        lines.append("\n💤 Sleep 候選：無")

    # Task 6: Sync vault index (Phase 12)
    try:
        from . import vault_db
        sync_result = vault_db.sync_all(VAULT)
        lines.append(
            f"\n🗂 <b>Vault 索引同步</b> — {sync_result['synced']} 筆"
            + (f"（⚠️ {sync_result['embed_failed']} 筆缺 embedding）" if sync_result['embed_failed'] else "")
        )
    except Exception as e:
        lines.append(f"\n🗂 Vault 索引同步失敗：{e}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Vault daily janitor")
    parser.add_argument("--push", action="store_true", help="Send report via Telegram")
    parser.add_argument("--execute", action="store_true",
                        help="Actually archive files (default: dry-run)")
    args = parser.parse_args()

    dry_run = not args.execute
    report = build_report(dry_run=dry_run)
    print(report)

    if args.push:
        _send_telegram(report)
        print("\n📤 Report sent via Telegram")


if __name__ == "__main__":
    main()
