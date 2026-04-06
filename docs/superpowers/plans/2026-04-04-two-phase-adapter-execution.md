# Two-Phase Adapter Execution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the `claude_local` adapter into a planning phase and an execution phase so Senior Engineers delegate to free DeerFlow assistants before burning paid turns on implementation.

**Architecture:** The `execute()` function in the claude_local adapter gains a `twoPhaseEnabled` flag. When enabled, it runs two sequential Claude Code invocations: Phase 1 (plan & delegate, capped turns, can't edit files) then Phase 2 (implement, remaining turns, plan injected as context). The split is enforced at the process level — two separate `runChildProcess` calls.

**Tech Stack:** TypeScript (Paperclip server), Markdown (agent instruction files)

**Spec:** `docs/superpowers/specs/2026-04-04-two-phase-adapter-execution-design.md`

---

## Prerequisites

The Paperclip fork must be cloned locally before starting:

```bash
cd ~/Repos
git clone git@github.com:tmartin2113/paperclip.git
cd paperclip
git checkout -b feat/two-phase-adapter
```

All adapter code paths below are relative to the Paperclip repo root.

---

### Task 1: Add config fields and update agentConfigurationDoc

**Files:**
- Modify: `packages/adapters/claude-local/src/index.ts`

- [ ] **Step 1: Read the current file**

Read `packages/adapters/claude-local/src/index.ts` to confirm the current `agentConfigurationDoc` content.

- [ ] **Step 2: Add new config fields to agentConfigurationDoc**

Add three new fields to the doc string:

```typescript
export const agentConfigurationDoc = `# claude_local agent configuration

Adapter: claude_local

Core fields:
- cwd (string, optional): default absolute working directory fallback for the agent process (created if missing when possible)
- instructionsFilePath (string, optional): absolute path to a markdown instructions file injected at runtime
- model (string, optional): Claude model id
- effort (string, optional): reasoning effort passed via --effort (low|medium|high)
- chrome (boolean, optional): pass --chrome when running Claude
- promptTemplate (string, optional): run prompt template
- maxTurnsPerRun (number, optional): max turns for one run
- dangerouslySkipPermissions (boolean, optional): pass --dangerously-skip-permissions to claude
- command (string, optional): defaults to "claude"
- extraArgs (string[], optional): additional CLI args
- env (object, optional): KEY=VALUE environment variables
- twoPhaseEnabled (boolean, optional): enable plan-then-execute two-phase mode (default: false)
- maxPlanningTurns (number, optional): max turns for Phase 1 planning (default: 10, only used when twoPhaseEnabled)
- planningModel (string, optional): override model for Phase 1 (e.g. claude-haiku-4-5-20251001 for cheaper planning)

Operational fields:
- timeoutSec (number, optional): run timeout in seconds
- graceSec (number, optional): SIGTERM grace period in seconds
`;
```

- [ ] **Step 3: Commit**

```bash
git add packages/adapters/claude-local/src/index.ts
git commit -m "feat: add twoPhaseEnabled, maxPlanningTurns, planningModel config fields to claude_local adapter"
```

---

### Task 2: Add prompt templates for Phase 1 and Phase 2

**Files:**
- Create: `packages/adapters/claude-local/src/server/two-phase-prompts.ts`

- [ ] **Step 1: Create the prompt templates file**

```typescript
import type { AdapterExecutionContext } from "@paperclipai/adapter-utils";
import { asString, parseObject, renderTemplate } from "@paperclipai/adapter-utils/server-utils";

const PLANNING_PROMPT_TEMPLATE = `You are in PLANNING MODE. You have {{maxPlanningTurns}} turns.

Your task:
{{taskTitle}}

{{taskDescription}}

## Your job right now

1. Read the task context file at skills/paperclip/references/run-context.md
2. Read relevant codebase files to understand the scope
3. Break the work into subtasks
4. For each subtask, decide: delegate to DeerFlow assistant or do yourself

Delegation rules — follow these strictly:
- Research, boilerplate, documentation, test fixtures → DELEGATE (free, runs on local GPU, zero cost)
- Complex implementation, architecture, security-sensitive code → DO YOURSELF

5. Create DeerFlow subtasks via Paperclip API for everything you're delegating.
   Your DeerFlow assistant IDs are in your instructions file — read it.
   Use the Paperclip skill's "Create subtask" endpoint with parentId set to the current task.
6. Post a plan comment on the issue listing:
   - What you delegated (with subtask links)
   - What you will implement yourself
   - Your implementation approach

## Constraints

- Do NOT write implementation code — no creating files, no editing files, no running tests
- You ARE allowed to read files to understand the codebase
- You ARE allowed to make Paperclip API calls (create subtasks, post comments, read issues)
- When done planning, exit cleanly

## Model tier guidance

You are in the planning phase — keep it efficient. Your implementation phase has a separate turn budget.
Research and boilerplate are FREE on DeerFlow. Every subtask you delegate saves paid turns in the next phase.`;

const EXECUTION_PROMPT_TEMPLATE = `You are in EXECUTION MODE. You have {{maxExecutionTurns}} turns.

Your task:
{{taskTitle}}

{{taskDescription}}

## Plan from planning phase

{{plan}}

## What was delegated to DeerFlow (do NOT redo this work)

{{delegatedSummary}}

## Your job

Implement the work items from the plan that are marked for you to do yourself.
Do NOT duplicate work that was delegated to your DeerFlow assistant — they handle those items on their own heartbeat.

Focus your turns on implementation:
- Write code, write tests, run tests
- Follow the plan's approach
- If the plan is empty or unclear, proceed with the full task using your best judgment

When done:
- Update the issue status and post a completion comment
- If you cannot finish in your remaining turns, post a progress comment listing what you completed and what remains
- Commit your work with conventional commit messages`;

export function buildPlanningPrompt(
  ctx: AdapterExecutionContext,
  maxPlanningTurns: number,
): string {
  const context = parseObject(ctx.context);
  const taskTitle = asString(context.issueTitle, asString(context.title, "Untitled task"));
  const taskDescription = asString(context.issueDescription, asString(context.description, "No description provided."));

  return renderTemplate(PLANNING_PROMPT_TEMPLATE, {
    maxPlanningTurns: String(maxPlanningTurns),
    taskTitle,
    taskDescription,
  });
}

export function buildExecutionPrompt(
  ctx: AdapterExecutionContext,
  maxExecutionTurns: number,
  plan: string,
  delegatedSummary: string,
): string {
  const context = parseObject(ctx.context);
  const taskTitle = asString(context.issueTitle, asString(context.title, "Untitled task"));
  const taskDescription = asString(context.issueDescription, asString(context.description, "No description provided."));

  return renderTemplate(EXECUTION_PROMPT_TEMPLATE, {
    maxExecutionTurns: String(maxExecutionTurns),
    taskTitle,
    taskDescription,
    plan: plan || "No structured plan was produced. Proceed with the full task.",
    delegatedSummary: delegatedSummary || "No subtasks were delegated.",
  });
}
```

- [ ] **Step 2: Commit**

```bash
git add packages/adapters/claude-local/src/server/two-phase-prompts.ts
git commit -m "feat: add Phase 1 (planning) and Phase 2 (execution) prompt templates"
```

---

### Task 3: Implement two-phase execution logic in execute.ts

**Files:**
- Modify: `packages/adapters/claude-local/src/server/execute.ts`

This is the core change. The existing `execute()` function is ~300 lines. We add a `executeTwoPhase()` function and gate on `twoPhaseEnabled` at the top of `execute()`.

- [ ] **Step 1: Read the current execute.ts**

Read the full file to understand the current structure. Key sections:
- Lines 538-570: config parsing (model, maxTurns, etc.)
- Lines 580-635: env setup, skills dir, task context injection
- Lines 640-680: session handling, prompt rendering, args building
- Lines 700-720: `runAttempt()` — runs Claude Code via `runChildProcess`
- Lines 730-830: `toAdapterResult()` — parses output into AdapterExecutionResult
- Lines 830-849: try/finally — run attempt with session retry

- [ ] **Step 2: Add imports for the two-phase prompts**

At the top of `execute.ts`, add:

```typescript
import { buildPlanningPrompt, buildExecutionPrompt } from "./two-phase-prompts.js";
```

- [ ] **Step 3: Add the mergeAdapterResults helper**

Add before the `execute()` function:

```typescript
function mergeAdapterResults(
  phase1: AdapterExecutionResult,
  phase2: AdapterExecutionResult,
): AdapterExecutionResult {
  return {
    ...phase2,
    usage: {
      inputTokens: (phase1.usage?.inputTokens ?? 0) + (phase2.usage?.inputTokens ?? 0),
      outputTokens: (phase1.usage?.outputTokens ?? 0) + (phase2.usage?.outputTokens ?? 0),
      cachedInputTokens: (phase1.usage?.cachedInputTokens ?? 0) + (phase2.usage?.cachedInputTokens ?? 0),
    },
    costUsd: (phase1.costUsd ?? 0) + (phase2.costUsd ?? 0),
    sessionId: phase2.sessionId,
    sessionParams: phase2.sessionParams,
    resultJson: {
      ...(phase2.resultJson ?? {}),
      phase1_summary: phase1.summary ?? null,
      phase1_input_tokens: phase1.usage?.inputTokens ?? 0,
      phase1_output_tokens: phase1.usage?.outputTokens ?? 0,
    },
  };
}

function extractPlanFromResult(result: AdapterExecutionResult): string {
  const resultText = result.summary || asString(result.resultJson?.result, "");
  if (resultText.length > 50) return resultText;
  return "No structured plan was produced. Proceed with the full task.";
}
```

- [ ] **Step 4: Add the executeTwoPhase function**

Add after the helpers, before `execute()`:

```typescript
async function executeTwoPhase(ctx: AdapterExecutionContext): Promise<AdapterExecutionResult> {
  const { runId, agent, config, context, onLog, onMeta, authToken } = ctx;

  const maxPlanningTurns = asNumber(config.maxPlanningTurns, 10);
  const totalMaxTurns = asNumber(config.maxTurnsPerRun, 60);
  const maxExecutionTurns = Math.max(10, totalMaxTurns - maxPlanningTurns);
  const planningModel = asString(config.planningModel, "").trim();

  const safeLog = async (msg: string) => {
    if (ctx.onLog) await ctx.onLog("stderr", `[paperclip:two-phase] ${msg}\n`);
  };

  await safeLog(`Phase 1: Planning (max ${maxPlanningTurns} turns, model: ${planningModel || config.model || "default"})`);

  // Phase 1: Plan & Delegate
  const phase1Prompt = buildPlanningPrompt(ctx, maxPlanningTurns);
  const phase1Config = {
    ...config,
    maxTurnsPerRun: maxPlanningTurns,
    ...(planningModel ? { model: planningModel } : {}),
    // Override the prompt template for Phase 1
    promptTemplate: phase1Prompt,
  };
  const phase1Ctx: AdapterExecutionContext = {
    ...ctx,
    config: phase1Config,
  };

  // Run Phase 1 using the existing execute function (single-phase mode)
  // Temporarily disable twoPhaseEnabled to avoid recursion
  phase1Ctx.config = { ...phase1Ctx.config, twoPhaseEnabled: false };
  let phase1Result: AdapterExecutionResult;
  try {
    phase1Result = await execute(phase1Ctx);
  } catch (err) {
    await safeLog(`Phase 1 failed: ${err}`);
    throw err;
  }

  // If Phase 1 had a fatal error (not max_turns), report it
  if (phase1Result.exitCode !== 0 && phase1Result.exitCode !== null && !isClaudeMaxTurnsResult(phase1Result.resultJson)) {
    await safeLog(`Phase 1 exited with error (code ${phase1Result.exitCode}), skipping Phase 2`);
    return phase1Result;
  }

  const plan = extractPlanFromResult(phase1Result);
  await safeLog(`Phase 1 complete. Plan length: ${plan.length} chars`);
  await safeLog(`Phase 2: Execution (max ${maxExecutionTurns} turns)`);

  // Phase 2: Execute
  const phase2Prompt = buildExecutionPrompt(ctx, maxExecutionTurns, plan, "See plan above for delegation details.");
  const phase2Config = {
    ...config,
    maxTurnsPerRun: maxExecutionTurns,
    twoPhaseEnabled: false,
    // Use the original model for execution (not planningModel)
    promptTemplate: phase2Prompt,
  };
  const phase2Ctx: AdapterExecutionContext = {
    ...ctx,
    config: phase2Config,
  };

  let phase2Result: AdapterExecutionResult;
  try {
    phase2Result = await execute(phase2Ctx);
  } catch (err) {
    await safeLog(`Phase 2 failed: ${err}`);
    // Return Phase 1 result merged with the error
    return {
      ...phase1Result,
      errorMessage: `Phase 2 failed: ${err}`,
      errorCode: "phase2_failed",
    };
  }

  await safeLog("Phase 2 complete. Merging results.");
  return mergeAdapterResults(phase1Result, phase2Result);
}
```

- [ ] **Step 5: Gate execute() on twoPhaseEnabled**

At the very top of the `execute()` function body (after the `const { runId, agent, ... } = ctx;` destructure), add:

```typescript
  // Two-phase execution: plan & delegate first, then implement
  if (asBoolean(config.twoPhaseEnabled, false)) {
    return executeTwoPhase(ctx);
  }
```

- [ ] **Step 6: Export isClaudeMaxTurnsResult if not already available in scope**

Check if `isClaudeMaxTurnsResult` is imported at the top of execute.ts. It should already be imported from `./parse.js`. Verify:

```typescript
import {
  parseClaudeStreamJson,
  describeClaudeFailure,
  detectClaudeLoginRequired,  // existing
  isClaudeMaxTurnsResult,     // should already be here
  isClaudeUnknownSessionError,
} from "./parse.js";
```

- [ ] **Step 7: Commit**

```bash
git add packages/adapters/claude-local/src/server/execute.ts
git commit -m "feat: implement two-phase execution - plan & delegate before implementation"
```

---

### Task 4: Update engineer-instructions.md

**Files:**
- Modify: `/home/prime/Projects/.paperclip/engineer-instructions.md`

- [ ] **Step 1: Read the current file**

Read `/home/prime/Projects/.paperclip/engineer-instructions.md`.

- [ ] **Step 2: Update the Workflow section**

Replace the Workflow section (lines 8-16) with:

```markdown
## Workflow

1. Read the issue description and acceptance criteria carefully
2. Understand the existing codebase before making changes — read relevant files first
3. **Plan and delegate first.** Break the work into subtasks. Delegate research, boilerplate, docs, and test fixtures to your DeerFlow assistant — it's free. Keep complex implementation for yourself.
4. Implement with tests. Write tests alongside code, not after.
5. Run the full test suite before marking done
6. Post results and update the issue status
```

- [ ] **Step 3: Update the Mandatory Delegation Triage section**

Replace lines 37-52 (the intro paragraph and table) with:

```markdown
## Mandatory Delegation Triage

You have a DeerFlow assistant (Haiku-tier, local Qwen 3.5 9B on GPU). It runs **fast and free** — zero API cost. Your turns cost real money. Delegation is not optional.

Your adapter may run in **two-phase mode**: Phase 1 is planning/delegation only (you cannot edit files), Phase 2 is implementation. If you're in Phase 1, your only job is to plan, delegate, and exit. If you're in single-phase mode, you must self-discipline: plan and delegate before writing code.

### Pre-Task Classification (Required)

Before starting any subtask or phase of work, classify it:

| Category | Examples | Who Does It |
|----------|----------|-------------|
| **Research** | Reading docs, summarizing code, investigating libraries, checking API signatures, gathering error messages | **DELEGATE to assistant** |
| **Boilerplate** | Test fixtures, data factories, type stubs, config scaffolding, migration templates, README sections | **DELEGATE to assistant** |
| **Documentation** | Writing/updating docs, ADRs, comments, issue descriptions, PR descriptions | **DELEGATE to assistant** |
| **Complex implementation** | Multi-file logic, architecture, security-sensitive code, nuanced debugging, code review | **Do it yourself** |

**Rule: If a subtask fits the first three categories, you MUST delegate it.** Your time on the Anthropic API costs money. Your assistant's time on local vLLM costs nothing.
```

- [ ] **Step 4: Add model tier guidance at the end of the file**

Append before the "When You're Stuck" section:

```markdown
## Model Usage

- **Sonnet** is your default. Use it for all standard work.
- **Haiku** is used for planning phases when configured by the adapter.
- **Opus** is reserved for genuinely lofty tasks — complex multi-system architecture, deep security audits, intricate cross-cutting refactors. Do not request or assume Opus for routine work.
- **DeerFlow (local vLLM)** is free. Every subtask you delegate there saves paid turns.
```

- [ ] **Step 5: Commit**

```bash
git -C /home/prime/Projects/.paperclip add engineer-instructions.md
git -C /home/prime/Projects/.paperclip commit -m "docs: align engineer instructions with two-phase adapter execution"
```

Note: If `/home/prime/Projects/.paperclip` is not a git repo, just note the change — it will be picked up by the volume mount.

---

### Task 5: Update base-instructions.md

**Files:**
- Modify: `/home/prime/Projects/.paperclip/base-instructions.md`

- [ ] **Step 1: Read the current file**

Read `/home/prime/Projects/.paperclip/base-instructions.md`.

- [ ] **Step 2: Update Paperclip Coordination section**

Replace lines 36-45 with:

```markdown
## Paperclip Coordination

You have access to the Paperclip API. On every task:

1. **Start**: Checkout the task via the Paperclip skill's heartbeat procedure (the adapter handles `in_progress` status)
2. **Progress**: Post comments on the issue with meaningful updates (not noise)
3. **Delegate**: If your adapter runs in two-phase mode, Phase 1 is for planning and delegation only — create DeerFlow subtasks for research, boilerplate, and docs before implementation
4. **Blockers**: If blocked, set status to `blocked` and comment explaining why
5. **Review**: When code is ready, set status to `in_review` and post the PR link
6. **Done**: Set status to `done` only when the PR is merged and tests pass

Use the Paperclip API endpoints available in your environment. Your agent JWT is injected automatically.
```

- [ ] **Step 3: Commit**

```bash
git -C /home/prime/Projects/.paperclip add base-instructions.md
git -C /home/prime/Projects/.paperclip commit -m "docs: add two-phase and delegation guidance to base instructions"
```

---

### Task 6: Update cto-instructions.md

**Files:**
- Modify: `/home/prime/Projects/.paperclip/cto-instructions.md`

- [ ] **Step 1: Read the current file**

Read `/home/prime/Projects/.paperclip/cto-instructions.md`.

- [ ] **Step 2: Add delegation context for Senior Engineers**

Find the section about delegating to Claude engineers (around lines 49-54) and add after it:

```markdown
### Writing Tasks for Senior Engineers

Senior Engineers run in two-phase mode: they plan and delegate before implementing. Write task descriptions that support this:

- **Clear acceptance criteria** — the engineer's planning phase uses these to scope the work
- **Explicit scope boundaries** — state what's in scope and what's NOT, so the engineer doesn't over-expand
- **Mention if research is needed** — the engineer will delegate research to their DeerFlow assistant (free), so flag it explicitly
- **One logical unit per task** — if a task has multiple independent pieces, create separate subtasks. This lets the engineer's planning phase produce a focused plan instead of an overwhelming one.
```

- [ ] **Step 3: Commit**

```bash
git -C /home/prime/Projects/.paperclip add cto-instructions.md
git -C /home/prime/Projects/.paperclip commit -m "docs: add task-writing guidance for two-phase Senior Engineers"
```

---

### Task 7: Enable two-phase on Backend Engineer (pilot)

**Files:**
- Modify: Agent config via Paperclip API or `patch-agents.js`

- [ ] **Step 1: Read the current patch-agents.js**

Read `~/Repos/Vibe-Stack/patch-agents.js` to understand how agent configs are patched.

- [ ] **Step 2: Create a patch script for two-phase config**

Create `~/Repos/Vibe-Stack/enable-two-phase.js`:

```javascript
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const envPath = path.resolve(__dirname, ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
    const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*)\s*$/);
    if (match && !process.env[match[1]]) process.env[match[1]] = match[2];
  }
}

const API_URL = process.env.PAPERCLIP_API_URL || "http://localhost:3100";
const PASSWORD = process.env.PAPERCLIP_ADMIN_PASSWORD;
if (!PASSWORD) {
  console.error("Set PAPERCLIP_ADMIN_PASSWORD in .env");
  process.exit(1);
}

async function request(method, path, cookie, body) {
  const url = new URL(path, API_URL);
  const options = {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(cookie ? { Cookie: cookie } : {}),
    },
  };
  return new Promise((resolve, reject) => {
    const req = http.request(url, options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        const setCookie = res.headers["set-cookie"]?.map((c) => c.split(";")[0]).join("; ");
        resolve({ status: res.statusCode, data: data ? JSON.parse(data) : null, cookie: setCookie });
      });
    });
    req.on("error", reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

async function main() {
  // Login
  const login = await request("POST", "/api/auth/sign-in/email", null, {
    email: "prime@vibe.local",
    password: PASSWORD,
  });
  if (login.status !== 200) {
    console.error("Login failed:", login.status, login.data);
    process.exit(1);
  }
  const cookie = login.cookie;
  console.log("Logged in");

  // Backend Engineer agent ID
  const BACKEND_ENGINEER_ID = "08c677aa-95b9-4724-bb56-ed21d9264f94";

  // Get current config
  const agent = await request("GET", `/api/agents/${BACKEND_ENGINEER_ID}`, cookie);
  const currentConfig = agent.data?.adapterConfig || {};
  console.log("Current adapterConfig:", JSON.stringify(currentConfig, null, 2));

  // Patch with two-phase enabled
  const newConfig = {
    ...currentConfig,
    twoPhaseEnabled: true,
    maxPlanningTurns: 10,
    planningModel: "claude-haiku-4-5-20251001",
  };

  const patch = await request("PATCH", `/api/agents/${BACKEND_ENGINEER_ID}`, cookie, {
    adapterConfig: newConfig,
  });

  if (patch.status === 200) {
    console.log("Backend Engineer updated with two-phase config");
    console.log("New adapterConfig:", JSON.stringify(newConfig, null, 2));
  } else {
    console.error("Patch failed:", patch.status, patch.data);
  }
}

main().catch(console.error);
```

- [ ] **Step 3: Run the patch (after the server image is rebuilt with two-phase support)**

```bash
cd ~/Repos/Vibe-Stack && node enable-two-phase.js
```

Expected: "Backend Engineer updated with two-phase config"

- [ ] **Step 4: Commit the script**

```bash
cd ~/Repos/Vibe-Stack && git add enable-two-phase.js
git commit -m "chore: add script to enable two-phase execution on Senior Engineers"
```

---

### Task 8: Build and deploy the updated Paperclip server

**Files:**
- Paperclip fork CI/CD or manual Docker build

- [ ] **Step 1: Verify the Paperclip fork builds locally**

```bash
cd ~/Repos/paperclip
pnpm install
pnpm build
```

Expected: Build succeeds with no errors.

- [ ] **Step 2: Build the Docker image**

```bash
cd ~/Repos/paperclip
docker build -t ghcr.io/tmartin2113/paperclip-server:two-phase -f Dockerfile .
```

Expected: Image builds successfully.

- [ ] **Step 3: Tag and push to GHCR**

```bash
docker tag ghcr.io/tmartin2113/paperclip-server:two-phase ghcr.io/tmartin2113/paperclip-server:latest
docker push ghcr.io/tmartin2113/paperclip-server:two-phase
docker push ghcr.io/tmartin2113/paperclip-server:latest
```

- [ ] **Step 4: Restart the Vibe Stack server with the new image**

```bash
cd ~/Repos/Vibe-Stack
docker compose pull server
docker compose up -d server
```

- [ ] **Step 5: Run the enable-two-phase script**

```bash
cd ~/Repos/Vibe-Stack && node enable-two-phase.js
```

- [ ] **Step 6: Commit the Paperclip fork changes**

```bash
cd ~/Repos/paperclip
git add -A
git commit -m "feat: two-phase adapter execution for claude_local - plan & delegate before implementation"
git push origin feat/two-phase-adapter
```

---

### Task 9: Smoke test — assign a task to Backend Engineer and verify two-phase behavior

- [ ] **Step 1: Unpause the Backend Engineer**

In the Paperclip UI, or via API:

```bash
curl -X POST "$PAPERCLIP_API_URL/api/agents/08c677aa-95b9-4724-bb56-ed21d9264f94/resume?companyId=93ec8aae-79b9-4017-b053-80dbca0ebad3" \
  -H "Authorization: Bearer $PAPERCLIP_API_KEY"
```

- [ ] **Step 2: Create a test task**

```bash
cd ~/Repos/Vibe-Stack && node create-task.js "Test two-phase: add a /healthz endpoint to the API that returns {status: ok, timestamp: Date.now()}" --assignee "08c677aa-95b9-4724-bb56-ed21d9264f94"
```

Or create via the Paperclip UI.

- [ ] **Step 3: Monitor the server logs for two-phase execution**

```bash
cd ~/Repos/Vibe-Stack && docker compose logs server -f 2>/dev/null | grep -i "two-phase\|phase.1\|phase.2\|planning"
```

Expected:
- `[paperclip:two-phase] Phase 1: Planning (max 10 turns, model: claude-haiku-4-5-20251001)`
- `[paperclip:two-phase] Phase 1 complete. Plan length: XXX chars`
- `[paperclip:two-phase] Phase 2: Execution (max 50 turns)`
- `[paperclip:two-phase] Phase 2 complete. Merging results.`

- [ ] **Step 4: Verify delegation happened**

Check the issue in the Paperclip UI:
- Phase 1 should have posted a plan comment
- DeerFlow subtasks should have been created (if the task had delegatable components)
- Phase 2 should have posted implementation results

- [ ] **Step 5: Check run metadata in the DB**

```bash
cd ~/Repos/Vibe-Stack && docker compose exec -w /app server node --input-type=module -e "
import postgres from '/app/node_modules/.pnpm/postgres@3.4.8/node_modules/postgres/src/index.js';
const sql = postgres({host:'/tmp',port:54329,user:'paperclip',database:'paperclip'});
const runs = await sql\`SELECT id, status, result_json->>'phase1_summary' as phase1, result_json->>'phase1_input_tokens' as p1_in, result_json->>'phase1_output_tokens' as p1_out FROM heartbeat_runs WHERE agent_id = '08c677aa-95b9-4724-bb56-ed21d9264f94' ORDER BY created_at DESC LIMIT 3\`;
console.log(JSON.stringify(runs, null, 2));
await sql.end();
"
```

Expected: Latest run has `phase1_summary` and `phase1_input_tokens` populated.
