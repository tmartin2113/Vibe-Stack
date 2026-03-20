#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# check-skill-upstreams.sh — Check forked skill repos for upstream updates
# ═══════════════════════════════════════════════════════════════
#
# Usage:
#   ./scripts/check-skill-upstreams.sh          # check only
#   ./scripts/check-skill-upstreams.sh --pull   # check and merge upstream
#   ./scripts/check-skill-upstreams.sh --push   # check, merge, and push forks
#
# Expects repos in skill-sources/ with an "upstream" remote configured.
# Repos without an upstream remote are skipped.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SOURCES="${SCRIPT_DIR}/../skill-sources"
MODE="${1:-check}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

total=0
behind_count=0
updated_count=0
failed_count=0

for dir in "$SKILL_SOURCES"/*/; do
  [ -d "$dir/.git" ] || continue
  name=$(basename "$dir")
  cd "$dir"

  # Determine upstream remote and branch
  if git remote | grep -q '^upstream$'; then
    remote="upstream"
  elif git remote | grep -q '^origin$'; then
    # Some repos use origin as the upstream (e.g. voltagent)
    # Only treat origin as upstream if there's no fork remote
    if ! git remote | grep -q '^fork$'; then
      # Skip — origin is the fork itself, no upstream configured
      echo -e "${YELLOW}⊘${NC} ${name} — no upstream remote, skipping"
      continue
    fi
    remote="origin"
  else
    echo -e "${YELLOW}⊘${NC} ${name} — no upstream remote, skipping"
    continue
  fi

  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")

  # Fetch upstream
  if ! git fetch "$remote" --quiet 2>/dev/null; then
    echo -e "${RED}✗${NC} ${name} — failed to fetch ${remote}"
    ((failed_count++)) || true
    continue
  fi

  # Count commits behind
  upstream_ref="${remote}/${branch}"
  if ! git rev-parse "$upstream_ref" &>/dev/null; then
    # Try common branch names
    for try_branch in main master; do
      if git rev-parse "${remote}/${try_branch}" &>/dev/null; then
        upstream_ref="${remote}/${try_branch}"
        break
      fi
    done
  fi

  behind=$(git rev-list "HEAD..${upstream_ref}" --count 2>/dev/null || echo 0)
  ((total++)) || true

  if [ "$behind" -eq 0 ]; then
    echo -e "${GREEN}✓${NC} ${name} — up to date"
    continue
  fi

  ((behind_count++)) || true
  echo -e "${RED}↓${NC} ${name} — ${behind} commits behind ${upstream_ref}"

  # Show summary of what's new
  git log "HEAD..${upstream_ref}" --oneline | head -5
  [ "$behind" -gt 5 ] && echo "  ... and $((behind - 5)) more"

  # Merge if requested
  if [ "$MODE" = "--pull" ] || [ "$MODE" = "--push" ]; then
    echo "  Merging ${upstream_ref}..."

    # Commit any uncommitted tag changes first
    if [ -n "$(git status --porcelain)" ]; then
      git add -A
      git commit -m "chore: local changes before upstream merge" --quiet 2>/dev/null || true
    fi

    if git merge "$upstream_ref" --no-edit 2>/dev/null; then
      echo -e "  ${GREEN}✓${NC} Merged successfully"
      ((updated_count++)) || true

      # Push if requested
      if [ "$MODE" = "--push" ]; then
        push_remote="origin"
        if git push "$push_remote" "$branch" 2>/dev/null; then
          echo -e "  ${GREEN}✓${NC} Pushed to ${push_remote}/${branch}"
        else
          echo -e "  ${RED}✗${NC} Failed to push to ${push_remote}/${branch}"
          ((failed_count++)) || true
        fi
      fi
    else
      echo -e "  ${RED}✗${NC} Merge conflict — resolve manually"
      git merge --abort 2>/dev/null || true
      ((failed_count++)) || true
    fi
  fi

  echo ""
done

# Summary
echo ""
echo "═══════════════════════════════════════"
echo " Checked: ${total} repos"
echo " Up to date: $((total - behind_count))"
echo " Behind upstream: ${behind_count}"
[ "$updated_count" -gt 0 ] && echo " Merged: ${updated_count}"
[ "$failed_count" -gt 0 ] && echo " Failed: ${failed_count}"
echo "═══════════════════════════════════════"

# Exit with error if any repos are behind (useful for CI)
[ "$behind_count" -gt 0 ] && [ "$MODE" = "check" ] && exit 1
[ "$failed_count" -gt 0 ] && exit 2
exit 0
