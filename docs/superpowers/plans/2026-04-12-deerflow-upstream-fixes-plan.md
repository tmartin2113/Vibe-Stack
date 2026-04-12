# DeerFlow Upstream Fix Batch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 6 high-value upstream fixes from bytedance/deer-flow into the Paperclip fork to improve reliability, prevent resource leaks, and add configuration flexibility.

**Architecture:** Each fix is manually ported by reading the upstream PR diff and applying the same logic to our fork's file structure. One commit per fix, dependency-ordered. Fix 1 (per-tool loop detection, PR #1988) is already present in the fork and is skipped.

**Tech Stack:** Python, LangGraph, threading, weakref, Docker subprocess.

**Spec:** `docs/superpowers/specs/2026-04-12-deerflow-upstream-fixes-design.md`

**Repo:** `/home/prime/Repos/paperclip` (branch: `master`)

---

## File Structure

### New Files

| File | Purpose |
|------|---------|
| `deerflow/backend/deerflow/agents/middlewares/llm_error_handling_middleware.py` | Circuit breaker middleware (Fix 3) |
| `deerflow/backend/deerflow/sandbox/file_operation_lock.py` | WeakValueDictionary file locks (Fix 4) |

### Modified Files

| File | Fixes |
|------|-------|
| `deerflow/backend/deerflow/agents/middlewares/dangling_tool_call_middleware.py` | Fix 2: raw provider payload normalization |
| `deerflow/backend/deerflow/agents/middlewares/loop_detection_middleware.py` | Fix 2: `_build_hard_stop_update()` |
| `deerflow/backend/deerflow/config/app_config.py` | Fix 3: `CircuitBreakerConfig` |
| `deerflow/backend/deerflow/agents/lead_agent/agent.py` | Fix 3: add middleware to chain |
| `deerflow/backend/deerflow/community/aio_sandbox/backend.py` | Fix 5: `list_running()` interface |
| `deerflow/backend/deerflow/community/aio_sandbox/local_backend.py` | Fix 5: container enumeration |
| `deerflow/backend/deerflow/community/aio_sandbox/aio_sandbox_provider.py` | Fix 5: `_reconcile_orphans()` |
| `deerflow/backend/deerflow/config/model_config.py` | Fix 6: `when_thinking_disabled` field |
| `deerflow/backend/deerflow/models/factory.py` | Fix 6: disable path logic |
| `deerflow/backend/deerflow/config/subagents_config.py` | Fix 7: `model` field on override |
| `deerflow/backend/deerflow/subagents/registry.py` | Fix 7: apply model override |

---

## Task 1: Dangling Tool-Call Fix (PR #2035)

**Problem:** When loop detection hard-stops a tool-call sequence, raw provider metadata in `additional_kwargs["tool_calls"]` survives, causing "tool_calls must be followed by tool messages" errors.

**Files:**
- Modify: `deerflow/backend/deerflow/agents/middlewares/dangling_tool_call_middleware.py`
- Modify: `deerflow/backend/deerflow/agents/middlewares/loop_detection_middleware.py`

- [ ] **Step 1: Add `_message_tool_calls()` to DanglingToolCallMiddleware**

In `deerflow/backend/deerflow/agents/middlewares/dangling_tool_call_middleware.py`, add this static method to the class, before `_build_patched_messages`:

```python
@staticmethod
def _message_tool_calls(msg) -> list[dict]:
    """Extract tool calls from both structured and raw provider formats.

    Some providers store tool calls in additional_kwargs["tool_calls"]
    with a different schema ({function: {name, arguments}}) instead of
    the LangChain-native msg.tool_calls list.
    """
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        return list(tool_calls)
    raw_tool_calls = (getattr(msg, "additional_kwargs", None) or {}).get("tool_calls") or []
    normalized = []
    for raw_tc in raw_tool_calls:
        fn = raw_tc.get("function") or {}
        name = fn.get("name", "")
        args_str = fn.get("arguments", "{}")
        tc_id = raw_tc.get("id", "")
        if name or tc_id:
            try:
                import json
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, TypeError):
                args = {}
            normalized.append({"name": name, "args": args, "id": tc_id})
    return normalized
```

