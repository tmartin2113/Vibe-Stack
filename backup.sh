#!/usr/bin/env bash
# backup.sh — Full Vibe Stack backup (all data stores).
# Intended for systemd timer: scripts/vibe-backup.timer
#
# Usage:  ./backup.sh [BACKUP_DIR]
#         BACKUP_DIR defaults to ./backups
#
# Produces:
#   <BACKUP_DIR>/<timestamp>_paperclip.sql.gz
#   <BACKUP_DIR>/<timestamp>_penpot.sql.gz
#   <BACKUP_DIR>/<timestamp>_gitea.db.gz
#   <BACKUP_DIR>/<timestamp>_minio.tar.gz
#   <BACKUP_DIR>/<timestamp>_vibe-data.tar.gz
#   <BACKUP_DIR>/<timestamp>_bulletin-data.tar.gz
#   <BACKUP_DIR>/<timestamp>_secrets.tar.gz
#   <BACKUP_DIR>/<timestamp>_manifest.txt
#
# Retention: keeps the last 7 backups (configurable via KEEP_COUNT).

set -euo pipefail

BACKUP_DIR="${1:-./backups}"
KEEP_COUNT="${KEEP_COUNT:-7}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
MANIFEST=""
ERRORS=0

# Encryption: set BACKUP_AGE_RECIPIENT to an age public key to encrypt backups.
# Generate one via: age-keygen (or run scripts/secrets-init.sh)
BACKUP_AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:-}"

# Off-site replication: set BACKUP_RCLONE_REMOTE to sync backups to cloud storage.
# Example: BACKUP_RCLONE_REMOTE=b2:vibe-stack-backups
BACKUP_RCLONE_REMOTE="${BACKUP_RCLONE_REMOTE:-}"

mkdir -p "$BACKUP_DIR"

log() { echo "[backup] $(date +%H:%M:%S) $*"; }

record() {
  local file="$1"
  local size checksum
  size="$(du -h "$file" | cut -f1)"
  checksum="$(sha256sum "$file" | cut -d' ' -f1)"
  log "  → $size ($checksum)"
  MANIFEST+="$(basename "$file")  $size  $checksum"$'\n'
}

try_backup() {
  local label="$1"; shift
  log "Backing up $label..."
  if ! "$@"; then
    log "  ✗ FAILED: $label"
    ERRORS=$((ERRORS + 1))
    return 1
  fi
}

# ── Paperclip Embedded Postgres ────────────────────────────────────
# Paperclip uses embedded-postgres inside the server container.
# Dump via pg_dump connecting to its internal port.
try_backup "Paperclip database" bash -c '
  docker compose exec -T server \
    pg_dump -h 127.0.0.1 -p 54329 -U paperclip -d paperclip --no-owner --no-privileges \
    | gzip > "'"$BACKUP_DIR/${TIMESTAMP}_paperclip.sql.gz"'"
' && record "$BACKUP_DIR/${TIMESTAMP}_paperclip.sql.gz"

# ── Penpot Postgres ────────────────────────────────────────────────
try_backup "Penpot database" bash -c '
  docker compose exec -T penpot-postgres \
    pg_dump -U penpot -d penpot --no-owner --no-privileges \
    | gzip > "'"$BACKUP_DIR/${TIMESTAMP}_penpot.sql.gz"'"
' && record "$BACKUP_DIR/${TIMESTAMP}_penpot.sql.gz"

# ── Gitea SQLite ───────────────────────────────────────────────────
try_backup "Gitea database" bash -c '
  docker compose exec -T gitea \
    sqlite3 /data/gitea/gitea.db ".backup '"'"'/tmp/gitea-backup.db'"'"'" &&
  docker compose cp gitea:/tmp/gitea-backup.db /tmp/gitea-backup.db &&
  gzip -c /tmp/gitea-backup.db > "'"$BACKUP_DIR/${TIMESTAMP}_gitea.db.gz"'" &&
  rm -f /tmp/gitea-backup.db
' && record "$BACKUP_DIR/${TIMESTAMP}_gitea.db.gz"

# ── MinIO Object Store ────────────────────────────────────────────
# Backup the MinIO data volume directly via docker cp
try_backup "MinIO data" bash -c '
  MINIO_CID=$(docker compose ps -q minio) &&
  docker cp "$MINIO_CID:/data" /tmp/minio-backup &&
  tar czf "'"$BACKUP_DIR/${TIMESTAMP}_minio.tar.gz"'" -C /tmp minio-backup &&
  rm -rf /tmp/minio-backup
' && record "$BACKUP_DIR/${TIMESTAMP}_minio.tar.gz"

# ── Vibe Data (SQLite databases) ──────────────────────────────────
# Contains: sessions.db, memory.db, spending_ledger.db, skills, etc.
# Use sqlite3 .backup for WAL-safe copies
try_backup "Vibe agent data" bash -c '
  VIBE_CID=$(docker compose ps -q vibe 2>/dev/null || true)
  if [ -z "$VIBE_CID" ]; then
    # Agent not running — backup volume directly via a temp container
    VIBE_CID=$(docker create --rm -v vibe-data:/data alpine:3 sleep 1)
    docker start "$VIBE_CID" >/dev/null
    docker cp "$VIBE_CID:/data" /tmp/vibe-data-backup
    docker stop "$VIBE_CID" >/dev/null 2>&1 || true
  else
    # Agent running — use sqlite3 .backup for WAL safety
    for db in sessions.db memory.db spending_ledger.db; do
      docker exec "$VIBE_CID" sh -c "
        if [ -f /home/vibe/.vibe/$db ]; then
          sqlite3 /home/vibe/.vibe/$db \".backup /tmp/$db\"
        fi
      " 2>/dev/null || true
    done
    docker cp "$VIBE_CID:/home/vibe/.vibe" /tmp/vibe-data-backup
  fi
  tar czf "'"$BACKUP_DIR/${TIMESTAMP}_vibe-data.tar.gz"'" -C /tmp vibe-data-backup
  rm -rf /tmp/vibe-data-backup
