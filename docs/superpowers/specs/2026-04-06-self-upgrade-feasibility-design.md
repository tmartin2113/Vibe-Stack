# Self-Upgrade Feasibility — Design

**Status:** Draft
**Date:** 2026-04-06
**Author:** prime + Claude (brainstorming session)

## Problem

Vibe-Stack has an existing `SelfUpgradePipeline` (`agents/self_upgrade.py`) and signal
accumulator (`agents/self_upgrade_trigger.py`) that were designed to let agents propose
and apply modifications to their own source code. The plumbing is in place end-to-end —
signals, thresholds, pytest/bandit/critic gates, a `vibe/self-upgrade*` branch prefix —
but **the pipeline has never fired a real proposal**. Not once. There are zero
`vibe/self-upgrade*` branches in the repo, no `UpgradeProposal` records, no commits
from the `vibe-self-upgrade` author.

The failure mode isn't one thing; it's a stack of mismatched assumptions:

1. **The gate is too expensive for the changes that would be safe.** Every proposal —
   including a docstring edit — has to run the full ~3000-test pytest suite in a
   tempdir copy. That's a 5+ minute gate on an LLM-generated patch that might be one
   line.
2. **The allowlist is too loose for the changes that would be risky.** "Anywhere under
   `agents/` except 4 immutables" is effectively "almost everything," including the
   workflow graph engine, LLM plumbing, storage, and the heartbeat itself. An LLM
   rewriting `graph_engine.py` is not a self-improvement, it's a self-immolation.
3. **The proposal format is a whole-file diff.** `UpgradeProposal(files=dict[path, str])`
   replaces file contents wholesale. There's no way to say "I am only tweaking one
   numeric constant" vs. "I am rewriting this module," so the gate has to assume the
   worst about every proposal.
4. **The signal accumulator has no path for observations that aren't code fixes.**
   When the critic sees a recurring problem it can't patch — a SearXNG config issue,
   a missing tool parameter, an infra problem — the observation is lost.
5. **Skill generation and prompt overrides — both of which already exist as parallel
   self-improvement mechanisms** (`skill_generator.py`, `AdapterRegistry.get_or_create()`)
   — are completely disconnected from the signal → proposal pipeline.

The 170 persisted `upgrade_signals.jsonl` entries are a monument to this: identical
`low_score` / `score 40` / empty-feedback records from a single March 28 burst,
classified but never acted on, because no mechanism existed to turn them into
anything useful.

This spec narrows and redefines what "feasible to upgrade" actually means, so the
next implementation effort produces a loop that can genuinely fire end-to-end.

## Goal

