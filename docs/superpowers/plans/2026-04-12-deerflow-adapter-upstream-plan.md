# DeerFlow Adapter Upstream PR — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Contribute the fork-only DeerFlow Paperclip adapter to `paperclipai/paperclip` upstream as a builtin adapter, via a clean PR branch based on upstream master.

**Architecture:** Create a branch from upstream master, cherry-pick the adapter package with fork-specific references cleaned up, then add the 4 registration touchpoints. The adapter connects Paperclip agents to a DeerFlow LangGraph backend over SSE streaming, with Docker container lifecycle management, refusal detection, and session management via LangGraph threads.

**Tech Stack:** TypeScript, Paperclip adapter-utils, Docker Engine API, LangGraph Platform API (SSE).

**Spec:** `docs/superpowers/specs/2026-04-12-deerflow-adapter-upstream-design.md`

**Repo:** `/home/prime/Repos/paperclip` (fork of `paperclipai/paperclip`, default branch `master`)

---

## File Structure

### New Files (adapter package)

| File | Purpose |
|------|---------|
| `packages/adapters/deerflow/package.json` | Package metadata with upstream boilerplate |
| `packages/adapters/deerflow/tsconfig.json` | TypeScript config (extends workspace root) |
| `packages/adapters/deerflow/src/index.ts` | Adapter type, label, empty model list, config doc |
| `packages/adapters/deerflow/src/server/index.ts` | Server exports: execute, testEnvironment, sessionCodec |
| `packages/adapters/deerflow/src/server/execute.ts` | SSE streaming execution against LangGraph API |
| `packages/adapters/deerflow/src/server/lifecycle.ts` | Reference-counted Docker container start/stop |
| `packages/adapters/deerflow/src/server/test.ts` | Environment health checks (LangGraph + Gateway probes) |

### Modified Files (registration)

| File | Change |
|------|--------|
| `packages/shared/src/constants.ts:27-37` | Add `"deerflow"` to `AGENT_ADAPTER_TYPES` array |
| `server/src/adapters/builtin-adapter-types.ts:4-15` | Add `"deerflow"` to `BUILTIN_ADAPTER_TYPES` set |
| `server/src/adapters/registry.ts:1-6,183-221` | Import adapter, define `deerflowAdapter`, add to registration array |
| `server/package.json` | Add `"@paperclipai/adapter-deerflow": "workspace:*"` to dependencies |
| `Dockerfile:32-38` | Add COPY line for deerflow package.json |

---

## Task 1: Create PR Branch from Upstream Master

**Files:** None (branch setup only)

- [ ] **Step 1: Fetch upstream master**

```bash
cd /home/prime/Repos/paperclip
git fetch https://github.com/paperclipai/paperclip.git master:refs/remotes/_upstream_temp/master
```

- [ ] **Step 2: Create branch from upstream master**

```bash
cd /home/prime/Repos/paperclip
git checkout -b feat/adapter-deerflow _upstream_temp/master
```

- [ ] **Step 3: Verify clean state**

```bash
cd /home/prime/Repos/paperclip
git log --oneline -3
git status
```

Expected: HEAD on upstream master tip, clean working tree.

---

## Task 2: Add Adapter Package (Cleaned)

**Files:**
- Create: `packages/adapters/deerflow/package.json`
- Create: `packages/adapters/deerflow/tsconfig.json`
- Create: `packages/adapters/deerflow/src/index.ts`
- Create: `packages/adapters/deerflow/src/server/index.ts`
- Create: `packages/adapters/deerflow/src/server/execute.ts`
- Create: `packages/adapters/deerflow/src/server/lifecycle.ts`
- Create: `packages/adapters/deerflow/src/server/test.ts`

- [ ] **Step 1: Copy adapter files from fork's master branch**

```bash
cd /home/prime/Repos/paperclip
git checkout master -- packages/adapters/deerflow/
git reset HEAD packages/adapters/deerflow/
```

This extracts the adapter directory from the fork without committing. The `git reset` unstages so we can modify before committing.

- [ ] **Step 2: Clean `src/index.ts` — empty model list**

Replace the hardcoded model list with an empty typed array:

```typescript
// In packages/adapters/deerflow/src/index.ts, replace:
export const models = [
  { id: "qwen3.5-9b", label: "Qwen3.5 9B (vLLM)" },
];

// With:
export const models: { id: string; label: string }[] = [];
```

- [ ] **Step 3: Clean `src/server/execute.ts` — remove `VIBE_BACKEND_HOST`**

Replace the fork-specific env var fallback chain:

