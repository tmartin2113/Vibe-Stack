#!/usr/bin/env bash
# backup.sh — Dump both Postgres databases and tar secrets.
# Intended for cron: 0 3 * * * /path/to/backup.sh
#
# Usage:  ./backup.sh [BACKUP_DIR]
#         BACKUP_DIR defaults to ./backups
#
# Produces:
#   <BACKUP_DIR>/<timestamp>_paperclip.sql.gz
#   <BACKUP_DIR>/<timestamp>_n8n.sql.gz
#   <BACKUP_DIR>/<timestamp>_secrets.tar.gz
#
# Retention: keeps the last 7 backups (configurable via KEEP_COUNT).

set -euo pipefail

BACKUP_DIR="${1:-./backups}"
KEEP_COUNT="${KEEP_COUNT:-7}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"

mkdir -p "$BACKUP_DIR"

log() { echo "[backup] $(date +%H:%M:%S) $*"; }

# ── Paperclip DB ─────────────────────────────────────────────────────────────
log "Dumping paperclip database..."
docker compose -f "$COMPOSE_FILE" exec -T db \
  pg_dump -U paperclip -d paperclip --no-owner --no-privileges \
  | gzip > "$BACKUP_DIR/${TIMESTAMP}_paperclip.sql.gz"
log "  → $(du -h "$BACKUP_DIR/${TIMESTAMP}_paperclip.sql.gz" | cut -f1)"

# ── n8n DB ───────────────────────────────────────────────────────────────────
# n8n postgres user is stored in a secret; read it from the local file.
N8N_USER="$(cat ./secrets/n8n_postgres_user.txt 2>/dev/null || echo "n8n")"
log "Dumping n8n database (user=$N8N_USER)..."
docker compose -f "$COMPOSE_FILE" exec -T postgres-n8n \
  pg_dump -U "$N8N_USER" -d n8n --no-owner --no-privileges \
  | gzip > "$BACKUP_DIR/${TIMESTAMP}_n8n.sql.gz"
log "  → $(du -h "$BACKUP_DIR/${TIMESTAMP}_n8n.sql.gz" | cut -f1)"

# ── Secrets ──────────────────────────────────────────────────────────────────
log "Archiving secrets directory..."
tar czf "$BACKUP_DIR/${TIMESTAMP}_secrets.tar.gz" -C . secrets/
log "  → $(du -h "$BACKUP_DIR/${TIMESTAMP}_secrets.tar.gz" | cut -f1)"

# ── Retention ────────────────────────────────────────────────────────────────
# Remove oldest backups beyond KEEP_COUNT (per suffix group).
for suffix in paperclip.sql.gz n8n.sql.gz secrets.tar.gz; do
  files=( $(ls -1t "$BACKUP_DIR"/*_"$suffix" 2>/dev/null) )
  if (( ${#files[@]} > KEEP_COUNT )); then
    for old in "${files[@]:$KEEP_COUNT}"; do
      log "Pruning old backup: $(basename "$old")"
      rm -f "$old"
    done
  fi
done

log "Backup complete. Files in $BACKUP_DIR:"
ls -lh "$BACKUP_DIR"/*"$TIMESTAMP"* 2>/dev/null
