#!/usr/bin/env bash
#
# Vibe Backup & Restore
#
# Backs up all persistent data: sessions DB, skill registry,
# skill outcomes, training data, and API keys.
#
# Usage:
#   ./scripts/backup-restore.sh backup  [backup-dir]
#   ./scripts/backup-restore.sh restore <backup-dir>
#   ./scripts/backup-restore.sh list    [backup-dir]
#
# Environment:
#   VIBE_HOME      — Override ~/.vibe (default: ~/.vibe)
#   VIBE_SKILLS    — Override skill registry (default: ./vibe_skills)
#   VIBE_BACKUPS   — Override backup root (default: ./backups)

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────

VIBE_HOME="${VIBE_HOME:-$HOME/.vibe}"
VIBE_SKILLS="${VIBE_SKILLS:-$(pwd)/vibe_skills}"
VIBE_BACKUPS="${VIBE_BACKUPS:-$(pwd)/backups}"
SKILL_OUTCOMES="${HOME}/.local/share/vibe_skills"
TRAINING_DATA="$(pwd)/training/data/pipeline"

# ── Helpers ─────────────────────────────────────────────────────────

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
err()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" >&2; }
die()  { err "$@"; exit 1; }

usage() {
    cat <<'USAGE'
Usage:
  backup-restore.sh backup  [backup-dir]   Create a timestamped backup
  backup-restore.sh restore <backup-dir>   Restore from a backup directory
  backup-restore.sh list    [backup-root]  List available backups

Backs up:
  ~/.vibe/sessions.db          Session store (SQLite)
  ~/.vibe/api_keys.json        API keys (preserved permissions)
  vibe_skills/                 Skill registry (all tiers + index)
  ~/.local/share/vibe_skills/  Skill outcome store (JSONL)
  training/data/pipeline/         SFT/DPO training data (JSONL)
USAGE
    exit 1
}

# ── Backup ──────────────────────────────────────────────────────────

