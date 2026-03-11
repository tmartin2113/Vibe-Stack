#!/usr/bin/env bash
# workspace-watchdog.sh
#
# Monitors /workspace for changes.
# Commits are GATED — only triggered by Open WebUI pipe approval signal.
# The pipe writes a .commit-approved sentinel file when user approves.
#
# Normal file changes: tracked but NOT committed (agent is still working)
# Approval signal:    .commit-approved appears → commit + push principal-coder
# Rejection signal:   .commit-rejected appears → reset staged changes, clean up

set -euo pipefail

WATCH_DIR="${WATCH_DIR:-/srv/sftp/workspace/files}"
BRANCH_NAME="principal-coder"
LOG_FILE="/var/log/vibe-watchdog.log"
MAX_LOG_BYTES=$((10 * 1024 * 1024))
DEPLOY_KEYS_DIR="${DEPLOY_KEYS_DIR:-/root/.ssh/deploy-keys}"
GIT_USER="${GIT_USER:-tmartin2113}"

log() {
    local level="$1"; shift
    local ts; ts=$(date '+%Y-%m-%dT%H:%M:%S')
    printf '[%s] [%-5s] %s\n' "$ts" "$level" "$*" | tee -a "$LOG_FILE"
    if [[ -f "$LOG_FILE" ]]; then
        local size; size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
        if (( size > MAX_LOG_BYTES )); then
            mv "$LOG_FILE" "${LOG_FILE}.1"; : > "$LOG_FILE"
        fi
    fi
}

info()  { log "INFO"  "$*"; }
warn()  { log "WARN"  "$*"; }
error() { log "ERROR" "$*"; }

# ── Git credential setup ──────────────────────────────────────
configure_credentials() {
    local ssh_dir="/root/.ssh"
    local deploy_keys_dir="${DEPLOY_KEYS_DIR}"

    [[ ! -d "$deploy_keys_dir" ]] && warn "Deploy keys dir not found: $deploy_keys_dir — remote push disabled" && return 1

    mkdir -p "$ssh_dir"
    cat > "$ssh_dir/config" << 'SSHEOF'
Host github.com
  User git
  IdentitiesOnly yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
SSHEOF

    local key_count=0
    for key in "$deploy_keys_dir"/*; do
        [ -f "$key" ] || continue
        case "$key" in *.pub) continue ;; esac
        echo "  IdentityFile $key" >> "$ssh_dir/config"
        key_count=$((key_count + 1))
    done

    chmod 600 "$ssh_dir/config"
    [[ $key_count -eq 0 ]] && warn "No deploy keys found — remote push disabled" && return 1
    info "SSH configured with $key_count deploy key(s)"
    return 0
}

is_git_repo() {
    git -C "$WATCH_DIR" rev-parse --is-inside-work-tree &>/dev/null
}

get_remote_url() {
    git -C "$WATCH_DIR" remote get-url origin 2>/dev/null || echo ""
}

normalize_remote_url() {
    local url="$1"
    # Convert HTTPS to SSH format for deploy key auth
    if [[ "$url" =~ ^https://github\.com/(.+)$ ]]; then
        echo "git@github.com:${BASH_REMATCH[1]}"
    elif [[ "$url" =~ ^git@github\.com:(.+)$ ]]; then
        echo "$url"
    else
        echo "$url"
    fi
}

# ── Approval-gated commit + push ─────────────────────────────
commit_and_push() {
    if ! is_git_repo; then
        warn "Not a git repo — skipping"
        return 0
    fi

    cd "$WATCH_DIR"
    git add -A

    if [[ -z "$(git status --porcelain)" ]]; then
        info "Nothing to commit after approval"
        return 0
    fi

    local changed_files; changed_files=$(git diff --cached --name-only | head -10 | tr '\n' ' ')
    local count; count=$(git diff --cached --name-only | wc -l)
    local msg="approved[$(date '+%Y-%m-%dT%H:%M:%S')]: ${count} file(s) — ${changed_files}"

    git commit -m "$msg" --quiet
    info "Committed: $msg"

    local remote_url; remote_url=$(get_remote_url)
    [[ -z "$remote_url" ]] && info "No remote — local only" && return 0

    remote_url=$(normalize_remote_url "$remote_url")

    if ! configure_credentials; then
        info "No credentials — local commit only"
        return 0
    fi

    # Push to principal-coder branch on whatever remote is configured
    if ! git show-ref --verify --quiet "refs/heads/$BRANCH_NAME"; then
        local current; current=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
        git branch "$BRANCH_NAME" "$current" 2>/dev/null || true
    fi

    if git push "$remote_url" "HEAD:refs/heads/$BRANCH_NAME" --force-with-lease --quiet 2>/dev/null; then
        info "Pushed to $remote_url → $BRANCH_NAME"
    else
        warn "Remote push failed — local commit preserved"
    fi
}

# ── Rejection handler ─────────────────────────────────────────
handle_rejection() {
    if ! is_git_repo; then return 0; fi
    cd "$WATCH_DIR"
    # Reset staged changes — don't commit rejected work
    git reset HEAD --quiet 2>/dev/null || true
    info "Rejection signal received — staged changes reset, no commit"
}

# ── Sentinel file processor ───────────────────────────────────
process_sentinel() {
    local sentinel="$1"

    if [[ "$sentinel" == *".commit-approved" ]]; then
        info "Approval signal received — committing"
        commit_and_push
        rm -f "$WATCH_DIR/.commit-approved"
    elif [[ "$sentinel" == *".commit-rejected" ]]; then
        info "Rejection signal received — resetting"
        handle_rejection
        rm -f "$WATCH_DIR/.commit-rejected"
    fi
}

# ── Main watch loop ───────────────────────────────────────────
main() {
    [[ ! -d "$WATCH_DIR" ]] && error "Watch dir not found: $WATCH_DIR" && exit 1
    command -v inotifywait &>/dev/null || error "inotify-tools not installed"

    configure_credentials || warn "Starting without GitHub credentials"

    info "Watchdog started | dir=$WATCH_DIR | branch=$BRANCH_NAME | approval-gated=true"

    while IFS= read -r line; do
        info "Event: $line"

        # Check for sentinel files specifically
        if [[ "$line" == *".commit-approved"* ]] || [[ "$line" == *".commit-rejected"* ]]; then
            sleep 0.5  # Brief delay to ensure write is complete
            if [[ "$line" == *".commit-approved"* ]]; then
                process_sentinel ".commit-approved"
            else
                process_sentinel ".commit-rejected"
            fi
        fi
        # All other file changes: log only, no commit
        # Commits only happen when Open WebUI pipe sends approval signal

    done < <(
        inotifywait \
            --monitor \
            --recursive \
            --quiet \
            --format '%w%f %e' \
            --event create,modify,close_write,delete,move \
            --exclude '/\.git/' \
            "$WATCH_DIR" 2>&1
    )

    error "inotifywait exited"
    exit 1
}

main

# ══════════════════════════════════════════════════════════════════
# /etc/systemd/system/workspace-watchdog.service
# ══════════════════════════════════════════════════════════════════
# [Unit]
# Description=Vibe Workspace Git Watchdog
# After=network.target docker.service
#
# [Service]
# Type=simple
# ExecStart=/usr/local/bin/workspace-watchdog.sh
# Restart=always
# RestartSec=5
# Environment=WATCH_DIR=/srv/sftp/workspace/files
# Environment=GIT_USER=tmartin2113
# Environment=DEPLOY_KEYS_DIR=/path/to/secrets/ssh
# StandardOutput=journal
# StandardError=journal
# User=root
#
# [Install]
# WantedBy=multi-user.target
