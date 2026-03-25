# Observability Dashboard — Design Spec

## Problem

There's no way to see per-run agent performance metrics without manually tailing logs. When agents waste turns, fail silently, or take too long, it's invisible until a human reviews the output.

## Design

Extend the existing Paperclip Dashboard page with an "Agent Runs" section showing per-run detail with performance metrics derived from data already in the `heartbeatRuns` table.

### What Already Exists

- `Dashboard.tsx` (372 lines) already fetches `heartbeatsApi.list()` → `HeartbeatRun[]`
- Each `HeartbeatRun` has: `agentId`, `status`, `startedAt`, `finishedAt`, `usageJson` (tokens), `invocationSource`, `exitCode`, `errorCode`, `contextSnapshot`
- The Dashboard already has agents loaded (`agentMap` for name lookups)
- The `contextSnapshot` contains `issueId` for task attribution

### What's Missing

1. The Dashboard doesn't render the `runs` data as a table — it only uses it for the `RunActivityChart` and `HeartbeatFrequencyChart`
2. There's no issue title join — runs have `contextSnapshot.issueId` but not the issue title
3. No summary metric cards for run performance

### Changes

#### 1. New API Endpoint: `GET /api/companies/:companyId/dashboard/runs`

Returns recent runs enriched with agent name and issue title. Added to the existing `dashboard.ts` route file.

**Response shape:**
```typescript
interface DashboardRun {
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
```

**Query:** LEFT JOIN `heartbeatRuns` with `agents` (for name) and LEFT JOIN `issues` (for title, via `contextSnapshot->>'issueId'`). The LEFT JOIN on issues is required because timer-based runs and runs without task assignment will have no `issueId`. Filter by `companyId`. Order by `startedAt DESC`, limit 50.

**Duration:** Computed server-side as `finishedAt - startedAt` in milliseconds.

**Tokens:** Extracted defensively from `usageJson` using `COALESCE(usageJson->>'inputTokens', usageJson->>'input_tokens')` and `COALESCE(usageJson->>'outputTokens', usageJson->>'output_tokens')` to handle both camelCase and snake_case adapter output. The JSONB `->>'` access happens post-filter (on the LIMIT 50 result set) so no expression index is needed.

**Pagination:** The API always returns up to 50 rows. The client handles pagination locally (show 20, then "Show more" for the rest). No `offset`/`cursor` parameter needed.

#### 2. New API Endpoint: `GET /api/companies/:companyId/dashboard/run-stats`

Returns aggregate metrics for the metric cards.

**Response shape:**
```typescript
interface DashboardRunStats {
  totalRuns: number;
  succeededRuns: number;
  failedRuns: number;
  successRate: number;         // percentage (0-100)
  avgDurationMs: number | null;
  avgInputTokens: number | null;
  avgOutputTokens: number | null;
}
```

**Query:** Aggregate from `heartbeatRuns` WHERE `companyId = :companyId` AND `startedAt >= NOW() - INTERVAL '14 days'`. Uses the same COALESCE pattern for token extraction. The existing index on `(companyId, agentId, startedAt)` covers the filter.

#### 3. New UI Component: `RunsTable`

A table component showing recent runs. Placed on the Dashboard below the existing charts, above Recent Activity / Recent Tasks.

**Columns:**
| Column | Source | Format |
|--------|--------|--------|
| Agent | `agentName` | Text with Identity component |
| Task | `issueTitle` | Linked to issue detail via `issueIdentifier`, truncated |
| Status | `status` | Colored badge |
| Tokens In | `inputTokens` | Number, formatted with comma separators |
| Tokens Out | `outputTokens` | Number, formatted with comma separators |
| Duration | `durationMs` | Human-readable (e.g., "2m 34s") |
| Source | `invocationSource` | Badge |
| When | `startedAt` | timeAgo format |

**Behavior:**
- Shows latest 20 runs by default
- "Show more" button loads next 20 (client-side pagination, up to 50 from the API)
- Row click navigates to the agent detail page's run tab
- Status color coding: `succeeded` = emerald, `failed` = red, `timed_out` = yellow, `cancelled` = gray, `running` = blue, `queued` = slate
- Invocation source badges: `timer`, `assignment`, `on_demand`, `automation`

**Loading/error behavior:** The runs section renders only once data arrives. On error, the section is hidden (matching existing Dashboard pattern where sections conditionally render with `{data && (...)}`). No independent skeleton — the page-level `PageSkeleton` covers initial load.

#### 4. New Summary Metric Cards

4 metric cards in a row above the runs table:

| Card | Value | Icon |
|------|-------|------|
| Total Runs (14d) | `totalRuns` | `Activity` |
| Success Rate | `successRate`% | `CheckCircle` |
| Avg Duration | formatted `avgDurationMs` | `Clock` |
| Avg Tokens/Run | `avgInputTokens + avgOutputTokens` | `Zap` |

Uses the existing `MetricCard` component. Cards render only once `runStats` data arrives, hidden on error.

### File Changes

| File | Change |
|------|--------|
| `server/src/routes/dashboard.ts` | Add 2 new route handlers |
| `server/src/services/dashboard.ts` | Add `runs()` and `runStats()` methods |
| `ui/src/api/dashboard.ts` | Add `runs()` and `runStats()` API calls |
| `ui/src/lib/queryKeys.ts` | Add `dashboardRuns` and `dashboardRunStats` keys |
| `ui/src/pages/Dashboard.tsx` | Add queries, metric cards section, RunsTable |
| `ui/src/components/RunsTable.tsx` | New component (~120 lines) |

### No Schema Changes

All data comes from existing `heartbeatRuns`, `agents`, and `issues` tables. No migrations needed.

## Success Criteria

1. Dashboard shows 4 metric cards with run performance stats (14-day window)
2. Runs table shows per-run detail with agent name, task, status, tokens, duration
3. Data loads from existing DB tables with no schema changes
4. Page renders in <2s (queries use existing index on `companyId + agentId + startedAt`; JSONB access is post-filter)
5. Fits naturally into the existing Dashboard layout below charts
6. Handles missing issue IDs gracefully (LEFT JOIN, nullable fields)
7. Handles both camelCase and snake_case token fields in usageJson