' && record "$BACKUP_DIR/${TIMESTAMP}_vibe-data.tar.gz"

# ── Bulletin Data ─────────────────────────────────────────────────
# Contains: BULLETIN.md, messages.db
try_backup "Bulletin data" bash -c '
  BULLETIN_CID=$(docker compose ps -q vibe 2>/dev/null || true)
  if [ -z "$BULLETIN_CID" ]; then
    BULLETIN_CID=$(docker create --rm -v bulletin-data:/data alpine:3 sleep 1)
    docker start "$BULLETIN_CID" >/dev/null
    mkdir -p /tmp/bulletin-backup
    docker cp "$BULLETIN_CID:/data/." /tmp/bulletin-backup/ 2>/dev/null || true
    docker stop "$BULLETIN_CID" >/dev/null 2>&1 || true
  else
    # Use sqlite3 .backup for WAL safety on messages.db
    docker exec "$BULLETIN_CID" sh -c "
      if [ -f /shared/bulletin/messages.db ]; then
        sqlite3 /shared/bulletin/messages.db \".backup /tmp/bulletin-messages.db\"
      fi
    " 2>/dev/null || true
    mkdir -p /tmp/bulletin-backup
    docker cp "$BULLETIN_CID:/shared/bulletin/." /tmp/bulletin-backup/ 2>/dev/null || true
  fi
  tar czf "'"$BACKUP_DIR/${TIMESTAMP}_bulletin-data.tar.gz"'" -C /tmp bulletin-backup
  rm -rf /tmp/bulletin-backup
' && record "$BACKUP_DIR/${TIMESTAMP}_bulletin-data.tar.gz"

# ── Secrets ────────────────────────────────────────────────────────
log "Archiving secrets directory..."
tar czf "$BACKUP_DIR/${TIMESTAMP}_secrets.tar.gz" -C . secrets/
record "$BACKUP_DIR/${TIMESTAMP}_secrets.tar.gz"

# ── Manifest ───────────────────────────────────────────────────────
log "Writing backup manifest..."
{
  echo "# Vibe Stack Backup Manifest"
  echo "# Timestamp: $TIMESTAMP"
  echo "# Date: $(date -Iseconds)"
  echo "#"
  echo "# filename  size  sha256"
  echo "$MANIFEST"
} > "$BACKUP_DIR/${TIMESTAMP}_manifest.txt"

# ── Encryption ─────────────────────────────────────────────────────
# Encrypt all backup files with age if BACKUP_AGE_RECIPIENT is set.
if [[ -n "$BACKUP_AGE_RECIPIENT" ]] && command -v age &>/dev/null; then
  log "Encrypting backups with age..."
  for file in "$BACKUP_DIR"/${TIMESTAMP}_*; do
    [[ "$file" == *.age ]] && continue  # skip already encrypted
    [[ "$file" == *_manifest.txt ]] && continue  # manifest stays readable
    if age -r "$BACKUP_AGE_RECIPIENT" -o "${file}.age" "$file" 2>/dev/null; then
      rm -f "$file"
      log "  Encrypted: $(basename "${file}.age")"
    else
      log "  WARNING: Failed to encrypt $(basename "$file")"
    fi
  done
elif [[ -n "$BACKUP_AGE_RECIPIENT" ]]; then
  log "WARNING: BACKUP_AGE_RECIPIENT set but 'age' not installed — backups NOT encrypted"
fi

# ── Retention ──────────────────────────────────────────────────────
# Remove oldest backups beyond KEEP_COUNT (per suffix group).
for suffix in paperclip.sql.gz penpot.sql.gz gitea.db.gz minio.tar.gz \
              vibe-data.tar.gz bulletin-data.tar.gz secrets.tar.gz manifest.txt \
              paperclip.sql.gz.age penpot.sql.gz.age gitea.db.gz.age minio.tar.gz.age \
              vibe-data.tar.gz.age bulletin-data.tar.gz.age secrets.tar.gz.age; do
  mapfile -t files < <(ls -1t "$BACKUP_DIR"/*_"$suffix" 2>/dev/null)
  if (( ${#files[@]} > KEEP_COUNT )); then
    for old in "${files[@]:$KEEP_COUNT}"; do
      log "Pruning old backup: $(basename "$old")"
      rm -f "$old"
    done
  fi
done

# ── Off-site Replication ──────────────────────────────────────────
# Sync backup directory to cloud storage via rclone.
if [[ -n "$BACKUP_RCLONE_REMOTE" ]] && command -v rclone &>/dev/null; then
  log "Replicating backups to $BACKUP_RCLONE_REMOTE..."
  if rclone sync "$BACKUP_DIR" "$BACKUP_RCLONE_REMOTE" \
    --transfers 4 --checkers 4 --log-level NOTICE 2>&1; then
    log "  Replication complete"
  else
    log "  WARNING: Replication failed"
    ERRORS=$((ERRORS + 1))
  fi
elif [[ -n "$BACKUP_RCLONE_REMOTE" ]]; then
  log "WARNING: BACKUP_RCLONE_REMOTE set but 'rclone' not installed — backups NOT replicated"
fi

# ── Summary ────────────────────────────────────────────────────────
log "Backup complete. Files in $BACKUP_DIR:"
ls -lh "$BACKUP_DIR"/*"$TIMESTAMP"* 2>/dev/null

if (( ERRORS > 0 )); then
  log "WARNING: $ERRORS backup(s) failed — check output above"
  exit 1
fi
