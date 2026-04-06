# Paperclip Run-Stats Tripometer — Design

**Date:** 2026-04-06
**Status:** Draft
**Repo:** `tmartin2113/paperclip` (changes ship via a new server image)

## Problem

The Paperclip dashboard's run-stats panel and the agent detail page's token/cost panel both display growing lifetime numbers with no way for the user to mark a checkpoint and watch progress *from that checkpoint forward*. During a live demo, watching the CTO agent execute a single task, there's no clean way to "zero out" the visible counters so the next few runs are isolated from the noise of historical activity.

The user wants a tripometer-style affordance: a soft reset that snapshots a baseline so the panels can display both **Lifetime** and **Since Reset** values side by side, without losing or modifying any underlying historical data. Resetting must be reversible, idempotent, and have zero side effects on billing, budgets, cost accounting, or anything else that reads the underlying lifetime counters.

## Goals

- Add a **Reset** affordance to the dashboard run-stats panel and the agent runtime token/cost panel.
- Each panel reset is **independent** (one click resets that panel only) and **soft** (lifetime data preserved).
- After a reset, the affected panel shows both **Lifetime** (always-on, primary) and **Since Reset** (small, muted, with a "since {timestamp}" subtitle).
- Before any reset has happened, only **Lifetime** is shown — the "Since Reset" line is hidden entirely.
- Reset is reversible: a "Show all-time" / "Clear reset" menu item nulls out the baseline and the "Since Reset" line goes away.
- All other consumers of the underlying counters (budget tracker, cost exports, finance events, etc.) continue to see the real lifetime values — they are **completely unaware** that a UI reset has happened.

## Non-Goals

- **Per-card resets** — explicitly rejected during brainstorming. One reset per panel.
- **Per-user reset views** — the reset state is global per company (dashboard) and global per agent (agent panel). Teammates see the same reset state.
- **Multiple trip counters (Trip A / Trip B)** — out of scope; one baseline per panel.
- **Historical comparison views** ("compare last week to this week") — out of scope.
- **Resetting cost_events / finance_events / heartbeat_runs rows** — never. These are never deleted.
- **Hard cap (14-day rolling window)** — explicitly removed. The current `runStats` query's `gte(startedAt, fourteenDaysAgo)` filter goes away in this change. Existing dashboards will start showing lifetime stats by default. This is intentional and consistent with the tripometer mental model: "you see everything until you reset, then you see everything since reset."

## Architecture Overview

Two independent baselines, two independent reset endpoints, two panels with the same dual-display UX:

### Dashboard panel (3 cards: Total Runs, Success Rate, Avg Duration)

- **Source data:** computed on-the-fly from `heartbeat_runs` rows (filterable by `started_at`).
- **Baseline storage:** new `companies.run_stats_reset_at TIMESTAMP NULL` column.
- **Behavior:** `dashboardService.runStats(companyId)` returns two stat blocks — `lifetime` (no time filter) and `sinceReset` (only present when `runStatsResetAt IS NOT NULL`, filtered to runs `started_at >= runStatsResetAt`).

### Agent runtime panel (4 cards: Input Tokens, Output Tokens, Cached Tokens, Total Cost)

- **Source data:** denormalized lifetime counters in `agent_runtime_state` (`totalInputTokens`, `totalOutputTokens`, `totalCachedInputTokens`, `totalCostCents`). These are incremented post-run, not derived from history rows, so there's nothing to filter.
- **Baseline storage:** four new baseline columns + one timestamp on `agent_runtime_state`.
- **Behavior:** when reset is triggered, the current `total_*` values are atomically copied into the matching `*Baseline` columns and `tokensResetAt` is set to `now()`. The frontend computes `sinceReset = total - baseline` for display. No `total_*` column is ever modified by a reset.

### Why two different mechanisms?

The UI is identical, but the backend has to handle them differently because the dashboard stats come from row-filterable history (`heartbeat_runs`) while the agent stats come from denormalized counters that have no history to filter. Both end up at the same place from the user's perspective: a dual-display panel with a soft, reversible baseline.

### Data flow (dashboard reset, end to end)

