# Agent Efficiency Fixes Design Spec

**Date:** 2026-03-25
**Scope:** 10 fixes across `~/Repos/Vibe-Stack/` and `~/paperclip/`
**Goal:** Eliminate scheduling bottlenecks, data blind spots, and silent failures identified from first agent run analysis.

---

## Section 1: Infrastructure & Data Fixes

### Fix #8 — Consolidate DBs to `~/.vibe/`

**Problem:** Two parallel DB directories exist (`~/.vibe/` and `~/.genesia/`). Code hardcodes `~/.vibe/` but data landed in `~/.genesia/` from an older version.

**Changes:**
- Since `~/.vibe/` DBs have schema but no data, and `~/.genesia/` has the data: copy `~/.genesia/*.db` files over `~/.vibe/` equivalents (schemas are identical — same codebase generated both)
- Delete `~/.genesia/` after copy
- No code changes needed — all paths already hardcode `~/.vibe/`

**Files:** None (data migration via shell commands)

### Fix #10 — TTL eviction sweep for artifact cache

**Problem:** `cleanup_expired()` exists in `artifact_store.py` but is never called on a schedule. 100% of cached entries are expired and never evicted.

**Changes:**
- In `agents/heartbeat.py`: after result posting, call `artifact_store.cleanup_expired()` and `artifact_store._evict_if_needed()`
- One call per heartbeat — cheap SQLite DELETE, no performance concern

**Files:** `agents/heartbeat.py`

### Fix #7 — Track tokens/second for local models

**Problem:** `cost_cents=0` for all events (expected for local models), but token counts are also missing on 91% of events. `agent_name` is never populated. No throughput metric exists.

**Changes:**
- `agents/spending_tracker.py`: Add `tokens_per_second REAL` and `generation_duration_ms INTEGER` columns to `cost_events` schema. Add migration logic for existing DBs.
- `agents/heartbeat.py`: Compute `tokens_per_second = output_tokens / (generation_duration_ms / 1000)` from the workflow result. Pass through to spending tracker. Populate `agent_name` from Paperclip agent config.
- `vibe/backends/vllm.py`: Ensure every `generate_chat()` response includes `input_tokens` and `output_tokens` from the vLLM usage response. Track wall-clock generation time.
- Leave `cost_cents=0` for local models — tokens/sec is the efficiency metric.

**Files:** `agents/spending_tracker.py`, `agents/heartbeat.py`, `vibe/backends/vllm.py`

---

## Section 2: vLLM & DeerFlow Fixes

### Fix #3 — Pre-flight context truncation before vLLM calls

**Problem:** Two 400 errors when prompts hit 28,673 tokens (1 over the 32K limit). No pre-flight check exists; the system relies on vLLM to reject oversized prompts.

**Changes:**
- In `vibe/backends/vllm.py` `generate_chat()`: Before sending to vLLM, estimate total prompt tokens using `estimate_tokens()`.
- If `estimated_input + max_tokens > max_model_len` (32768, configurable via env `VLLM_MAX_MODEL_LEN`):
  - Truncate the oldest non-system messages from the conversation history
  - Preserve: system prompt (first message) and latest user message (last message)
  - Log WARNING with original vs truncated token count
- Fallback: if after truncation it still exceeds, reduce `max_tokens` to fit within remaining budget (minimum 256 tokens output)

**Files:** `vibe/backends/vllm.py`

### Fix #2 — DeerFlow retry for incomplete task pickups

**Problem:** VIB-8 was assigned to the CTO Assistant, the adapter acknowledged it in 10 seconds without doing real work, and the task sat untouched for 2h 18m.

**Changes:**
- In `~/paperclip/packages/adapters/deerflow/src/server/execute.ts`: After SSE stream completes, check if the response contains a substantive result.
- Substantive = response body has >50 tokens of content (not just status/metadata).
- If not substantive: set issue back to `todo` status, add `<!-- deerflow-retry:N -->` HTML comment.
- Max 2 retries before escalating to `blocked` with a comment explaining the adapter failed to produce output.

**Files:** `~/paperclip/packages/adapters/deerflow/src/server/execute.ts`

### Fix #4 — Increase DeerFlow worker concurrency

**Problem:** DeerFlow LangGraph server runs with effectively 1 concurrent task. All agent runs serialize, causing queueing delays.

**Changes:**
- In `~/Repos/Vibe-Stack/docker-compose.override.yml` (deerflow-langgraph service, lines 72-87): The `langgraph dev` command runs a single uvicorn worker by default. Add `--workers 3` to the command to allow 3 concurrent LangGraph threads.
- Subagent executor pools in `~/paperclip/deerflow/backend/src/subagents/executor.py` already support 3 concurrent subagents — no change needed there.
- Constraint: vLLM has `max-num-seqs 8` and VRAM at 89.6%. 3 concurrent workers is safe; higher risks OOM or vLLM queue starvation.

**Files:** `~/Repos/Vibe-Stack/docker-compose.override.yml`

---

