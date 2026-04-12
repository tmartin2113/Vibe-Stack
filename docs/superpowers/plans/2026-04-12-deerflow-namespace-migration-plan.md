# DeerFlow Namespace Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the DeerFlow backend from `src.*` to `deerflow.*` namespace to align with upstream bytedance/deer-flow and unblock future cherry-picks.

**Architecture:** `git mv` the source directory, `sed` all Python imports and config module paths, update CLAUDE.md documentation paths. One atomic commit in the Paperclip repo, one follow-up commit in the Vibe-Stack repo.

**Tech Stack:** Python, sed, git, Docker, uv/pytest.

**Spec:** `docs/superpowers/specs/2026-04-12-deerflow-namespace-migration-design.md`

**Repos:**
- Paperclip: `/home/prime/Repos/paperclip` (branch: `master`)
- Vibe-Stack: `/home/prime/Repos/Vibe-Stack` (branch: `main`)

---

## File Structure

### Renamed Directory

| Before | After |
|--------|-------|
| `deerflow/backend/src/` | `deerflow/backend/deerflow/` |

All ~125 Python files inside move with the directory. Git tracks this as a rename.

### Modified Files (Paperclip repo)

| File | Change |
|------|--------|
| `deerflow/backend/deerflow/**/*.py` (~90 files) | `from src.` → `from deerflow.`, `import src.` → `import deerflow.` |
| `deerflow/backend/tests/**/*.py` (~25 files) | Same import rewrite |
| `deerflow/backend/debug.py` | Same import rewrite |
| `deerflow/backend/langgraph.json` | Module paths `src.` → `deerflow.` |
| `deerflow/backend/Dockerfile:33` | Gateway CMD path |
| `deerflow/config.example.yaml` | Module paths in `use:` fields |
| `deerflow/backend/CLAUDE.md` | `src/` → `deerflow/` in documentation paths |

### Modified Files (Vibe-Stack repo)

| File | Change |
|------|--------|
| `deerflow/config.yaml` | 11 `use: "src.*"` → `use: "deerflow.*"` entries + comments |

---

## Task 1: Rename Source Directory

**Files:**
- Rename: `deerflow/backend/src/` → `deerflow/backend/deerflow/`

- [ ] **Step 1: Rename the directory with git mv**

```bash
cd /home/prime/Repos/paperclip
git mv deerflow/backend/src deerflow/backend/deerflow
```

- [ ] **Step 2: Verify the rename**

```bash
cd /home/prime/Repos/paperclip
ls deerflow/backend/deerflow/__init__.py
ls deerflow/backend/deerflow/agents/
```

Expected: Both paths exist. `src/` no longer exists.

---

## Task 2: Rewrite Python Imports in Source Files

**Files:**
- Modify: All `*.py` files under `deerflow/backend/deerflow/`

- [ ] **Step 1: Rewrite `from src.` imports**

```bash
cd /home/prime/Repos/paperclip
find deerflow/backend/deerflow -name "*.py" -not -path "*/__pycache__/*" -exec sed -i 's/from src\./from deerflow./g' {} +
```

- [ ] **Step 2: Rewrite `import src.` imports**

```bash
cd /home/prime/Repos/paperclip
find deerflow/backend/deerflow -name "*.py" -not -path "*/__pycache__/*" -exec sed -i 's/import src\./import deerflow./g' {} +
```

- [ ] **Step 3: Rewrite string references to `"src."`**

Some files reference module paths as strings (e.g., in `resolve_variable` calls or test fixtures). Catch those too:

```bash
cd /home/prime/Repos/paperclip
find deerflow/backend/deerflow -name "*.py" -not -path "*/__pycache__/*" -exec sed -i 's/"src\./"deerflow./g' {} +
```

- [ ] **Step 4: Verify zero remaining `src.` imports in source**

```bash
cd /home/prime/Repos/paperclip
grep -rn "from src\.\|import src\.\|\"src\." deerflow/backend/deerflow/ --include="*.py" --exclude-dir=__pycache__
```

Expected: No output.

---

## Task 3: Rewrite Python Imports in Test Files

**Files:**
- Modify: All `*.py` files under `deerflow/backend/tests/`

- [ ] **Step 1: Rewrite `from src.` imports**

```bash
cd /home/prime/Repos/paperclip
find deerflow/backend/tests -name "*.py" -not -path "*/__pycache__/*" -exec sed -i 's/from src\./from deerflow./g' {} +
```