Replace the current "one pipeline, one gate, file diffs only" model with **five
tiered artifact types**, where each tier is defined by *what the agent is producing*
(not by which file it's touching). Each tier has its own trigger condition, storage,
validation, and reversibility story, scaled to the risk it carries.

The tiering is built around an agent's-eye view of what's safe to change, not just
what's *allowed* to change. An LLM rewriting `git_tools.py` is "allowed" under the
current immutable list, but nobody sane would trust it. By contrast, an LLM tuning
a system-prompt append or writing a "dear future-me" memory note is genuinely within
its competence.

### Success criteria

- The dispatcher can fire end-to-end on real signals and produce visible artifacts.
- At least one real Paperclip issue or memory note is produced by an actual agent
  run (not a test harness) after Milestone 1 ships.
- The Improvements tab in the Paperclip UI becomes the single dashboard for all
  self-upgrade activity.
- No self-upgrade artifact can touch any file in the expanded "Never" list without
  a hand-written PR from a human.
- The stale 170-signal store from 2026-03-28 is wiped and the new signal format
  (with `id` + `artifact_ref`) prevents signal replay on dispatcher restart.

### Non-goals

- Agents merging their own PRs. All tier 1b and tier 2 proposals land on a branch
  with a companion Paperclip issue assigned to the human user — never auto-merged.
- Agents editing the workflow graph, LLM plumbing, storage layer, heartbeat, or
  any subsystem on the expanded "Never" list.
- Agents modifying the self-upgrade pipeline itself, the signal trigger, the skill
  security layer, or core config. These remain hard immutables.
- Integrating with the existing DeerFlow `self_upgrade_agent` subagent (see
  [Future Work](#future-work) for what this subagent is and why it's out of scope).
- Fixing the Milestone -1 prerequisite: agents aren't actually running real
  workflows yet because of outstanding container-config issues documented in
  `~/.claude/projects/-home-prime/memory/vibe-stack-setup-progress.md`. This spec
  describes the system that *will* fire once agents start working; nothing here
  unblocks that prerequisite.

## Tier Overview

| Tier | Artifact | Storage | Gate | Reversible by | Approves |
|---|---|---|---|---|---|
| **0** | Memory note | `memory_store` (new `MemoryType.LESSON`) | None | Deleting the record | Nobody (auto) |
| **1a** | Skill (or skill refinement) | `~/.vibe/skills/` | Outcome-score A/B cooldown | Deleting the skill | Nobody (auto) |
| **1b** | Prompt override | `agents/prompt_library/overrides.yaml` | Schema + append-only diff check + prompt-critic + canonical smoke | `git revert` | Human merges branch |
| **2** | Narrow typed code edit | Files under `agents/` matching one of 5 edit types | Edit-type-specific gate | `git revert` | Human merges branch |
| **3** | Issue report | New Paperclip issue | Report schema + self-critique ≥ 70 | Closing the issue | Human triages |
| **∞** | Everything else | — | — | — | **Blocked unconditionally** |

**Invariants:**

- Tier 0 and Tier 1a never touch the repo. They are per-install data files and
  deleting them is a full rollback.
- Tier 1b and Tier 2 are the only tiers that produce git commits. Both land on
  `vibe/self-upgrade/*` branches that a human merges by hand. Agents cannot merge.
- Tier 3 produces **no code at all** — it's a structured ticket with evidence.
- The "Never" set is substantially larger than today's immutable list and is
  enforced mechanically before any gate runs.

## Tier 0 — Memory notes

A durable "dear future-me" note an agent writes when it finishes a task and notices
something worth remembering next time. Scoped to `(role, task_type, optional tag)`.
Auto-injected into the context of any future agent matching that scope. Scored by
whether future runs do better after the note exists.

### Data model

Memory notes are a new entry type inside the existing `memory_store` (not a new
store). Rationale: `memory_store.py` already provides pluggable storage backends,
BM25 + vector search, TTL cleanup, and citation tracking. Adding a new
`MemoryType.LESSON` variant with scope-field metadata reuses all of that at the
cost of one new `list_by_scope()` method.

```python
# In memory_types.py (or wherever MemoryType lives)
class MemoryType(str, Enum):
    FACT = "fact"
    CONVERSATION = "conversation"
    LESSON = "lesson"          # NEW

# Lesson-shaped metadata (stored in the memory_store entry's metadata dict):
{
    "lesson": "When generating FastAPI endpoints, include Pydantic request validation...",
    "role": "backend_engineer",        # or "*" for role-agnostic
    "task_type": "code_generation",    # or "*" for type-agnostic
    "tag": "fastapi",                  # optional free-text narrower scope
    "author_agent_id": "...",
    "author_run_id": "...",
    "uses": 0,
    "outcome_delta": null,             # avg(score_after) - avg(score_before)
    "last_used_at": null,
    "status": "active"                 # active | decayed | superseded
}
```

### Write path

At the end of a run, a new `memory_note_node` runs after the critic. It invokes a
short LLM call against the critic adapter asking: *"Given this run's output and
your score of N, is there a lesson worth writing for future runs? If so, give it
in ≤3 sentences scoped to (role, task_type)."*

**Gating condition: emit only when `critic_score < 85` AND
`critic_feedback` is non-empty.** Successful runs don't teach much; runs where the
critic itself had nothing concrete to say don't have a lesson to extract.

On a non-empty response, the node writes a `MemoryType.LESSON` entry via the
existing `memory_store.write()` interface.

### Read path (injection)

`heartbeat_context.py` builds the context block passed to the specialist. We add:

```python
lessons = memory_store.list_by_scope(
    role=agent_role,
    task_type=task_type,
    status="active",
    order_by="outcome_delta DESC",
    limit=5,
)
if lessons:
    context.append_block("## Lessons from past runs", render_lessons(lessons))
```

Each rendered lesson is tagged with its memory-store entry ID so the critic can
attribute outcome scoring back to specific lessons.

### Scoring

When a run finishes, for each lesson that was injected into it:

1. `uses += 1`
2. Update rolling `outcome_delta = avg(score_of_runs_with_this_lesson) - avg(baseline_score_for_scope)`
3. The baseline is computed lazily from the last K runs *without* any matching
   lesson injected.

### Decay

- After 10 uses, if `outcome_delta < 0`, set `status = decayed`. No more injection.
- When a new lesson supersedes an older one (same scope, newer, higher
  `outcome_delta`), mark the old one `status = superseded`.
- No hard TTL. Good lessons should live forever.

### Gates

None. Writing a `MemoryType.LESSON` is as gated as writing any other memory entry.

## Tier 1a — Skill refinement

Augments the existing `skill_generator.py` loop with one new path: trigger-initiated
refinement of existing skills.

### What exists today

`skill_generator.py` generates ephemeral skills on-demand when a task has no matching
skill. `skill_outcome_store.py` scores each use. Skills with avg score ≥ 85 over
repeated use are promoted from ephemeral → local tier. This loop runs on every
workflow.

### What changes

One new entry point on `skill_generator`:

```python
def refine(
    existing_skill: Skill,
    accumulated_feedback: list[SignalRecord],
) -> Skill:
    """Generate a v2 candidate for an existing skill that's scoring poorly."""
```

Called by `Tier1aBuilder` when the dispatcher sees a low-score cluster on a task
type that already has a skill. The candidate v2 enters an **A/B cooldown**:

- `skill_outcome_store` gains a `version` field on outcome records.
- A new `pick_active_version(skill_name)` method splits incoming uses between v1
  and v2 using a deterministic bucket (e.g. `hash(run_id) % 2`).
- After both versions hit K uses (default: 10), the higher-avg version wins. The
  loser is archived with `status=superseded_by=<v_winner_id>`.

### Storage

Unchanged: skills stay in `~/.vibe/skills/`. No repo commits. Local to the
install, which matches their existing lifecycle.

### Gates

The A/B cooldown *is* the gate. No new infrastructure.

## Tier 1b — Prompt overrides

A new repo-committed YAML file that `AdapterRegistry` reads at init. Overrides can
append (never replace) additional instructions to any of the 17 specialist adapter
prompts, scoped by `(adapter_type, task_type, tag)`.

### File location & format

`agents/prompt_library/overrides.yaml` (new file, empty initially):

```yaml
overrides:
  - id: override_01HZ...
    adapter_type: CODE                 # one of the 17 adapter types in adapters.py
    scope:
      task_type: code_generation       # optional narrower scope
      tag: fastapi                     # optional further narrowing
    append: |
      When generating FastAPI endpoints, always include Pydantic request
      validation (BaseModel for request bodies) and explicit response_model
      on route decorators.
    signal_refs: [sig_01HY..., sig_01HY...]
    author_agent_id: ...
    author_run_id: ...
    created_at: 2026-04-06T...
    status: active                     # active | decayed | superseded
```

### Append-only constraint (hard)

A Tier 1b override can only *add* text to the base prompt. It cannot:

- Replace the base prompt
- Delete safety clauses
- Override the agent's role identity
- Modify any existing override entry's `append` text (only `status` can change)

**Cost of this restriction:** if the base prompt in `adapters.py` has a wrong
instruction, Tier 1b can only counter-instruct it ("ignore the previous about X"),
not fix the source. Fixing the source is a Tier 2 `prompt_constant` edit, which has
its own stricter gate.

### Loading

`AdapterRegistry.__init__()` reads `overrides.yaml` once at startup and indexes by
`adapter_type`. `get_or_create(adapter_type, context)` looks up matching overrides
by scope and appends them to the base prompt in order of creation. Cache
invalidation is trivial because overrides are append-only — adding a new one only
matters for future `get_or_create` calls.

### Gates

None of these run the full pytest suite. Each is cheap:

1. **Schema validation** — YAML parses, required fields present, `adapter_type` is a
   known type from the existing list of 17, no `replace:` key, `append` ≤ 500 chars,
   `status` ∈ {active, decayed, superseded}.
2. **Append-only enforcement** — mechanical diff check against the current file:
   only allowed operations are "add a new override entry" or "change an existing
   entry's `status` field". Any modification of an existing `append` text → reject.
3. **Prompt-critic** — a dedicated LLM critic scores the proposed `append` on
   (a) specificity, (b) non-contradiction with the base prompt, (c) concreteness,
   (d) actionability. Threshold ≥ 80.
4. **Canonical smoke test** — load the override and run 1–3 canonical fixture
   tasks for that `adapter_type`, comparing scores to a stored baseline. Must not
   regress below baseline within tolerance. Fixtures live in
   `tests/canonical/{adapter_type}/` (new directory, seeded from real successful
   runs — see [Canonical Fixtures](#canonical-fixtures)).
5. **Bandit** — skipped. YAML has no Python.
6. **Full pytest** — skipped. An `overrides.yaml` change cannot break existing
   tests; the only runtime effect is extra text in a specialist's system prompt.

### Why this is safe to land without the full test suite

The only runtime effect of an override is that one specialist's system prompt gets
additional text appended. There's no code path change, no API change, no schema
change. Worst case: the model ignores the override (no effect) or follows it to a
worse answer — and the canonical smoke test is designed to catch that before
commit, and the outcome store will catch any regression on real runs after merge,
with a revert path via setting `status: decayed` (itself a Tier 1b operation).

### Commit & PR

On pass, the pipeline:

1. Commits the YAML change to `vibe/self-upgrade/prompt-override-{id}`.
2. Opens a PR assigned to the human user.
3. Files a companion Paperclip issue labeled
   `self-upgrade, auto-generated, tier-1b` with the branch name, commit hash,
   gate outputs, and PR URL in a structured YAML frontmatter block (see
   [Paperclip Integration](#paperclip-integration)).

## Tier 2 — Narrow typed code edits

This is the design-heaviest tier. The core idea: **agents don't propose "new file
content" — they propose a typed edit whose shape is mechanically verifiable**. The
pipeline proves the edit matches its claimed type via AST analysis, then runs a
gate scaled to that type. No more full-suite pytest runs for docstring changes.

### The 5 edit types

| Edit type | What it is | Allowlisted files |
|---|---|---|
| `prompt_constant` | Replace the value of a named string constant in adapters.py | `agents/adapters.py` only |
| `threshold_tweak` | Change the literal RHS of a single numeric/bool constant | `heuristic_critic.py`, `llm_retry.py`, `complexity_triage.py`, `parallel_subtasks.py` |
| `dict_list_append` | Add new entries to an existing list/dict literal | `task_type_registry.py`, `router_classification.py`, `tools/registry.py` |
| `docstring` | Replace the docstring of a named symbol | Any non-immutable file |
| `new_test_file` | Create a new test file under `tests/` | `tests/**` (non-existing path only) |

**Deliberately excluded:** new function additions, any code deletion, multi-file
edits, import additions, renames, any change to control flow. If an agent needs
any of these, it files a Tier 3 report.

### Proposal format

Replace the existing `UpgradeProposal(files=dict[path, str])` with a typed sum:

```python
@dataclass
class TypedEdit:
    edit_type: Literal[
        "prompt_constant", "threshold_tweak",
        "dict_list_append", "docstring", "new_test_file"
    ]
    target_path: str
    rationale: str
    signal_refs: list[str]                # contributing signals
    # Exactly one of these is populated based on edit_type:
    prompt_constant: PromptConstantEdit | None = None
    threshold: ThresholdEdit | None = None
    dict_list_append: DictListAppendEdit | None = None
    docstring: DocstringEdit | None = None
    new_test_file: NewTestFileEdit | None = None


@dataclass
class PromptConstantEdit:
    constant_name: str            # e.g. "CODE_SYSTEM_PROMPT"
    new_value: str


@dataclass
class ThresholdEdit:
    constant_name: str            # e.g. "POOR_SCORE_THRESHOLD"
    old_value: int | float | bool | str
    new_value: int | float | bool | str


@dataclass
class DictListAppendEdit:
    container_name: str           # e.g. "CODE_PATTERNS"
    new_entries: list             # each entry type-checked against existing entries


@dataclass
class DocstringEdit:
    symbol_path: str              # e.g. "agents.router.Router.classify"
    new_docstring: str


@dataclass
class NewTestFileEdit:
    test_file_path: str           # under tests/
    content: str
```

### AST verification — the mechanical contract

Before any gate runs, a `TypedEditValidator` loads the target file with `libcst`
and proves the proposed edit matches its claimed type:

- **`threshold_tweak`**: find the assignment to `constant_name`, verify the only
  change in the resulting diff is the RHS literal, verify `old_value` matches the
  current RHS, verify `new_value` is the same literal type as `old_value`. If the
  proposed edit touches anything else in the file — reject before any test runs.
- **`dict_list_append`**: find the named container, verify every existing entry is
  preserved byte-for-byte, verify the diff is purely insertions into the literal,
  verify new entries match the inferred element type.
- **`docstring`**: verify only docstring nodes changed and they're attached to the
  named symbol. Reject any non-docstring change.
- **`prompt_constant`**: verify only the RHS of the named constant changed. The
  RHS must remain a string literal.
- **`new_test_file`**: verify `target_path` is under `tests/`, doesn't exist, and
  the content parses as a valid Python module whose top-level statements are only
  imports and `test_*` function definitions (no top-level side effects).

The core of why Tier 2 is trustworthy: we don't ask *"is this file content good?"*
— we ask *"is the agent's claimed edit actually what it's doing?"* A malicious or
confused proposal that claims `edit_type=docstring` but tries to change control
flow gets rejected before any test runs.

### Gates per type

| Edit type | Gates (in order) |
|---|---|
| `prompt_constant` | Schema → AST verify → prompt-critic ≥ 80 → canonical smoke for adapter_type → bandit on file |
| `threshold_tweak` | Schema → AST verify → targeted pytest (`tests/test_{module}.py`) → **signal replay** → bandit on file |
| `dict_list_append` | Schema → AST verify → targeted pytest → backward-compat check |
| `docstring` | Schema → AST verify → ruff + black only. No test run. |
| `new_test_file` | Schema → path validation → run the new file in isolation → ruff + black |

**Signal replay** (new infrastructure, `threshold_tweak` only): re-run
`self_upgrade_trigger.analyse()` over the last 30 days of persisted signals using
the new threshold and compare the resulting signal counts / classifications to
the current threshold. The new value must not have produced a materially worse
classification rate on historical data (e.g. not more than 2× the signal volume
or fewer than half). This is the thing that turns a threshold tweak from "the
agent's reasoning" into a measurable improvement.

**Backward-compat check** (`dict_list_append` only): after applying the append,
re-run the registry's lookup for every existing entry and assert identical
resolution. This catches cases where a new regex shadows an existing one.

### Expanded "Never" set (enforced pre-validation)

A proposed edit targeting any of the following is rejected before AST verification
regardless of edit type:

- **Current immutables** — `agents/self_upgrade.py`, `agents/self_upgrade_trigger.py`,
  `agents/skill_security.py`, `agents/config.py`, `.env`, `.env.example`
- **Workflow core** — `graph.py`, `graph_engine.py`, `graph_runners.py`,
  `graph_nodes.py`, `nodes.py`, `state.py`, `specialist_nodes.py`, `output_nodes.py`
- **LLM plumbing** — `llm_backend.py`, `backend_pool.py`, `vibe/backends/*`.
  (Note: `llm_retry.py` is deliberately *not* in the Never list — it's
  allowlisted for the `threshold_tweak` edit type only, since constant-only
  tweaks to retry counts are low-risk. No other edit type can touch it because
  no other edit type has it in its allowlist.)
- **Storage** — `agents/storage/*`, `message_store.py`, `memory_store.py`,
  `artifact_store.py`, `spending_tracker.py`, `session_store.py`, `embedder.py`
- **Heartbeat** — `heartbeat.py`, `heartbeat_context.py`, `heartbeat_progress.py`,
  `heartbeat_signals.py`, `heartbeat_spending.py`, `heartbeat_formatting.py`,
  `workflow_factory.py`
- **Skill subsystem plumbing** — `skill_registry*.py`, `skill_loader.py`,
  `skill_generator.py`, `skill_outcome_store.py`, `skill_cleanup.py`,
  `skill_search.py`, `skill_remote.py`
- **External clients** — `paperclip_client.py`, `ws_client.py`, `messenger_client.py`,
  `api_key_manager.py`
- **Sandbox** — `agents/sandbox/*`
- **Resource layer** — `resource_discovery.py`, `resource_allocator.py`
- **Orchestrator + main** — `main.py`, `orchestrator.py`, `daemon.py`,
  `cancellation.py`, `intent_classifier.py`

The logic: if a broken change would (a) brick the heartbeat loop, (b) corrupt
persistent data, (c) silently give the agent more authority, or (d) require a
running stack to even test — it's out.

### Commit & PR

On pass, the pipeline:

1. Commits the typed edit to `vibe/self-upgrade/{edit_type}-{id}`.
2. Commit message auto-generated from `rationale` + `signal_refs`.
3. Opens a PR assigned to the human user.
4. Files a companion Paperclip issue labeled
   `self-upgrade, auto-generated, tier-2, edit-type:{kind}` with branch, commit,
   gate outputs, and PR URL in a structured YAML frontmatter block.

## Tier 3 — Issue reports (no code)

When signals point to something structural that isn't in any Tier 2 edit type — or
when the agent's own confidence is low — the builder files a Paperclip issue
instead of attempting a diff. No code, no commit, no branch.

### When the dispatcher picks Tier 3

- Signal pattern doesn't map to any of the 5 Tier 2 edit types
- Target file is in the "Never" set
- A higher-tier builder returned `LowConfidence` and the dispatcher fell
  back (see [Dispatch logic](#dispatch-logic))
- A prior Tier 2 attempt for the same signal cluster failed its gate
- Anything the classifier's fallback rule catches

### Report data model

```python
@dataclass
class IssueReport:
    report_id: str                          # ulid
    title: str                              # short, PR-style
    signal_refs: list[str]                  # contributing signal ids
    evidence: list[EvidenceRow]
    hypothesis: str                         # agent's root-cause explanation
    suggested_change: str                   # prose, not code
    suggested_change_kind: Literal[
        "code", "config", "infra", "prompt", "data", "external"
    ]
    confidence: float                       # 0.0-1.0
    author_agent_id: str
    author_role: str
    created_at: str


@dataclass
class EvidenceRow:
    run_id: str
    task_type: str
    score: int
    excerpt: str                            # <= 500 chars
```

### Filing path

1. `Tier3Builder` drafts the report by invoking the critic adapter with the
   accumulated signals as context.
2. A one-shot **self-critique** pass scores the draft on (evidence quality,
   clarity, specificity). Below 70 → the report is dropped and the signals remain
   accumulating for another try on the next heartbeat. This prevents garbage
   reports from flooding the Improvements tab.
3. On self-critique pass, `PaperclipClient.create_issue()` files the issue.
   After the Milestone 1 client extension (see [Paperclip Integration](#paperclip-integration)),
   the call looks like:

```python
paperclip_client.create_issue(
    title=f"[self-report] {report.title}",
    description=render_report(report),
    priority="medium",
    labels=[
        "self-upgrade", "auto-generated", "tier-3",
        f"kind:{report.suggested_change_kind}",
    ],
    assignee_user_id=config.human_triage_user_id,  # new param, Milestone 1
    # NB: assignee_agent_id is intentionally NOT set - no agent picks this up.
    # company_id is bound at client init, not passed per-call.
)
```

**`assignee_user_id` — not `assignee_agent_id` — is what prevents the issue from
being auto-processed.** Setting it to the human user means no agent tries to pick
it up regardless of labels, which cleanly bypasses the server-side auto-routing at
`paperclip/server/src/services/heartbeat.ts:1362` that would otherwise stamp
`taskType = "self_upgrade"` on the issue.

4. On successful filing, the contributing signals are updated in
   `upgrade_signals.jsonl` with `artifact_ref=<report_id>`. The dispatcher skips
   already-referenced signals on future runs.
5. If the issue is resolved in Paperclip (status → `done`), the dispatcher's next
   pass clears the signals entirely. Closed loop.

### Gates

Schema validation only. The content quality comes from the self-critique step,
not from tests.

### Why Tier 3 matters

Right now, when an agent notices something it can't patch, the observation is
lost. The agent exits; the signal either accumulates anonymously in
`upgrade_signals.jsonl` or disappears entirely. Tier 3 is the honest escalation
path: it captures observations as durable, triageable work items without forcing
the agent to pretend it knows how to write the fix. Even if *no* Tier 2 edit ever
fires, Tier 3 alone would be the biggest improvement to the feedback loop.

## Trigger rewiring: dispatcher model

`self_upgrade_trigger.py` is refactored from "one pipeline, one proposal type"
into a **dispatcher** that routes accumulated signals to the right tier based on
signal shape.

### New signal format

`upgrade_signals.jsonl` entries gain two new fields:

```json
{
  "id": "sig_01HZ...",                    // NEW - ulid
  "artifact_ref": null,                    // NEW - set when dispatched
  "category": "low_score",
  "task_type": "code_generation",
  "detail": "Score 40/100...",
  "score": 40,
  "source_node": "critic",
  "timestamp": "2026-..."
}
```

When a tier produces an artifact, contributing signals are rewritten with
`artifact_ref` populated. The dispatcher skips already-referenced signals on
subsequent runs. This fixes a current latent bug: signals accumulate forever and
would re-fire every heartbeat once agents start running.

### Stale signal migration

The existing 170 entries in `~/.vibe/skills/upgrade_signals.jsonl` are
unambiguously test data: all identical `low_score` / `score 40` / empty-feedback
records from a single March 28 burst. **The migration wipes the file.** A new
empty file is created on first dispatcher run.

Rationale for wipe over backfill: these signals have zero diagnostic value (empty
feedback strings mean the critic literally had nothing to say), and backfilling
them would either produce one noisy Tier 3 report on first run or require
per-entry triage to classify. A clean slate matches the first-real-run state we
want.

### Dispatch logic

```
analyse(state) → signals accumulate → threshold met →
  classify_signals(signals) → pick tier → route to tier-specific builder →
  builder produces artifact → tier-specific pipeline validates →
  signal housekeeping (mark or clear)
```

**Classification rules** (evaluated in order, first match wins; no LLM call
inside the classifier itself — LLM work happens inside tier builders):

| Signal pattern | Tier |
|---|---|
| Actionable critic feedback string on a single task, no matching prior signals | **0** (memory note) |
| Repeated same critic_pattern, same task_type, with actionable feedback | **1b** (prompt override) |
| Low_score cluster on task_type with an existing skill scoring poorly | **1a** (skill refinement) |
| Low_score cluster on task_type with no matching skill | **1a** (new ephemeral skill via existing path) |
| Threshold-shaped signal (e.g. retry count maxed consistently, score-threshold mismatch) | **2** (`threshold_tweak`) |
| New task pattern not matching any registry entry | **2** (`dict_list_append` on `task_type_registry.py`) |
| Tool failures with a clear parameter-level fix | **2** (`prompt_constant` edit warning the specialist about the tool quirk) |
| Target in Never set OR nothing else matches | **3** (report) |

**Fallback to Tier 3 from builders.** The classifier is deliberately heuristic
and has no notion of confidence. However, any *builder* (Tier 0 through Tier 2)
may return a `LowConfidence` result from its `build()` method — for example,
`Tier2Builder` may classify a signal cluster as `threshold_tweak` but the LLM
called inside the builder may fail to produce a well-formed `TypedEdit`. In
that case, the dispatcher re-routes the signal cluster to `Tier3Builder`,
which always succeeds (worst case: the report fails its self-critique ≥ 70
gate and the signals stay accumulating for another try). This keeps the
classifier cheap and pushes the confidence decision into the tier that
actually did the LLM work.

### Builders

One builder per tier, each small and independently testable:

- `Tier0Builder.build(signals) -> MemoryNote`
- `Tier1aBuilder.build(signals) -> SkillRefinementRequest`
- `Tier1bBuilder.build(signals) -> PromptOverride`
- `Tier2Builder.build(signals) -> TypedEdit`
- `Tier3Builder.build(signals) -> IssueReport`

Each builder is where the LLM work happens for that tier. Swappable one at a time
as the tiers ship.

### DispatchResult

```python
DispatchResult = (
    Tier0Written(note_id)
    | Tier1aQueued(refinement_id, cooldown_until)
    | Tier1bCommitted(branch, commit, pr_url, issue_id)
    | Tier2Committed(branch, commit, pr_url, issue_id, edit_type)
    | Tier3Filed(issue_id)
    | Rejected(reason, signal_refs)
)
```

The heartbeat logs the result and posts a Paperclip comment on the originating
run so the activity is visible in-UI.

### What happens to `self_upgrade.py`

The existing `SelfUpgradePipeline` becomes `Tier2Pipeline` and is narrowed to
handle only `TypedEdit` proposals. The old `UpgradeProposal(files=dict)` interface
is removed entirely. Kept-and-reused: `IMMUTABLE_PATHS` (expanded), `MAX_DIFF_LINES`,
`_run_tests` (now called only for edit types that need it), `_run_bandit`
(similar), `_apply_and_commit`, `_generate_diff_text`.

`self_upgrade.py` itself remains in the immutable list — agents cannot modify the
pipeline that validates them.

## Paperclip integration

### The Improvements tab already exists

`paperclip/ui/src/pages/Improvements.tsx` filters all company issues by label:

```ts
const IMPROVEMENT_LABEL_NAMES = new Set(["self-upgrade", "auto-generated"]);
```

Any issue carrying either label appears in the tab. Reuses the existing
`IssuesList` component, live-updates via the WebSocket provider. **Zero new UI
work required.**

### The client partially supports what we need

`agents/paperclip_client.py` already accepts `labels: Optional[List[str]]` on
both `create_issue` (line 483) and `update_issue` (line 423). The company id is
bound to the client instance at init time (used inside `create_issue` at line
495), so the filing path does not need to pass `company_id` explicitly.

**One client extension is required for Milestone 1:** the current
`create_issue(title, description, priority, labels)` signature does not support
`assignee_user_id`. Tier 3 (and Tier 1b/Tier 2 companion issues) rely on
assigning issues to a human user rather than an agent, so `create_issue` must
be extended to accept `assignee_user_id: Optional[str]`. This also requires
verifying the Paperclip server's `POST /api/companies/{companyId}/issues`
endpoint accepts `assigneeUserId` in the request body — if it doesn't, the
server-side endpoint must be extended first. Both changes are in-scope for
Milestone 1.

### Label conventions (for all tiers that file issues)

All Paperclip issues produced by the dispatcher use these labels:

- `self-upgrade` — canonical "this issue was produced by the self-upgrade system"
- `auto-generated` — canonical "not hand-written"
- `tier-{0|1a|1b|2|3}` — which tier produced it
- `kind:{code|config|infra|prompt|data|external}` — Tier 3 only, from
  `suggested_change_kind`
- `edit-type:{prompt_constant|threshold_tweak|dict_list_append|docstring|new_test_file}` — Tier 2 only

### Assignment convention (all tiers, all code-producing tiers)

**All Paperclip issues produced by the dispatcher are assigned to the human
user**, never to an agent. This is the sole mechanism by which we prevent
auto-processing:

- No agent picks the issue up (because no `assignee_agent_id`)
- Server-side label → taskType routing at `heartbeat.ts:1362` is irrelevant (no
  agent is running)
- The human reviews, merges, or closes by hand

### Companion issue body format (Tier 1b and Tier 2)

Both tiers file a companion issue that links to the PR. The description includes a
structured YAML frontmatter block so that *if* we ever want to integrate with the
DeerFlow `self_upgrade_agent` later (see [Future Work](#future-work)), the parse
path already exists:

```markdown
---
branch: vibe/self-upgrade/threshold-tweak-01HZ...
commit: abc123...
edit_type: threshold_tweak
gates:
  schema: passed
  ast_verify: passed
  targeted_pytest: passed
  signal_replay: passed
  bandit: passed
pr_url: https://github.com/tmartin2113/Vibe-Stack/pull/...
signal_refs:
  - sig_01HY...
  - sig_01HY...
---

## What changed

`POOR_SCORE_THRESHOLD` in `agents/self_upgrade_trigger.py` lowered from 60 to 55...

## Rationale

...

## Gate outputs

<truncated pytest output, signal replay results, etc.>
```

## Canonical fixtures

Tier 1b's smoke test requires a small set of canonical fixture tasks per
adapter type, with stored baseline scores to compare against.

### Structure

```
tests/canonical/
├── CODE/
│   ├── task_001.json          # {prompt, expected_keywords, baseline_score}
│   ├── task_002.json
│   └── baseline.json          # rolling baseline scores per task
├── RESEARCH/
│   └── ...
├── CRITIC/
│   └── ...
└── ... (17 adapter types total)
```

### Seeding strategy

**Do not hand-author canonical fixtures up front.** Instead, the first 5–10 real
agent runs per adapter type are automatically captured by a new
`canonical_harvester` hook that stores high-scoring runs as fixtures. Once a
fixture set exists for an adapter type, Tier 1b can propose overrides for that
type. Until then, Tier 1b proposals for that type fall back to Tier 3 (report
only).

This ties Tier 1b capability directly to actual usage: types the agents are
exercising get fixtures, types they aren't don't — which is the same ordering we
care about for self-upgrade priority.

## Milestones

The design above covers all five tiers. The **first implementation plan should
only cover Milestone 0 + Milestone 1.** Everything else is documented here for
future planning, not for immediate build.

### Milestone -1 (prerequisite, not in this spec)

Fix the outstanding container-config issues blocking agents from running real
workflows: missing instruction files, `PAPERCLIP_AGENT_ID` not set for the `vibe`
container, Claude Code credentials not persisting across container recreation.
Until this is done, the dispatcher has no real signals to work with. This is its
own separate spec.

### Milestone 0 — Dispatcher rewiring (no new tiers active)

**Scope:** refactor `self_upgrade_trigger.py` into a dispatcher, rename
`self_upgrade.py` → `Tier2Pipeline` (kept dormant), add signal `id` and
`artifact_ref` fields, wipe stale signals, wire the heartbeat to call the
dispatcher. All accumulated signals default-classify to Tier 3 but there's no
Tier 3 builder yet — the dispatcher logs a stub result and takes no action.

**Demonstrable artifact:** first real agent run producing a log entry showing
"dispatched N signals to Tier 3 (stub)". No Paperclip issue, no memory note, no
commit. This milestone proves the wiring.

**Code surface:**

- Refactor `agents/self_upgrade_trigger.py` into the dispatcher (signal
  accumulator + classifier). Preserve file name so the immutable-list entry
  still matches.
- Narrow `agents/self_upgrade.py` to a `Tier2Pipeline` class that only handles
  `TypedEdit` inputs. Keep the file name. Remove the old
  `UpgradeProposal(files=dict)` interface entirely. Pipeline stays dormant in
  Milestone 0 — no Tier 2 proposals are generated yet.
- New `agents/self_upgrade_dispatcher.py` (routes classified signals to tier
  builders; Milestone 0 ships with only a stub Tier 3 path)
- Edit `agents/heartbeat.py` to call the dispatcher instead of the old pipeline
- Migration script: wipe `~/.vibe/skills/upgrade_signals.jsonl`
- Tests for dispatcher, classifier, signal housekeeping, migration

### Milestone 1 — Tier 0 + Tier 3 (the real MVP)

**Scope:** the two tiers that require no new gates and no repo commits. Memory
notes and issue reports. After this ships, the system is genuinely more useful to
run day-to-day.

**Demonstrable artifacts:**

- Memory notes accumulate in `memory_store` with `MemoryType.LESSON`
- `heartbeat_context.py` injects matching lessons into specialist context
- Paperclip issues tagged `[self-report]` appear in the Improvements tab with
  structured evidence
- Outcome-delta scoring begins populating on memory notes after a few runs

**Code surface:**

- New `agents/memory_note_node.py` — workflow node that runs after critic
- Edit `agents/critic_nodes.py` — optionally emit memory notes when
  `critic_score < 85 AND critic_feedback != ""`
- Edit `agents/graph.py` / `nodes.py` — wire the memory note node into the graph
- Edit `agents/memory_store.py` — add `MemoryType.LESSON`, `list_by_scope()`,
  `outcome_delta` scoring helpers
- Edit `agents/heartbeat_context.py` — load and inject matching lessons
- New `agents/self_upgrade/tier0_builder.py`
- New `agents/self_upgrade/tier3_builder.py`
- Edit `agents/paperclip_client.py` — extend `create_issue` to accept
  `assignee_user_id: Optional[str]`, pass through as `assigneeUserId` in the
  request body. Verify the Paperclip server endpoint
  `POST /api/companies/{companyId}/issues` accepts this field; if not, extend
  the server endpoint first (separate Paperclip PR before this milestone can
  land).
- Add `VIBE_HUMAN_TRIAGE_USER_ID` env var + matching `SystemConfig` field,
  consumed by `Tier3Builder` as the assignee target.
- Tests for builder, injection, scoring, filing, self-critique gate

### Milestone 2 — Tier 1a skill refinement (future)

Adds `skill_generator.refine()`, A/B cooldown in `skill_outcome_store`,
`Tier1aBuilder`.

### Milestone 3 — Tier 1b prompt overrides (future)

Adds `agents/prompt_library/overrides.yaml`, `AdapterRegistry` loader, append-only
enforcement, prompt-critic, `Tier1bPipeline`, canonical fixture harvester,
smoke-test infrastructure.

### Milestone 4 — Tier 2 typed edits (future, staged)

Adds `TypedEdit` dataclasses, `TypedEditValidator` (libcst), per-type gates.
Rolled out one edit type at a time in order of increasing risk:

1. `docstring` (lint-only gate, trivial)
2. `new_test_file` (isolated test run)
3. `dict_list_append` (AST + backward-compat)
4. `threshold_tweak` (AST + signal replay + targeted pytest)
5. `prompt_constant` (AST + prompt-critic + canonical smoke)

Adds `libcst` to `requirements-production.lock`.

## Future work (out of scope for this spec)

### DeerFlow `self_upgrade_agent` integration

`paperclip/deerflow/backend/src/subagents/builtins/self_upgrade_agent.py` is a
pre-built DeerFlow subagent specifically designed to handle self-upgrade tasks.
Key properties:

- **It's an *applier*, not a *proposer*.** Its system prompt expects the task to
  already contain branch name, commit hash, and validation gate results.
- **Its expected workflow** is: read issue → check out branch → run validation
  suite → report pass/fail. It does not generate proposals and it does not merge.
- **It has `disallowed_tools=["task", "ask_clarification", "present_files"]`** —
  no recursive delegation, no clarification loop, no file surfacing.
- **20-minute timeout, 40 max turns** — tuned specifically for "check out a
  branch, run the suite, report."

This subagent is the natural home for a **second-pass validator** on Tier 1b / Tier
2 proposals: our Python pipeline runs first-pass gates in its local process, then
this subagent could re-run validation in an isolated DeerFlow sandbox with a
different test environment. Double validation before a human merge.

**Out of scope for this spec** because (a) the MVP is explicitly "assign to human,
human merges by hand," (b) integrating the subagent requires building a
proposer → issue → agent → subagent routing path that adds complexity without
unlocking new functionality for the human reviewer, and (c) the issue body format
specified in [Paperclip Integration](#paperclip-integration) is already designed
to be subagent-parseable, so enabling this later is a config flip rather than a
refactor.

A future spec can enable this as an optional second-pass gate behind a config
flag like `VIBE_SELF_UPGRADE_DOUBLE_VALIDATE=true`.

### Cross-install skill sharing

Skills written by Tier 1a stay in `~/.vibe/skills/` per-install. A future spec
could add a mechanism to promote well-performing skills (high `uses`, high
`outcome_delta`) from local to an approved tier in the repo, making them
shareable across installs. This is deliberately out of scope here because
skill performance is heavily dependent on local model choice and hardware.

### Memory note promotion to prompt overrides

A memory note with very high `outcome_delta` over many uses is functionally
equivalent to a prompt override — both are "inject this text into a specialist's
context." A future enhancement could automatically promote high-performing
memory notes into prompt override proposals, bridging Tier 0 and Tier 1b. Not in
scope for this spec because it adds a cross-tier dependency that isn't needed
for either tier to work independently.

## Open questions

None at time of writing. All decisions from the brainstorming session are
captured above.

## Appendix: Decisions summary

| Decision | Value |
|---|---|
| Scope model | Tiered frame with MVP inside |
| Tier 1 paths | Skills (runtime, `~/.vibe/`) + prompt overrides (repo-committed YAML) |
| Tier 1b constraint | Append-only (hard) |
| Tier 1b smoke test | Required (canonical fixtures) |
| Memory note emission | `critic_score < 85 AND critic_feedback != ""` |
| Memory note store | Reuse existing `memory_store` with new `MemoryType.LESSON` |
| Memory note author | Critic (not specialist) |
| Tier 2 edit types | `prompt_constant`, `threshold_tweak`, `dict_list_append`, `docstring`, `new_test_file` |
| Tier 2 gate model | Per-edit-type, not full suite |
| Tier 2 AST library | `libcst` |
| Tier 3 assignee | Human user (never agent) |
| Tier 1b/Tier 2 assignee | Human user (Option A bypass) |
| Stale signal migration | Wipe |
| DeerFlow subagent integration | Out of scope, future work only |
| First implementation scope | Milestone 0 + Milestone 1 |
