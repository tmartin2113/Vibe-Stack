# DeerFlow Sandbox & Misc Fixes — Design Spec

> Port 11 upstream PRs covering model factory fix, clarification coercion, file locks, sandbox audit middleware, subagent cancellation, event loop isolation, skill cache, and path mapping refactor.

## Scope

### Group A: Independent Quick Fixes (#2017, #1997, #1714)

**#2017 — Model factory duplicate kwarg fix (1 line)**

Current (`models/factory.py:73`): `model_class(**kwargs, **model_settings_from_config)` — if both dicts contain the same key, Python raises `TypeError: got multiple values for keyword argument`. Fix: merge into one dict with explicit precedence.

Change to: `model_class(**{**model_settings_from_config, **kwargs})` — kwargs (caller-provided) override config defaults.

**#1997 — ClarificationMiddleware options coercion (15 lines)**

Current (`middlewares/clarification_middleware.py`): `options = args.get("options", [])` — if the LLM returns options as a JSON string instead of a list (common with smaller models), the middleware iterates over characters instead of option items.

Fix: Add coercion after extracting options:
```python
if isinstance(options, str):
    try:
        import json
        parsed = json.loads(options)
        if isinstance(parsed, list):
            options = parsed
    except (json.JSONDecodeError, TypeError):
        options = [options]
```

**#1714 — Wire file operation locks into sandbox tools**

Current: `file_operation_lock.py` exists with `get_file_lock(sandbox_id, path)` using `WeakValueDictionary`, but `tools.py` doesn't use it. `str_replace_tool` and `write_file_tool` both do read-modify-write without locking.

Fix: In `tools.py`, wrap the read-modify-write in `str_replace_tool` and the write in `write_file_tool` with `get_file_lock()`:
```python
from deerflow.sandbox.file_operation_lock import get_file_lock

# In str_replace_tool, after resolving path:
lock = get_file_lock(sandbox_id, path)
with lock:
    content = sandbox.read_file(path)
    # ... replace logic ...
    sandbox.write_file(path, content)

# In write_file_tool, after resolving path:
lock = get_file_lock(sandbox_id, path)
with lock:
    sandbox.write_file(path, content, append)
```

### Group B: Sandbox Audit Middleware (#1532 → #1881 → #1872)

**#1532 — NEW `sandbox_audit_middleware.py` (base)**

New middleware that classifies bash commands into 3 risk tiers before execution:
- **High risk** (blocked): `rm -rf /`, `chmod 777`, `curl | sh`, `dd if=`, network exfil patterns
- **Medium risk** (logged + allowed): `apt install`, `pip install`, `git clone`, `wget`, `curl`
- **Low risk** (allowed silently): all other commands

Implementation: `SandboxAuditMiddleware` as an `AgentMiddleware` that intercepts `bash` tool calls in `after_model()`, classifies the command, and either allows, logs, or blocks.

**#1881 — Compound command splitting + expanded patterns**

Enhancement to audit middleware:
- Split compound commands (`&&`, `||`, `;`, `|`) and classify each segment
- Overall risk = highest of any segment
- Expanded patterns: 15 high-risk, 6 medium-risk

**#1872 — Input sanitisation**

Pre-execution validation in audit middleware:
- Reject empty commands
- Reject commands exceeding max length (configurable, default 10000 chars)
- Reject commands containing null bytes (`\x00`)

### Group C: Subagent Improvements (#1873 → #1965)

**#1873 — Cooperative subagent cancellation**

Current: Subagents can only be stopped by timeout. No cooperative cancellation.

Add:
- `CANCELLED` to `SubagentStatus` enum
- `cancel_event: threading.Event` on `SubagentResult`
- `request_cancel_background_task(task_id)` function
- In executor's `_aexecute()`: check `cancel_event.is_set()` between agent steps
- Return `SubagentStatus.CANCELLED` when event is set

**#1965 — Event loop isolation**

Current: `asyncio.run()` creates a new event loop per subagent execution. This can fail when called from within an existing event loop.

Add:
- `_isolated_loop_pool: dict[int, asyncio.AbstractEventLoop]` — one loop per thread
- `_execute_in_isolated_loop(coro)` — runs a coroutine in the thread's dedicated loop
- Replace `asyncio.run()` calls in executor with `_execute_in_isolated_loop()`