## Section 3: Agent Intelligence Fixes

### Fix #1 — CTO permission bootstrap robustness

**Problem:** CTO hit a permissions wall during delegation despite `tasks:assign` being auto-granted. Likely a timing/refresh issue.

**Changes:**
- `cto-instructions.md`: Add instruction that on permission error during delegation, retry once after 10 seconds before self-reporting as blocked.
- `bootstrap-all.js`: After creating all agents, add a verification step that queries each agent's effective permissions via `GET /api/agents/{id}` and logs a warning if `canAssignTasks` is not active. If CTO lacks it, explicitly grant via `PATCH /agents/{id}/permissions`.

**Files:** `/home/prime/Projects/.paperclip/cto-instructions.md`, `bootstrap-all.js`

### Fix #6 — Issue title dedup before CTO creates subtasks

**Problem:** CTO created duplicate VIB-5/VIB-6 (identical title, 7 seconds apart). No dedup guard exists.

**Changes:**
- `cto-instructions.md`: Add delegation rule — before creating a subtask, GET the parent's existing children and check titles for duplicates. Skip if a substantially similar title exists.
- `agents/orchestrator.py`: In DECOMPOSE phase, fetch existing children of the parent issue before creating subtasks. Filter out any subtask whose normalized title (lowercased, prefix-stripped) matches an existing child. Log a warning when a duplicate is skipped.

**Files:** `/home/prime/Projects/.paperclip/cto-instructions.md`, `agents/orchestrator.py`

### Fix #5 — CTO rebalance during review phase

**Problem:** 4 agents (Backend, Frontend, QA, Security) were idle for 2+ hours while UX Engineer ground through 8 sequential subtasks. No load-balancing mechanism exists.

**Changes:**
- `cto-instructions.md`: Add a new step at the start of Phase 3 (Review):
  1. Fetch all children of the parent issue
  2. Identify backlogged agents (3+ pending/in-progress subtasks assigned to one agent)
  3. Identify idle agents (all assigned subtasks completed)
  4. For each pending subtask on a backlogged agent: if an idle agent's role/capabilities overlap, reassign via `PATCH /api/issues/{id}` with new `assigneeAgentId`
  5. Add `<!-- rebalanced-from:{original_agent} -->` comment for traceability
- `agents/orchestrator.py`: Add `_rebalance_children()` function called during POLL phase. Detects backlog/idle imbalance across children. Reassigns pending (not in-progress) subtasks. Caps at 2 reassignments per poll cycle to avoid thrashing.

**Files:** `/home/prime/Projects/.paperclip/cto-instructions.md`, `agents/orchestrator.py`

### Fix #9 — Make output critic scoring more robust

**Problem:** `_parse_critic_output()` in `critic_nodes.py` silently defaults all scores to 50 when regex parsing fails. This bypasses the quality-gated iteration loop.

**Changes:**
- `agents/critic_nodes.py` `_parse_critic_output()`:
  - Accept more format variations: "Overall Score: 72", "overall - 72", "Overall: 72/100", "OVERALL 72"
  - Broaden regex to: `r'(\d+)\s*(?:/\s*100|%)?'` and match dimension names case-insensitively with flexible separators (`:`, `-`, `=`, whitespace)
  - If zero dimensions parsed from the SCORES section, fall back to scanning the entire response for any line containing "overall" or "score" with a number
  - If still nothing parsed, make a second LLM call with a minimal prompt: "Rate the quality of the above output from 0 to 100. Reply with only the number."
  - Log at WARNING level (not DEBUG) when primary parsing fails, so failures are visible in structured logs

**Files:** `agents/critic_nodes.py`

---

## File Change Summary

| File | Fixes |
|------|-------|
| `agents/heartbeat.py` | #10, #7 |
| `agents/spending_tracker.py` | #7 |
| `vibe/backends/vllm.py` | #7, #3 |
| `agents/orchestrator.py` | #6, #5 |
| `agents/critic_nodes.py` | #9 |
| `bootstrap-all.js` | #1 |
| `/home/prime/Projects/.paperclip/cto-instructions.md` | #1, #6, #5 |
| `~/paperclip/packages/adapters/deerflow/src/server/execute.ts` | #2 |
| `~/Repos/Vibe-Stack/docker-compose.override.yml` | #4 |
| Data migration (no code) | #8 |

## Execution Order

1. Fix #8 (DB migration) — prerequisite, ensures clean data state
2. Fix #10 (TTL eviction) — simple, no dependencies
3. Fix #7 (token tracking) — schema migration, standalone
4. Fix #3 (context truncation) — standalone vLLM fix
5. Fix #9 (critic scoring) — standalone
6. Fix #1 (CTO permissions) — bootstrap + instructions
7. Fix #6 (dedup) — instructions + orchestrator
8. Fix #5 (rebalance) — instructions + orchestrator (depends on #6 being in orchestrator first)
9. Fix #2 (DeerFlow retry) — Paperclip fork
10. Fix #4 (DeerFlow workers) — Docker config