do_backup() {
    local target="${1:-}"
    if [ -z "$target" ]; then
        local timestamp
        timestamp="$(date '+%Y%m%d-%H%M%S')"
        target="${VIBE_BACKUPS}/vibe-backup-${timestamp}"
    fi

    mkdir -p "$target"
    log "Starting backup → $target"

    local count=0

    # Session store (SQLite — use .backup for WAL-safe copy)
    if [ -f "${VIBE_HOME}/sessions.db" ]; then
        log "  Backing up sessions.db"
        # sqlite3 .backup produces a consistent snapshot even with WAL mode
        if command -v sqlite3 &>/dev/null; then
            sqlite3 "${VIBE_HOME}/sessions.db" ".backup '${target}/sessions.db'"
        else
            cp "${VIBE_HOME}/sessions.db" "${target}/sessions.db"
            # Also grab WAL/SHM if present
            [ -f "${VIBE_HOME}/sessions.db-wal" ] && cp "${VIBE_HOME}/sessions.db-wal" "${target}/"
            [ -f "${VIBE_HOME}/sessions.db-shm" ] && cp "${VIBE_HOME}/sessions.db-shm" "${target}/"
        fi
        count=$((count + 1))
    fi

    # API keys (preserve permissions)
    if [ -f "${VIBE_HOME}/api_keys.json" ]; then
        log "  Backing up api_keys.json"
        cp -p "${VIBE_HOME}/api_keys.json" "${target}/api_keys.json"
        count=$((count + 1))
    fi

    # Skill registry
    if [ -d "${VIBE_SKILLS}" ]; then
        log "  Backing up skill registry"
        mkdir -p "${target}/vibe_skills"
        # Copy only local and official tiers + index (skip temp)
        for tier in official local; do
            if [ -d "${VIBE_SKILLS}/${tier}" ]; then
                cp -r "${VIBE_SKILLS}/${tier}" "${target}/vibe_skills/${tier}"
            fi
        done
        [ -f "${VIBE_SKILLS}/.index.json" ] && cp "${VIBE_SKILLS}/.index.json" "${target}/vibe_skills/"
        count=$((count + 1))
    fi

    # Skill outcome store
    if [ -d "${SKILL_OUTCOMES}" ]; then
        log "  Backing up skill outcomes"
        mkdir -p "${target}/skill_outcomes"
        cp "${SKILL_OUTCOMES}"/*.jsonl "${target}/skill_outcomes/" 2>/dev/null || true
        count=$((count + 1))
    fi

    # Training data
    if [ -d "${TRAINING_DATA}" ]; then
        log "  Backing up training data"
        mkdir -p "${target}/training_data"
        cp "${TRAINING_DATA}"/*.jsonl "${target}/training_data/" 2>/dev/null || true
        count=$((count + 1))
    fi

    # Write manifest
    cat > "${target}/MANIFEST.json" <<EOF
{
  "created_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "vibe_home": "${VIBE_HOME}",
  "skill_dir": "${VIBE_SKILLS}",
  "items_backed_up": ${count},
  "hostname": "$(hostname)"
}
EOF

    log "Backup complete: ${count} item(s) → ${target}"
    echo "$target"
}

# ── Restore ─────────────────────────────────────────────────────────

do_restore() {
    local source="${1:-}"
    [ -z "$source" ] && die "Usage: backup-restore.sh restore <backup-dir>"
    [ ! -d "$source" ] && die "Backup directory not found: $source"
    [ ! -f "$source/MANIFEST.json" ] && die "Not a valid backup (missing MANIFEST.json): $source"

    log "Restoring from $source"

    # Session store
    if [ -f "${source}/sessions.db" ]; then
        log "  Restoring sessions.db"
        mkdir -p "${VIBE_HOME}"
        cp "${source}/sessions.db" "${VIBE_HOME}/sessions.db"
        # Remove stale WAL/SHM
        rm -f "${VIBE_HOME}/sessions.db-wal" "${VIBE_HOME}/sessions.db-shm"
    fi

    # API keys (preserve permissions)
    if [ -f "${source}/api_keys.json" ]; then
        log "  Restoring api_keys.json"
        mkdir -p "${VIBE_HOME}"
        cp -p "${source}/api_keys.json" "${VIBE_HOME}/api_keys.json"
        chmod 600 "${VIBE_HOME}/api_keys.json"
    fi

    # Skill registry
    if [ -d "${source}/vibe_skills" ]; then
        log "  Restoring skill registry"
        mkdir -p "${VIBE_SKILLS}"
        for tier in official local; do
            if [ -d "${source}/vibe_skills/${tier}" ]; then
                cp -r "${source}/vibe_skills/${tier}" "${VIBE_SKILLS}/${tier}"
            fi
        done
        [ -f "${source}/vibe_skills/.index.json" ] && cp "${source}/vibe_skills/.index.json" "${VIBE_SKILLS}/"
    fi

    # Skill outcomes
    if [ -d "${source}/skill_outcomes" ]; then
        log "  Restoring skill outcomes"
        mkdir -p "${SKILL_OUTCOMES}"
        cp "${source}/skill_outcomes/"*.jsonl "${SKILL_OUTCOMES}/" 2>/dev/null || true
    fi

    # Training data
    if [ -d "${source}/training_data" ]; then
        log "  Restoring training data"
        mkdir -p "${TRAINING_DATA}"
        cp "${source}/training_data/"*.jsonl "${TRAINING_DATA}/" 2>/dev/null || true
    fi

    log "Restore complete from: $source"
}

# ── List ────────────────────────────────────────────────────────────

do_list() {
    local root="${1:-${VIBE_BACKUPS}}"
    if [ ! -d "$root" ]; then
        log "No backups directory found at: $root"
        return 0
    fi

    echo "Available backups in ${root}:"
    echo ""

    local found=0
    for dir in "${root}"/vibe-backup-*; do
        [ ! -d "$dir" ] && continue
        found=1
        local name
        name="$(basename "$dir")"
        local manifest="${dir}/MANIFEST.json"
        if [ -f "$manifest" ]; then
            local created items
            created="$(grep -o '"created_at": *"[^"]*"' "$manifest" | cut -d'"' -f4)"
            items="$(grep -o '"items_backed_up": *[0-9]*' "$manifest" | grep -o '[0-9]*')"
            printf "  %-30s  %s  (%s items)\n" "$name" "$created" "$items"
        else
            printf "  %-30s  (no manifest)\n" "$name"
        fi
    done

    if [ "$found" -eq 0 ]; then
        echo "  (no backups found)"
    fi
}

# ── Main ────────────────────────────────────────────────────────────

case "${1:-}" in
    backup)  shift; do_backup "$@" ;;
    restore) shift; do_restore "$@" ;;
    list)    shift; do_list "$@" ;;
    *)       usage ;;
esac
