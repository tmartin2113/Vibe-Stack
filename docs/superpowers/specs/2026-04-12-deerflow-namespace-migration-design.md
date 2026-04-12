# DeerFlow Namespace Migration — Design Spec

> Rename `src.*` imports to `deerflow.*` to align with upstream bytedance/deer-flow and unblock future cherry-picks.

## Context

The fork of DeerFlow (vendored at `~/Repos/paperclip/deerflow/backend/`) was taken from `bytedance/deer-flow` on 2026-03-14. That same day, upstream merged PR #1131 which split the backend into a publishable harness with `deerflow.*` imports and an `app/` layer. The fork kept `src.*` imports. This means every upstream cherry-pick requires manual import translation — 284 commits have accumulated since the fork point.

This migration renames the source directory and rewrites all imports in one atomic pass, making future upstream syncs apply cleanly.

## Scope

This spec covers only the namespace rename. It does not cover:
- The upstream harness/app directory split (deferred — organizational, not functional)
- The gateway replacement (separate sub-project)
- Cherry-picking upstream fixes (separate sub-project, unblocked by this)

## Changes

### Directory Rename (Paperclip repo)

Rename `deerflow/backend/src/` to `deerflow/backend/deerflow/`.

This is a `git mv` so git tracks the rename for blame history. The `__init__.py` at the root of the directory keeps it a valid Python package — just with a new name.

### Python Import Rewrite (Paperclip repo)

**Pattern:** All `from src.` → `from deerflow.` and `import src.` → `import deerflow.`

**Scope:** 350 occurrences across 101 files in `deerflow/backend/`.

Files affected:
- `deerflow/backend/deerflow/**/*.py` (source — ~90 files after rename)
- `deerflow/backend/tests/**/*.py` (tests — ~25 files)
- `deerflow/backend/debug.py` (1 file)

The rewrite is a mechanical `sed` / `find-replace`. No logic changes.

### Config File Updates (Paperclip repo)

| File | Change |
|------|--------|
| `deerflow/backend/langgraph.json:8` | `"src.agents:make_lead_agent"` → `"deerflow.agents:make_lead_agent"` |
| `deerflow/backend/Dockerfile:33` | `src.gateway.app:app` → `deerflow.gateway.app:app` |
| `deerflow/config.example.yaml` | All `src.*` module paths → `deerflow.*` |

### Config File Updates (Vibe-Stack repo)

| File | Change |
|------|--------|
| `deerflow/config.yaml` | 11 `use: "src.*"` entries → `use: "deerflow.*"`, plus comment on line 7 and example on line 15 |

### What Does NOT Change

- **Docker compose volume mounts** — they mount to `/app/backend/`, the rename is inside that path
- **Paperclip adapter** (`packages/adapters/deerflow/`) — communicates over HTTP, no Python imports
- **Vibe Stack agent code** (`agents/`) — separate Python codebase, does not import DeerFlow
- **DeerFlow frontend** (`deerflow/frontend/`) — JavaScript, unaffected
- **DeerFlow skills** (`deerflow/skills/`) — loaded by path from config, not imported via `src.*`

## Verification

After the migration, these must all pass:

1. **Zero remaining `src.*` references:**
   ```bash
   grep -r "from src\.\|import src\." deerflow/backend/ --include="*.py" --exclude-dir=__pycache__
   # Expected: no output
   
   grep -r '"src\.' deerflow/ --include="*.yaml" --include="*.json" --exclude-dir=__pycache__
   # Expected: no output
   ```

2. **DeerFlow tests pass:**
   ```bash
   cd deerflow/backend && uv run pytest tests/ -x
   ```

3. **Docker build succeeds:**
   ```bash
   docker build -f deerflow/backend/Dockerfile deerflow/backend/
   ```

4. **Git tracks renames:**
   ```bash
   git log --follow --oneline deerflow/backend/deerflow/agents/lead_agent.py | head -5
   # Expected: shows commits from before the rename
   ```

## Commit Strategy

Two commits, one per repo:

1. **Paperclip repo:** `refactor(deerflow): rename src namespace to deerflow`
   - Directory rename + all import/config rewrites
   - Pushed to `tmartin2113/paperclip` master

2. **Vibe-Stack repo:** `refactor(deerflow): update config for deerflow namespace`
   - Config path updates in `deerflow/config.yaml`