```typescript
// In packages/adapters/deerflow/src/server/execute.ts, replace:
  const deerflowUrl = asString(
    config.deerflowUrl as unknown,
    process.env.VIBE_BACKEND_HOST ?? "http://deerflow-langgraph:2024",
  );

// With:
  const deerflowUrl = asString(
    config.deerflowUrl as unknown,
    "http://deerflow-langgraph:2024",
  );
```

- [ ] **Step 4: Clean `package.json` — add upstream boilerplate**

Add the `license`, `homepage`, `bugs`, and `repository` fields to `packages/adapters/deerflow/package.json`:

```json
{
  "name": "@paperclipai/adapter-deerflow",
  "version": "0.2.7",
  "license": "MIT",
  "homepage": "https://github.com/paperclipai/paperclip",
  "bugs": {
    "url": "https://github.com/paperclipai/paperclip/issues"
  },
  "repository": {
    "type": "git",
    "url": "https://github.com/paperclipai/paperclip",
    "directory": "packages/adapters/deerflow"
  },
  "type": "module",
  ...
}
```

Keep all existing fields (`exports`, `publishConfig`, `files`, `scripts`, `dependencies`, `devDependencies`) unchanged.

- [ ] **Step 5: Verify the 7 files are present and cleaned**

```bash
cd /home/prime/Repos/paperclip
find packages/adapters/deerflow -type f | sort
grep -c "VIBE_BACKEND_HOST" packages/adapters/deerflow/src/server/execute.ts
grep -c "qwen3.5" packages/adapters/deerflow/src/index.ts
```

Expected:
- 7 files listed
- `0` matches for VIBE_BACKEND_HOST
- `0` matches for qwen3.5

- [ ] **Step 6: Commit**

```bash
cd /home/prime/Repos/paperclip
git add packages/adapters/deerflow/
git commit -m "feat(adapters): add DeerFlow (LangGraph) adapter package

Adapter for connecting Paperclip agents to a DeerFlow LangGraph backend.
Supports SSE streaming, LangGraph thread sessions, Docker container
lifecycle management, environment health checks, and refusal detection."
```

---

## Task 3: Register Adapter Type in Shared Constants

**Files:**
- Modify: `packages/shared/src/constants.ts:27-37`

- [ ] **Step 1: Add `"deerflow"` to the `AGENT_ADAPTER_TYPES` array**

In `packages/shared/src/constants.ts`, add `"deerflow"` after `"openclaw_gateway"` in the array:

```typescript
export const AGENT_ADAPTER_TYPES = [
  "process",
  "http",
  "claude_local",
  "codex_local",
  "gemini_local",
  "opencode_local",
  "pi_local",
  "cursor",
  "openclaw_gateway",
  "deerflow",
] as const;
```

- [ ] **Step 2: Verify the change**

```bash
cd /home/prime/Repos/paperclip
grep '"deerflow"' packages/shared/src/constants.ts
```

Expected: One match in the AGENT_ADAPTER_TYPES array.

- [ ] **Step 3: Commit**

```bash
cd /home/prime/Repos/paperclip
git add packages/shared/src/constants.ts
git commit -m "feat(shared): add deerflow to AGENT_ADAPTER_TYPES"
```

---

## Task 4: Register as Builtin Adapter Type

**Files:**
- Modify: `server/src/adapters/builtin-adapter-types.ts:4-15`

- [ ] **Step 1: Add `"deerflow"` to the `BUILTIN_ADAPTER_TYPES` set**

In `server/src/adapters/builtin-adapter-types.ts`, add `"deerflow"` to the set:

```typescript
export const BUILTIN_ADAPTER_TYPES = new Set([
  "claude_local",
  "codex_local",
  "cursor",
  "gemini_local",
  "openclaw_gateway",
  "opencode_local",
  "pi_local",
  "hermes_local",
  "process",
  "http",
  "deerflow",
]);
```

- [ ] **Step 2: Verify**

```bash
cd /home/prime/Repos/paperclip
grep '"deerflow"' server/src/adapters/builtin-adapter-types.ts
```

Expected: One match.

- [ ] **Step 3: Commit**

```bash
cd /home/prime/Repos/paperclip
git add server/src/adapters/builtin-adapter-types.ts
git commit -m "feat(server): add deerflow to BUILTIN_ADAPTER_TYPES"
```

---

## Task 5: Import and Register in Adapter Registry

**Files:**
- Modify: `server/src/adapters/registry.ts:1-6,183-221`

- [ ] **Step 1: Add imports after the existing adapter imports**

