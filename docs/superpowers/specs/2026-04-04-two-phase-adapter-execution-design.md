# Two-Phase Adapter Execution for Senior Engineers

**Date:** 2026-04-04
**Status:** Draft
**Problem:** Senior Engineers (claude_local) hit max turns repeatedly on the same task without delegating to their free DeerFlow assistants. Prompt-level instructions ("MUST delegate") are ignored under task pressure. Starting April 4 2026, third-party harnesses draw from extra usage credits instead of subscription — every wasted turn is real money.

## Root Cause

The `claude_local` adapter runs a single Claude Code invocation per heartbeat. The agent gets the task, sees code to write, and starts writing immediately. By the time it considers delegation, it's already deep in implementation and out of turns. The delegation instructions in `engineer-instructions.md` and the `paperclip` skill compete with the model's instinct to just do the work — and lose.

## Solution

Split the `claude_local` adapter's `execute()` function into two sequential Claude Code invocations per heartbeat run:

**Phase 1 — Plan & Delegate** (capped at `maxPlanningTurns`, default 10)
- Reads the task context
- Breaks the work into subtasks
- Creates DeerFlow subtasks via Paperclip API for anything that qualifies (research, boilerplate, docs)
- Outputs a structured plan of what remains for Phase 2
- Cannot write implementation code — prompt constrains it to planning and delegation only

**Phase 2 — Execute** (remaining turns from `maxTurnsPerRun - maxPlanningTurns`)
- Receives the plan from Phase 1 injected as context
- Knows what was delegated and what it owns
- Executes only the implementation work
- Posts results and updates issue status

The adapter enforces the split at the process level — two separate `runChildProcess` calls. Phase 1 physically exits before Phase 2 starts. The LLM cannot skip planning because it's a separate invocation.

## Adapter Changes (`packages/adapters/claude-local/src/server/execute.ts`)

### New Config Fields

```typescript
// Added to agent's adapterConfig (adapter_config jsonb in agents table)
maxPlanningTurns: number    // default: 10, cap for Phase 1
planningModel: string       // optional, override model for Phase 1 (e.g. use haiku for cheap planning)
twoPhaseEnabled: boolean    // default: false, opt-in per agent
```

`twoPhaseEnabled` defaults to `false` so existing agents are unaffected. Enable it on Senior Engineers only.

### Execution Flow

```
execute(ctx) {
  if (!config.twoPhaseEnabled) {
    // Existing single-phase flow — unchanged
    return existingExecute(ctx);
  }

  // Phase 1: Plan & Delegate
  phase1Result = runClaudeCode({
    prompt: PLANNING_PROMPT_TEMPLATE(task),
    maxTurns: config.maxPlanningTurns || 10,
    model: config.planningModel || config.model,
    sessionId: null,  // fresh session, no resume
    skillsDir: skillsDir,  // needs paperclip skill for API calls
    instructionsFile: config.instructionsFilePath,
  });

  // Extract plan from Phase 1 output
  plan = extractPlanFromResult(phase1Result);

  // Phase 2: Execute
  phase2Result = runClaudeCode({
    prompt: EXECUTION_PROMPT_TEMPLATE(task, plan),
    maxTurns: (config.maxTurnsPerRun || 60) - (config.maxPlanningTurns || 10),
    model: config.model,
    sessionId: null,  // fresh session
    skillsDir: skillsDir,
    instructionsFile: config.instructionsFilePath,
  });

  // Merge results: combine usage from both phases
  return mergeAdapterResults(phase1Result, phase2Result);
}
```

### Phase 1 Prompt Template

```
You are in PLANNING MODE. You have {{maxPlanningTurns}} turns.

Your task:
{{taskTitle}}
{{taskDescription}}

## Your job right now

1. Read the task and understand what's needed
2. Read the codebase to understand relevant files and patterns
3. Break the work into subtasks
4. For each subtask, decide: delegate to DeerFlow assistant or do yourself

Delegation rules:
- Research, boilerplate, documentation, test fixtures → DELEGATE (free, local GPU)
- Complex implementation, architecture, security-sensitive code → DO YOURSELF

5. Create DeerFlow subtasks via Paperclip API for everything you're delegating
6. Post a plan comment on the issue listing what you delegated and what you'll implement

## Constraints

- Do NOT write implementation code
- Do NOT create files, edit files, or run tests
- You ARE allowed to read files to understand the codebase
- You ARE allowed to make Paperclip API calls (create subtasks, post comments)
- When done planning, exit cleanly

Your DeerFlow assistant IDs are in your instructions file. Use them.
```

