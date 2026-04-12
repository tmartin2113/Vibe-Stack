# DeerFlow Upstream Fix Batch — Design Spec

> Port 7 high-value upstream fixes from bytedance/deer-flow to the Paperclip fork, one commit per fix.

## Context

The DeerFlow namespace migration (`src.*` → `deerflow.*`) is complete, so upstream code changes can now be ported without import translation. This spec covers 7 fixes from upstream PRs that address reliability, resource leaks, and configuration gaps. Each is manually ported (not cherry-picked) because upstream's file paths (`packages/harness/deerflow/`) differ from ours (`deerflow/backend/deerflow/`).

## Dropped

**PR #1867 (checkpoint rollback on cancellation)** — requires `runtime/runs/worker.py` which doesn't exist in our fork. Our architecture uses Paperclip's external run management, not LangGraph's internal runtime worker.

## Fixes

### Fix 1: Per-Tool Loop Detection Frequency (PR #1988)

**Problem:** Hash-based loop detection only catches identical tool call sets. An agent calling `read_file` on 40 different files produces unique hashes, bypassing detection and exhausting the recursion limit (150K-225K tokens per failed run).

**Solution:** Add Layer 2 frequency tracking alongside existing Layer 1 hash detection. Same tool name called N times triggers warn (30) then hard stop (50).

**Files:**
- Modify: `deerflow/backend/deerflow/agents/middlewares/loop_detection_middleware.py`

**Changes:**
- Add `_tool_freq` and `_tool_freq_warned` dicts (per-thread, per-tool-name counters)
- In `_track_and_check()`, after hash check, count per-tool-name frequency and return warning/stop messages at thresholds
- Fix `_apply()` to use the actual warning message from `_track_and_check()` for hard stops instead of hardcoded `_HARD_STOP_MSG`

### Fix 2: Dangling Tool-Call Fix After Loop Interruption (PR #2035)

**Problem:** When loop detection hard-stops a tool-call sequence, provider-level raw tool-call metadata in `additional_kwargs["tool_calls"]` and `additional_kwargs["function_call"]` survives, causing "tool_calls must be followed by tool messages" errors on the next model invocation.

**Solution:** Two changes:

**Files:**
- Modify: `deerflow/backend/deerflow/agents/middlewares/dangling_tool_call_middleware.py`
- Modify: `deerflow/backend/deerflow/agents/middlewares/loop_detection_middleware.py`

**Changes in DanglingToolCallMiddleware:**
- New `_message_tool_calls()` static method that normalizes tool calls from both structured `msg.tool_calls` AND raw provider payloads in `additional_kwargs["tool_calls"]` (provider format: `{function: {name, arguments}}`)

**Changes in LoopDetectionMiddleware:**
- New `_build_hard_stop_update()` static method that strips all tool-call metadata from forced-stop messages: clears `tool_calls`, removes `additional_kwargs["tool_calls"]` and `additional_kwargs["function_call"]`, changes `finish_reason` from `"tool_calls"` to `"stop"`

### Fix 3: LLM Circuit Breaker (PR #2095)

**Problem:** When vLLM is down or rate-limited, agents retry indefinitely, exhausting resources and potentially triggering rate-limit bans.

**Solution:** Thread-safe circuit breaker state machine (closed → open → half_open) on a new `LLMErrorHandlingMiddleware`. After N consecutive failures (default 5), circuit opens and fast-fails for `recovery_timeout_sec` (default 60s). One probe request allowed in half-open state.

**Files:**
- Create: `deerflow/backend/deerflow/agents/middlewares/llm_error_handling_middleware.py`
- Modify: `deerflow/backend/deerflow/config/app_config.py`
- Modify: `deerflow/backend/deerflow/agents/lead_agent/agent.py` (add middleware to chain)

**Changes:**
- New middleware class with `_circuit_lock`, `_circuit_failure_count`, `_circuit_open_until`, `_circuit_state`, `_circuit_probe_in_flight` state
- `_check_circuit()` / `_record_success()` / `_record_failure()` methods
- Integration in `wrap_model_call` / `awrap_model_call`: check circuit before call, record success/failure after
- New `CircuitBreakerConfig` in `app_config.py` with `failure_threshold` (default 5) and `recovery_timeout_sec` (default 60)
- Add to middleware chain in `agent.py`

