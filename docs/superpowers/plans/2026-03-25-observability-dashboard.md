# Observability Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the Paperclip Dashboard with per-run agent performance metrics — summary cards and a detailed runs table showing agent, task, status, tokens, duration, and wake reason.

**Architecture:** Add two new endpoints to the existing dashboard service (`runs` and `runStats`), extend the UI Dashboard page with metric cards and a new `RunsTable` component. All data from existing `heartbeatRuns` table — no schema changes.

**Tech Stack:** TypeScript, Drizzle ORM, Express, React, React Query, Tailwind, shadcn/ui, Lucide icons

**Spec:** `docs/superpowers/specs/2026-03-25-observability-dashboard-design.md`

---

### Task 1: Add `runs()` and `runStats()` to dashboard service

**Files:**
- Modify: `/home/prime/paperclip/server/src/services/dashboard.ts`

- [ ] **Step 1: Read the existing dashboard service**

Read `/home/prime/paperclip/server/src/services/dashboard.ts` to understand the current pattern. The service exports a function that takes `db` and returns an object with methods. You'll add two new methods.

- [ ] **Step 2: Add the `runs()` method**

Add this method to the returned object in `dashboardService`, after the existing `summary` method. You'll need to add imports for `heartbeatRuns`, `desc`, `agents as agentsTable` (rename to avoid conflict if needed):

Update the imports at the top:
```typescript
import { and, desc, eq, gte, inArray, sql } from "drizzle-orm";
import type { Db } from "@paperclipai/db";
import { agents, approvals, companies, costEvents, heartbeatRuns, issues } from "@paperclipai/db";
import { notFound } from "../errors.js";
```

Add the `runs` method:
```typescript
    runs: async (companyId: string) => {
      const rows = await db
        .select({
          id: heartbeatRuns.id,
          agentId: heartbeatRuns.agentId,
          agentName: agents.name,
          issueId: sql<string | null>`${heartbeatRuns.contextSnapshot} ->> 'issueId'`.as("issueId"),
          status: heartbeatRuns.status,
          invocationSource: heartbeatRuns.invocationSource,
          startedAt: heartbeatRuns.startedAt,
          finishedAt: heartbeatRuns.finishedAt,
          errorCode: heartbeatRuns.errorCode,
          usageJson: heartbeatRuns.usageJson,
          createdAt: heartbeatRuns.createdAt,
        })
        .from(heartbeatRuns)
        .leftJoin(agents, eq(heartbeatRuns.agentId, agents.id))
        .where(eq(heartbeatRuns.companyId, companyId))
        .orderBy(desc(heartbeatRuns.startedAt))
        .limit(50);

      // Collect issue IDs for batch lookup
      const issueIds = [...new Set(rows.map((r) => r.issueId).filter(Boolean))] as string[];
      const issueMap = new Map<string, { title: string; identifier: string | null }>();
      if (issueIds.length > 0) {
        const issueRows = await db
          .select({ id: issues.id, title: issues.title, identifier: issues.identifier })
          .from(issues)
          .where(inArray(issues.id, issueIds));
        for (const row of issueRows) {
          issueMap.set(row.id, { title: row.title, identifier: row.identifier });
        }
      }

      return rows.map((r) => {
        const usage = r.usageJson as Record<string, unknown> | null;
        const inputTokens = Number(usage?.inputTokens ?? usage?.input_tokens ?? 0) || null;
        const outputTokens = Number(usage?.outputTokens ?? usage?.output_tokens ?? 0) || null;
        const durationMs =
          r.startedAt && r.finishedAt
            ? new Date(r.finishedAt).getTime() - new Date(r.startedAt).getTime()
            : null;
        const issue = r.issueId ? issueMap.get(r.issueId) : null;

        return {
          id: r.id,
          agentId: r.agentId,
          agentName: r.agentName,
          issueId: r.issueId,
          issueTitle: issue?.title ?? null,
          issueIdentifier: issue?.identifier ?? null,
          status: r.status,
          invocationSource: r.invocationSource,
          startedAt: r.startedAt,
          finishedAt: r.finishedAt,
          durationMs,
          inputTokens,
          outputTokens,
          errorCode: r.errorCode,
          createdAt: r.createdAt,
        };
      });
    },
```

