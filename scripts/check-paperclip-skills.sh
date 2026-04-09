#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# check-paperclip-skills.sh
# ═══════════════════════════════════════════════════════════════
#
# Verify that the Paperclip server container is loading the expected
# skills directory, and dump the list of available skills for visibility.
#
# What this script checks:
#   1. The server container is running.
#   2. The `PAPERCLIP_SKILLS_DIR` env var inside the container is set
#      (fork-built image bakes in `PAPERCLIP_SKILLS_DIR=/app/skills`).
#   3. That path exists and is a directory.
#   4. That it contains `paperclip/SKILL.md` (the core heartbeat skill).
#   5. Lists every skill directory and prints its line count and the
#      first "# heading" so operators can sanity-check content without
#      opening the file.
#   6. Warns if `/paperclip/.npm/_npx/` still contains a stale
#      @paperclipai npx cache (run cleanup-paperclip-skill-cache.sh).
#
# Usage:
#   ./scripts/check-paperclip-skills.sh
#   SERVER_CONTAINER=my-container ./scripts/check-paperclip-skills.sh

set -euo pipefail

CONTAINER="${SERVER_CONTAINER:-vibe-stack-server-1}"

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
NC=$'\033[0m'

fail() { echo "${RED}FAIL${NC} $*"; EXIT_CODE=1; }
warn() { echo "${YELLOW}WARN${NC} $*"; }
ok()   { echo "${GREEN} OK ${NC} $*"; }

EXIT_CODE=0

if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  fail "container '${CONTAINER}' is not running"
  echo "  hint: run 'docker compose ps' or set SERVER_CONTAINER"
  exit 1
fi
ok "container '${CONTAINER}' is running"

SKILLS_DIR="$(docker exec "${CONTAINER}" sh -c 'printf "%s" "${PAPERCLIP_SKILLS_DIR:-}"')"
if [[ -z "${SKILLS_DIR}" ]]; then
  warn "PAPERCLIP_SKILLS_DIR is not set inside the container"
  warn "  the adapter will fall back to module-relative path math"
  warn "  rebuild against an image that bakes the env var, or add it to docker-compose.yml"
  SKILLS_DIR="/app/skills"
  warn "  (using ${SKILLS_DIR} as the fallback target for this check)"
else
  ok "PAPERCLIP_SKILLS_DIR=${SKILLS_DIR}"
fi

if docker exec "${CONTAINER}" test -d "${SKILLS_DIR}"; then
  ok "${SKILLS_DIR} exists and is a directory"
else
  fail "${SKILLS_DIR} does not exist or is not a directory"
  EXIT_CODE=1
fi

CORE_SKILL="${SKILLS_DIR}/paperclip/SKILL.md"
if docker exec "${CONTAINER}" test -f "${CORE_SKILL}"; then
  ok "core heartbeat skill present: ${CORE_SKILL}"
else
  fail "core heartbeat skill missing: ${CORE_SKILL}"
  EXIT_CODE=1
fi

echo
echo "== skills in ${SKILLS_DIR} =="
docker exec "${CONTAINER}" sh -c "
  set -eu
  if [ ! -d '${SKILLS_DIR}' ]; then exit 0; fi
  for d in '${SKILLS_DIR}'/*/; do
    [ -d \"\$d\" ] || continue
    name=\"\$(basename \"\$d\")\"
    skill_md=\"\$d/SKILL.md\"
    if [ -f \"\$skill_md\" ]; then
      lines=\$(wc -l < \"\$skill_md\")
      heading=\$(grep -m1 '^#' \"\$skill_md\" | sed 's/^# *//' | head -c 80)
      printf '  %-30s %4s lines  %s\n' \"\$name\" \"\$lines\" \"\$heading\"
    else
      printf '  %-30s (no SKILL.md)\n' \"\$name\"
    fi
  done
"

echo
echo "== stale npx skill cache =="
STALE_NPX="$(docker exec "${CONTAINER}" sh -c "
  set -eu
  if [ ! -d /paperclip/.npm/_npx ]; then exit 0; fi
  find /paperclip/.npm/_npx -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read -r entry; do
    if [ -d \"\$entry/node_modules/@paperclipai\" ]; then
      du -sh \"\$entry\" 2>/dev/null | awk '{print \$1 \" \" \$2}'
    fi
  done
")"
if [[ -n "${STALE_NPX}" ]]; then
  warn "stale @paperclipai npx caches found in /paperclip/.npm/_npx/:"
  printf '%s\n' "${STALE_NPX}" | sed 's/^/    /'
  warn "  run scripts/cleanup-paperclip-skill-cache.sh --apply to purge them"
else
  ok "no stale @paperclipai npx caches in /paperclip/.npm/_npx/"
fi

echo
if [[ ${EXIT_CODE} -eq 0 ]]; then
  ok "paperclip skills look healthy"
else
  fail "one or more skill checks failed (see above)"
fi
exit ${EXIT_CODE}
