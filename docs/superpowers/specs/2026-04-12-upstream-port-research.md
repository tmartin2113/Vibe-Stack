# Upstream Port Research — Memory + Sandbox/Misc Batches

> Reference data for writing specs and plans. Generated 2026-04-12.

## Memory Improvements (6 PRs)

### Dependency Order
```
1. #1353 (storage abstraction)      -- foundation, no deps
2. #1467 (clear/delete APIs)        -- depends on #1353
3. #1538 (create/update APIs)       -- depends on #1467
4. #1521 (import/export)            -- depends on #1353
5. #1668 (correction detection)     -- depends on #1353
6. #1804 (dedup + reinforcement)    -- depends on #1668
```

### PR #1353 — Configurable memory storage abstraction
- **NEW** `storage.py` (205 lines): `MemoryStorage` ABC, `FileMemoryStorage` (path validation, mtime cache, atomic write), `get_memory_storage()` singleton
- **`updater.py`**: gutted 161 lines of file I/O, moved to storage.py. Backward-compat wrappers remain.
- **`memory_config.py`**: added `storage_class` field
- **CRITICAL**: Fork has custom `paperclip_ctx` plumbing in updater.py that must be preserved

### PR #1467 — Memory management actions (clear/delete)
- **`updater.py`**: added `clear_memory_data()`, `delete_memory_fact()`
- **Gateway**: `DELETE /api/memory`, `DELETE /api/memory/facts/{fact_id}`
- Depends on #1353

### PR #1538 — Manual fact CRUD
- **`updater.py`**: added `create_memory_fact()`, `update_memory_fact()` with validation
- **Gateway**: `POST /api/memory/facts`, `PATCH /api/memory/facts/{fact_id}`
- Depends on #1467

### PR #1521 — Import/export
- **`updater.py`**: added `import_memory_data()`
- **Gateway**: `GET /api/memory/export`, `POST /api/memory/import`
- Depends on #1353

### PR #1668 — Structured reflection + correction detection
- **`prompt.py`**: major enhancement — error/retry detection, user correction patterns, `correction` category, `sourceError` field
- **`queue.py`**: `correction_detected` flag, OR-merge across updates
- **`updater.py`**: `correction_hint` in prompt
- **`memory_middleware.py`**: `_CORRECTION_PATTERNS` (12 patterns EN+CN), `detect_correction()`, `_extract_message_text()` helper
- Biggest functional change

### PR #1804 — Case-insensitive dedup + reinforcement
- **`updater.py`**: `_fact_content_key()` uses `.casefold()`, `reinforcement_detected` param
- **`queue.py`**: `reinforcement_detected` flag
- **`memory_middleware.py`**: `_REINFORCEMENT_PATTERNS` (13 patterns), `detect_reinforcement()`, mutual exclusion with correction
- Fork has NO `_fact_content_key()` at all — dedup doesn't exist yet

### Fork-Specific Concerns
- `paperclip_ctx` plumbing in queue.py, updater.py, memory_middleware.py for syncing facts to Paperclip API
- Fork uses `print()` not `logging.getLogger`
- Fork has no dedup, no storage abstraction, no CRUD APIs, no correction/reinforcement detection

---

## Sandbox/Misc Fixes (11 PRs)

### Dependency Chains
```
Sandbox audit:  #1532 -> #1881 -> #1872 (sequential)
Sandbox path:   #1808 -> #1935 (PathMapping refactor first)
Subagent:       #1873 -> #1965 (cancellation before event loop)
Independent:    #2017, #1997, #1714, #1924
```

### Recommended Application Order
1. **#2017** — 1-line fix: `model_class(**{**model_settings_from_config, **kwargs})` prevents duplicate kwarg TypeError
2. **#1997** — 15 lines: ClarificationMiddleware string-serialized options coercion
3. **#1714** — file write conflict locks: fork has `file_operation_lock.py` but tools.py doesn't use it. Wire `get_file_lock()` into `str_replace_tool` and `write_file_tool`
4. **#1532** — NEW `sandbox_audit_middleware.py` (204 lines): 3-tier bash command classification
5. **#1881** — compound command splitting, expanded patterns (15 high-risk, 6 medium-risk)
6. **#1872** — input sanitisation: empty/oversized/null-byte rejection
7. **#1873** — cooperative subagent cancellation: `CANCELLED` status, `cancel_event`, `request_cancel_background_task()`
8. **#1965** — event loop isolation: `_isolated_loop_pool`, `_execute_in_isolated_loop()`, fresh loop per thread
9. **#1924** — nonblocking skill cache: thread-safe cache with background refresh, version counter, `prime_enabled_skills_cache()` at import
10. **#1808** — BREAKING: `PathMapping` dataclass replaces `dict[str,str]`, read-only flag, custom mounts from config
11. **#1935** — path resolution in file content, `_agent_written_paths` tracking (depends on #1808)

### Fork State Summary
| PR | Fork Status | Size |
|----|------------|------|
| #2017 | Missing, 1-line | Trivial |
| #1997 | Missing | 15 lines |
| #1714 | Partial — lock module exists, not wired | Small |
| #1532 | Missing entirely | Medium (new file) |
| #1881 | Missing | Medium |
| #1872 | Missing | Easy |
| #1873 | Missing | Medium |
| #1965 | Missing | Medium |
| #1924 | Missing | Large (128 new lines) |
| #1808 | Missing — BREAKING refactor | Large |
| #1935 | Missing | Medium (depends on #1808) |
