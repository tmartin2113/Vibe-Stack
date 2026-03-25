# Wake Self for Remaining Inbox — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a heartbeat run completes, agents self-wake if they have remaining actionable issues, eliminating the 60s polling delay between tasks.

**Architecture:** Extract a pure `shouldSelfWake()` function for testability. Add a ~20-line block in the run completion handler that queries for remaining issues and calls `enqueueWakeup`. Follows the existing CTO-wake pattern (async void IIFE with try/catch).

**Tech Stack:** TypeScript, Drizzle ORM, Vitest

**Spec:** `docs/superpowers/specs/2026-03-24-wake-self-remaining-inbox-design.md`

---

### Task 1: Add `shouldSelfWake` pure function with tests

**Files:**
- Modify: `/home/prime/paperclip/server/src/services/heartbeat.ts` (add exported function)
- Create: `/home/prime/paperclip/server/src/__tests__/heartbeat-self-wake.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `/home/prime/paperclip/server/src/__tests__/heartbeat-self-wake.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import { shouldSelfWake } from "../services/heartbeat.js";

describe("shouldSelfWake", () => {
  it("returns true when outcome is succeeded", () => {
    expect(shouldSelfWake("succeeded", undefined)).toBe(true);
  });

  it("returns true when outcome is failed with task-specific error", () => {
    expect(shouldSelfWake("failed", "some_task_error")).toBe(true);
  });

  it("returns true when outcome is failed with no errorCode", () => {
    expect(shouldSelfWake("failed", undefined)).toBe(true);
  });

  it("returns true when outcome is failed with null errorCode", () => {
    expect(shouldSelfWake("failed", null)).toBe(true);
  });

  it("returns false when outcome is failed with auth_failed", () => {
    expect(shouldSelfWake("failed", "auth_failed")).toBe(false);
  });

  it("returns false when outcome is failed with claude_auth_required", () => {
    expect(shouldSelfWake("failed", "claude_auth_required")).toBe(false);
  });

  it("returns false when outcome is failed with adapter_failed", () => {
    expect(shouldSelfWake("failed", "adapter_failed")).toBe(false);
  });

  it("returns false when outcome is failed with timeout", () => {
    expect(shouldSelfWake("failed", "timeout")).toBe(false);
  });

  it("returns false when outcome is timed_out", () => {
    expect(shouldSelfWake("timed_out", undefined)).toBe(false);
  });

  it("returns false when outcome is cancelled", () => {
    expect(shouldSelfWake("cancelled", undefined)).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/prime/paperclip/server && npx vitest run src/__tests__/heartbeat-self-wake.test.ts`
Expected: FAIL — `shouldSelfWake` is not exported from heartbeat.ts

- [ ] **Step 3: Implement `shouldSelfWake`**

Add to `/home/prime/paperclip/server/src/services/heartbeat.ts`, near the other exported utility functions (after `shouldResetTaskSessionForWake` or similar). Add it as an exported function:

```typescript
const SYSTEMIC_ERROR_CODES = new Set([
  "auth_failed",
  "claude_auth_required",
  "adapter_failed",
  "timeout",
]);

/**
 * Determines whether an agent should self-wake to process remaining inbox
 * items after a heartbeat run completes.
 *
 * Returns true for successful runs and task-specific failures.
 * Returns false for systemic failures (auth, adapter, timeout) and cancellations.
 */
export function shouldSelfWake(
  outcome: string,
  errorCode: string | null | undefined,
): boolean {
  if (outcome === "succeeded") return true;
  if (outcome === "failed" && !SYSTEMIC_ERROR_CODES.has(errorCode ?? "")) return true;
  return false;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/prime/paperclip/server && npx vitest run src/__tests__/heartbeat-self-wake.test.ts`
Expected: 10 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/prime/paperclip/server
git add src/services/heartbeat.ts src/__tests__/heartbeat-self-wake.test.ts
git commit -m "Add shouldSelfWake() pure function with tests

Determines whether an agent should self-wake after a run completes.
Returns true for success and task-specific failures, false for
systemic failures (auth, adapter, timeout) and cancellations.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: Add self-wake logic to run completion handler

**Files:**
- Modify: `/home/prime/paperclip/server/src/services/heartbeat.ts:1632-1671` (add block between `finalizeAgentStatus` and CTO-wake)

- [ ] **Step 1: Read the insertion point**

Read `/home/prime/paperclip/server/src/services/heartbeat.ts` lines 1630-1675 to confirm the exact insertion point. The new code goes after the auth-failure pause block (line 1645) and before the CTO-wake block (line 1647).

- [ ] **Step 2: Add the self-wake block**

Insert the following after line 1645 (end of the auth-failure pause block) and before line 1647 (CTO-wake comment):

```typescript
      // Self-wake if agent has remaining actionable inbox items.
      if (shouldSelfWake(outcome, adapterResult.errorCode)) {
        void (async () => {
          try {
            const remaining = await db
              .select({ id: issues.id })
              .from(issues)
              .where(
                and(
                  eq(issues.assigneeAgentId, agent.id),
                  inArray(issues.status, ["todo", "in_progress"]),
                ),
              )
              .limit(1);
            if (remaining.length > 0) {
              logger.info({ agentId: agent.id, runId: run.id }, "Enqueueing self-wake for remaining inbox items");
              await enqueueWakeup(agent.id, {
                source: "automation",
                triggerDetail: "system",
                reason: "inbox_remaining",
                payload: { completedRunId: run.id },
                requestedByActorType: "system",
                requestedByActorId: null,
                contextSnapshot: { source: "heartbeat.inbox_remaining" },
              });
            }
          } catch (err) {
            logger.warn({ err, agentId: agent.id, runId: run.id }, "failed to self-wake for remaining inbox");
          }
        })();
      }
```

- [ ] **Step 3: Verify no TypeScript errors**

Run: `cd /home/prime/paperclip/server && npx tsc --noEmit`
Expected: No errors. All imports (`issues`, `inArray`, `and`, `eq`) are already available in scope.

- [ ] **Step 4: Run the full test suite to verify no regressions**

Run: `cd /home/prime/paperclip/server && npx vitest run`
Expected: All existing tests pass, plus the 9 new tests from Task 1.

- [ ] **Step 5: Commit**

```bash
cd /home/prime/paperclip/server
git add src/services/heartbeat.ts
git commit -m "Add self-wake for remaining inbox after run completion

After a heartbeat run completes, agents check for remaining actionable
issues (todo/in_progress) and self-wake with reason 'inbox_remaining'.
Skips systemic failures (auth, adapter, timeout) to prevent retry storms.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```