### Phase 2 Prompt Template

```
You are in EXECUTION MODE. You have {{maxExecutionTurns}} turns.

Your task:
{{taskTitle}}
{{taskDescription}}

## Plan from planning phase

{{plan}}

## What was delegated to DeerFlow (do NOT redo this work)

{{delegatedSubtasks}}

## Your job

Implement the work items marked as "do yourself" in the plan above.
Do not duplicate work that was delegated — your DeerFlow assistant handles those items separately.

When done, update the issue status and post a completion comment.
If you cannot finish in your remaining turns, post a progress comment
with what you completed and what remains.
```

### Result Merging

```typescript
function mergeAdapterResults(
  phase1: AdapterExecutionResult,
  phase2: AdapterExecutionResult
): AdapterExecutionResult {
  return {
    ...phase2,  // Phase 2 result is primary
    usage: {
      inputTokens: (phase1.usage?.inputTokens ?? 0) + (phase2.usage?.inputTokens ?? 0),
      outputTokens: (phase1.usage?.outputTokens ?? 0) + (phase2.usage?.outputTokens ?? 0),
      cachedInputTokens: (phase1.usage?.cachedInputTokens ?? 0) + (phase2.usage?.cachedInputTokens ?? 0),
    },
    costUsd: (phase1.costUsd ?? 0) + (phase2.costUsd ?? 0),
    // Session from Phase 2 (Phase 1 session is discarded)
    sessionId: phase2.sessionId,
    sessionParams: phase2.sessionParams,
    // If either phase hit max turns, flag it
    resultJson: {
      ...phase2.resultJson,
      phase1_summary: phase1.summary,
      phase1_input_tokens: phase1.usage?.inputTokens ?? 0,
      phase1_output_tokens: phase1.usage?.outputTokens ?? 0,
    },
  };
}
```

### Plan Extraction