### Group D: Skill Cache (#1924)

**#1924 — Nonblocking skill cache**

Current: Every call to `load_skills()` does a full filesystem scan. Expensive and not thread-safe.

Add thread-safe cache with background refresh:
- `_skills_cache: list[SkillMetadata]` — cached result
- `_cache_version: int` — monotonically increasing version counter
- `_cache_lock: threading.Lock` — protects reads/writes
- `_refresh_in_progress: bool` — prevents concurrent refreshes
- `get_cached_skills() -> list[SkillMetadata]` — returns cached list, triggers background refresh if stale
- `invalidate_skills_cache()` — forces next access to refresh
- `prime_enabled_skills_cache()` — called at import time to pre-populate cache
- Config mtime check: refresh only when `extensions_config.json` has changed

### Group E: Path Mapping Refactor (#1808 → #1935)

**#1808 — PathMapping dataclass (BREAKING)**

Current: Path mappings are `dict[str, str]` (container_path → local_path). `VolumeMountConfig` exists in `sandbox_config.py` with a `read_only` flag but isn't integrated.

Add:
- `PathMapping` dataclass in `sandbox/path_mapping.py`:
  ```python
  @dataclass(frozen=True)
  class PathMapping:
      container_path: str
      local_path: str
      read_only: bool = False
  ```
- Refactor `LocalSandbox.__init__` to accept `list[PathMapping]` instead of `dict[str, str]`
- Update `LocalSandboxProvider._setup_path_mappings()` to create `PathMapping` objects, including mounts from `SandboxConfig.mounts` (integrating `VolumeMountConfig`)
- `replace_virtual_path()` and `replace_virtual_paths_in_command()` work with `PathMapping` list
- Write operations check `read_only` flag and raise error if attempting to write to a read-only mount

**#1935 — Path resolution in file content + written paths tracking**

Add:
- `_agent_written_paths: set[str]` on sandbox state — tracks paths the agent has written to
- After `write_file_tool` and `str_replace_tool`, add the path to the tracking set
- `replace_virtual_paths_in_content(content, path_mappings)` — resolve virtual paths found within file content (for generated scripts, configs, etc.)

---

## Dependency Order

```
Group A (independent, do first):
  1. #2017  (1-line model factory fix)
  2. #1997  (clarification coercion)
  3. #1714  (wire file locks)

Group B (sequential):
  4. #1532  (sandbox audit base)
  5. #1881  (compound splitting + patterns)
  6. #1872  (input sanitisation)

Group C (sequential):
  7. #1873  (subagent cancellation)
  8. #1965  (event loop isolation)

Group D (independent):
  9. #1924  (skill cache)

Group E (sequential):
  10. #1808  (PathMapping dataclass)
  11. #1935  (path tracking + content resolution)
```

## Testing Strategy

- `tests/test_model_factory_kwargs.py` — verify no TypeError with overlapping kwargs
- `tests/test_clarification_coercion.py` — string options get parsed to list
- `tests/test_file_locks_integration.py` — concurrent writes don't corrupt
- `tests/test_sandbox_audit.py` — command classification (high/medium/low), compound splitting, input validation
- `tests/test_subagent_cancellation.py` — cancel event, status transitions
- `tests/test_event_loop_isolation.py` — nested loop handling
- `tests/test_skill_cache.py` — cache hit/miss, invalidation, background refresh
- `tests/test_path_mapping.py` — PathMapping dataclass, read-only enforcement, content path resolution

## Risks

1. **PathMapping is BREAKING** — all code using `dict[str, str]` path mappings must be updated. Contained within `sandbox/` module.
2. **Audit middleware false positives** — overly aggressive patterns could block legitimate commands. Mitigate with logging-only mode for medium risk.
3. **Event loop pool cleanup** — loops in `_isolated_loop_pool` need cleanup on thread death. Use `atexit` or weak references.
4. **Skill cache staleness** — stale cache during rapid skill enable/disable. Mitigate with explicit `invalidate_skills_cache()` call in gateway skill update endpoint.