- [ ] **Step 2: Rewrite `import src.` imports**

```bash
cd /home/prime/Repos/paperclip
find deerflow/backend/tests -name "*.py" -not -path "*/__pycache__/*" -exec sed -i 's/import src\./import deerflow./g' {} +
```

- [ ] **Step 3: Rewrite string references to `"src."` in tests**

Tests may have string references in mock patches (e.g., `@patch("src.agents.foo")`):

```bash
cd /home/prime/Repos/paperclip
find deerflow/backend/tests -name "*.py" -not -path "*/__pycache__/*" -exec sed -i 's/"src\./"deerflow./g' {} +
```

- [ ] **Step 4: Rewrite the conftest.py sys.modules mock**

The `tests/conftest.py` mocks `src.subagents.executor` to break circular imports. Check and update:

```bash
cd /home/prime/Repos/paperclip
grep -n "src\." deerflow/backend/tests/conftest.py
```

If any hits, fix them:

```bash
sed -i 's/src\./deerflow./g' deerflow/backend/tests/conftest.py
```

- [ ] **Step 5: Rewrite debug.py**

```bash
cd /home/prime/Repos/paperclip
sed -i 's/from src\./from deerflow./g; s/import src\./import deerflow./g' deerflow/backend/debug.py
```

- [ ] **Step 6: Verify zero remaining `src.` references in tests and debug**

```bash
cd /home/prime/Repos/paperclip
grep -rn "from src\.\|import src\.\|\"src\." deerflow/backend/tests/ deerflow/backend/debug.py --include="*.py" --exclude-dir=__pycache__
```

Expected: No output.

---

## Task 4: Update Config and Docker Files

**Files:**
- Modify: `deerflow/backend/langgraph.json`
- Modify: `deerflow/backend/Dockerfile:33`
- Modify: `deerflow/config.example.yaml`

- [ ] **Step 1: Update langgraph.json**

Replace `src.` with `deerflow.` in both the graph entry point and the checkpointer path:

```bash
cd /home/prime/Repos/paperclip
sed -i 's|src\.|deerflow.|g' deerflow/backend/langgraph.json
```

Verify:

```bash
cat deerflow/backend/langgraph.json
```

Expected:
```json
{
  "$schema": "https://langgra.ph/schema.json",
  "dependencies": ["."],
  "env": ".env",
  "graphs": {
    "lead_agent": "deerflow.agents:make_lead_agent"
  },
  "checkpointer": {
    "path": "./deerflow/agents/checkpointer/async_provider.py:make_checkpointer"
  }
}
```

Note: The checkpointer `path` uses a filesystem path (`./src/agents/...`) not a module path. This becomes `./deerflow/agents/...` since the directory was renamed.

- [ ] **Step 2: Update Dockerfile gateway CMD**

```bash
cd /home/prime/Repos/paperclip
sed -i 's|src\.gateway\.app:app|deerflow.gateway.app:app|' deerflow/backend/Dockerfile
```

Verify:

```bash
grep "uvicorn" deerflow/backend/Dockerfile
```

Expected: `CMD ["sh", "-c", "uv run uvicorn deerflow.gateway.app:app --host 0.0.0.0 --port 8001"]`

- [ ] **Step 3: Update config.example.yaml**

```bash
cd /home/prime/Repos/paperclip
sed -i 's|src\.|deerflow.|g' deerflow/config.example.yaml
```

Verify:

```bash
grep "deerflow\." deerflow/config.example.yaml | head -5
```

Expected: Module paths now use `deerflow.*`.

---

## Task 5: Update CLAUDE.md Documentation

**Files:**
- Modify: `deerflow/backend/CLAUDE.md`

- [ ] **Step 1: Update `src/` path references to `deerflow/`**

The CLAUDE.md has ~19 references to `src/` as directory paths in the project structure and architecture docs:

```bash
cd /home/prime/Repos/paperclip
sed -i 's|backend/src/|backend/deerflow/|g' deerflow/backend/CLAUDE.md
sed -i 's|│   ├── src/|│   ├── deerflow/|g' deerflow/backend/CLAUDE.md
sed -i 's|│   │   ├── src/|│   │   ├── deerflow/|g' deerflow/backend/CLAUDE.md
```

- [ ] **Step 2: Update `src.` module path references**