- [ ] **Step 3: Add the `runStats()` method**

Add after the `runs` method:

```typescript
    runStats: async (companyId: string) => {
      const fourteenDaysAgo = new Date(Date.now() - 14 * 24 * 60 * 60 * 1000);

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
        .where(
          and(
            eq(heartbeatRuns.companyId, companyId),
            gte(heartbeatRuns.startedAt, fourteenDaysAgo),
          ),
        );

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
    },
```

- [ ] **Step 4: Verify TypeScript compiles**

Run: `cd /home/prime/paperclip/server && npx tsc --noEmit`
Expected: No errors.

- [ ] **Step 5: Commit**

```bash
cd /home/prime/paperclip/server
git add src/services/dashboard.ts
git commit -m "Add runs() and runStats() to dashboard service

Queries heartbeatRuns with agent name join and issue title batch
lookup. Extracts tokens from usageJson (camelCase + snake_case).
Computes duration and 14-day aggregates.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: Add dashboard route handlers

**Files:**
- Modify: `/home/prime/paperclip/server/src/routes/dashboard.ts`

- [ ] **Step 1: Read the existing dashboard route**

Read `/home/prime/paperclip/server/src/routes/dashboard.ts`. Currently has one route: `GET /companies/:companyId/dashboard`.

- [ ] **Step 2: Add two new route handlers**

Add after the existing route handler (before `return router`):

```typescript
  router.get("/companies/:companyId/dashboard/runs", async (req, res) => {
    const companyId = req.params.companyId as string;
    assertCompanyAccess(req, companyId);
    const runs = await svc.runs(companyId);
    res.json(runs);
  });

  router.get("/companies/:companyId/dashboard/run-stats", async (req, res) => {
    const companyId = req.params.companyId as string;
    assertCompanyAccess(req, companyId);
    const stats = await svc.runStats(companyId);
    res.json(stats);
  });
```

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd /home/prime/paperclip/server && npx tsc --noEmit`
Expected: No errors.

- [ ] **Step 4: Commit**

```bash
cd /home/prime/paperclip/server
git add src/routes/dashboard.ts
git commit -m "Add /dashboard/runs and /dashboard/run-stats routes

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 3: Add UI API client and query keys

**Files:**
- Modify: `/home/prime/paperclip/ui/src/api/dashboard.ts`
- Modify: `/home/prime/paperclip/ui/src/lib/queryKeys.ts`

- [ ] **Step 1: Add types and API calls to dashboard.ts**

Add the following types and methods to `/home/prime/paperclip/ui/src/api/dashboard.ts`. Keep the existing `summary` method and `DashboardSummary` import. The final file should be:

```typescript
import type { DashboardSummary } from "@paperclipai/shared";
import { api } from "./client";

export interface DashboardRun {
  id: string;
  agentId: string;
  agentName: string;
  issueId: string | null;
  issueTitle: string | null;
  issueIdentifier: string | null;
  status: string;
  invocationSource: string;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
  inputTokens: number | null;
  outputTokens: number | null;
  errorCode: string | null;
  createdAt: string;
}

export interface DashboardRunStats {
  totalRuns: number;
  succeededRuns: number;
  failedRuns: number;
  successRate: number;
  avgDurationMs: number | null;
  avgInputTokens: number | null;
  avgOutputTokens: number | null;
}

export const dashboardApi = {
  summary: (companyId: string) => api.get<DashboardSummary>(`/companies/${companyId}/dashboard`),
  runs: (companyId: string) => api.get<DashboardRun[]>(`/companies/${companyId}/dashboard/runs`),
  runStats: (companyId: string) => api.get<DashboardRunStats>(`/companies/${companyId}/dashboard/run-stats`),
};
```

- [ ] **Step 2: Add query keys**

In `/home/prime/paperclip/ui/src/lib/queryKeys.ts`, add two new keys after the existing `dashboard` key (line 64):

```typescript
  dashboardRuns: (companyId: string) => ["dashboard-runs", companyId] as const,
  dashboardRunStats: (companyId: string) => ["dashboard-run-stats", companyId] as const,
