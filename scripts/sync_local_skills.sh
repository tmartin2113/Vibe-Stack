#!/usr/bin/env bash
#
# sync_local_skills.sh — Pull upstream changes from skill source forks
# and rsync skills into vibe_skills/official/, then re-index.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCES_DIR="/home/prime/Repos/skill-sources"
DEST_DIR="$PROJECT_ROOT/vibe_skills/official"
LOG_FILE="$PROJECT_ROOT/logs/skill-sync.log"

mkdir -p "$DEST_DIR" "$(dirname "$LOG_FILE")"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

# Source repos and their skill paths
declare -A REPOS=(
    ["anthropics-skills"]="skills"
    ["obra-superpowers"]="skills"
    ["vercel-agent-skills"]="skills"
)

total_synced=0
total_skipped=0

for repo in "${!REPOS[@]}"; do
    skills_path="${REPOS[$repo]}"
    repo_dir="$SOURCES_DIR/$repo"

    if [[ ! -d "$repo_dir" ]]; then
        log "WARN: $repo_dir not found, skipping"
        continue
    fi

    log "--- Pulling upstream for $repo ---"

    # Pull latest from upstream
    if git -C "$repo_dir" remote get-url upstream &>/dev/null; then
        git -C "$repo_dir" fetch upstream main --quiet 2>>"$LOG_FILE" || {
            log "WARN: Failed to fetch upstream for $repo, using local state"
        }
        git -C "$repo_dir" merge upstream/main --no-edit --quiet 2>>"$LOG_FILE" || {
            log "WARN: Merge conflict in $repo, using current state"
        }
    else
        log "WARN: No upstream remote for $repo, skipping pull"
    fi

    # Sync each skill directory
    src_skills_dir="$repo_dir/$skills_path"
    if [[ ! -d "$src_skills_dir" ]]; then
        log "WARN: $src_skills_dir not found, skipping"
        continue
    fi

    for skill_dir in "$src_skills_dir"/*/; do
        skill_name="$(basename "$skill_dir")"

        # Skip template directory (handled separately)
        if [[ "$skill_name" == "template" ]]; then
            log "  Skipping template/ (handled separately)"
            continue
        fi

        # Skip non-directories (e.g. .zip files)
        if [[ ! -d "$skill_dir" ]]; then
            continue
        fi

        # Must have SKILL.md
        if [[ ! -f "$skill_dir/SKILL.md" ]]; then
            log "  Skipping $skill_name: no SKILL.md"
            ((total_skipped++)) || true
            continue
        fi

        # rsync into official dir (--delete removes files no longer in source)
        rsync -a --delete \
            --exclude='*.zip' \
            --exclude='.git' \
            "$skill_dir" "$DEST_DIR/$skill_name/"

        log "  Synced: $skill_name (from $repo)"
        ((total_synced++)) || true
    done
done

log ""
log "=== Sync complete: $total_synced skills synced, $total_skipped skipped ==="

# Re-index from disk
log "Re-indexing skills from disk..."
python3 "$SCRIPT_DIR/update_official_skills.py" --local --verbose 2>&1 | tee -a "$LOG_FILE"

log "Done."