- [ ] **Step 2: Update `_build_patched_messages` to use `_message_tool_calls`**

Replace the two loops that check `getattr(msg, "tool_calls", None) or []` with calls to `self._message_tool_calls(msg)`:

In the "Check if any patching is needed" section, replace:
```python
for tc in getattr(msg, "tool_calls", None) or []:
```
with:
```python
for tc in self._message_tool_calls(msg):
```

And in the "Build new list with patches" section, replace the same pattern:
```python
for tc in getattr(msg, "tool_calls", None) or []:
```
with:
```python
for tc in self._message_tool_calls(msg):
```

- [ ] **Step 3: Add `_build_hard_stop_update()` to LoopDetectionMiddleware**

In `deerflow/backend/deerflow/agents/middlewares/loop_detection_middleware.py`, add this static method to the `LoopDetectionMiddleware` class, before `_apply`:

```python
@staticmethod
def _build_hard_stop_update(last_msg, content: str) -> dict:
    """Build a message update that strips all tool-call metadata.

    When loop detection forces a stop, raw provider-level tool call
    metadata in additional_kwargs must also be cleared, otherwise the
    next model invocation fails with 'tool_calls must be followed by
    tool messages'.
    """
    from copy import deepcopy

    update: dict = {"tool_calls": [], "content": content}
    additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
    for key in ("tool_calls", "function_call"):
        additional_kwargs.pop(key, None)
    update["additional_kwargs"] = additional_kwargs
    response_metadata = deepcopy(getattr(last_msg, "response_metadata", {}) or {})
    if response_metadata.get("finish_reason") == "tool_calls":
        response_metadata["finish_reason"] = "stop"
    update["response_metadata"] = response_metadata
    return update
```

- [ ] **Step 4: Update `_apply` to use `_build_hard_stop_update`**

In the `_apply` method, replace the hard_stop block:

```python
if hard_stop:
    # Strip tool_calls from the last AIMessage to force text output
    messages = state.get("messages", [])
    last_msg = messages[-1]
    stripped_msg = last_msg.model_copy(
        update={
            "tool_calls": [],
            "content": self._append_text(last_msg.content, warning),
        }
    )
    return {"messages": [stripped_msg]}
```

with:

```python
if hard_stop:
    # Strip all tool-call metadata (structured + raw provider) to force text output
    messages = state.get("messages", [])
    last_msg = messages[-1]
    update = self._build_hard_stop_update(
        last_msg,
        self._append_text(last_msg.content, warning),
    )
    stripped_msg = last_msg.model_copy(update=update)
    return {"messages": [stripped_msg]}
```

- [ ] **Step 5: Run tests**

```bash
cd /home/prime/Repos/paperclip/deerflow/backend
PYTHONPATH=. uv run pytest tests/test_loop_detection_middleware.py tests/test_dangling_tool_call_middleware.py -x -v --no-header -q 2>&1 | tail -10
```

Expected: All tests pass (the existing test_dangling_tool_call_middleware tests should still work; new behavior is additive).

- [ ] **Step 6: Commit**

```bash
cd /home/prime/Repos/paperclip
git add deerflow/backend/deerflow/agents/middlewares/dangling_tool_call_middleware.py \
        deerflow/backend/deerflow/agents/middlewares/loop_detection_middleware.py
git commit -m "fix(deerflow): repair dangling tool-call history after loop interruption

Port of bytedance/deer-flow#2035. DanglingToolCallMiddleware now
normalizes raw provider payloads in additional_kwargs. LoopDetection
hard-stop strips all tool-call metadata including finish_reason.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: LLM Circuit Breaker (PR #2095)

**Problem:** When vLLM is down, agents retry indefinitely, exhausting resources.

**Files:**
- Create: `deerflow/backend/deerflow/agents/middlewares/llm_error_handling_middleware.py`
- Modify: `deerflow/backend/deerflow/config/app_config.py`
- Modify: `deerflow/backend/deerflow/agents/lead_agent/agent.py`

- [ ] **Step 1: Create the middleware**

Create `deerflow/backend/deerflow/agents/middlewares/llm_error_handling_middleware.py`:

```python
"""Middleware providing LLM error handling with circuit breaker protection.

Prevents rate-limit bans and resource exhaustion by fast-failing after
N consecutive model call failures. Uses a three-state circuit breaker:
closed (normal) -> open (fast-fail) -> half_open (probe one request).
"""

