# DeerFlow Memory Improvements — Design Spec

> Port 6 upstream memory PRs (#1353, #1467, #1538, #1521, #1668, #1804) to the Paperclip fork. Adds storage abstraction, CRUD APIs, import/export, correction detection, and case-insensitive dedup with reinforcement.

## Context

The fork's memory system (`deerflow/agents/memory/`) has file I/O hardcoded in `updater.py`, a read-only gateway API, no deduplication, no correction detection, and no manual fact management. Upstream addressed all of these in 6 PRs with a clear dependency chain. The fork must preserve its `paperclip_ctx` plumbing (Paperclip API sync) throughout.

## Scope

### What changes

| PR | What | Files touched |
|----|------|---------------|
| #1353 | Storage abstraction layer | NEW `memory/storage.py`, MODIFY `memory/updater.py`, `config/memory_config.py` |
| #1467 | Clear/delete memory APIs | MODIFY `memory/updater.py`, `gateway/routers/memory.py` |
| #1538 | Manual fact create/update | MODIFY `memory/updater.py`, `gateway/routers/memory.py` |
| #1521 | Import/export | MODIFY `memory/updater.py`, `gateway/routers/memory.py` |
| #1668 | Correction detection | MODIFY `memory/prompt.py`, `memory/queue.py`, `memory/updater.py`, `agents/middlewares/memory_middleware.py` |
| #1804 | Case-insensitive dedup + reinforcement | MODIFY `memory/updater.py`, `memory/queue.py`, `agents/middlewares/memory_middleware.py` |

### What does NOT change

- `paperclip_ctx` plumbing (preserved in queue.py, updater.py, memory_middleware.py)
- Memory data schema (version "1.0", same fact structure)
- Prompt injection pipeline (prompt.py `format_memory_for_injection`)
- Message filtering logic (memory_middleware.py `_filter_messages_for_memory`)
- Upload mention stripping logic
- Per-agent memory support (already in fork)

### Fork-specific concerns

1. **`paperclip_ctx`** — threaded through `queue.py:ConversationContext`, `updater.py:update_memory()`, and `memory_middleware.py:after_agent()`. The storage abstraction must not break this flow. `_sync_facts_to_paperclip()` stays in `updater.py`, not in storage.
2. **`print()` logging** — fork uses `print()`, not `logging.getLogger()`. Keep consistent with fork convention. Do not migrate to logging in this batch.
3. **No `client.py` conformance** — the embedded `DeerFlowClient` has gateway-conformance tests. New gateway endpoints need matching client methods and test coverage.

---

## Design

### 1. Storage Abstraction (#1353)

**New file: `deerflow/agents/memory/storage.py`**

Abstract base class `MemoryStorage` with methods:
- `load(agent_name: str | None) -> dict` — load memory data
- `save(data: dict, agent_name: str | None) -> bool` — save memory data
- `exists(agent_name: str | None) -> bool` — check if memory file exists

Concrete implementation `FileMemoryStorage`:
- Moves all file I/O from `updater.py` into this class
- Keeps: mtime cache, atomic write (temp file + rename), directory creation
- Path resolution: same logic as current `_get_memory_file_path()`

**Singleton access:** `get_memory_storage() -> MemoryStorage` returns the configured storage instance. Default is `FileMemoryStorage`.

**Config addition:** `storage_class` field on `MemoryConfig` (optional string, default `None` = `FileMemoryStorage`). Not exposed in gateway config endpoint (internal concern).

**`updater.py` changes:**
- Remove `_get_memory_file_path()`, `_load_memory_from_file()`, `_save_memory_to_file()`, `_memory_cache`
- `get_memory_data()` delegates to `get_memory_storage().load()`
- `reload_memory_data()` delegates to storage (storage handles cache invalidation)
- `_save_memory_to_file()` callers use `get_memory_storage().save()`
- Keep `_sync_facts_to_paperclip()` in updater (not a storage concern)
- Keep `_create_empty_memory()` in updater (shared by storage and updater)

### 2. Clear/Delete APIs (#1467)

**`updater.py` additions:**
- `clear_memory_data(agent_name=None) -> bool` — reset to empty memory structure, save via storage
- `delete_memory_fact(fact_id: str, agent_name=None) -> bool` — remove single fact by ID, save

**Gateway additions (`memory.py`):**
- `DELETE /api/memory` — calls `clear_memory_data()`, returns empty `MemoryResponse`
- `DELETE /api/memory/facts/{fact_id}` — calls `delete_memory_fact()`, returns updated `MemoryResponse`

**Client additions (`client.py`):**
- `clear_memory() -> dict`
- `delete_memory_fact(fact_id: str) -> dict`

### 3. Manual Fact CRUD (#1538)

**`updater.py` additions:**
- `create_memory_fact(content, category, confidence, agent_name=None) -> dict` — validate, create fact with generated ID, append, save. Returns the new fact dict.
- `update_memory_fact(fact_id, content=None, category=None, confidence=None, agent_name=None) -> dict | None` — find fact by ID, update provided fields, save. Returns updated fact or `None` if not found.

**Validation:**
- `content`: non-empty string, max 500 chars
- `category`: must be one of `preference`, `knowledge`, `context`, `behavior`, `goal`
- `confidence`: 0.0-1.0 float

**Gateway additions (`memory.py`):**
- `POST /api/memory/facts` — body: `{content, category, confidence}`, returns `Fact`
- `PATCH /api/memory/facts/{fact_id}` — body: partial `{content?, category?, confidence?}`, returns `Fact`

**Client additions (`client.py`):**
- `create_memory_fact(content, category, confidence) -> dict`
- `update_memory_fact(fact_id, content=None, category=None, confidence=None) -> dict`

### 4. Import/Export (#1521)

**`updater.py` additions:**
- `import_memory_data(data: dict, merge: bool = False, agent_name=None) -> bool` — if `merge=False`, replace entirely; if `merge=True`, merge facts (append non-duplicate by ID), keep newer summaries by `updatedAt`.

**Gateway additions (`memory.py`):**
- `GET /api/memory/export` — returns full `MemoryResponse` (reuses existing `get_memory()` endpoint logic, but explicit export semantic)
- `POST /api/memory/import` — body: `{data: MemoryResponse, merge: bool}`, calls `import_memory_data()`

**Client additions (`client.py`):**
- `export_memory() -> dict`
- `import_memory(data: dict, merge: bool = False) -> dict`

### 5. Correction Detection (#1668)

**`memory_middleware.py` additions:**
- `_CORRECTION_PATTERNS`: list of 12 compiled regex patterns for EN+CN correction phrases (e.g., "actually", "no, I meant", "that's wrong", "not X, Y")
- `detect_correction(messages: list) -> bool` — scan recent human messages for correction patterns
- `_extract_message_text(msg) -> str` — helper to extract text content from any message type

**`queue.py` changes:**
- Add `correction_detected: bool = False` field to `ConversationContext`
- `add()` accepts `correction_detected` param, passed through
- When replacing a pending update for the same thread, OR-merge `correction_detected` (if either old or new detected correction, keep it True)

**`updater.py` changes:**
- `update_memory()` accepts `correction_hint: bool = False`
- When `correction_hint=True`, append correction instruction to LLM prompt: "The user has corrected or contradicted previous information. Pay special attention to identifying which existing facts should be removed and replaced."

**`prompt.py` changes:**
- Add `correction` to the list of valid fact categories
- Add `sourceError` field documentation (optional field on facts indicating the corrected fact ID)
- Update `MEMORY_UPDATE_PROMPT` rules to mention correction handling

**`memory_middleware.py` `after_agent()` changes:**
- After filtering messages, call `detect_correction()` on filtered messages
- Pass `correction_detected` to `queue.add()`

### 6. Case-Insensitive Dedup + Reinforcement (#1804)

**`updater.py` additions:**
- `_fact_content_key(content: str) -> str` — normalize fact content for comparison: `.casefold()`, strip whitespace, collapse multiple spaces. Returns the normalized key.
- In `_apply_updates()`, before appending a new fact, check if any existing fact has the same content key. If so, skip the new fact (dedup). If the existing fact has lower confidence, update it to the new confidence (reinforcement boost).
- `update_memory()` accepts `reinforcement_detected: bool = False`. When true, existing facts that match new facts get a confidence boost (min of current + 0.1, capped at 1.0).

**`queue.py` changes:**
- Add `reinforcement_detected: bool = False` to `ConversationContext`
- `add()` accepts `reinforcement_detected` param
- OR-merge `reinforcement_detected` when replacing pending updates (same as correction)

**`memory_middleware.py` additions:**
- `_REINFORCEMENT_PATTERNS`: list of 13 compiled regex patterns for reinforcement phrases (e.g., "as I mentioned", "like I said", "remember that", "I always")
- `detect_reinforcement(messages: list) -> bool` — scan recent human messages
- Mutual exclusion: if `correction_detected` is True, `reinforcement_detected` is forced False (correction takes priority)

**`memory_middleware.py` `after_agent()` changes:**
- After detecting correction, also detect reinforcement
- Apply mutual exclusion
- Pass both flags to `queue.add()`

---

## Dependency Order

```
1. Storage abstraction (#1353)     -- foundation, no deps
2. Clear/delete APIs (#1467)       -- depends on #1353 (uses storage)
3. Manual fact CRUD (#1538)        -- depends on #1467 (same patterns)
4. Import/export (#1521)           -- depends on #1353 (uses storage)
5. Correction detection (#1668)    -- depends on #1353 (storage for save)
6. Dedup + reinforcement (#1804)   -- depends on #1668 (mutual exclusion)
```

Tasks 2-4 can be parallelized after Task 1, but for clean commits we'll do them sequentially. Tasks 5-6 are strictly sequential.

## Testing Strategy

Each task gets its own test file or test class within `tests/`:
- `tests/test_memory_storage.py` — storage ABC, FileMemoryStorage load/save/exists, mtime cache, atomic write
- `tests/test_memory_crud.py` — clear, delete, create, update fact operations
- `tests/test_memory_import_export.py` — full replace, merge mode, conflict resolution
- `tests/test_memory_correction.py` — pattern detection, OR-merge, prompt hint, correction category
- `tests/test_memory_dedup.py` — content key normalization, dedup on insert, reinforcement boost, mutual exclusion
- `tests/test_client.py` — gateway conformance tests for new client methods (extend existing `TestGatewayConformance`)

All tests use in-memory or temp-dir storage, no external dependencies.

## Risks

1. **Storage migration** — existing `memory.json` files continue to work because `FileMemoryStorage` uses the same path resolution. No data migration needed.
2. **Paperclip sync ordering** — `_sync_facts_to_paperclip()` stays in `updater.py` and runs after `storage.save()`, same as today. No behavioral change.
3. **Correction false positives** — patterns like "actually" can appear in normal conversation. Mitigated by requiring the pattern to appear in the most recent 2-3 human messages only, and by making correction a hint (the LLM still decides what to update).
4. **Dedup aggressiveness** — `.casefold()` normalization means "Python" and "python" match. This is intentional — the upstream behavior prevents near-duplicate facts.
