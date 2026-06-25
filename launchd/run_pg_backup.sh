#!/bin/bash
# run_pg_backup.sh — daily pg_dump of the central brain Postgres to Drive.
#
# markdown remains the source of truth; this dump only speeds up recovery (restore
# the index without re-running sync_all over the whole vault). Keeps the last N dumps.
#
# Env (optional):
#   SB_PG_CONTAINER   docker container name (default: sb-pg)
#   SB_PG_DATABASES   space-separated db list (default: "sb_personal sb_lab")
#   SB_BACKUP_DIR     output dir (default: <PJ_save>/backups/second-brain-pg)
#   SB_BACKUP_KEEP    how many dumps per db to keep (default: 7)
set -uo pipefail

CONTAINER="${SB_PG_CONTAINER:-sb-pg}"
DATABASES="${SB_PG_DATABASES:-sb_personal sb_lab}"
KEEP="${SB_BACKUP_KEEP:-7}"
_SD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_DIR="$(cd "$_SD/../../.." && pwd)/backups/second-brain-pg"
BACKUP_DIR="${SB_BACKUP_DIR:-$DEFAULT_DIR}"

DOCKER="${DOCKER_BIN:-/usr/local/bin/docker}"
[ -x "$DOCKER" ] || DOCKER="$(command -v docker || echo docker)"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"

for DB in $DATABASES; do
  # skip databases that don't exist (e.g. sb_lab only on the work host)
  if ! "$DOCKER" exec "$CONTAINER" psql -U postgres -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw "$DB"; then
    echo "[pg-backup] skip $DB (not present)"
    continue
  fi
  OUT="$BACKUP_DIR/${DB}-${STAMP}.sql.gz"
  if "$DOCKER" exec "$CONTAINER" pg_dump -U postgres "$DB" | gzip > "$OUT"; then
    echo "[pg-backup] wrote $OUT ($(du -h "$OUT" | cut -f1))"
  else
    echo "[pg-backup] FAILED dumping $DB" >&2
    rm -f "$OUT"
    continue
  fi
  # rotation: keep newest $KEEP for this db
  ls -1t "$BACKUP_DIR/${DB}-"*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    echo "[pg-backup] prune $old"
    rm -f "$old"
  done
done