import logging
import threading
import time
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

_CIRCUIT_OPEN_MSG = (
    "[CIRCUIT BREAKER] The language model is temporarily unavailable after "
    "{failures} consecutive failures. Requests will resume automatically "
    "in {remaining}s. If this persists, check the model service health."
)


class LLMErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """LLM error handling with circuit breaker.

    Args:
        failure_threshold: Consecutive failures before opening circuit. Default: 5.
        recovery_timeout_sec: Seconds to fast-fail before probing. Default: 60.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_sec: int = 60,
    ):
        super().__init__()
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec
        self._circuit_lock = threading.Lock()
        self._circuit_failure_count = 0
        self._circuit_open_until = 0.0
        self._circuit_state = "closed"  # closed | open | half_open
        self._circuit_probe_in_flight = False

    def _check_circuit(self) -> str | None:
        """Check circuit state. Returns error message if open, None if OK to proceed."""
        with self._circuit_lock:
            now = time.monotonic()
            if self._circuit_state == "closed":
                return None
            if self._circuit_state == "open":
                if now >= self._circuit_open_until:
                    if not self._circuit_probe_in_flight:
                        self._circuit_state = "half_open"
                        self._circuit_probe_in_flight = True
                        logger.info("Circuit breaker half-open — allowing probe request")
                        return None
                remaining = max(1, int(self._circuit_open_until - now))
                return _CIRCUIT_OPEN_MSG.format(
                    failures=self._circuit_failure_count,
                    remaining=remaining,
                )
            # half_open but probe already in flight — reject
            if self._circuit_probe_in_flight:
                return _CIRCUIT_OPEN_MSG.format(
                    failures=self._circuit_failure_count,
                    remaining=self.recovery_timeout_sec,
                )
            return None

    def _record_success(self) -> None:
        with self._circuit_lock:
            if self._circuit_state == "half_open":
                logger.info("Circuit breaker probe succeeded — closing circuit")
            self._circuit_failure_count = 0
            self._circuit_state = "closed"
            self._circuit_open_until = 0.0
            self._circuit_probe_in_flight = False

    def _record_failure(self) -> None:
        with self._circuit_lock:
            self._circuit_failure_count += 1
            self._circuit_probe_in_flight = False
            if self._circuit_failure_count >= self.failure_threshold:
                self._circuit_state = "open"
                self._circuit_open_until = time.monotonic() + self.recovery_timeout_sec
                logger.error(
                    "Circuit breaker opened after %d failures — fast-failing for %ds",
                    self._circuit_failure_count,
                    self.recovery_timeout_sec,
                )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        circuit_msg = self._check_circuit()
        if circuit_msg:
            return AIMessage(content=circuit_msg)
        try:
            result = handler(request)
            self._record_success()
            return result
        except Exception as exc:
            # Let GraphBubbleUp propagate without counting
            if type(exc).__name__ == "GraphBubbleUp":
                with self._circuit_lock:
                    self._circuit_probe_in_flight = False
                raise
            self._record_failure()
            raise

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        circuit_msg = self._check_circuit()
        if circuit_msg:
            return AIMessage(content=circuit_msg)
        try:
            result = await handler(request)
            self._record_success()
            return result
        except Exception as exc:
            if type(exc).__name__ == "GraphBubbleUp":
                with self._circuit_lock:
                    self._circuit_probe_in_flight = False
                raise
            self._record_failure()
            raise
```

- [ ] **Step 2: Add CircuitBreakerConfig to app_config.py**

In `deerflow/backend/deerflow/config/app_config.py`, add after the imports:

```python
class CircuitBreakerConfig(BaseModel):
    """Circuit breaker settings for LLM error handling."""
    failure_threshold: int = Field(default=5, description="Consecutive failures before opening circuit")
    recovery_timeout_sec: int = Field(default=60, description="Seconds to fast-fail before probing")
```

And add the field to the `AppConfig` class (after `tool_groups`):

```python
circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig, description="Circuit breaker config")
```

- [ ] **Step 3: Wire into agent middleware chain**

In `deerflow/backend/deerflow/agents/lead_agent/agent.py`, add the import:

```python
from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware
```

In `_build_middlewares`, add after `LoopDetectionMiddleware()` in the initial list:

```python
middlewares = [ThreadDataMiddleware(), UploadsMiddleware(), SandboxMiddleware(), DanglingToolCallMiddleware(), LoopDetectionMiddleware(), LLMErrorHandlingMiddleware()]
```

- [ ] **Step 4: Run tests**

```bash
cd /home/prime/Repos/paperclip/deerflow/backend
PYTHONPATH=. uv run pytest tests/ -x --no-header -q 2>&1 | tail -5
```

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/prime/Repos/paperclip
git add deerflow/backend/deerflow/agents/middlewares/llm_error_handling_middleware.py \
        deerflow/backend/deerflow/config/app_config.py \
        deerflow/backend/deerflow/agents/lead_agent/agent.py
git commit -m "feat(deerflow): add LLM circuit breaker middleware

Port of bytedance/deer-flow#2095. Thread-safe circuit breaker
(closed/open/half_open) on LLMErrorHandlingMiddleware. After 5
consecutive failures, fast-fails for 60s before probing.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Sandbox Memory Leak Fix (PR #2096)

**Problem:** File operation locks accumulate unboundedly in long-running processes.

**Files:**
- Create: `deerflow/backend/deerflow/sandbox/file_operation_lock.py`

- [ ] **Step 1: Create file_operation_lock.py**

Create `deerflow/backend/deerflow/sandbox/file_operation_lock.py`:

```python
"""Centralized file operation locks using WeakValueDictionary.

