#!/usr/bin/env bash
#
# bootstrap-skills.sh — Import forked skill repos into Paperclip company_skills.
#
# Run once after initial setup, or after pulling new skill repos.
# Skills are upserted, so re-running is idempotent.
#
# Auth: creates a short-lived board API key via the Paperclip database, uses it
# for the import, then immediately revokes it. Requires docker exec access to
# paperclip-db-1. Does not require PAPERCLIP_API_KEY to be set.
#
# Optional env overrides:
#   PAPERCLIP_API_URL     — Paperclip server URL (default: http://localhost:3100)
#   PAPERCLIP_COMPANY_ID  — Company UUID (auto-detected from DB if not set)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Load env vars (caller wins over .env) ─────────────────────────────────────
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  while IFS= read -r line; do
    [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
    key="${line%%=*}"
    [[ -z "${!key+x}" ]] && export "${line?}" 2>/dev/null || true
  done < "$PROJECT_ROOT/.env"
fi

API_URL="${PAPERCLIP_API_URL:-http://localhost:3100}"
COMPANY_ID="${PAPERCLIP_COMPANY_ID:-}"

# ── Auto-detect company ID ─────────────────────────────────────────────────────
if [[ -z "$COMPANY_ID" ]]; then
  COMPANY_ID=$(docker exec paperclip-db-1 psql -U paperclip -t -A \
    -c "SELECT id FROM companies WHERE name='Vibe-Stack' LIMIT 1;" 2>/dev/null \
    || docker exec paperclip-db-1 psql -U paperclip -t -A \
       -c "SELECT id FROM companies LIMIT 1;" 2>/dev/null || echo "")
  if [[ -z "$COMPANY_ID" ]]; then
    echo "ERROR: Could not detect company ID." >&2
    echo "       Set PAPERCLIP_COMPANY_ID in .env, or run:" >&2
    echo "       docker exec paperclip-db-1 psql -U paperclip -c 'SELECT id,name FROM companies;'" >&2
    exit 1
  fi
  echo "  Auto-detected company: $COMPANY_ID"
fi

# ── Skill source directories ───────────────────────────────────────────────────
declare -a SKILL_SOURCES=(
  "$PROJECT_ROOT/skill-sources/anthropics-skills/skills"
  "$PROJECT_ROOT/skill-sources/obra-superpowers/skills"
  "$PROJECT_ROOT/skill-sources/vercel-agent-skills/skills"
)

# ── Create temporary board API key ────────────────────────────────────────────
TMP_TOKEN="pcp_board_$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
TMP_HASH=$(python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$TMP_TOKEN")
ADMIN_USER_ID=$(docker exec paperclip-db-1 psql -U paperclip -t -A \
  -c "SELECT u.id FROM instance_user_roles r JOIN \"user\" u ON u.id=r.user_id LIMIT 1;" 2>/dev/null || echo "")

if [[ -z "$ADMIN_USER_ID" ]]; then
  echo "ERROR: No instance_admin user found in database." >&2
  exit 1
fi

docker exec paperclip-db-1 psql -U paperclip -q -c \
  "INSERT INTO board_api_keys (user_id, name, key_hash) VALUES ('$ADMIN_USER_ID', 'bootstrap-skills-tmp', '$TMP_HASH');" \
  &>/dev/null

cleanup() {
  docker exec paperclip-db-1 psql -U paperclip -q -c \
    "UPDATE board_api_keys SET revoked_at=NOW() WHERE key_hash='$TMP_HASH';" &>/dev/null
}
trap cleanup EXIT

# ── Import helper ──────────────────────────────────────────────────────────────
import_source() {
  local source_path="$1"
  if [[ ! -d "$source_path" ]]; then
    printf "  SKIP  %s/%s (not found)\n" "$(basename "$(dirname "$source_path")")" "$(basename "$source_path")"
    return 0
  fi

  local response http_status body count
  response=$(curl -sf \
    -w "\n%{http_code}" \
    -X POST \
    -H "Authorization: Bearer $TMP_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"source\": \"$source_path\"}" \
    "$API_URL/api/companies/$COMPANY_ID/skills/import" 2>&1) || {
    printf "  ERROR %s — curl failed\n" "$(basename "$source_path")"
    return 0
  }

  http_status=$(echo "$response" | tail -1)
  body=$(echo "$response" | sed '$d')

  if [[ "$http_status" -ge 200 && "$http_status" -lt 300 ]]; then
    count=$(echo "$body" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    arr = d.get('imported', []) if isinstance(d, dict) else d
    print(len(arr) if isinstance(arr, list) else '?')
except Exception:
    print('?')
" 2>/dev/null || echo "?")
    printf "  OK    %-50s → %s skills\n" "$(basename "$(dirname "$source_path")")/$(basename "$source_path")" "$count"
  else
    printf "  ERROR %s/%s — HTTP %s\n" "$(basename "$(dirname "$source_path")")" "$(basename "$source_path")" "$http_status"
    echo "$body" | head -2 | sed 's/^/        /'
  fi
}

# ── Run ────────────────────────────────────────────────────────────────────────
echo "Importing forked skills into Paperclip company_skills..."
echo "  API URL: $API_URL"
echo "  Company: $COMPANY_ID"
echo ""

for src in "${SKILL_SOURCES[@]}"; do
  import_source "$src"
done

echo ""
echo "Done. Imported skills are now:"
echo "  • Visible in the Paperclip UI (Skills tab for each agent)"
echo "  • Available to claude_local agents on next run"
echo "  • Also in vibe_skills/official/ for DeerFlow agents (via sync_local_skills.sh)"