```bash
cd /home/prime/Repos/paperclip
sed -i 's|`src\.|`deerflow.|g' deerflow/backend/CLAUDE.md
sed -i 's|(src/|(deerflow/|g' deerflow/backend/CLAUDE.md
```

- [ ] **Step 3: Update the project structure tree**

The tree in CLAUDE.md shows `src/` as a directory. Update it:

```bash
cd /home/prime/Repos/paperclip
sed -i 's|│   ├── src/$|│   ├── deerflow/|' deerflow/backend/CLAUDE.md
```

- [ ] **Step 4: Verify no remaining `src/` references in CLAUDE.md**

```bash
cd /home/prime/Repos/paperclip
grep -n "src/" deerflow/backend/CLAUDE.md
grep -n '`src\.' deerflow/backend/CLAUDE.md
```

Expected: No output (or only incidental references like "source code" that don't refer to the directory).

---

## Task 6: Verify and Commit (Paperclip)

**Files:** None new — verification only.

- [ ] **Step 1: Full grep verification**

```bash
cd /home/prime/Repos/paperclip
echo "=== Python imports ==="
grep -rn "from src\.\|import src\." deerflow/backend/ --include="*.py" --exclude-dir=__pycache__ | wc -l

echo "=== Config module paths ==="
grep -rn '"src\.' deerflow/ --include="*.yaml" --include="*.json" --exclude-dir=__pycache__ | wc -l

echo "=== Dockerfile ==="
grep "src\." deerflow/backend/Dockerfile | wc -l
```

Expected: All three report `0`.

- [ ] **Step 2: Run DeerFlow tests**

```bash
cd /home/prime/Repos/paperclip/deerflow/backend
PYTHONPATH=. uv run pytest tests/ -x --no-header -q 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 3: Check git diff stats**

```bash
cd /home/prime/Repos/paperclip
git diff --stat | tail -5
```

Expected: Large diff showing renames + modifications.

- [ ] **Step 4: Commit**

```bash
cd /home/prime/Repos/paperclip
git add -A deerflow/backend/
git add deerflow/config.example.yaml
git commit -m "$(cat <<'COMMITEOF'
refactor(deerflow): rename src namespace to deerflow

Align with upstream bytedance/deer-flow namespace convention.
Directory renamed src/ → deerflow/, all Python imports and config
module paths updated. Unblocks future upstream cherry-picks.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
COMMITEOF
)"
```

- [ ] **Step 5: Push**

```bash
cd /home/prime/Repos/paperclip
git push origin master
```

---

## Task 7: Update Vibe-Stack Config

**Files:**
- Modify: `deerflow/config.yaml` (in Vibe-Stack repo)

- [ ] **Step 1: Update module paths**

```bash
cd /home/prime/Repos/Vibe-Stack
sed -i 's|"src\.|"deerflow.|g' deerflow/config.yaml
```

- [ ] **Step 2: Update comments**

The comment on line 7 references `src.*` namespace and line 15 has an example:

```bash
cd /home/prime/Repos/Vibe-Stack
sed -i 's|"src\.\*" namespace|"deerflow.*" namespace|g' deerflow/config.yaml
sed -i 's|from src\.|from deerflow.|g' deerflow/config.yaml
```

- [ ] **Step 3: Verify**

```bash
cd /home/prime/Repos/Vibe-Stack
grep "src\." deerflow/config.yaml
```

Expected: No output.

- [ ] **Step 4: Commit**

```bash
cd /home/prime/Repos/Vibe-Stack
git add deerflow/config.yaml
git commit -m "$(cat <<'COMMITEOF'
refactor(deerflow): update config for deerflow namespace

Update module paths from src.* to deerflow.* to match the namespace
rename in the Paperclip repo.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
COMMITEOF
)"
```

---

## Summary

| Task | Description | Repo | Files |
|------|-------------|------|-------|
| 1 | Rename source directory | Paperclip | 1 dir (~125 files) |
| 2 | Rewrite source imports | Paperclip | ~90 files |
| 3 | Rewrite test/debug imports | Paperclip | ~26 files |
| 4 | Update config/Docker files | Paperclip | 3 files |
| 5 | Update CLAUDE.md docs | Paperclip | 1 file |
| 6 | Verify and commit | Paperclip | 0 (verification) |
| 7 | Update Vibe-Stack config | Vibe-Stack | 1 file |

**Total: ~125 files renamed, ~120 files modified, 2 commits across 2 repos, 7 tasks**