Prevents unbounded memory growth in long-running processes by
automatically dropping lock entries when no thread holds a reference.
"""

import threading
import weakref

_LockKey = tuple[str, str]  # (sandbox_id, path)
_FILE_OPERATION_LOCKS: weakref.WeakValueDictionary[_LockKey, threading.Lock] = weakref.WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()


def get_file_lock(sandbox_id: str, path: str) -> threading.Lock:
    """Get or create a lock for a specific file in a sandbox.

    The lock is automatically garbage collected when no thread holds
    a reference to it (via WeakValueDictionary).
    """
    key: _LockKey = (sandbox_id, path)
    with _REGISTRY_LOCK:
        lock = _FILE_OPERATION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_OPERATION_LOCKS[key] = lock
        return lock
```

- [ ] **Step 2: Run tests**

```bash
cd /home/prime/Repos/paperclip/deerflow/backend
PYTHONPATH=. uv run pytest tests/ -x --no-header -q 2>&1 | tail -5
```

Expected: All tests pass (new module, no consumers yet — available for future use).

- [ ] **Step 3: Commit**

```bash
cd /home/prime/Repos/paperclip
git add deerflow/backend/deerflow/sandbox/file_operation_lock.py
git commit -m "fix(deerflow): prevent memory leak in file operation locks

Port of bytedance/deer-flow#2096. WeakValueDictionary-backed file
locks that are GC'd when no thread holds a reference.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Orphaned Container Cleanup (PR #1976)

**Problem:** If DeerFlow crashes, sandbox containers leak — invisible to the provider on restart.

**Files:**
- Modify: `deerflow/backend/deerflow/community/aio_sandbox/backend.py`
- Modify: `deerflow/backend/deerflow/community/aio_sandbox/local_backend.py`
- Modify: `deerflow/backend/deerflow/community/aio_sandbox/aio_sandbox_provider.py`

- [ ] **Step 1: Add `list_running()` to SandboxBackend**

In `deerflow/backend/deerflow/community/aio_sandbox/backend.py`, add a method to the `SandboxBackend` class:

```python
def list_running(self) -> list:
    """List running sandbox containers. Returns list of SandboxInfo.

    Default implementation returns empty list (e.g. for RemoteSandboxBackend).
    LocalContainerBackend overrides with Docker container enumeration.
    """
    return []
```

- [ ] **Step 2: Add container enumeration to LocalContainerBackend**

In `deerflow/backend/deerflow/community/aio_sandbox/local_backend.py`, add these methods to `LocalContainerBackend`:

```python
def _batch_inspect(self, container_names: list[str]) -> list[dict]:
    """Batch-inspect containers via a single docker inspect call."""
    if not container_names:
        return []
    try:
        result = subprocess.run(
            [self._runtime, "inspect"] + container_names,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return []
        import json
        return json.loads(result.stdout)
    except Exception:
        return []

@staticmethod
def _parse_docker_timestamp(ts: str) -> float:
    """Parse Docker's nanosecond ISO 8601 timestamp to epoch seconds."""
    from datetime import datetime, timezone
    # Truncate nanoseconds to microseconds, normalize Z suffix
    ts = ts.rstrip("Z").rstrip("z")
    # Docker uses up to 9 fractional digits; Python handles up to 6
    if "." in ts:
        base, frac = ts.rsplit(".", 1)
        frac = frac[:6]  # truncate to microseconds
        ts = f"{base}.{frac}"
    try:
        dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0