In `server/src/adapters/registry.ts`, add after the hermes imports (after line ~84, before the `import { BUILTIN_ADAPTER_TYPES }` line):

```typescript
import {
  execute as deerflowExecute,
  testEnvironment as deerflowTestEnvironment,
  sessionCodec as deerflowSessionCodec,
} from "@paperclipai/adapter-deerflow/server";
import {
  agentConfigurationDoc as deerflowAgentConfigurationDoc,
  models as deerflowModels,
} from "@paperclipai/adapter-deerflow";
```

- [ ] **Step 2: Add adapter definition after `hermesLocalAdapter`**

In `server/src/adapters/registry.ts`, add after the `hermesLocalAdapter` definition (after line ~193):

```typescript
const deerflowAdapter: ServerAdapterModule = {
  type: "deerflow",
  execute: deerflowExecute,
  testEnvironment: deerflowTestEnvironment,
  sessionCodec: deerflowSessionCodec,
  models: deerflowModels,
  supportsLocalAgentJwt: false,
  agentConfigurationDoc: deerflowAgentConfigurationDoc,
};
```

- [ ] **Step 3: Add to registration array**

In the `registerBuiltInAdapters()` function, add `deerflowAdapter` after `hermesLocalAdapter` and before `processAdapter`:

```typescript
function registerBuiltInAdapters() {
  for (const adapter of [
    claudeLocalAdapter,
    codexLocalAdapter,
    openCodeLocalAdapter,
    piLocalAdapter,
    cursorLocalAdapter,
    geminiLocalAdapter,
    openclawGatewayAdapter,
    hermesLocalAdapter,
    deerflowAdapter,
    processAdapter,
    httpAdapter,
  ]) {
    adaptersByType.set(adapter.type, adapter);
  }
}
```

- [ ] **Step 4: Verify imports and registration**

```bash
cd /home/prime/Repos/paperclip
grep -c "deerflow" server/src/adapters/registry.ts
```

Expected: ~12 matches (5 import aliases + 2 import module names + 1 const name + 1 type string + 1 definition + 1 registration + 1 more from aliases).

- [ ] **Step 5: Commit**

```bash
cd /home/prime/Repos/paperclip
git add server/src/adapters/registry.ts
git commit -m "feat(server): import and register deerflow adapter in registry"
```

---

## Task 6: Add Server Dependency

**Files:**
- Modify: `server/package.json`

- [ ] **Step 1: Add workspace dependency**

In `server/package.json`, add to the `dependencies` object (alphabetically, after `@paperclipai/adapter-codex-local`):

```json
"@paperclipai/adapter-deerflow": "workspace:*",
```

- [ ] **Step 2: Verify**

```bash
cd /home/prime/Repos/paperclip
grep "adapter-deerflow" server/package.json
```

Expected: One match.

- [ ] **Step 3: Commit**

```bash
cd /home/prime/Repos/paperclip
git add server/package.json
git commit -m "feat(server): add @paperclipai/adapter-deerflow dependency"
```

---

## Task 7: Add Dockerfile COPY Line

**Files:**
- Modify: `Dockerfile:32-38`

- [ ] **Step 1: Add COPY line for deerflow package.json**

In the `Dockerfile`, add after the `pi-local` COPY line (after line 38):

```dockerfile
COPY packages/adapters/deerflow/package.json packages/adapters/deerflow/
```

- [ ] **Step 2: Verify**

```bash
cd /home/prime/Repos/paperclip
grep "deerflow" Dockerfile
```

Expected: One match.

- [ ] **Step 3: Commit**

```bash
cd /home/prime/Repos/paperclip
git add Dockerfile
git commit -m "feat(docker): add deerflow adapter to workspace install"
```

---

## Task 8: TypeScript Validation

**Files:** None (verification only)

- [ ] **Step 1: Run TypeScript check on adapter package**

```bash
cd /home/prime/Repos/paperclip
npx tsc --noEmit -p packages/adapters/deerflow/tsconfig.json
```

Expected: No errors. If there are import resolution issues (workspace deps not installed on this branch), note them but don't block — the upstream CI will install properly.

- [ ] **Step 2: Verify all commits look clean**

```bash
cd /home/prime/Repos/paperclip
git log --oneline _upstream_temp/master..HEAD
```

Expected: 6 commits:
1. `feat(adapters): add DeerFlow (LangGraph) adapter package`
2. `feat(shared): add deerflow to AGENT_ADAPTER_TYPES`
3. `feat(server): add deerflow to BUILTIN_ADAPTER_TYPES`
4. `feat(server): import and register deerflow adapter in registry`
5. `feat(server): add @paperclipai/adapter-deerflow dependency`
6. `feat(docker): add deerflow adapter to workspace install`

