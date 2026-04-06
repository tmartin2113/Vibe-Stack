# Paperclip Run-Stats Tripometer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tripometer-style soft reset to the Paperclip dashboard run-stats panel and the agent detail token/cost panel — each panel keeps showing the lifetime number primary, with an optional "since reset" line that resets to zero on demand. Lifetime data is never modified.

**Architecture:** Two new endpoints write a baseline (a timestamp on `companies` for the dashboard side, snapshotted token/cost values + a timestamp on `agent_runtime_state` for the agent side). Existing read paths gain `lifetime` + `sinceReset` blocks; the frontend renders both stacked when a baseline is set, lifetime-only otherwise. Two React panels grow a shadcn `DropdownMenu` kebab in the panel header. The current 14-day rolling-window filter on dashboard runStats is removed as part of this change.

**Tech Stack:** TypeScript, drizzle-orm (Postgres), Express, React + React Query, shadcn/ui (DropdownMenu)

**Spec:** `docs/superpowers/specs/2026-04-06-paperclip-runstats-tripometer-design.md`

**Repo:** `~/Repos/paperclip-tripometer` (commits go to the user's `tmartin2113/paperclip` fork; never push to upstream `paperclipai/paperclip`). The Vibe Stack uses the resulting GHCR image — see Task 16.

---

## Prerequisites

The work happens in an isolated git worktree at `~/Repos/paperclip-tripometer`, on branch `feature/runstats-tripometer` (created off `master`). The Vibe Stack `~/Repos/Vibe-Stack` should also be available (only used for the spec/plan reference and the final image deploy).

Verify before starting:

```bash
cd ~/Repos/paperclip-tripometer && git status
cd ~/Repos/paperclip-tripometer && git remote -v
```

Expected: clean tree on `feature/runstats-tripometer`. The only remote is `origin`, which points at `tmartin2113/paperclip` (the user's fork — there is no upstream `paperclipai` remote configured here, so pushing to `origin` is safe).

---

## File Map

**Created:**
- `packages/db/src/migrations/0029_runstats_tripometer.sql` (or whatever number `pnpm db:generate` assigns)
- `server/src/__tests__/dashboard-runstats-tripometer.test.ts`
- `server/src/__tests__/agent-runtime-reset-tokens.test.ts`

**Modified:**
- `packages/db/src/schema/companies.ts` — add `runStatsResetAt` column
- `packages/db/src/schema/agent_runtime_state.ts` — add 4 baseline columns + reset timestamp
- `server/src/services/dashboard.ts` — modify `runStats`, add `resetRunStats`
- `server/src/routes/dashboard.ts` — add reset route
- `server/src/services/agents.ts` — modify the runtime-state read query, add `resetRuntimeStateTokens`
- `server/src/routes/agents.ts` — add reset-tokens route
- `packages/shared/src/...` — update `AgentRuntimeState` and `DashboardRunStats` shared types (find via grep before editing)
- `ui/src/api/dashboard.ts` — update interface, add reset method
- `ui/src/api/agents.ts` — add reset-tokens method, update interface
- `ui/src/pages/Dashboard.tsx` — kebab menu + dual-display rendering
- `ui/src/pages/AgentDetail.tsx` — kebab menu + dual-display rendering

**UI tests deferred.** The `ui/` package has `vitest` configured but no `@testing-library/react` and no existing test files. Setting up frontend test infra is out of scope for this feature; we cover the backend with tests and the frontend with a final manual smoke test on the running Vibe Stack (Task 16).

---

## Task 1: Add `runStatsResetAt` column to `companies`

**Files:**
- Modify: `packages/db/src/schema/companies.ts`
- Generate: `packages/db/src/migrations/0029_*.sql` (drizzle picks the name)

- [ ] **Step 1: Read the current companies schema**

```bash
cd ~/Repos/paperclip-tripometer && cat packages/db/src/schema/companies.ts
```

- [ ] **Step 2: Add the column to the schema**

In `packages/db/src/schema/companies.ts`, find the column block (the object passed as the second argument to `pgTable`) and add this column right after the existing `spentMonthlyCents` line (or anywhere alongside the other metric-related columns):

```typescript
  runStatsResetAt: timestamp("run_stats_reset_at", { withTimezone: true }),
```

Make sure `timestamp` is already imported from `drizzle-orm/pg-core` at the top of the file. It should be — verify with `grep timestamp packages/db/src/schema/companies.ts`. If not, add it to the import list.

- [ ] **Step 3: Generate the migration**

```bash
cd ~/Repos/paperclip-tripometer && pnpm db:generate
```

Expected: a new migration file appears under `packages/db/src/migrations/` numbered `0029_*.sql` containing exactly:

```sql
ALTER TABLE "companies" ADD COLUMN "run_stats_reset_at" timestamp with time zone;
```

If `pnpm db:generate` produces other unrelated changes, stop and investigate — there may be drift in the schema directory. Do not proceed until the generated migration only contains the `companies` ALTER.

- [ ] **Step 4: Inspect the generated migration**

```bash
cd ~/Repos/paperclip-tripometer && cat packages/db/src/migrations/0029_*.sql
```

Confirm it matches the expected ALTER above and contains nothing else.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/paperclip-tripometer && git add packages/db/src/schema/companies.ts packages/db/src/migrations/0029_*.sql packages/db/src/migrations/meta/
git commit -m "feat(db): add run_stats_reset_at to companies for dashboard tripometer"
```

---

## Task 2: Add baseline columns to `agent_runtime_state`

**Files:**
- Modify: `packages/db/src/schema/agent_runtime_state.ts`
- Generate: next `packages/db/src/migrations/0030_*.sql`

- [ ] **Step 1: Read the current agent_runtime_state schema**

```bash
cd ~/Repos/paperclip-tripometer && cat packages/db/src/schema/agent_runtime_state.ts
```

You should see the existing `totalInputTokens`, `totalOutputTokens`, `totalCachedInputTokens`, `totalCostCents` columns.

- [ ] **Step 2: Add the five new columns**

In `packages/db/src/schema/agent_runtime_state.ts`, add these columns immediately after the existing `totalCostCents` line (so the baseline columns sit right next to the values they shadow):

```typescript
    tokensResetAt: timestamp("tokens_reset_at", { withTimezone: true }),
    totalInputTokensBaseline: bigint("total_input_tokens_baseline", { mode: "number" }).notNull().default(0),
    totalOutputTokensBaseline: bigint("total_output_tokens_baseline", { mode: "number" }).notNull().default(0),
    totalCachedInputTokensBaseline: bigint("total_cached_input_tokens_baseline", { mode: "number" }).notNull().default(0),
    totalCostCentsBaseline: bigint("total_cost_cents_baseline", { mode: "number" }).notNull().default(0),
```

`timestamp` and `bigint` are both already imported from `drizzle-orm/pg-core` in this file (verify with `head -3 packages/db/src/schema/agent_runtime_state.ts`).

- [ ] **Step 3: Generate the migration**

```bash
cd ~/Repos/paperclip-tripometer && pnpm db:generate
```

Expected output: new migration file `packages/db/src/migrations/0030_*.sql` containing exactly:

```sql
ALTER TABLE "agent_runtime_state" ADD COLUMN "tokens_reset_at" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "agent_runtime_state" ADD COLUMN "total_input_tokens_baseline" bigint DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "agent_runtime_state" ADD COLUMN "total_output_tokens_baseline" bigint DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "agent_runtime_state" ADD COLUMN "total_cached_input_tokens_baseline" bigint DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "agent_runtime_state" ADD COLUMN "total_cost_cents_baseline" bigint DEFAULT 0 NOT NULL;
```

- [ ] **Step 4: Inspect the generated migration**

```bash
cd ~/Repos/paperclip-tripometer && cat packages/db/src/migrations/0030_*.sql
```

Confirm it matches above and contains no other unrelated ALTERs.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/paperclip-tripometer && git add packages/db/src/schema/agent_runtime_state.ts packages/db/src/migrations/0030_*.sql packages/db/src/migrations/meta/
git commit -m "feat(db): add token/cost baseline columns to agent_runtime_state"
```

---

## Task 3: Failing test for `dashboardService.runStats` lifetime + sinceReset shape

**Files:**
- Create: `server/src/__tests__/dashboard-runstats-tripometer.test.ts`
- Will reference (no edits yet): `server/src/services/dashboard.ts`

- [ ] **Step 1: Look at how an existing service test sets up its DB**

```bash
cd ~/Repos/paperclip-tripometer && cat server/src/__tests__/companies-route-path-guard.test.ts | head -60
```

Note the imports, the test runner pattern (vitest), and how they get a Db instance. Copy that setup style for the new test file.

- [ ] **Step 2: Create the test file**

`server/src/__tests__/dashboard-runstats-tripometer.test.ts`:

```typescript
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { makeTestDb, type TestDb } from "./helpers/test-db.js"; // adapt to whatever helper the file in Step 1 uses
import { companies, agents, heartbeatRuns } from "@paperclipai/db";
import { dashboardService } from "../services/dashboard.js";
import { eq } from "drizzle-orm";

describe("dashboardService.runStats — tripometer behavior", () => {
  let db: TestDb;
  let companyId: string;
  let agentId: string;

  beforeAll(async () => {
    db = await makeTestDb();
  });
  afterAll(async () => {
    await db.close();
  });

  beforeEach(async () => {
    // Clean in dependency order: children before parents
    await db.delete(heartbeatRuns);
    await db.delete(agents);
    await db.delete(companies);

    [{ id: companyId }] = await db.insert(companies).values({ name: "Test Co" }).returning({ id: companies.id });
    [{ id: agentId }] = await db
      .insert(agents)
      .values({ companyId, name: "Test Agent", role: "engineer" })
      .returning({ id: agents.id });
  });

  async function insertRun(opts: { startedAt: Date; status: "succeeded" | "failed" }) {
    await db.insert(heartbeatRuns).values({
      companyId,
      agentId,
      invocationSource: "scheduled",
      status: opts.status,
      startedAt: opts.startedAt,
      finishedAt: new Date(opts.startedAt.getTime() + 1000),
    });
  }

  it("returns lifetime stats and sinceReset === null when runStatsResetAt is null", async () => {
    const now = Date.now();
    await insertRun({ startedAt: new Date(now - 30 * 86400_000), status: "succeeded" }); // 30 days ago
    await insertRun({ startedAt: new Date(now - 1 * 86400_000), status: "failed" }); // 1 day ago

    const svc = dashboardService(db);
    const stats = await svc.runStats(companyId);

    expect(stats.lifetime.totalRuns).toBe(2);
    expect(stats.lifetime.succeededRuns).toBe(1);
    expect(stats.lifetime.failedRuns).toBe(1);
    expect(stats.sinceReset).toBeNull();
    expect(stats.resetAt).toBeNull();
  });

  it("returns both blocks when runStatsResetAt is set, with sinceReset filtered to runs after reset", async () => {
    const now = Date.now();
    const resetAt = new Date(now - 12 * 60 * 60 * 1000); // reset 12 hours ago
    await insertRun({ startedAt: new Date(now - 24 * 60 * 60 * 1000), status: "succeeded" }); // before reset
    await insertRun({ startedAt: new Date(now - 6 * 60 * 60 * 1000), status: "succeeded" }); // after reset
    await insertRun({ startedAt: new Date(now - 1 * 60 * 60 * 1000), status: "failed" }); // after reset

    await db.update(companies).set({ runStatsResetAt: resetAt }).where(eq(companies.id, companyId));

    const svc = dashboardService(db);
    const stats = await svc.runStats(companyId);

    expect(stats.lifetime.totalRuns).toBe(3);
    expect(stats.sinceReset).not.toBeNull();
    expect(stats.sinceReset!.totalRuns).toBe(2);
    expect(stats.sinceReset!.succeededRuns).toBe(1);
    expect(stats.sinceReset!.failedRuns).toBe(1);
    expect(stats.resetAt).toEqual(resetAt.toISOString());
  });

  it("returns sinceReset zeros when runStatsResetAt is in the future (defensive)", async () => {
    const now = Date.now();
    await insertRun({ startedAt: new Date(now - 1 * 60 * 60 * 1000), status: "succeeded" });

    await db
      .update(companies)
      .set({ runStatsResetAt: new Date(now + 60 * 60 * 1000) })
      .where(eq(companies.id, companyId));

    const svc = dashboardService(db);
    const stats = await svc.runStats(companyId);

    expect(stats.lifetime.totalRuns).toBe(1);
    expect(stats.sinceReset).not.toBeNull();
    expect(stats.sinceReset!.totalRuns).toBe(0);
  });
});
```

**Important:** The exact import path for the test DB helper (`./helpers/test-db.js` above) is a placeholder until you confirm what helper exists in `server/src/__tests__/`. In Step 1 you read an existing test file — match its DB-setup pattern. If there's no `makeTestDb` helper and tests bring their own setup, replicate that. The dependency-order delete pattern works regardless of helper.

- [ ] **Step 3: Run the test to confirm it fails**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test dashboard-runstats-tripometer
```

Expected: failures along the lines of `stats.lifetime is undefined` or `stats.sinceReset is undefined` — because `runStats` currently returns the flat shape with no `lifetime`/`sinceReset` keys. If the test fails because of import errors first, fix those before continuing (the helper path placeholder, missing imports, etc.).

- [ ] **Step 4: Commit the failing test**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/__tests__/dashboard-runstats-tripometer.test.ts
git commit -m "test(server): add failing tests for dashboard runStats tripometer shape"
```

---

## Task 4: Make `dashboardService.runStats` return the new shape

**Files:**
- Modify: `server/src/services/dashboard.ts:181-212`

- [ ] **Step 1: Read the current implementation**

```bash
cd ~/Repos/paperclip-tripometer && sed -n '180,215p' server/src/services/dashboard.ts
```

Confirm it matches the snippet in the spec (single query, 14-day filter, flat response shape).

- [ ] **Step 2: Replace the runStats implementation**

In `server/src/services/dashboard.ts`, replace the entire `runStats` method (the function value, lines roughly 181-212) with this:

```typescript
    runStats: async (companyId: string) => {
      const [company] = await db
        .select({ resetAt: companies.runStatsResetAt })
        .from(companies)
        .where(eq(companies.id, companyId));

      const baseConditions = [
        eq(heartbeatRuns.companyId, companyId),
        ne(heartbeatRuns.invocationSource, "checkout_upsert"),
      ];

      const computeBlock = async (extraConditions: typeof baseConditions) => {
        const [stats] = await db
          .select({
            totalRuns: sql<number>`count(*)::int`,
            succeededRuns: sql<number>`count(*) filter (where ${heartbeatRuns.status} = 'succeeded')::int`,
            failedRuns: sql<number>`count(*) filter (where ${heartbeatRuns.status} = 'failed')::int`,
            avgDurationMs: sql<number | null>`avg(extract(epoch from (${heartbeatRuns.finishedAt} - ${heartbeatRuns.startedAt})) * 1000)::int`,
            avgInputTokens: sql<number | null>`avg(coalesce((${heartbeatRuns.usageJson} ->> 'inputTokens')::int, (${heartbeatRuns.usageJson} ->> 'input_tokens')::int))::int`,
            avgOutputTokens: sql<number | null>`avg(coalesce((${heartbeatRuns.usageJson} ->> 'outputTokens')::int, (${heartbeatRuns.usageJson} ->> 'output_tokens')::int))::int`,
          })
          .from(heartbeatRuns)
          .where(and(...extraConditions));

        const totalRuns = Number(stats.totalRuns);
        return {
          totalRuns,
          succeededRuns: Number(stats.succeededRuns),
          failedRuns: Number(stats.failedRuns),
          successRate: totalRuns > 0 ? Number(((Number(stats.succeededRuns) / totalRuns) * 100).toFixed(1)) : 0,
          avgDurationMs: stats.avgDurationMs ? Number(stats.avgDurationMs) : null,
          avgInputTokens: stats.avgInputTokens ? Number(stats.avgInputTokens) : null,
          avgOutputTokens: stats.avgOutputTokens ? Number(stats.avgOutputTokens) : null,
        };
      };

      const lifetime = await computeBlock(baseConditions);
      const sinceReset = company?.resetAt
        ? await computeBlock([...baseConditions, gte(heartbeatRuns.startedAt, company.resetAt)])
        : null;

      return {
        lifetime,
        sinceReset,
        resetAt: company?.resetAt ? company.resetAt.toISOString() : null,
      };
    },
```

The 14-day filter (`gte(heartbeatRuns.startedAt, fourteenDaysAgo)`) and the `fourteenDaysAgo` constant declaration are gone. `gte` is still imported (used inside the conditional). `sql`, `and`, `eq`, `ne` are all already imported. Verify with `head -5 server/src/services/dashboard.ts`.

- [ ] **Step 3: Run the test**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test dashboard-runstats-tripometer
```

Expected: all three test cases pass.

- [ ] **Step 4: Run the full server test suite**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test
```

Expected: full pass. If any other test that consumed the old `{ totalRuns, ... }` flat shape now fails, fix those callers — they need to read `stats.lifetime.totalRuns` instead. Investigate and fix; do not proceed with broken existing tests.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/services/dashboard.ts
git commit -m "feat(server): runStats returns lifetime + sinceReset blocks, drops 14-day filter"
```

---

## Task 5: Failing test for `dashboardService.resetRunStats`

**Files:**
- Modify: `server/src/__tests__/dashboard-runstats-tripometer.test.ts`

- [ ] **Step 1: Add a new describe block to the existing test file**

Append this to `server/src/__tests__/dashboard-runstats-tripometer.test.ts` (after the existing `describe` block):

```typescript
describe("dashboardService.resetRunStats", () => {
  let db: TestDb;
  let companyId: string;

  beforeAll(async () => {
    db = await makeTestDb();
  });
  afterAll(async () => {
    await db.close();
  });

  beforeEach(async () => {
    await db.delete(companies);
    [{ id: companyId }] = await db.insert(companies).values({ name: "Test Co" }).returning({ id: companies.id });
  });

  it("sets runStatsResetAt to now when called without clear", async () => {
    const svc = dashboardService(db);
    const before = Date.now();
    await svc.resetRunStats(companyId);
    const after = Date.now();

    const [row] = await db.select({ resetAt: companies.runStatsResetAt }).from(companies).where(eq(companies.id, companyId));
    expect(row.resetAt).not.toBeNull();
    const ts = row.resetAt!.getTime();
    expect(ts).toBeGreaterThanOrEqual(before);
    expect(ts).toBeLessThanOrEqual(after + 100); // 100ms slack
  });

  it("sets runStatsResetAt back to null when called with clear: true", async () => {
    const svc = dashboardService(db);
    await svc.resetRunStats(companyId);
    await svc.resetRunStats(companyId, true);

    const [row] = await db.select({ resetAt: companies.runStatsResetAt }).from(companies).where(eq(companies.id, companyId));
    expect(row.resetAt).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test dashboard-runstats-tripometer
```

Expected: failure on `svc.resetRunStats is not a function`.

- [ ] **Step 3: Commit the failing test**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/__tests__/dashboard-runstats-tripometer.test.ts
git commit -m "test(server): add failing tests for dashboardService.resetRunStats"
```

---

## Task 6: Implement `dashboardService.resetRunStats`

**Files:**
- Modify: `server/src/services/dashboard.ts`

- [ ] **Step 1: Add the new method to the service object**

In `server/src/services/dashboard.ts`, inside the object returned by `dashboardService(db)`, add this method right after the existing `runStats`:

```typescript
    resetRunStats: async (companyId: string, clear = false) => {
      await db
        .update(companies)
        .set({ runStatsResetAt: clear ? null : new Date() })
        .where(eq(companies.id, companyId));
    },
```

- [ ] **Step 2: Run the test**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test dashboard-runstats-tripometer
```

Expected: both new test cases pass; previous tests still pass.

- [ ] **Step 3: Commit**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/services/dashboard.ts
git commit -m "feat(server): dashboardService.resetRunStats writes/clears the baseline"
```

---

## Task 7: Add `POST /companies/:companyId/dashboard/run-stats/reset` route

**Files:**
- Modify: `server/src/routes/dashboard.ts`

- [ ] **Step 1: Read the current routes file**

```bash
cd ~/Repos/paperclip-tripometer && cat server/src/routes/dashboard.ts
```

Confirm the existing pattern (`assertCompanyAccess`, `svc.runStats(...)`, JSON response).

- [ ] **Step 2: Add the POST route**

In `server/src/routes/dashboard.ts`, add this route immediately after the existing `GET /run-stats` route, inside the same `dashboardRoutes` function:

```typescript
  router.post("/companies/:companyId/dashboard/run-stats/reset", async (req, res) => {
    const companyId = req.params.companyId as string;
    assertCompanyAccess(req, companyId);
    const clear = (req.body as { clear?: unknown } | undefined)?.clear === true;
    await svc.resetRunStats(companyId, clear);
    res.status(204).end();
  });
```

- [ ] **Step 3: Verify the route by running the full server test suite**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test
```

Expected: all green. The new route doesn't have a dedicated test file (auth is just `assertCompanyAccess`, and the underlying service method is already covered); the existing route auth tests cover the helper itself.

- [ ] **Step 4: Commit**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/routes/dashboard.ts
git commit -m "feat(server): add POST /dashboard/run-stats/reset route"
```

---

## Task 8: Failing test for agent runtime state read returning baselines

**Files:**
- Create: `server/src/__tests__/agent-runtime-reset-tokens.test.ts`

- [ ] **Step 1: Create the test file**

`server/src/__tests__/agent-runtime-reset-tokens.test.ts`:

```typescript
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { makeTestDb, type TestDb } from "./helpers/test-db.js"; // adapt to actual helper
import { companies, agents, agentRuntimeState } from "@paperclipai/db";
import { agentService } from "../services/agents.js";
import { eq } from "drizzle-orm";

describe("agentService runtime-state with token baselines", () => {
  let db: TestDb;
  let companyId: string;
  let agentId: string;

  beforeAll(async () => {
    db = await makeTestDb();
  });
  afterAll(async () => {
    await db.close();
  });

  beforeEach(async () => {
    // Clean in dependency order: children before parents
    await db.delete(agentRuntimeState);
    await db.delete(agents);
    await db.delete(companies);

    [{ id: companyId }] = await db.insert(companies).values({ name: "Test Co" }).returning({ id: companies.id });
    [{ id: agentId }] = await db
      .insert(agents)
      .values({ companyId, name: "Test Agent", role: "engineer" })
      .returning({ id: agents.id });
    await db.insert(agentRuntimeState).values({
      agentId,
      companyId,
      adapterType: "claude_local",
      totalInputTokens: 1000,
      totalOutputTokens: 500,
      totalCachedInputTokens: 100,
      totalCostCents: 250,
    });
  });

  it("read returns baseline columns and tokensResetAt as null when never reset", async () => {
    const svc = agentService(db);
    const state = await svc.getRuntimeState(agentId);
    // method name might differ — adapt to whatever the existing read method is called
    expect(state.tokensResetAt).toBeNull();
    expect(state.totalInputTokensBaseline).toBe(0);
    expect(state.totalOutputTokensBaseline).toBe(0);
    expect(state.totalCachedInputTokensBaseline).toBe(0);
    expect(state.totalCostCentsBaseline).toBe(0);
  });

  it("resetRuntimeStateTokens snapshots all four totals into baselines and sets tokensResetAt", async () => {
    const svc = agentService(db);
    const before = Date.now();
    await svc.resetRuntimeStateTokens(agentId);
    const after = Date.now();

    const [row] = await db.select().from(agentRuntimeState).where(eq(agentRuntimeState.agentId, agentId));
    expect(row.totalInputTokensBaseline).toBe(1000);
    expect(row.totalOutputTokensBaseline).toBe(500);
    expect(row.totalCachedInputTokensBaseline).toBe(100);
    expect(row.totalCostCentsBaseline).toBe(250);
    expect(row.tokensResetAt).not.toBeNull();
    const ts = row.tokensResetAt!.getTime();
    expect(ts).toBeGreaterThanOrEqual(before);
    expect(ts).toBeLessThanOrEqual(after + 100);
  });

  it("resetRuntimeStateTokens with clear: true zeros out baselines and nulls tokensResetAt", async () => {
    const svc = agentService(db);
    await svc.resetRuntimeStateTokens(agentId);
    await svc.resetRuntimeStateTokens(agentId, true);

    const [row] = await db.select().from(agentRuntimeState).where(eq(agentRuntimeState.agentId, agentId));
    expect(row.totalInputTokensBaseline).toBe(0);
    expect(row.totalOutputTokensBaseline).toBe(0);
    expect(row.totalCachedInputTokensBaseline).toBe(0);
    expect(row.totalCostCentsBaseline).toBe(0);
    expect(row.tokensResetAt).toBeNull();
  });
});
```

**Important:** The exact name of the existing read method (e.g. `getRuntimeState`, `getAgentRuntimeState`, `runtimeState`, etc.) needs to be confirmed by reading `server/src/services/agents.ts`. Use the actual name in the test. If the existing read method doesn't return the new fields yet, that's expected — the test asserting it does is what makes Task 9 a real change.

- [ ] **Step 2: Run the test to confirm it fails**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test agent-runtime-reset-tokens
```

Expected failures: missing fields on the returned object, and `svc.resetRuntimeStateTokens is not a function`.

- [ ] **Step 3: Commit the failing test**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/__tests__/agent-runtime-reset-tokens.test.ts
git commit -m "test(server): add failing tests for agent runtime token baseline reset"
```

---

## Task 9: Surface baseline columns from the agent runtime state read

**Files:**
- Modify: `server/src/services/agents.ts`

- [ ] **Step 1: Find the existing read method**

```bash
cd ~/Repos/paperclip-tripometer && grep -n "agentRuntimeState\b" server/src/services/agents.ts
```

Identify the function that reads from `agentRuntimeState` (the one returning the data the existing GET `/agents/:id/runtime-state` route serves). It's likely a `select()` building a column list.

- [ ] **Step 2: Add the five new fields to that select**

Wherever the existing read method does `db.select({ ... }).from(agentRuntimeState)...` (or `db.select().from(...)` if it selects everything), make sure it includes:

```typescript
  tokensResetAt: agentRuntimeState.tokensResetAt,
  totalInputTokensBaseline: agentRuntimeState.totalInputTokensBaseline,
  totalOutputTokensBaseline: agentRuntimeState.totalOutputTokensBaseline,
  totalCachedInputTokensBaseline: agentRuntimeState.totalCachedInputTokensBaseline,
  totalCostCentsBaseline: agentRuntimeState.totalCostCentsBaseline,
```

If the existing read uses `db.select().from(agentRuntimeState)` (whole-row), the new columns will already be returned automatically — but you still need to make sure the return type the function exposes (and any shared `AgentRuntimeState` type) includes them.

- [ ] **Step 3: Update the shared `AgentRuntimeState` type if it exists**

```bash
cd ~/Repos/paperclip-tripometer && grep -rn "AgentRuntimeState" packages/shared/src/ 2>/dev/null
```

If the type is defined in `packages/shared/src/...`, add the five new fields there. Match the existing field naming/casing.

- [ ] **Step 4: Run the read tests**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test agent-runtime-reset-tokens
```

Expected: the first test ("read returns baseline columns ...") now passes. The other two still fail.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/services/agents.ts packages/shared/src
git commit -m "feat(server): expose token baseline columns on runtime-state read"
```

---

## Task 10: Implement `agentService.resetRuntimeStateTokens`

**Files:**
- Modify: `server/src/services/agents.ts`

- [ ] **Step 1: Add the new method**

In `server/src/services/agents.ts`, inside the object returned by `agentService(db)`, add this method (place it near the existing `resetSession` or similar reset/runtime methods if there are any, else at the end of the runtime-state-related methods):

```typescript
    resetRuntimeStateTokens: async (agentId: string, clear = false) => {
      await db.transaction(async (tx) => {
        if (clear) {
          await tx
            .update(agentRuntimeState)
            .set({
              tokensResetAt: null,
              totalInputTokensBaseline: 0,
              totalOutputTokensBaseline: 0,
              totalCachedInputTokensBaseline: 0,
              totalCostCentsBaseline: 0,
            })
            .where(eq(agentRuntimeState.agentId, agentId));
          return;
        }
        const [current] = await tx
          .select({
            totalInputTokens: agentRuntimeState.totalInputTokens,
            totalOutputTokens: agentRuntimeState.totalOutputTokens,
            totalCachedInputTokens: agentRuntimeState.totalCachedInputTokens,
            totalCostCents: agentRuntimeState.totalCostCents,
          })
          .from(agentRuntimeState)
          .where(eq(agentRuntimeState.agentId, agentId));
        if (!current) return; // no runtime state row yet — nothing to snapshot
        await tx
          .update(agentRuntimeState)
          .set({
            tokensResetAt: new Date(),
            totalInputTokensBaseline: current.totalInputTokens,
            totalOutputTokensBaseline: current.totalOutputTokens,
            totalCachedInputTokensBaseline: current.totalCachedInputTokens,
            totalCostCentsBaseline: current.totalCostCents,
          })
          .where(eq(agentRuntimeState.agentId, agentId));
      });
    },
```

- [ ] **Step 2: Run the tests**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test agent-runtime-reset-tokens
```

Expected: all three test cases now pass.

- [ ] **Step 3: Run the full server suite to catch regressions**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/services/agents.ts
git commit -m "feat(server): agentService.resetRuntimeStateTokens snapshots/clears baselines"
```

---

## Task 11: Add `POST /agents/:agentId/runtime-state/reset-tokens` route

**Files:**
- Modify: `server/src/routes/agents.ts`

- [ ] **Step 1: Look at how the existing `/runtime-state` and `/runtime-state/reset-session` routes are wired**

```bash
cd ~/Repos/paperclip-tripometer && grep -n "runtime-state" server/src/routes/agents.ts
```

Find the existing pattern: how the company id is resolved from the agent, and how `assertCompanyAccess` is called. Match it for the new route.

- [ ] **Step 2: Add the new POST route**

In `server/src/routes/agents.ts`, add the new route immediately after the existing `/runtime-state/reset-session` route, mirroring its structure for fetching the agent and asserting company access:

```typescript
  router.post("/agents/:agentId/runtime-state/reset-tokens", async (req, res) => {
    const agentId = req.params.agentId as string;
    const targetAgent = await svc.getAgentById(agentId); // adapt to whatever name the existing routes use
    if (!targetAgent) {
      return res.status(404).json({ error: "Agent not found" });
    }
    assertCompanyAccess(req, targetAgent.companyId);
    const clear = (req.body as { clear?: unknown } | undefined)?.clear === true;
    await svc.resetRuntimeStateTokens(agentId, clear);
    res.status(204).end();
  });
```

`getAgentById` is a placeholder — use whatever method name the existing reset-session route uses to look up the agent. Read 3-5 lines around `runtime-state/reset-session` to see the exact pattern.

- [ ] **Step 3: Run the full server suite**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
cd ~/Repos/paperclip-tripometer && git add server/src/routes/agents.ts
git commit -m "feat(server): add POST /agents/:agentId/runtime-state/reset-tokens route"
```

---

## Task 12: Update the dashboard frontend API client and types

**Files:**
- Modify: `ui/src/api/dashboard.ts`

- [ ] **Step 1: Read the current file**

```bash
cd ~/Repos/paperclip-tripometer && cat ui/src/api/dashboard.ts
```

- [ ] **Step 2: Update the types and add the reset method**

Replace the existing `DashboardRunStats` interface with this:

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
```

Then in the `dashboardApi` object (or wherever the existing `runStats` method lives), add:

```typescript
  resetRunStats: (companyId: string, clear = false) =>
    api.post<void>(`/companies/${encodeURIComponent(companyId)}/dashboard/run-stats/reset`, { clear }),
```

- [ ] **Step 3: Verify the project type-checks**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter ui typecheck
```

Expected: errors in `Dashboard.tsx` referring to the old flat shape (`runStats.totalRuns` etc.). Those are real and will be fixed in Task 13. Do not commit until Task 13 also lands — keep the working tree dirty into the next task.

- [ ] **Step 4: Do not commit yet**

The shared types change is incomplete on its own — committing now would break the build. Combine with Task 13 in a single commit.

---

## Task 13: Wire the dashboard panel to the new shape and add the kebab menu

**Files:**
- Modify: `ui/src/pages/Dashboard.tsx`
- Create (optional): `ui/src/components/StatCard.tsx`

- [ ] **Step 1: Read the current panel block**

```bash
cd ~/Repos/paperclip-tripometer && sed -n '270,310p' ui/src/pages/Dashboard.tsx
```

Identify the JSX block for the runStats panel. It currently contains tile cards reading `runStats.totalRuns`, `runStats.successRate`, etc.

- [ ] **Step 2: Add the imports**

Near the top of `ui/src/pages/Dashboard.tsx`, ensure these imports exist (add what's missing):

```typescript
import { MoreVertical } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useMutation, useQueryClient } from "@tanstack/react-query";
```

`useQueryClient` may already be imported — check before adding.

- [ ] **Step 3: Add the reset mutation hook**

Near the existing `useQuery({ queryKey: queryKeys.dashboardRunStats(...) })` call, add:

```typescript
  const queryClient = useQueryClient();
  const resetRunStatsMutation = useMutation({
    mutationFn: (clear: boolean) => dashboardApi.resetRunStats(selectedCompanyId!, clear),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.dashboardRunStats(selectedCompanyId!) });
    },
  });
```

- [ ] **Step 4: Add a small reusable StatCard subcomponent**

Create `ui/src/components/StatCard.tsx`:

```typescript
type StatCardProps = {
  label: string;
  lifetime: string | number;
  sinceReset: string | number | null;
  resetAt: string | null;
};

export function StatCard({ label, lifetime, sinceReset, resetAt }: StatCardProps) {
  return (
    <div className="rounded-lg border p-4">
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="text-2xl font-semibold">{lifetime}</div>
      {sinceReset !== null && resetAt !== null && (
        <div className="mt-1 text-xs text-muted-foreground">
          <span className="font-medium">{sinceReset}</span>
          <span className="ml-1">since {new Date(resetAt).toLocaleString()}</span>
        </div>
      )}
    </div>
  );
}
```

The same component is used by `AgentDetail.tsx` in Task 15 — extracting it now keeps things DRY.

Import it in `Dashboard.tsx`:

```typescript
import { StatCard } from "@/components/StatCard";
```

- [ ] **Step 5: Replace the existing panel block**

In `Dashboard.tsx`, the runStats panel block (the JSX from the existing `{runStats && (` opening down through its closing) becomes:

```tsx
{runStats && (
  <div className="rounded-lg border bg-card">
    <div className="flex items-center justify-between border-b px-4 py-2">
      <div className="text-sm font-medium">Run statistics</div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label="Run statistics menu"
          >
            <MoreVertical className="h-4 w-4" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onSelect={() => resetRunStatsMutation.mutate(false)}
            disabled={resetRunStatsMutation.isPending}
          >
            Reset counter
          </DropdownMenuItem>
          {runStats.resetAt !== null && (
            <DropdownMenuItem
              onSelect={() => resetRunStatsMutation.mutate(true)}
              disabled={resetRunStatsMutation.isPending}
            >
              Show all-time
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
    <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-3">
      <StatCard
        label="Total runs"
        lifetime={runStats.lifetime.totalRuns}
        sinceReset={runStats.sinceReset?.totalRuns ?? null}
        resetAt={runStats.resetAt}
      />
      <StatCard
        label="Success rate"
        lifetime={`${runStats.lifetime.successRate}%`}
        sinceReset={runStats.sinceReset !== null ? `${runStats.sinceReset.successRate}%` : null}
        resetAt={runStats.resetAt}
      />
      <StatCard
        label="Avg duration"
        lifetime={runStats.lifetime.avgDurationMs != null ? `${Math.round(runStats.lifetime.avgDurationMs / 1000)}s` : "—"}
        sinceReset={
          runStats.sinceReset?.avgDurationMs != null
            ? `${Math.round(runStats.sinceReset.avgDurationMs / 1000)}s`
            : null
        }
        resetAt={runStats.resetAt}
      />
    </div>
  </div>
)}
```

If the original panel had additional context (a sub-paragraph saying "X succeeded, Y failed", etc.), preserve that information by adding a fourth `StatCard` or by including it inside the success-rate card's `label`/subtitle. Read your original panel block carefully and don't drop content.

- [ ] **Step 6: Run the type check and the dev build**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter ui typecheck
cd ~/Repos/paperclip-tripometer && pnpm --filter ui build
```

Expected: both succeed. Fix any type errors (likely places where the old `runStats.totalRuns` flat-shape was referenced elsewhere in the file).

- [ ] **Step 7: Commit Tasks 12 + 13 together**

```bash
cd ~/Repos/paperclip-tripometer && git add ui/src/api/dashboard.ts ui/src/pages/Dashboard.tsx ui/src/components/StatCard.tsx
git commit -m "feat(ui): dashboard runStats kebab menu + lifetime/since-reset display"
```

---

## Task 14: Update the agent runtime state frontend API client and types

**Files:**
- Modify: `ui/src/api/agents.ts`

- [ ] **Step 1: Read the current file around the runtimeState method**

```bash
cd ~/Repos/paperclip-tripometer && sed -n '105,125p' ui/src/api/agents.ts
```

Locate the `AgentRuntimeState` type (it may be in `ui/src/api/agents.ts` or imported from `@paperclipai/shared`).

- [ ] **Step 2: Add the five new fields to `AgentRuntimeState`**

Wherever `AgentRuntimeState` is declared, add:

```typescript
  tokensResetAt: string | null;
  totalInputTokensBaseline: number;
  totalOutputTokensBaseline: number;
  totalCachedInputTokensBaseline: number;
  totalCostCentsBaseline: number;
```

- [ ] **Step 3: Add the `resetRuntimeStateTokens` API method**

In the `agentApi` object in `ui/src/api/agents.ts`, near the existing `resetSession`, add:

```typescript
  resetRuntimeStateTokens: (id: string, clear = false, companyId?: string) =>
    api.post<void>(agentPath(id, companyId, "/runtime-state/reset-tokens"), { clear }),
```

- [ ] **Step 4: Don't commit yet — Task 15 is the consumer**

Same reason as Task 12. The type change leaves the working tree in a half-state until the AgentDetail page consumes it.

---

## Task 15: Wire the agent runtime panel to the new shape and add the kebab menu

**Files:**
- Modify: `ui/src/pages/AgentDetail.tsx`

- [ ] **Step 1: Locate the existing token/cost panel**

```bash
cd ~/Repos/paperclip-tripometer && grep -n "totalCostCents\|totalInputTokens\|totalOutputTokens\|totalCachedInputTokens" ui/src/pages/AgentDetail.tsx
```

The cards live around `runtimeState.totalCostCents` (~line 991). Read 30 lines around each occurrence to understand the panel layout.

- [ ] **Step 2: Add the imports (if not already present)**

```typescript
import { MoreVertical } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { StatCard } from "@/components/StatCard";
```

- [ ] **Step 3: Add the reset mutation hook**

Near the existing runtime-state query, add:

```typescript
  const queryClient = useQueryClient();
  const resetTokensMutation = useMutation({
    mutationFn: (clear: boolean) => agentApi.resetRuntimeStateTokens(agentId, clear, companyId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.agentRuntimeState(agentId) });
    },
  });
```

`queryKeys.agentRuntimeState(...)` is whatever key the existing `useQuery` for runtime state uses — read it from the file.

- [ ] **Step 4: Compute since-reset values**

Before the JSX, derive the four since-reset values:

```typescript
  const sinceReset = runtimeState?.tokensResetAt
    ? {
        inputTokens: runtimeState.totalInputTokens - runtimeState.totalInputTokensBaseline,
        outputTokens: runtimeState.totalOutputTokens - runtimeState.totalOutputTokensBaseline,
        cachedInputTokens:
          runtimeState.totalCachedInputTokens - runtimeState.totalCachedInputTokensBaseline,
        costCents: runtimeState.totalCostCents - runtimeState.totalCostCentsBaseline,
      }
    : null;
```

- [ ] **Step 5: Wrap the existing four cards in a new panel container with the kebab menu**

Replace the existing token/cost panel JSX block with:

```tsx
{runtimeState && (
  <div className="rounded-lg border bg-card">
    <div className="flex items-center justify-between border-b px-4 py-2">
      <div className="text-sm font-medium">Token usage & cost</div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label="Token panel menu"
          >
            <MoreVertical className="h-4 w-4" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            onSelect={() => resetTokensMutation.mutate(false)}
            disabled={resetTokensMutation.isPending}
          >
            Reset counter
          </DropdownMenuItem>
          {runtimeState.tokensResetAt !== null && (
            <DropdownMenuItem
              onSelect={() => resetTokensMutation.mutate(true)}
              disabled={resetTokensMutation.isPending}
            >
              Show all-time
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
    <div className="grid grid-cols-2 gap-3 p-4 sm:grid-cols-4">
      <StatCard
        label="Input tokens"
        lifetime={runtimeState.totalInputTokens.toLocaleString()}
        sinceReset={sinceReset ? sinceReset.inputTokens.toLocaleString() : null}
        resetAt={runtimeState.tokensResetAt}
      />
      <StatCard
        label="Output tokens"
        lifetime={runtimeState.totalOutputTokens.toLocaleString()}
        sinceReset={sinceReset ? sinceReset.outputTokens.toLocaleString() : null}
        resetAt={runtimeState.tokensResetAt}
      />
      <StatCard
        label="Cached tokens"
        lifetime={runtimeState.totalCachedInputTokens.toLocaleString()}
        sinceReset={sinceReset ? sinceReset.cachedInputTokens.toLocaleString() : null}
        resetAt={runtimeState.tokensResetAt}
      />
      <StatCard
        label="Total cost"
        lifetime={formatCents(runtimeState.totalCostCents)}
        sinceReset={sinceReset ? formatCents(sinceReset.costCents) : null}
        resetAt={runtimeState.tokensResetAt}
      />
    </div>
  </div>
)}
```

`StatCard` is the same shared component from Task 13. `formatCents` is the existing helper used at `ui/src/pages/AgentDetail.tsx:991` (and elsewhere) — reuse it.

- [ ] **Step 6: Type check and build**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter ui typecheck
cd ~/Repos/paperclip-tripometer && pnpm --filter ui build
```

Expected: both succeed. Fix any other call sites that referenced the old runtime state shape.

- [ ] **Step 7: Commit Tasks 14 + 15 together**

```bash
cd ~/Repos/paperclip-tripometer && git add ui/src/api/agents.ts ui/src/pages/AgentDetail.tsx
git commit -m "feat(ui): agent runtime panel kebab menu + lifetime/since-reset display"
```

---

## Task 16: Build, ship, and smoke-test on the running Vibe Stack

**Files:**
- (None modified — operational task)

- [ ] **Step 1: Run the full test suite one more time**

```bash
cd ~/Repos/paperclip-tripometer && pnpm --filter server test && pnpm --filter ui typecheck && pnpm --filter ui build
```

Expected: all green.

- [ ] **Step 2: Push the feature branch**

```bash
cd ~/Repos/paperclip-tripometer && git push -u origin feature/runstats-tripometer
```

(`origin` here points at `tmartin2113/paperclip` — the user's fork. There is no upstream `paperclipai` remote configured in this clone, so pushing to `origin` is safe. The branch will be merged to `master` separately after final review.)

- [ ] **Step 3: Build and push the new server image**

The server image is `ghcr.io/tmartin2113/paperclip-server:latest`. Use whatever build pipeline the user has set up — most likely a GitHub Actions workflow on the fork that builds and pushes on push to `main`. Verify the workflow ran successfully:

```bash
gh run list --repo tmartin2113/paperclip --limit 3
```

If no workflow exists, the user will need to build and push manually. In that case, ask before proceeding.

- [ ] **Step 4: Pull the new image into Vibe Stack and recreate the server**

```bash
cd ~/Repos/Vibe-Stack && docker compose pull server && docker compose up -d server
```

Expected: server container recreates and reaches `(healthy)` within ~30 seconds. The new column migrations run automatically on startup (via Paperclip's standard migration runner).

- [ ] **Step 5: Manual smoke test — dashboard side**

1. Open the Paperclip UI in a browser.
2. Navigate to the dashboard.
3. Confirm the run-stats panel is present and shows lifetime numbers (no 14-day window).
4. Open the kebab menu — confirm "Reset counter" appears, "Show all-time" does NOT appear.
5. Click "Reset counter".
6. Panel re-fetches; the "since reset" line appears under each card showing "0" (or current values right at the moment of reset for avg-style stats) with a "since {timestamp}" subtitle.
7. Open the kebab menu again — confirm "Show all-time" now appears.
8. Click "Show all-time".
9. The "since reset" line disappears; only lifetime is shown.

- [ ] **Step 6: Manual smoke test — agent side**

1. Navigate to an agent detail page (e.g. CTO).
2. Find the token usage / cost panel.
3. Open the kebab menu — confirm "Reset counter" appears.
4. Click "Reset counter".
5. Each of the four cards now shows the "since reset" line with `0` for tokens and `$0.00` for cost, with a "since {timestamp}" subtitle.
6. Trigger a heartbeat (or wait for one to fire) and verify the lifetime numbers grow while the since-reset numbers also grow but stay below lifetime.
7. Click "Show all-time" — since-reset lines disappear.

- [ ] **Step 7: Commit any final tweaks**

If the smoke test surfaces any small issues (label wording, spacing, missed edge case), fix them, run the build, and commit a `fix:` follow-up commit.

- [ ] **Step 8: Done**

Push any final commits to fork and announce completion.
