#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# cleanup-paperclip-skill-cache.sh
# ═══════════════════════════════════════════════════════════════
#
# Purge stale `npx paperclipai` caches from the Paperclip server
# container's persistent `/paperclip` volume.
#
# Context:
#   Early Paperclip adapters and bootstrap steps used to shell out to
#   `npx paperclipai` which extracted the upstream `@paperclipai/*`
#   packages (including their bundled SKILL.md files) into
#   `/paperclip/.npm/_npx/<hash>/node_modules/@paperclipai/...`.
#
#   The fork-built server image at `ghcr.io/tmartin2113/paperclip-server`
#   no longer uses `npx paperclipai` at runtime — it loads skills from
#   `/app/skills` (or `$PAPERCLIP_SKILLS_DIR`). The npx cache is now a
#   stale artifact that silently hangs around in the persistent volume.
#
#   On a volume that's been through multiple upgrade paths the cache
#   can contain several hundred MB of orphaned package metadata, and
#   its presence is confusing when debugging skill delivery because
#   the file tree still shows a complete-looking @paperclipai layout.
#
# What this script does:
#   1. docker exec into `vibe-stack-server-1` (or the name passed via
#      $SERVER_CONTAINER / --container).
#   2. Lists the contents of /paperclip/.npm/_npx/ and reports their
#      combined size.
#   3. With `--apply`, deletes them. Without flags, runs in dry-run
#      mode and prints what would be removed.
#
# Usage:
#   ./scripts/cleanup-paperclip-skill-cache.sh            # dry-run
#   ./scripts/cleanup-paperclip-skill-cache.sh --apply    # actually delete
#   SERVER_CONTAINER=my-container ./scripts/cleanup-paperclip-skill-cache.sh --apply

set -euo pipefail

CONTAINER="${SERVER_CONTAINER:-vibe-stack-server-1}"
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --container)
      CONTAINER="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '1,40p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
  echo "error: container '${CONTAINER}' is not running" >&2
  echo "hint: run 'docker compose ps' to see running services, or set SERVER_CONTAINER" >&2
  exit 1
fi

NPX_CACHE_DIR="/paperclip/.npm/_npx"

if ! docker exec "${CONTAINER}" test -d "${NPX_CACHE_DIR}"; then
  echo "no npx cache at ${NPX_CACHE_DIR} (nothing to clean up)"
  exit 0
fi

echo "== stale @paperclipai npx caches in ${CONTAINER}:${NPX_CACHE_DIR} =="
docker exec "${CONTAINER}" sh -c "
  set -eu
  find '${NPX_CACHE_DIR}' -mindepth 1 -maxdepth 1 -type d | while read -r entry; do
    name=\"\$(basename \"\$entry\")\"
    if [ -d \"\$entry/node_modules/@paperclipai\" ]; then
      bytes=\$(du -sb \"\$entry\" 2>/dev/null | awk '{print \$1}')
      human=\$(du -sh \"\$entry\" 2>/dev/null | awk '{print \$1}')
      echo \"  \$name (\$human)  → \$entry\"
    fi
  done
"

if [[ ${APPLY} -ne 1 ]]; then
  echo
  echo "dry-run — rerun with --apply to delete"
  exit 0
fi

echo
echo "== deleting stale caches =="
docker exec "${CONTAINER}" sh -c "
  set -eu
  find '${NPX_CACHE_DIR}' -mindepth 1 -maxdepth 1 -type d | while read -r entry; do
    if [ -d \"\$entry/node_modules/@paperclipai\" ]; then
      rm -rf \"\$entry\"
      echo \"  deleted \$entry\"
    fi
  done
"

echo
echo "done. verify active skill dir with:"
echo "  scripts/check-paperclip-skills.sh"