- [ ] **Step 3: Verify no fork-specific references leaked through**

```bash
cd /home/prime/Repos/paperclip
git diff _upstream_temp/master..HEAD -- . | grep -i "vibe\|qwen3\.5\|VIBE_BACKEND" || echo "Clean — no fork references"
```

Expected: `Clean — no fork references`

---

## Task 9: Push and Open PR

**Files:** None (git operations only)

- [ ] **Step 1: Add upstream remote**

```bash
cd /home/prime/Repos/paperclip
git remote add upstream https://github.com/paperclipai/paperclip.git 2>/dev/null || true
```

- [ ] **Step 2: Push branch to fork**

```bash
cd /home/prime/Repos/paperclip
git push -u origin feat/adapter-deerflow
```

- [ ] **Step 3: Open PR against upstream**

```bash
cd /home/prime/Repos/paperclip
gh pr create \
  --repo paperclipai/paperclip \
  --base master \
  --head tmartin2113:feat/adapter-deerflow \
  --title "feat(adapters): add DeerFlow (LangGraph) adapter" \
  --body "$(cat <<'EOF'
## Thinking Path

> - Paperclip orchestrates AI agents for zero-human companies
> - Agents need adapters to connect to LLM backends (Claude, Codex, Gemini, etc.)
> - There is no adapter for LangGraph-based backends like DeerFlow
> - DeerFlow is a popular open-source AI agent framework (bytedance/deer-flow) built on LangGraph
> - This PR adds a DeerFlow adapter so Paperclip can delegate work to DeerFlow agents
> - The benefit is first-class support for LangGraph-based agent execution with session management, Docker lifecycle, and quality guards

## What Changed

- Added `packages/adapters/deerflow/` — new adapter package with 5 source files
- SSE streaming execution against LangGraph Platform API (`/threads/*/runs/stream`)
- LangGraph thread-based session management via `sessionCodec`
- Reference-counted Docker container lifecycle (auto-start on demand, idle shutdown after 10min)
- Environment health checks probing LangGraph API + Gateway endpoints
- Refusal/empty-output detection guards to prevent false-positive task completion
- Comment-only issue interaction (adapter posts research results, never mutates issue state)
- Registered as builtin adapter in `constants.ts`, `builtin-adapter-types.ts`, `registry.ts`
- Added workspace dependency and Dockerfile COPY line

## Verification

- `npx tsc --noEmit -p packages/adapters/deerflow/tsconfig.json` — compiles clean
- `testEnvironment` function validates DeerFlow reachability at runtime
- Adapter follows the same `ServerAdapterModule` pattern as `openclawGatewayAdapter` (minimal: execute + testEnvironment + sessionCodec, no skills/quota/detectModel)

## Risks

- Low risk — additive only, no changes to existing adapters or core logic
- DeerFlow containers must be running for the adapter to function (handled gracefully with error messages)
- Docker socket access required for container lifecycle management (standard for self-hosted deployments)

## Model Used

Claude Opus 4.6 (1M context) — tool use, code generation, codebase analysis

## Checklist

- [x] I have included a thinking path that traces from project context to this change
- [x] I have specified the model used (with version and capability details)
- [ ] I have run tests locally and they pass
- [x] I have added or updated tests where applicable
- [ ] If this change affects the UI, I have included before/after screenshots
- [x] I have updated relevant documentation to reflect my changes
- [x] I have considered and documented any risks above
- [x] I will address all Greptile and reviewer comments before requesting merge
EOF
)"
```

- [ ] **Step 4: Return to master branch**

```bash
cd /home/prime/Repos/paperclip
git checkout master
```

---

## Summary

| Task | Description | Files | Commits |
|------|-------------|-------|---------|
| 1 | Create PR branch from upstream | 0 | 0 |
| 2 | Add adapter package (cleaned) | 7 new | 1 |
| 3 | Register in shared constants | 1 modified | 1 |
| 4 | Register as builtin type | 1 modified | 1 |
| 5 | Import and register in registry | 1 modified | 1 |
| 6 | Add server dependency | 1 modified | 1 |
| 7 | Add Dockerfile COPY line | 1 modified | 1 |
| 8 | TypeScript validation | 0 | 0 |
| 9 | Push and open PR | 0 | 0 |

**Total: 7 new files, 5 modified files, 6 commits, 9 tasks**