def list_running(self) -> list:
    """Enumerate running DeerFlow sandbox containers via Docker CLI."""
    from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo
    try:
        result = subprocess.run(
            [self._runtime, "ps", "--filter", f"name={self._container_prefix}-",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return []
        container_names = [
            name.strip() for name in result.stdout.strip().splitlines()
            if name.strip().startswith(self._container_prefix + "-")
        ]
        if not container_names:
            return []

        inspections = self._batch_inspect(container_names)
        sandboxes = []
        for info in inspections:
            name = info.get("Name", "").lstrip("/")
            sandbox_id = name.replace(self._container_prefix + "-", "", 1) if name else ""
            if not sandbox_id:
                continue
            state = info.get("State", {})
            started_at = state.get("StartedAt", "")
            sandboxes.append(SandboxInfo(
                sandbox_id=sandbox_id,
                container_id=info.get("Id", ""),
                container_name=name,
                created_at=self._parse_docker_timestamp(started_at),
            ))
        return sandboxes
    except Exception as e:
        logger.warning("Failed to enumerate running containers: %s", e)
        return []
```

Also add `import subprocess` and `logger = logging.getLogger(__name__)` to the top of the file if not already present.

- [ ] **Step 3: Add `_reconcile_orphans()` to AioSandboxProvider**

In `deerflow/backend/deerflow/community/aio_sandbox/aio_sandbox_provider.py`, add this method and call it from `__init__`:

```python
def _reconcile_orphans(self) -> None:
    """Adopt orphaned containers from a previous process into the warm pool.

    Called once at startup before the idle checker begins. Discovered
    containers are added to the warm pool unconditionally — the idle
    checker will reclaim them if they stay unused.
    """
    import time
    try:
        running = self._backend.list_running()
        if not running:
            return
        current_time = time.time()
        adopted = 0
        with self._lock:
            for info in running:
                if info.sandbox_id in self._sandboxes or info.sandbox_id in self._warm_pool:
                    continue
                self._warm_pool[info.sandbox_id] = (info, current_time)
                adopted += 1
        if adopted:
            logger.info("Reconciled %d orphaned container(s) into warm pool", adopted)
    except Exception as e:
        logger.warning("Orphan reconciliation failed (non-fatal): %s", e)
```

Call `self._reconcile_orphans()` in `__init__` before starting the idle checker.

- [ ] **Step 4: Run tests**

```bash
cd /home/prime/Repos/paperclip/deerflow/backend
PYTHONPATH=. uv run pytest tests/ -x --no-header -q 2>&1 | tail -5
```

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/prime/Repos/paperclip
git add deerflow/backend/deerflow/community/aio_sandbox/backend.py \
        deerflow/backend/deerflow/community/aio_sandbox/local_backend.py \
        deerflow/backend/deerflow/community/aio_sandbox/aio_sandbox_provider.py
git commit -m "fix(deerflow): add startup reconciliation for orphaned containers

Port of bytedance/deer-flow#1976. On startup, enumerate running
containers via Docker CLI, adopt them into the warm pool, and let
the idle checker reclaim them.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `when_thinking_disabled` Model Config (PR #1970)

**Problem:** No way to specify custom settings for non-thinking mode.

**Files:**
- Modify: `deerflow/backend/deerflow/config/model_config.py`
- Modify: `deerflow/backend/deerflow/models/factory.py`

- [ ] **Step 1: Add field to ModelConfig**

In `deerflow/backend/deerflow/config/model_config.py`, add after the `supports_vision` field:

```python
when_thinking_disabled: dict | None = Field(
    default_factory=lambda: None,
    description="Extra settings to be passed to the model when thinking is disabled",
)
```

- [ ] **Step 2: Update factory.py**

In `deerflow/backend/deerflow/models/factory.py`, add `"when_thinking_disabled"` to the `exclude` set in `model_dump()`:

```python
model_settings_from_config = model_config.model_dump(
    exclude_none=True,
    exclude={
        "use",
        "name",
        "display_name",
        "description",
        "supports_thinking",
        "supports_reasoning_effort",
        "when_thinking_enabled",
        "when_thinking_disabled",
        "thinking",
        "supports_vision",
    },
)
```

Then replace the existing `if not thinking_enabled and has_thinking_settings:` block:

```python
if not thinking_enabled and has_thinking_settings:
    if effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
        # OpenAI-compatible gateway: thinking is nested under extra_body
        kwargs.update({"extra_body": {"thinking": {"type": "disabled"}}})
        kwargs.update({"reasoning_effort": "minimal"})
    elif effective_wte.get("thinking", {}).get("type"):
        # Native langchain_anthropic: thinking is a direct constructor parameter
        kwargs.update({"thinking": {"type": "disabled"}})
```

with:

```python
if not thinking_enabled:
    if model_config.when_thinking_disabled is not None:
        # User-provided disable settings take full precedence
        model_settings_from_config.update(model_config.when_thinking_disabled)
    elif has_thinking_settings:
        if effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            kwargs.update({"extra_body": {"thinking": {"type": "disabled"}}})
            kwargs.update({"reasoning_effort": "minimal"})
        elif (disable_chat_template_kwargs := effective_wte.get("extra_body", {}).get("chat_template_kwargs")):
            # vLLM chat template: disable thinking via chat_template_kwargs
            disabled_kwargs = {k: False for k in disable_chat_template_kwargs}
            kwargs.update({"extra_body": {"chat_template_kwargs": disabled_kwargs}})
        elif effective_wte.get("thinking", {}).get("type"):
            kwargs.update({"thinking": {"type": "disabled"}})
```

- [ ] **Step 3: Run tests**

```bash
cd /home/prime/Repos/paperclip/deerflow/backend
PYTHONPATH=. uv run pytest tests/test_model_factory.py tests/ -x --no-header -q 2>&1 | tail -5
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
cd /home/prime/Repos/paperclip
git add deerflow/backend/deerflow/config/model_config.py \
        deerflow/backend/deerflow/models/factory.py
git commit -m "feat(deerflow): add when_thinking_disabled model config

Port of bytedance/deer-flow#1970. New when_thinking_disabled field on
ModelConfig. User-provided settings take precedence over hardcoded
disable logic. Also adds vLLM chat_template_kwargs disable path.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Per-Subagent Model Override (PR #2064)

**Problem:** `SubagentOverrideConfig` silently drops the `model` key from config.yaml.

**Files:**
- Modify: `deerflow/backend/deerflow/config/subagents_config.py`
- Modify: `deerflow/backend/deerflow/subagents/registry.py`

- [ ] **Step 1: Add `model` field to SubagentOverrideConfig**

In `deerflow/backend/deerflow/config/subagents_config.py`, add to `SubagentOverrideConfig`:

```python
class SubagentOverrideConfig(BaseModel):
    """Per-agent configuration overrides."""

    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Timeout in seconds for this subagent (None = use global default)",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        description="Model name for this subagent (None = use default model)",
    )
```

Add `get_model_for` method to `SubagentsAppConfig`:

```python
def get_model_for(self, agent_name: str) -> str | None:
    """Get the model override for a specific agent, if set.

    Args:
        agent_name: The name of the subagent.

    Returns:
        Model name if overridden, None otherwise.
    """
    override = self.agents.get(agent_name)
    if override is not None and override.model is not None:
        return override.model
    return None
```

Update the logging in `load_subagents_config_from_dict` to include model overrides:

```python
overrides_summary = {}
for name, override in _subagents_config.agents.items():
    parts = []
    if override.timeout_seconds is not None:
        parts.append(f"timeout={override.timeout_seconds}s")
    if override.model is not None:
        parts.append(f"model={override.model}")
    if parts:
        overrides_summary[name] = ", ".join(parts)
```

- [ ] **Step 2: Wire model override in registry.py**

In `deerflow/backend/deerflow/subagents/registry.py`, update `get_subagent_config` to also apply model overrides:

```python
def get_subagent_config(name: str) -> SubagentConfig | None:
    """Get a subagent configuration by name, with config.yaml overrides applied."""
    config = BUILTIN_SUBAGENTS.get(name)
    if config is None:
        return None

    from deerflow.config.subagents_config import get_subagents_app_config

    app_config = get_subagents_app_config()
    overrides: dict = {}

    # Timeout override
    effective_timeout = app_config.get_timeout_for(name)
    if effective_timeout != config.timeout_seconds:
        logger.debug(f"Subagent '{name}': timeout overridden ({config.timeout_seconds}s -> {effective_timeout}s)")
        overrides["timeout_seconds"] = effective_timeout

    # Model override
    effective_model = app_config.get_model_for(name)
    if effective_model is not None and effective_model != config.model:
        logger.debug(f"Subagent '{name}': model overridden ('{config.model}' -> '{effective_model}')")
        overrides["model"] = effective_model

    if overrides:
        config = replace(config, **overrides)

    return config
```

- [ ] **Step 3: Run tests**

```bash
cd /home/prime/Repos/paperclip/deerflow/backend
PYTHONPATH=. uv run pytest tests/test_subagent_timeout_config.py tests/ -x --no-header -q 2>&1 | tail -5
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
cd /home/prime/Repos/paperclip
git add deerflow/backend/deerflow/config/subagents_config.py \
        deerflow/backend/deerflow/subagents/registry.py
git commit -m "feat(deerflow): allow per-subagent model override in config

Port of bytedance/deer-flow#2064. SubagentOverrideConfig now exposes
model field. Config.yaml can specify different models per subagent.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Final Verification

- [ ] **Step 1: Run full test suite**

```bash
cd /home/prime/Repos/paperclip/deerflow/backend
PYTHONPATH=. uv run pytest tests/ -x --no-header -q 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 2: Verify commit history**

```bash
cd /home/prime/Repos/paperclip
git log --oneline -8
```

Expected: 6 new commits for fixes 2-7.

- [ ] **Step 3: Push**

```bash
cd /home/prime/Repos/paperclip
git push origin master
```

---

## Summary

| Task | Fix | PR | Type | Files |
|------|-----|-----|------|-------|
| — | Per-tool loop detection | #1988 | Already done | — |
| 1 | Dangling tool-call fix | #2035 | Bug fix | 2 modified |
| 2 | LLM Circuit Breaker | #2095 | New feature | 1 new, 2 modified |
| 3 | Sandbox memory leak | #2096 | Bug fix | 1 new |
| 4 | Orphaned container cleanup | #1976 | Bug fix | 3 modified |
| 5 | `when_thinking_disabled` | #1970 | New feature | 2 modified |
| 6 | Per-subagent model override | #2064 | New feature | 2 modified |
| 7 | Final verification | — | Verification | 0 |

**Total: 2 new files, 11 modified files, 6 commits, 7 tasks**
