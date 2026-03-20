#!/usr/bin/env bash
# health-report.sh — Check all Vibe Stack services and post status to Paperclip.
#
# Posts a structured health report as a Paperclip issue comment.
# Only posts on status changes (OK→degraded or degraded→OK) to avoid noise.
#
# Required env vars (from .env):
#   PAPERCLIP_API_URL, PAPERCLIP_API_KEY, PAPERCLIP_COMPANY_ID

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="${REPO_DIR}/.health-state"

# Load .env
if [[ -f "${REPO_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_DIR}/.env"
  set +a
fi

# Replace Docker-internal hostnames with localhost for host-side access
PAPERCLIP_API_URL="${PAPERCLIP_API_URL:-http://localhost:3100}"
PAPERCLIP_API_URL="${PAPERCLIP_API_URL/http:\/\/server:/http://localhost:}"
PAPERCLIP_API_KEY="${PAPERCLIP_API_KEY:-}"
PAPERCLIP_COMPANY_ID="${PAPERCLIP_COMPANY_ID:-}"

log() { echo "[health] $(date +%H:%M:%S) $*"; }

# ── Health Checks ─────────────────────────────────────────────────

ISSUES=()
WARNINGS=()

# 1. Docker container health
log "Checking container health..."
COMPOSE_STATUS=$(cd "$REPO_DIR" && docker compose ps --format json 2>/dev/null || echo "")
if [[ -n "$COMPOSE_STATUS" ]]; then
  while IFS= read -r line; do
    name=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin).get('Name',''))" 2>/dev/null || true)
    state=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin).get('State',''))" 2>/dev/null || true)
    health=$(echo "$line" | python3 -c "import sys,json; print(json.load(sys.stdin).get('Health',''))" 2>/dev/null || true)

    if [[ -z "$name" ]]; then continue; fi

    if [[ "$state" != "running" ]]; then
      ISSUES+=("Container **$name** is $state")
    elif [[ "$health" == "unhealthy" ]]; then
      ISSUES+=("Container **$name** is unhealthy")
    fi
  done <<< "$COMPOSE_STATUS"
else
  ISSUES+=("Cannot read Docker Compose status")
fi

# 2. Disk usage
log "Checking disk usage..."
DISK_PCT=$(df / --output=pcent | tail -1 | tr -d ' %')
if (( DISK_PCT >= 90 )); then
  ISSUES+=("Disk usage at **${DISK_PCT}%** (critical)")
elif (( DISK_PCT >= 80 )); then
  WARNINGS+=("Disk usage at **${DISK_PCT}%**")
fi

