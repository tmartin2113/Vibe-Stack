# DeerFlow Adapter Upstream PR — Design Spec

> Contribute the fork-only DeerFlow Paperclip adapter to `paperclipai/paperclip` as a first-class builtin adapter.

## Context

The DeerFlow adapter (`packages/adapters/deerflow/`) connects Paperclip agents to a DeerFlow LangGraph backend over SSE. It was built in the `tmartin2113/paperclip` fork and has been production-tested since March 2026. Upstream Paperclip ships 8 builtin adapters but none for LangGraph-based backends. This PR adds DeerFlow as the 9th.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Naming | Keep `deerflow` | Adapter is DeerFlow-specific (assistant ID, context schema, container lifecycle). A generic `langgraph` adapter would look different. |
| Registration | Builtin adapter | Matches how all other adapters are registered. Minimal import cost — type metadata only until an agent runs. |
| PR scope | Adapter + registration only | No seed scripts, compose files, CI workflows, or docs. Tight scope for easy review. |
| Process | Submit PR directly | Let the code speak for itself. |

## Changes

### Adapter Package — `packages/adapters/deerflow/`

Six existing files, two requiring modification:

#### `src/index.ts` — Generalize model list

```typescript
// Before (fork-specific):
export const models = [
  { id: "qwen3.5-9b", label: "Qwen3.5 9B (vLLM)" },
];

// After (upstream-ready):
export const models: { id: string; label: string }[] = [];
```

Empty model list. Models are runtime-configured via the DeerFlow gateway's `config.yaml`, not hardcoded in the adapter.

Everything else in `index.ts` stays: `type`, `label`, `agentConfigurationDoc`.

#### `src/server/execute.ts:195-198` — Remove fork env var

```typescript
// Before (fork-specific VIBE_BACKEND_HOST fallback):
const deerflowUrl = asString(
  config.deerflowUrl as unknown,
  process.env.VIBE_BACKEND_HOST ?? "http://deerflow-langgraph:2024",
);

// After (standard config → default):
const deerflowUrl = asString(
  config.deerflowUrl as unknown,
  "http://deerflow-langgraph:2024",
);
```

`VIBE_BACKEND_HOST` is a Vibe Stack-specific env var. The upstream adapter should rely only on `config.deerflowUrl` (set per-agent in Paperclip) with a sensible Docker Compose default.

#### `package.json` — Add upstream boilerplate

Add fields matching upstream adapter conventions:

```json
{
  "license": "MIT",
  "homepage": "https://github.com/paperclipai/paperclip",
  "bugs": {
    "url": "https://github.com/paperclipai/paperclip/issues"
  },
  "repository": {
    "type": "git",
    "url": "https://github.com/paperclipai/paperclip",
    "directory": "packages/adapters/deerflow"
  }
}
```

#### Files unchanged

| File | Reason |
|------|--------|
| `src/server/index.ts` | Exports + sessionCodec — already generic |
| `src/server/lifecycle.ts` | Docker lifecycle management — uses standard Docker Engine API, container name filter `deerflow` is project convention |
| `src/server/test.ts` | Environment health checks — probes LangGraph + Gateway URLs |
| `tsconfig.json` | Standard TypeScript config |

### Registration — 4 files, minimal changes

#### `packages/shared/src/constants.ts`

Add `"deerflow"` to the `AGENT_ADAPTER_TYPES` array:

```typescript
export const AGENT_ADAPTER_TYPES = [
  "process",
  "http",
  "claude_local",
  "codex_local",
  // ... existing entries ...
  "openclaw_gateway",
  "deerflow",        // ← add
] as const;
```

#### `server/src/adapters/builtin-adapter-types.ts`

Add `"deerflow"` to the `BUILTIN_ADAPTER_TYPES` set:

```typescript
export const BUILTIN_ADAPTER_TYPES = new Set([
  // ... existing entries ...
  "process",
  "http",
  "deerflow",        // ← add
]);
```

