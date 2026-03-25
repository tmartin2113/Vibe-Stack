#!/usr/bin/env bash
# Agent health monitor — run via cron, e.g. every 15 minutes:
#   */15 * * * * /home/prime/Repos/Vibe-Stack/scripts/check-agent-health.sh >> /var/log/agent-health.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
PAPERCLIP_URL="${PAPERCLIP_URL:-http://localhost:3100}"
LOG_FILE="/tmp/agent-health-last-run.json"

# Load env
if [[ -f "${REPO_DIR}/.env" ]]; then
  set -o allexport
  # shellcheck disable=SC1091
  source "${REPO_DIR}/.env"
  set +o allexport
fi

# Support both PAPERCLIP_API_KEY and legacy PAPERCLIP_API_TOKEN
TOKEN="${PAPERCLIP_API_KEY:-${PAPERCLIP_API_TOKEN:-}}"
if [[ -z "$TOKEN" ]]; then
  echo "[$(date -u +%FT%TZ)] ERROR: PAPERCLIP_API_KEY not set, skipping health check"
  exit 0
fi

SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"

_notify_slack() {
  local message="$1"
  [[ -z "$SLACK_WEBHOOK_URL" ]] && return 0
  curl -sf --max-time 10 \
    -H "Content-Type: application/json" \
    -d "{\"text\": \":x: *Paperclip Agent Alert*\\n${message}\"}" \
    "$SLACK_WEBHOOK_URL" >/dev/null 2>&1 \
    && echo "[$(date -u +%FT%TZ)] Slack alert sent" \
    || echo "[$(date -u +%FT%TZ)] WARNING: Slack notification failed (non-fatal)"
}

echo "[$(date -u +%FT%TZ)] Running agent health check..."

# Fetch all agents across all companies
RESPONSE=$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "$PAPERCLIP_URL/api/agents" 2>/dev/null) || {
  echo "[$(date -u +%FT%TZ)] ERROR: Could not reach Paperclip API at $PAPERCLIP_URL"
  exit 0
}

# Find agents in error status
ERROR_AGENTS=$(echo "$RESPONSE" | python3 -c "
import sys, json
agents = json.load(sys.stdin)
errors = [a for a in (agents.get('data') or agents if isinstance(agents, list) else []) if a.get('status') == 'error']
for a in errors:
    print(f\"{a.get('name','?')} (company: {a.get('companyId','?')}, id: {a.get('id','?')})\")
" 2>/dev/null || echo "")

if [[ -z "$ERROR_AGENTS" ]]; then
  echo "[$(date -u +%FT%TZ)] All agents healthy."
  # Clear sentinel file if it previously had errors
  rm -f /tmp/paperclip-agent-errors.txt
else
  echo "[$(date -u +%FT%TZ)] ALERT — agents in error state:"
  echo "$ERROR_AGENTS"
  # Write sentinel file so external tooling can pick it up
  echo "$ERROR_AGENTS" > /tmp/paperclip-agent-errors.txt
  # Send Slack alert
  AGENT_LIST=$(echo "$ERROR_AGENTS" | sed 's/^/• /' | tr '\n' '\\n')
  _notify_slack "Agents in error state on $(hostname):\\n${AGENT_LIST}"
fi