1. User clicks the kebab menu next to the run-stats panel header → selects "Reset counter".
2. Frontend `useMutation` calls `POST /companies/:companyId/dashboard/run-stats/reset`.
3. Server's `assertCompanyAccess` middleware authorizes the request.
4. `dashboardService.resetRunStats(companyId)` runs `UPDATE companies SET run_stats_reset_at = now() WHERE id = ?`.
5. Server returns `204 No Content`.
6. Frontend's `onSuccess` invalidates `queryKeys.dashboardRunStats(companyId)`.
7. React Query re-fetches `GET /companies/:companyId/dashboard/run-stats`.
8. Service returns the new response shape: `{ lifetime: {...}, sinceReset: {...}, resetAt: "..." }`.
9. Panel re-renders showing the Since-Reset row at zero with a fresh "since {time}" subtitle.

The agent reset flow is identical except:
- Endpoint is `POST /agents/:agentId/runtime-state/reset`
- Auth uses `assertAgentAccess` (or whatever the existing convention is for agent-scoped routes)
- The service method snapshots four `total_*` columns into the four `*Baseline` columns inside one transaction
- The query invalidated is the agent runtime state query

## Data Model

### `companies` table — new column

```typescript
// packages/db/src/schema/companies.ts
runStatsResetAt: timestamp("run_stats_reset_at", { withTimezone: true }),
```

- Nullable (no `.notNull()`). `NULL` means "never reset; show lifetime only".
- Default `NULL`. No backfill needed; existing companies start in the unreset state.
- Timezone-aware to match `created_at`, `started_at`, etc. on related tables.

### `agent_runtime_state` table — new columns

```typescript
// packages/db/src/schema/agent_runtime_state.ts
tokensResetAt: timestamp("tokens_reset_at", { withTimezone: true }),
totalInputTokensBaseline: bigint("total_input_tokens_baseline", { mode: "number" }).notNull().default(0),
totalOutputTokensBaseline: bigint("total_output_tokens_baseline", { mode: "number" }).notNull().default(0),
totalCachedInputTokensBaseline: bigint("total_cached_input_tokens_baseline", { mode: "number" }).notNull().default(0),
totalCostCentsBaseline: bigint("total_cost_cents_baseline", { mode: "number" }).notNull().default(0),
```

- `tokensResetAt` is nullable; `NULL` means "never reset".
- The four `*Baseline` columns are NOT NULL with default 0 — they only become meaningful when `tokensResetAt` is set, but having them default to 0 means the `total - baseline` math is always safe to compute.
- All four baseline columns and the timestamp must be set together inside a single transaction; partial updates are not valid states.

### Migration

One drizzle migration generated via the repo's standard `pnpm db:generate` flow, adding all five columns. No data backfill, no rewrites of existing rows.

## Backend Changes

### `server/src/services/dashboard.ts`

**Modify** `runStats(companyId)`:

- Look up the company's `runStatsResetAt` once.
- Drop the existing `gte(heartbeatRuns.startedAt, fourteenDaysAgo)` filter entirely.
- Compute the lifetime stats block (no time filter).
- If `runStatsResetAt` is set, run a second query with `gte(heartbeatRuns.startedAt, runStatsResetAt)` to compute the `sinceReset` block.
- Return shape:

```typescript
{
  lifetime: {
    totalRuns, succeededRuns, failedRuns, successRate,
    avgDurationMs, avgInputTokens, avgOutputTokens,
  },
  sinceReset: { /* same shape */ } | null,
  resetAt: string | null,
}
```

**Add** `resetRunStats(companyId, clear = false)`:

- `clear === false`: `UPDATE companies SET run_stats_reset_at = now() WHERE id = ?`
- `clear === true`: `UPDATE companies SET run_stats_reset_at = NULL WHERE id = ?`

### `server/src/routes/dashboard.ts`

**Add** `POST /companies/:companyId/dashboard/run-stats/reset`:

```typescript
router.post("/companies/:companyId/dashboard/run-stats/reset", async (req, res) => {
  const companyId = req.params.companyId as string;
  assertCompanyAccess(req, companyId);
  const clear = req.body?.clear === true;
  await svc.resetRunStats(companyId, clear);
  res.status(204).end();
});
```