#### `server/src/adapters/registry.ts`

Import and register following the existing pattern:

```typescript
// Imports (add alongside other adapter imports):
import {
  execute as deerflowExecute,
  testEnvironment as deerflowTestEnvironment,
  sessionCodec as deerflowSessionCodec,
} from "@paperclipai/adapter-deerflow/server";
import {
  agentConfigurationDoc as deerflowAgentConfigurationDoc,
  models as deerflowModels,
} from "@paperclipai/adapter-deerflow";

// Adapter definition (add alongside other adapter objects):
const deerflowAdapter: ServerAdapterModule = {
  type: "deerflow",
  execute: deerflowExecute,
  testEnvironment: deerflowTestEnvironment,
  sessionCodec: deerflowSessionCodec,
  models: deerflowModels,
  supportsLocalAgentJwt: false,
  agentConfigurationDoc: deerflowAgentConfigurationDoc,
};

// Registration (add to the registerBuiltInAdapters array):
function registerBuiltInAdapters() {
  for (const adapter of [
    // ... existing adapters ...
    deerflowAdapter,   // ← add
  ]) {
    adaptersByType.set(adapter.type, adapter);
  }
}
```

No `listSkills`, `syncSkills`, `listModels`, `getQuotaWindows`, or `detectModel` — DeerFlow doesn't support these Paperclip interfaces. Matches the minimal `openclawGatewayAdapter` shape.

#### `server/package.json`

Add workspace dependency:

```json
{
  "dependencies": {
    "@paperclipai/adapter-deerflow": "workspace:*"
  }
}
```

### Dockerfile

Add COPY line for workspace install, alongside other adapter packages:

```dockerfile
COPY packages/adapters/deerflow/package.json packages/adapters/deerflow/
```

## What's NOT in this PR

| Excluded | Reason |
|----------|--------|
| `docker-compose.yml` DeerFlow services | Infrastructure, not adapter concern |
| `packages/db/src/seed-deerflow.ts` | Fork convenience script |
| `.github/workflows/deerflow-build.yml` | Fork CI |
| `server/src/routes/system-status.ts` | Fork-specific health checks |
| `.env.example` VIBE_BACKEND_HOST | Fork env var being removed |
| Documentation beyond `agentConfigurationDoc` | Can follow separately |

## Adapter Capabilities Summary

| Capability | Supported | Notes |
|------------|-----------|-------|
| Execute (SSE streaming) | Yes | Full LangGraph `/threads/*/runs/stream` with tool call dedup, content extraction |
| Session management | Yes | LangGraph thread ID as session key |
| Environment test | Yes | Probes LangGraph API + Gateway health + model availability |
| Container lifecycle | Yes | Reference-counted Docker start/stop with idle shutdown |
| Refusal detection | Yes | Regex-based guard against LLM non-answers |
| Empty output detection | Yes | Guards against acknowledgment-only and no-tool-call responses |
| Issue commenting | Yes | Posts research results back to Paperclip issues (comment-only, no state mutation) |
| Skills | No | DeerFlow has its own skill system, not exposed via Paperclip |
| Model listing | No | Models configured in DeerFlow config.yaml, not dynamically listed |
| Quota tracking | No | DeerFlow uses local/self-hosted LLMs |

## Testing

The adapter has no unit tests in the fork. For the upstream PR:

- Rely on TypeScript compilation (`tsc --noEmit`) for type correctness
- The `testEnvironment` function serves as runtime integration validation
- Existing upstream CI (CodeQL, PR Checks) will validate the registration changes

## PR Metadata

Per upstream `CONTRIBUTING.md` requirements:

- **Title:** `feat(adapters): add DeerFlow (LangGraph) adapter`
- **Greptile review:** Must achieve 5/5
- **Template sections:** Thinking Path, What Changed, Verification, Risks, Model Used, Checklist
