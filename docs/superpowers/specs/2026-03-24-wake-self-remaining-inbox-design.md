# Wake Self for Remaining Inbox — Design Spec

## Problem

When an agent completes a task and has additional `todo` issues assigned to it, nobody wakes it. The agent exits and the tasks sit idle until either:
- The 60-second polling timer fires (workaround we set via `heartbeat.intervalSec`)
- A human manually triggers the heartbeat

This adds unnecessary latency (up to 60s) and wastes polling cycles when the agent has no inbox items.

## Design

After a heartbeat run completes, check if the agent has remaining actionable issues (`todo` or `in_progress`). If so, enqueue a self-wake with reason `inbox_remaining`.

### Location

`/home/prime/paperclip/server/src/services/heartbeat.ts`, after `finalizeAgentStatus(agent.id, outcome)` at approximately line 1632, inside the run completion handler.

### Logic

```
Run completes → finalizeAgentStatus()
  → Is outcome eligible for re-wake?
    → YES: Query issues table for remaining todo items
      → Row exists: enqueueWakeup(agent.id, { reason: "inbox_remaining", ... })
      → No rows: do nothing
    → NO: do nothing
```

### Eligible Outcomes

| Outcome | Re-wake? | Reason |
|---------|----------|--------|
| `succeeded` | Yes | Agent completed work, may have more |
| `failed` with task-specific error | Yes | Failure was task-specific, other tasks may succeed |
| `failed` with `auth_failed` | No | Systemic — retrying is pointless until human re-authenticates |
| `failed` with `claude_auth_required` | No | Systemic — same as above |
| `failed` with `adapter_failed` | No | Systemic — adapter itself is broken |
| `failed` with `timeout` errorCode | No | Systemic — likely resource issue |
| `timed_out` outcome | No | Systemic — agent exceeded time limit |
| `cancelled` | No | Intentional stop — don't override |

**Implementation:** Check `outcome === "succeeded"` OR (`outcome === "failed"` AND `adapterResult.errorCode` is NOT in `["auth_failed", "claude_auth_required", "adapter_failed", "timeout"]`). Note: the timeout outcome is `"timed_out"` (not `"timeout"`) — it falls through naturally since it's neither `"succeeded"` nor `"failed"`.

### Database Query

Direct query against the `issues` table (follows the actionable-issues check pattern inside `enqueueWakeup` at lines 2034-2043):

```sql
SELECT id FROM issues
WHERE assignee_agent_id = :agentId
  AND status IN ('todo', 'in_progress')
LIMIT 1
```

This is an existence check only — we don't need the full issue, just whether one exists. Includes `in_progress` to match the existing actionable-issues pattern (an `in_progress` issue whose run failed is still actionable).

### Wake Parameters

```typescript
await enqueueWakeup(agent.id, {
  source: "automation",
  triggerDetail: "system",
  reason: "inbox_remaining",
  payload: { completedRunId: run.id },
  requestedByActorType: "system",
  requestedByActorId: null,
  contextSnapshot: { source: "heartbeat.inbox_remaining" },
});
```

### Safety Guards

1. **`maxConcurrentRuns` policy** (default 1) — prevents overlapping runs. The self-wake is enqueued, not executed immediately.
2. **`wakeOnDemand` policy** — `enqueueWakeup` already checks agent status (paused, terminated agents are skipped).
3. **Auth failure pause** — the existing auth-failure handler (lines 1634-1645) pauses ALL agents before the self-wake code runs, so the wake will be rejected.
4. **Retry loop risk on persistent task failure** — if a specific task keeps failing with a non-systemic error, the agent will re-wake and pick the SAME task again. There is no per-issue retry counter at the heartbeat level. Mitigation: the non-systemic error filter blocks the most common loop causes (auth, adapter, timeout). For edge cases where a task-specific error loops, manual intervention (marking the issue `blocked`) is the current escape hatch. **Follow-up:** consider adding a `maxConsecutiveFailures` per-issue guard in a future iteration.
5. **Timer polling unchanged** — the 60s `intervalSec` can be kept as a safety net or removed. The self-wake handles the primary case; the timer handles edge cases (e.g., tasks assigned while agent was mid-run and wake was debounced).
6. **Deferred wakeup promotion race** — `releaseIssueExecutionAndPromote` (line 1605) runs before this code and may promote a deferred wakeup for the same agent. If both fire, `maxConcurrentRuns` prevents double-runs and `enqueueWakeup`'s no-actionable-issues guard deduplicates. The self-wake is harmlessly redundant in this case.

### Wrapped in async void

Follow the existing pattern at line 1649-1671 (CTO wake after engineer completion): wrap in `void (async () => { try { ... } catch { logger.warn(...) } })()` to avoid blocking the completion flow.

## Changes Required

### File: `/home/prime/paperclip/server/src/services/heartbeat.ts`

Add ~20 lines after `finalizeAgentStatus` (line ~1632), before the existing CTO-wake block (line 1647). The new block:

1. Checks outcome eligibility
2. Queries `issues` table for remaining actionable items (`todo` or `in_progress`) assigned to this agent
3. If found, calls `enqueueWakeup` with `reason: "inbox_remaining"`

No other files need changes.

## Success Criteria

1. Agent completes a task successfully → immediately picks up the next `todo` task (no 60s delay)
2. Agent fails with auth error → does NOT re-wake (avoids retry storm)
3. Agent fails with task-specific error → re-wakes and picks up a different task
4. Agent completes last task in inbox → does NOT wake (no wasted heartbeat)
5. Logs show `inbox_remaining` reason when self-wake fires