Phase 1's Claude Code output is parsed from the stream JSON. The plan is extracted from:
1. The `result` field of the parsed output (Claude's final message)
2. Or from the issue comment posted during Phase 1 (fetched via Paperclip API as fallback)

```typescript
function extractPlanFromResult(result: AdapterExecutionResult): string {
  // Primary: Claude's result text
  const resultText = result.summary || asString(result.resultJson?.result, "");
  if (resultText.length > 50) return resultText;

  // Fallback: empty plan — Phase 2 proceeds with full task context
  return "No structured plan was produced. Proceed with the full task.";
}
```

### Error Handling

- **Phase 1 fails or times out**: Skip Phase 2, report the error. The task stays `in_progress` for retry on next heartbeat.
- **Phase 1 hits max turns**: Normal — it planned as much as it could. Proceed to Phase 2 with whatever plan was produced.
- **Phase 1 exits with code 0 but no plan**: Proceed to Phase 2 with full task context (graceful degradation).
- **Phase 2 fails**: Report normally, same as current single-phase behavior.
- **Phase 2 hits max turns**: Report `error_max_turns` as today, but the issue should have less remaining work since research/boilerplate was delegated.

### Session Handling

- Phase 1 always starts a fresh session (no `--resume`). Its session is discarded after planning.
- Phase 2 starts a fresh session. It can resume on subsequent heartbeats if the task needs multiple runs (existing behavior).
- `clearSessionForMaxTurns` only applies to Phase 2.

## Config Changes Per Agent

Update Senior Engineer agent configs via `PATCH /api/agents/{id}` or DB patch:

```json
{
  "adapterConfig": {
    "twoPhaseEnabled": true,
    "maxPlanningTurns": 10,
    "maxTurnsPerRun": 60
  }
}
```

This gives 10 turns for planning/delegation and 50 turns for implementation. Adjust per agent based on role complexity.

### Optional: Cheaper Planning Model

For cost optimization, Phase 1 can use a cheaper model since planning doesn't need Sonnet-level capability:

```json
{
  "adapterConfig": {
    "twoPhaseEnabled": true,
    "planningModel": "claude-haiku-4-5-20251001",
    "maxPlanningTurns": 10
  }
}
```

Haiku can read code, break down tasks, and make API calls. It's significantly cheaper for the planning phase.

## Model Tier Policy

With extra usage billing, model selection directly impacts cost. The adapter should enforce a model hierarchy:

| Tier | Model | When to use | Cost |
|------|-------|-------------|------|
| **Default** | Sonnet | All standard engineering work — implementation, debugging, code review | Baseline |
| **Planning** | Haiku (recommended) or Sonnet | Phase 1 planning & delegation — reading code, breaking down tasks, making API calls | ~10x cheaper than Sonnet |
| **DeerFlow** | Qwen 3.5 9B (local vLLM) | Research, boilerplate, docs, test fixtures | Free |
| **Opus** | Opus | **Almost never.** Only for tasks that demonstrably fail on Sonnet after a genuine attempt — complex multi-system architecture decisions, subtle cross-cutting security audits, or intricate debugging that Sonnet cannot resolve. | ~5x more than Sonnet |

**Opus rules:**
- No agent should be configured with Opus as its default model
- Opus is justified for lofty, high-complexity tasks — full-system architecture, complex multi-service refactors, deep security audits, or tasks where getting it right the first time matters more than cost
- The CTO may use Opus for architectural decisions that span the full system
- Senior Engineers may escalate to Opus when a task is genuinely complex enough to warrant it — but routine implementation, bug fixes, and standard feature work stay on Sonnet
- When in doubt, start with Sonnet. Escalate to Opus if the task demands it, not as a default

The `planningModel` config field lets you run Phase 1 on Haiku while Phase 2 stays on Sonnet. This is the recommended default for all Senior Engineers.

## What This Does NOT Change

- The `paperclip` skill — unchanged, loaded in both phases
- The `engineer-instructions.md` — unchanged, appended in both phases
- The heartbeat service (`heartbeat.ts`) — unchanged, it calls `execute()` and gets back a single `AdapterExecutionResult`
- DeerFlow adapter — unchanged
- CTO agent — unchanged (runs Python heartbeat, not claude_local two-phase)
- Single-phase agents — unchanged (`twoPhaseEnabled: false` is default)

## Files Modified

| File | Change |
|------|--------|
| `packages/adapters/claude-local/src/server/execute.ts` | Add two-phase execution logic, prompt templates, result merging |
| `packages/adapters/claude-local/src/index.ts` | Export new config fields in `agentConfigurationDoc` |
| `/home/prime/Projects/.paperclip/engineer-instructions.md` | Align delegation triage with two-phase — Phase 1 is planning, reinforce what to delegate |
| `/home/prime/Projects/.paperclip/base-instructions.md` | Minor: note that checkout/status is adapter-managed in two-phase mode |
| `/home/prime/Projects/.paperclip/cto-instructions.md` | Note that Senior Engineers plan before executing; write task descriptions with clear scope/criteria |

## Cost Impact

Assuming a task that currently takes 60 turns in single-phase (and hits max turns):

| Metric | Single-phase (today) | Two-phase |
|--------|---------------------|-----------|
| Planning turns | 0 | 10 (Sonnet or Haiku) |
| Implementation turns | 60 (hits max, incomplete) | 50 (focused on impl only) |
| DeerFlow cost | $0 (never delegated) | $0 (local vLLM) |
| Research done by Sonnet | Yes (wasted $) | No (delegated to free GPU) |
| Task completion | Incomplete (repeats) | Higher completion rate |
| Repeat runs | 5+ (observed) | Fewer — less work per run |

The 10 planning turns cost extra per run, but eliminate the repeated max-turns cycles that burn 300+ turns across 5 failed attempts on the same task.

## Observability

Phase metadata is included in `resultJson` so the dashboard can show:
- Whether a run used two-phase execution
- How many turns Phase 1 consumed
- Phase 1 summary (what was planned/delegated)
- Phase 2 summary (what was implemented)

The plan comment posted during Phase 1 is visible on the issue in the Paperclip UI.

## Rollout

1. Implement in the Paperclip fork (`tmartin2113/paperclip`)
2. Build and push new server image to GHCR
3. Enable on one Senior Engineer (Backend Engineer) as pilot
4. Monitor: does it delegate? does Phase 2 complete without hitting max turns?
5. If successful, enable on all Senior Engineers

## Risks

- **Phase 1 might not delegate**: The planning prompt is still an LLM prompt. But it's constrained to planning-only (no code editing tools), so the model has nothing to do except plan and delegate. Much harder to ignore than a suggestion buried in a long instruction file.
- **Plan quality varies**: Bad plans lead to bad Phase 2 execution. Mitigated by graceful degradation (Phase 2 gets full task context regardless).
- **Two invocations = ~2x startup overhead**: Each Claude Code process takes a few seconds to start. Adding 5-10 seconds for Phase 1 startup is negligible vs. the 12-26 minute runs observed.
- **Paperclip fork maintenance**: This adds ~150 lines to the adapter. The change is self-contained in `execute.ts` behind the `twoPhaseEnabled` flag.