### Fix 4: Sandbox Memory Leak (PR #2096)

**Problem:** File operation locks accumulate in a plain dict — entries are never removed, causing unbounded memory growth in long-running processes.

**Solution:** Replace `dict` with `weakref.WeakValueDictionary` so locks are GC'd when no thread holds a reference.

**Files:**
- Create: `deerflow/backend/deerflow/sandbox/file_operation_lock.py`
- Modify: `deerflow/backend/deerflow/sandbox/tools.py` (import from new module)

**Changes:**
- New module with `_FILE_OPERATION_LOCKS: weakref.WeakValueDictionary[tuple[str, str], threading.Lock]` and a `get_file_lock(sandbox_id, path)` function
- Update `tools.py` to import and use the centralized lock

### Fix 5: Orphaned Container Cleanup (PR #1976)

**Problem:** If the DeerFlow process crashes or is killed, sandbox containers are left running. On restart, they're invisible to the provider and leak resources.

**Solution:** Startup reconciliation — enumerate running containers at init, adopt them into the warm pool, let the idle checker reclaim them.

**Files:**
- Modify: `deerflow/backend/deerflow/community/aio_sandbox/aio_sandbox_provider.py`
- Modify: `deerflow/backend/deerflow/community/aio_sandbox/backend.py`
- Modify: `deerflow/backend/deerflow/community/aio_sandbox/local_backend.py`

**Changes:**
- New `list_running()` method on `SandboxBackend` (default returns `[]`)
- `LocalContainerBackend.list_running()` — `docker ps --filter name=<prefix>-`, batch inspect, parse timestamps
- `_parse_docker_timestamp()` helper for Docker's nanosecond ISO 8601
- `AioSandboxProvider._reconcile_orphans()` — called at `__init__`, adopts discovered containers into warm pool
- `destroy()` fallback from `container_id` to `container_name`
- SIGHUP handler alongside SIGTERM/SIGINT

### Fix 6: `when_thinking_disabled` Model Config (PR #1970)

**Problem:** Models have `when_thinking_enabled` overrides but no equivalent for the disabled path. Users can't specify custom settings for non-thinking mode.

**Solution:** New `when_thinking_disabled` field on `ModelConfig`. When thinking is disabled, user-provided settings take precedence over hardcoded disable logic.

**Files:**
- Modify: `deerflow/backend/deerflow/config/model_config.py`
- Modify: `deerflow/backend/deerflow/models/factory.py`

**Changes:**
- New `when_thinking_disabled: dict | None` field on `ModelConfig` (default `None`)
- In `factory.py`, check `when_thinking_disabled` first in the disable path; if set, apply it and skip hardcoded logic
- Add field to exclusion set so it doesn't leak into model constructor kwargs

### Fix 7: Per-Subagent Model Override (PR #2064)

**Problem:** `SubagentConfig` (dataclass) has a `model` field, but `SubagentOverrideConfig` (Pydantic, for config.yaml) doesn't expose it — Pydantic silently drops the key.

**Solution:** Add `model` field to `SubagentOverrideConfig` and wire it through.

**Files:**
- Modify: `deerflow/backend/deerflow/config/subagents_config.py`
- Modify: `deerflow/backend/deerflow/subagents/registry.py`

**Changes:**
- New `model: str | None` field on `SubagentOverrideConfig` (default `None`, `min_length=1`)
- New `get_model_for(agent_name)` method on `SubagentsAppConfig`
- In `registry.py`, apply model override via `dataclasses.replace()` when config specifies one

**Config usage:**
```yaml
subagents:
  agents:
    general-purpose:
      model: local-vllm
    bash:
      model: cloud-model
```

## Commit Strategy

7 commits in dependency order, each with its own tests:

1. `fix(deerflow): add per-tool-type frequency to loop detection`
2. `fix(deerflow): repair dangling tool-call history after loop interruption`
3. `feat(deerflow): add LLM circuit breaker middleware`
4. `fix(deerflow): prevent memory leak in file operation locks`
5. `fix(deerflow): add startup reconciliation for orphaned containers`
6. `feat(deerflow): add when_thinking_disabled model config`
7. `feat(deerflow): allow per-subagent model override in config`

## Verification

After all 7 commits: `cd deerflow/backend && PYTHONPATH=. uv run pytest tests/ -x`