```

- [ ] **Step 3: Commit**

```bash
cd /home/prime/paperclip/ui
git add src/api/dashboard.ts src/lib/queryKeys.ts
git commit -m "Add dashboard runs API client and query keys

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 4: Create RunsTable component

**Files:**
- Create: `/home/prime/paperclip/ui/src/components/RunsTable.tsx`

- [ ] **Step 1: Create the component**

Create `/home/prime/paperclip/ui/src/components/RunsTable.tsx`:

```tsx
import { useState } from "react";
import { Link, useNavigate } from "@/lib/router";
import { Identity } from "./Identity";
import { timeAgo } from "../lib/timeAgo";
import type { DashboardRun } from "../api/dashboard";

const STATUS_COLORS: Record<string, string> = {
  succeeded: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
  failed: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  timed_out: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/40 dark:text-yellow-300",
  cancelled: "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400",
  running: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300",
  queued: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400",
};

const SOURCE_LABELS: Record<string, string> = {
  timer: "Timer",
  assignment: "Assigned",
  on_demand: "On Demand",
  automation: "Auto",
};

function formatDuration(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
}

function formatTokens(n: number | null): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

export function RunsTable({ runs }: { runs: DashboardRun[] }) {
  const navigate = useNavigate();
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? runs : runs.slice(0, 20);

  if (runs.length === 0) {
    return (
      <div className="border border-border rounded-md p-4">
        <p className="text-sm text-muted-foreground">No recent runs.</p>
      </div>
    );
  }

  return (
    <div>
      <div className="border border-border rounded-md overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-muted/50">
              <th className="text-left px-3 py-2 font-medium text-muted-foreground">Agent</th>
              <th className="text-left px-3 py-2 font-medium text-muted-foreground">Task</th>
              <th className="text-left px-3 py-2 font-medium text-muted-foreground">Status</th>
              <th className="text-right px-3 py-2 font-medium text-muted-foreground">Tokens In</th>
              <th className="text-right px-3 py-2 font-medium text-muted-foreground">Tokens Out</th>
              <th className="text-right px-3 py-2 font-medium text-muted-foreground">Duration</th>
              <th className="text-left px-3 py-2 font-medium text-muted-foreground">Source</th>
              <th className="text-right px-3 py-2 font-medium text-muted-foreground">When</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((run) => (
              <tr
                key={run.id}
                className="border-b border-border last:border-b-0 hover:bg-accent/50 transition-colors cursor-pointer"
                onClick={() => navigate(`/agents/${run.agentId}/runs`)}
              >
                <td className="px-3 py-2">
                  <Identity name={run.agentName} size="sm" />
                </td>
                <td className="px-3 py-2 max-w-[200px] truncate">
                  {run.issueIdentifier ? (
                    <Link
                      to={`/issues/${run.issueIdentifier}`}
                      className="text-foreground hover:underline"
                    >
                      {run.issueTitle ?? run.issueIdentifier}
                    </Link>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </td>
                <td className="px-3 py-2">
                  <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[run.status] ?? STATUS_COLORS.queued}`}>
                    {run.status}
                  </span>
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{formatTokens(run.inputTokens)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{formatTokens(run.outputTokens)}</td>
                <td className="px-3 py-2 text-right tabular-nums">{formatDuration(run.durationMs)}</td>
                <td className="px-3 py-2">
                  <span className="text-xs text-muted-foreground">
                    {SOURCE_LABELS[run.invocationSource] ?? run.invocationSource}
                  </span>
                </td>
                <td className="px-3 py-2 text-right text-muted-foreground">
                  {run.startedAt ? timeAgo(run.startedAt) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!showAll && runs.length > 20 && (
        <button
          onClick={() => setShowAll(true)}
          className="mt-2 text-sm text-muted-foreground hover:text-foreground underline underline-offset-2"
        >
          Show {runs.length - 20} more runs
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript compiles**

Run: `cd /home/prime/paperclip/ui && npx tsc --noEmit`
Expected: No errors. (If `Identity` or `Link` imports need path adjustment, fix them.)

- [ ] **Step 3: Commit**

```bash
cd /home/prime/paperclip/ui
git add src/components/RunsTable.tsx
git commit -m "Add RunsTable component for dashboard observability

Shows per-run detail: agent, task, status, tokens, duration, source.
Client-side pagination (20 initially, expand to 50).

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 5: Integrate into Dashboard page

**Files:**
- Modify: `/home/prime/paperclip/ui/src/pages/Dashboard.tsx`

- [ ] **Step 1: Read the Dashboard page**

Read `/home/prime/paperclip/ui/src/pages/Dashboard.tsx` to understand the current structure. Key landmarks:
- Lines 1-28: imports
- Lines 44-94: existing queries
- Lines 226-276: existing metric cards grid
- Lines 278-300: chart grids
- Lines 302-366: Recent Activity and Recent Tasks grids
- Line 368: closing `</>` of the `{data && (...)}` block

- [ ] **Step 2: Add imports**

Add to the existing imports at the top of the file:

```typescript
import { Activity, CheckCircle, Clock, Zap } from "lucide-react";
import { RunsTable } from "../components/RunsTable";
```

(Note: `Activity`, `CheckCircle`, `Clock`, `Zap` are from lucide-react. The file already imports from lucide-react on line 22 — add these to that import.)

- [ ] **Step 3: Add the two new React Query hooks**

Add after the existing `dailyCosts` query (around line 94):

```typescript
  const { data: dashboardRuns } = useQuery({
    queryKey: queryKeys.dashboardRuns(selectedCompanyId!),
    queryFn: () => dashboardApi.runs(selectedCompanyId!),
    enabled: !!selectedCompanyId,
  });

  const { data: runStats } = useQuery({
    queryKey: queryKeys.dashboardRunStats(selectedCompanyId!),
    queryFn: () => dashboardApi.runStats(selectedCompanyId!),
    enabled: !!selectedCompanyId,
  });
```

- [ ] **Step 4: Add the Agent Performance section**

Insert after the second chart grid (after the `</div>` on line ~300, which closes the Daily Spend / Heartbeat Activity grid) and before the Recent Activity / Recent Tasks grid:

```tsx
          {/* Agent Performance */}
          {runStats && (
            <div className="grid grid-cols-2 xl:grid-cols-4 gap-1 sm:gap-2">
              <MetricCard
                icon={Activity}
                value={runStats.totalRuns}
                label="Total Runs (14d)"
              />
              <MetricCard
                icon={CheckCircle}
                value={`${runStats.successRate}%`}
                label="Success Rate"
                description={
                  <span>{runStats.succeededRuns} succeeded, {runStats.failedRuns} failed</span>
                }
              />
              <MetricCard
                icon={Clock}
                value={runStats.avgDurationMs != null ? `${Math.round(runStats.avgDurationMs / 1000)}s` : "—"}
                label="Avg Duration"
              />
              <MetricCard
                icon={Zap}
                value={
                  runStats.avgInputTokens != null && runStats.avgOutputTokens != null
                    ? (runStats.avgInputTokens + runStats.avgOutputTokens).toLocaleString()
                    : "—"
                }
                label="Avg Tokens/Run"
                description={
                  runStats.avgInputTokens != null
                    ? <span>{runStats.avgInputTokens.toLocaleString()} in, {(runStats.avgOutputTokens ?? 0).toLocaleString()} out</span>
                    : undefined
                }
              />
            </div>
          )}

          {dashboardRuns && (
            <div>
              <h3 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide mb-3">
                Recent Runs
              </h3>
              <RunsTable runs={dashboardRuns} />
            </div>
          )}
```

- [ ] **Step 5: Verify TypeScript compiles**

Run: `cd /home/prime/paperclip/ui && npx tsc --noEmit`
Expected: No errors.

- [ ] **Step 6: Build the UI to verify**

Run: `cd /home/prime/paperclip/ui && npx vite build`
Expected: Build succeeds.

- [ ] **Step 7: Commit**

```bash
cd /home/prime/paperclip/ui
git add src/pages/Dashboard.tsx
git commit -m "Add agent performance metrics and runs table to Dashboard

Shows 4 metric cards (total runs, success rate, avg duration, avg tokens)
and a per-run detail table with agent, task, status, tokens, duration.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```