Same `assertCompanyAccess` pattern as the existing GET routes. Any authenticated user with company access can reset.

### `server/src/services/agents.ts` (or wherever the runtime state is currently read)

**Modify** the existing query that returns runtime state for the agent detail page to also return the four baseline columns and `tokensResetAt`. Frontend computes `sinceReset = total - baseline` for each card.

**Add** `resetRuntimeState(agentId, clear = false)`:

- `clear === false`: inside a transaction, `SELECT total_input_tokens, total_output_tokens, total_cached_input_tokens, total_cost_cents FROM agent_runtime_state WHERE agent_id = ?` and then `UPDATE` setting all four `*_baseline` columns to those snapshot values and `tokens_reset_at = now()`.
- `clear === true`: `UPDATE agent_runtime_state SET tokens_reset_at = NULL, total_input_tokens_baseline = 0, ..., total_cost_cents_baseline = 0 WHERE agent_id = ?` (zero out the baselines too so they don't linger as stale state).

### `server/src/routes/agents.ts`

**Add** `POST /agents/:agentId/runtime-state/reset`:

```typescript
router.post("/agents/:agentId/runtime-state/reset", async (req, res) => {
  const agentId = req.params.agentId as string;
  const targetAgent = await svc.getAgent(agentId);  // 404 if missing
  assertCompanyAccess(req, targetAgent.companyId);  // existing pattern: agent → company → check
  const clear = req.body?.clear === true;
  await svc.resetRuntimeState(agentId, clear);
  res.status(204).end();
});
```

Note: there is no `assertAgentAccess` helper. The convention in `server/src/routes/agents.ts` (e.g. lines 117, 292) is to first look up the agent, then call `assertCompanyAccess(req, targetAgent.companyId)`. We follow that pattern.

## Frontend Changes

### `ui/src/api/dashboard.ts`

```typescript
export interface DashboardRunStatsBlock {
  totalRuns: number;
  succeededRuns: number;
  failedRuns: number;
  successRate: number;
  avgDurationMs: number | null;
  avgInputTokens: number | null;
  avgOutputTokens: number | null;
}

export interface DashboardRunStats {
  lifetime: DashboardRunStatsBlock;
  sinceReset: DashboardRunStatsBlock | null;
  resetAt: string | null;
}

export const dashboardApi = {
  // ...existing
  resetRunStats: (companyId: string, clear = false) =>
    api.post(`/companies/${companyId}/dashboard/run-stats/reset`, { clear }),
};
```

### `ui/src/api/agents.ts` (or wherever agent runtime state is fetched)

Add the four baseline fields and `tokensResetAt` to the existing runtime state interface. Add `resetRuntimeState(agentId, clear?)` method.

### `ui/src/pages/Dashboard.tsx`

The runStats panel currently lives around line 276 with three tile cards. Wrap it:

1. Add a header row with the panel title on the left and a `DropdownMenu` (shadcn/ui, already in the project's `components.json`) on the right, triggered by a `MoreVertical` icon from `lucide-react`.
2. Menu items:
   - **"Reset counter"** — always visible. Disabled when a mutation is in flight.
   - **"Show all-time"** — only visible when `runStats?.resetAt !== null`.
3. Each card now renders two values stacked:
   - The big primary number is `runStats.lifetime.<field>`.
   - When `runStats.sinceReset !== null`, render a second muted line below with `runStats.sinceReset.<field>` and a tiny subtitle `since {formatRelative(runStats.resetAt)}`.
   - When `runStats.sinceReset === null`, the second line is not rendered at all.
4. Wire the menu items to `useMutation` calls. `onSuccess` invalidates `queryKeys.dashboardRunStats(companyId)` so the panel re-fetches immediately.

### `ui/src/pages/AgentDetail.tsx`

Locate the existing token/cost panel (the one near `runtimeState.totalCostCents` around line 991). Apply the same treatment:

1. Header row with panel title + kebab menu.
2. Menu items: "Reset counter" / "Show all-time" (when `tokensResetAt !== null`).
3. Each of the four cards (Input Tokens, Output Tokens, Cached Tokens, Total Cost) shows the lifetime number primary and, when reset is set, the `total - baseline` value as a muted secondary line with the "since {time}" subtitle.
4. Wire to `useMutation` for `agentApi.resetRuntimeState(agentId, clear?)`. Invalidate the agent runtime state query on success.

### Display semantics summary

| State | Lifetime line | Since Reset line | Subtitle |
|---|---|---|---|
| Never reset (`resetAt === null`) | Visible (primary) | **Hidden** | None |
| Just reset (`resetAt === now`) | Visible (unchanged) | Visible at 0 | "since just now" |
| Reset earlier today | Visible (growing) | Visible (growing from 0) | "since 2:30 PM" |
| Reset cleared via "Show all-time" | Visible (primary) | **Hidden** | None |

## Testing

### Server-side (`server/src/services/*.test.ts`)

- `dashboardService.runStats` — three cases:
  1. `runStatsResetAt === null` → response has `lifetime` populated, `sinceReset === null`, `resetAt === null`. Lifetime block has no time filter applied (verify with rows older than 14 days that *did not* show up under the old query but *do* show up now).
  2. `runStatsResetAt` set in the past → both `lifetime` and `sinceReset` are populated. The `sinceReset` totals are strictly ≤ the `lifetime` totals. Only runs after `resetAt` count toward `sinceReset`.
  3. `runStatsResetAt` set in the future (defensive — clock skew, manual SQL meddling) → `sinceReset` returns zeros across the board, no errors.

- `dashboardService.resetRunStats` — two cases:
  1. `clear === false` → `runStatsResetAt` is set to a value within the last second.
  2. `clear === true` → `runStatsResetAt` is set back to NULL even when it was previously set.

- `agentService.resetRuntimeState` — two cases:
  1. `clear === false` → all four `*_baseline` columns equal the corresponding `total_*` columns at the moment of reset, and `tokens_reset_at` is within the last second. Verify atomicity by mocking a mid-transaction failure and asserting nothing changed.
  2. `clear === true` → all four `*_baseline` columns are 0 and `tokens_reset_at` is NULL.

- Agent runtime state read query — verify it returns the new baseline fields and `tokensResetAt` in the response.

### Route auth (`server/src/routes/*.test.ts`)

- `POST /companies/:companyId/dashboard/run-stats/reset`:
  - 204 when caller is in the company.
  - 403 when caller is not in the company.
  - 401 when unauthenticated.
- `POST /agents/:agentId/runtime-state/reset`:
  - 204 when caller belongs to the same company as the agent.
  - 404 when the agent does not exist.
  - 403 when caller is not in the agent's company.
  - 401 when unauthenticated.

### Frontend (`ui/src/pages/*.test.tsx`)

- Dashboard runStats panel:
  - Renders only the lifetime row when `resetAt === null` (assert the "since reset" subtitle does not appear in the DOM).
  - After reset, renders both rows with the muted "since {time}" subtitle.
  - Clicking the kebab → "Reset counter" item triggers the mutation, optimistically disables the menu item, and re-fetches on success.
  - When `resetAt !== null`, the "Show all-time" menu item is present; clicking it issues `clear: true` and the panel reverts to lifetime-only.
- Agent token/cost panel mirrors the dashboard tests with its own four cards and endpoint.

### Integration smoke

A single end-to-end test that exercises the dashboard reset path: POST reset → GET stats → assert response shape includes both blocks → POST reset with `clear: true` → GET stats → assert `sinceReset === null`. No Playwright / browser-driving e2e changes; component tests cover the UI interactions.

## Migration / Rollout

- One drizzle migration adds the five new columns.
- Migration is forward-only and additive; no data is rewritten.
- The behavior change for existing dashboards (loss of the 14-day rolling window) takes effect on first deploy. Worth a one-line release note.
- The new server image needs to be built and pushed to GHCR (`ghcr.io/tmartin2113/paperclip-server:latest`) the same way any other Paperclip change is shipped. Vibe Stack picks it up via `docker compose pull && docker compose up -d server`.

## Open Questions

None — all design decisions resolved during brainstorming.