# 3. Backup freshness
log "Checking backup freshness..."
BACKUP_DIR="${REPO_DIR}/backups"
if [[ -d "$BACKUP_DIR" ]]; then
  LATEST_MANIFEST=$(ls -1t "$BACKUP_DIR"/*_manifest.txt 2>/dev/null | head -1 || true)
  if [[ -n "$LATEST_MANIFEST" ]]; then
    BACKUP_AGE_SECS=$(( $(date +%s) - $(stat -c %Y "$LATEST_MANIFEST") ))
    BACKUP_AGE_HOURS=$(( BACKUP_AGE_SECS / 3600 ))
    if (( BACKUP_AGE_HOURS > 25 )); then
      WARNINGS+=("Latest backup is **${BACKUP_AGE_HOURS}h** old (expected < 25h)")
    fi
  else
    WARNINGS+=("No backup manifests found in $BACKUP_DIR")
  fi
else
  WARNINGS+=("Backup directory does not exist")
fi

# 4. vLLM health
log "Checking vLLM..."
if ! curl -sf --max-time 5 http://localhost:8000/health >/dev/null 2>&1; then
  WARNINGS+=("vLLM health check failed (may not be running)")
fi

# 5. Paperclip health
log "Checking Paperclip..."
if ! curl -sf --max-time 5 http://localhost:3100/api/health >/dev/null 2>&1; then
  ISSUES+=("Paperclip health check failed")
fi

# ── Determine Status ──────────────────────────────────────────────

if (( ${#ISSUES[@]} > 0 )); then
  STATUS="degraded"
elif (( ${#WARNINGS[@]} > 0 )); then
  STATUS="warning"
else
  STATUS="ok"
fi

log "Status: $STATUS (${#ISSUES[@]} issues, ${#WARNINGS[@]} warnings)"

# ── Check for Status Change ───────────────────────────────────────

PREV_STATUS="unknown"
if [[ -f "$STATE_FILE" ]]; then
  PREV_STATUS=$(cat "$STATE_FILE")
fi

echo "$STATUS" > "$STATE_FILE"

if [[ "$STATUS" == "$PREV_STATUS" ]]; then
  log "No status change ($STATUS) — skipping Paperclip post"
  exit 0
fi

log "Status changed: $PREV_STATUS → $STATUS — posting to Paperclip"

# ── Build Report ──────────────────────────────────────────────────

REPORT="## System Health Report\n\n"
REPORT+="**Status:** $STATUS (was: $PREV_STATUS)\n"
REPORT+="**Time:** $(date -Iseconds)\n"
REPORT+="**Disk:** ${DISK_PCT}%\n\n"

if (( ${#ISSUES[@]} > 0 )); then
  REPORT+="### Issues\n"
  for issue in "${ISSUES[@]}"; do
    REPORT+="- $issue\n"
  done
  REPORT+="\n"
fi

if (( ${#WARNINGS[@]} > 0 )); then
  REPORT+="### Warnings\n"
  for warning in "${WARNINGS[@]}"; do
    REPORT+="- $warning\n"
  done
  REPORT+="\n"
fi

if [[ "$STATUS" == "ok" ]]; then
  REPORT+="All systems operational.\n"
fi

# ── Post to Paperclip ─────────────────────────────────────────────

if [[ -z "$PAPERCLIP_API_KEY" || -z "$PAPERCLIP_COMPANY_ID" ]]; then
  log "PAPERCLIP_API_KEY or PAPERCLIP_COMPANY_ID not set — printing report only"
  echo -e "$REPORT"
  exit 0
fi

# Find or create the "System Health" issue
HEALTH_ISSUE_ID=""

# Search for existing health issue
EXISTING=$(curl -sf --max-time 10 \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  "${PAPERCLIP_API_URL}/api/companies/${PAPERCLIP_COMPANY_ID}/issues?search=System+Health&limit=1" \
  2>/dev/null || echo "[]")

HEALTH_ISSUE_ID=$(echo "$EXISTING" | python3 -c "
import sys, json
data = json.load(sys.stdin)
issues = data if isinstance(data, list) else data.get('issues', data.get('data', []))
for i in issues:
    if i.get('title') == 'System Health':
        print(i['id'])
        break
" 2>/dev/null || true)

if [[ -z "$HEALTH_ISSUE_ID" ]]; then
  # Create the health issue
  log "Creating System Health issue..."
  CREATE_RESULT=$(curl -sf --max-time 10 \
    -X POST \
    -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"title\": \"System Health\", \"description\": \"Auto-generated system health monitoring issue. Comments are added on status changes.\"}" \
    "${PAPERCLIP_API_URL}/api/companies/${PAPERCLIP_COMPANY_ID}/issues" \
    2>/dev/null || echo "{}")

  HEALTH_ISSUE_ID=$(echo "$CREATE_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true)

  if [[ -z "$HEALTH_ISSUE_ID" ]]; then
    log "Failed to create health issue — printing report only"
    echo -e "$REPORT"
    exit 0
  fi
  log "Created health issue: $HEALTH_ISSUE_ID"
fi

# Post status change as a comment
COMMENT_BODY=$(echo -e "$REPORT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null)

curl -sf --max-time 10 \
  -X POST \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"body\": ${COMMENT_BODY}}" \
  "${PAPERCLIP_API_URL}/api/issues/${HEALTH_ISSUE_ID}/comments" \
  >/dev/null 2>&1 || log "Failed to post comment (non-fatal)"

log "Health report posted to Paperclip issue $HEALTH_ISSUE_ID"
