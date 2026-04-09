# Tier 1a — Skill Refinement via Versioned A/B Cooldown

**Status:** Draft
**Date:** 2026-04-08
**Author:** prime + Claude (brainstorming session)

## Problem

`agents/skill_cleanup.py` currently runs an auto-refinement path: whenever a
skill scores below `REFINEMENT_THRESHOLD` (70) with non-empty feedback, it
calls `SkillGeneratorNode.refine_skill()`, which **overwrites `SKILL.md` in
place**. There is no version history, no A/B test, no audit trail, no record
of what changed between "the skill the agent used yesterday" and "the skill
the agent will use today." The next workflow run silently gets the rewritten
skill content.

This is the bug the original self-upgrade feasibility spec
(`2026-04-06-self-upgrade-feasibility-design.md`) identified as Tier 1a: skill
refinement should be **dispatcher-gated, versioned, and A/B-tested** before a
refinement is treated as a winner. The dispatcher and tier-routing
infrastructure shipped in M0 + M1 (merged in #37), but Tier 1a itself stayed
as a TODO in the classifier (`self_upgrade_dispatcher.py:136`) because the M0
+ M1 scope was explicitly limited to Tier 0 (memory notes) and Tier 3 (issue
reports).

This spec narrows the original Tier 1a design to something shippable as its
own discrete feature: **A/B versioning for skill refinements, scoped to
per-install `~/.vibe/skills/` only, with no repo commits, no PR flow, and no
dependency on Tier 1b/2 infrastructure.** It intentionally drops the spec's
"Tier 1a also creates new skills" rule because the workflow graph already
does that via the existing `skill_generator` call on capability gap.

## Goal

Replace the current "rewrite-in-place on every low score" loop with a
**dispatcher-gated, versioned, deterministically-routed A/B cooldown**. A v2
candidate is drafted only when a signal cluster (≥3 non-empty signals on the
same task_type) indicates the skill is genuinely underperforming, not on
every individual low score. v1 and v2 coexist on disk as sibling directories.
Every workflow run deterministically buckets to one version via
`sha256(session_id) % 2`. After K=20 total outcomes (10 per version), the
higher-avg version wins, the loser is archived, and the winner takes the
canonical name.

### Success criteria

- `skill_cleanup.record_skill_outcomes` no longer calls `refine_skill`. The
  in-place rewrite path is deleted entirely.
- A single LLM refinement call produces exactly one new `__v2` sibling
  directory, with the original left untouched.
- Every workflow run deterministically picks the same version for the same
  `session_id`, regardless of which Python process runs it.
- After 20 recorded outcomes (10 per version), one winner is picked, the
  loser is moved to `~/.vibe/skills/archive/{name}__superseded_{YYYYMMDD}/`,
  and the winner resumes the canonical skill name.
- All A/B logic is unit-testable without a running workflow, without an LLM,
  and without Paperclip.
- The existing `skill_registry`, `skill_loader`, and `skill_outcome_store`
  behavior for single-version skills is unchanged (backwards compatible by
  construction).
- A reviewer can audit the A/B mechanics by reading exactly one new file
  (`agents/skill_ab.py`) and one new builder (`agents/self_upgrade/tier1a_builder.py`).

### Non-goals

- Creating *new* skills from signal clusters. New skill creation already runs
  today inside the workflow graph when the router detects a capability gap —
  Tier 1a explicitly does not duplicate that path. Signal clusters with no
  matching skill fall through to Tier 3 (issue report), which is what the
  dispatcher already does for unclassified clusters.
- A/B testing more than two versions at once. If `__v2` already exists for a
  skill, Tier1aBuilder returns `LowConfidence("A/B in progress")` and the
  dispatcher falls through to Tier 3. No nested or concurrent experiments.
- Cross-install skill sharing. Versioned skills remain in local
  `~/.vibe/skills/` — never committed to the repo.
- Updating the signal_name/skill_name relationship in `skill_outcome_store`.
  The outcome store already keys by `skill_name`; we use the versioned name
  (e.g. `myCodeSkill__v2`) as the skill_name directly, so no schema change.
- Tier 1b prompt overrides and Tier 2 typed code edits. Those are separate
  specs.
- Changes to `skill_registry.py` beyond the minimum necessary to
  register/unregister a versioned sibling directory.

## Architecture overview

Tier 1a consists of six affected modules — two new, four modified — plus one
new test file per new module. The dispatcher classifier gains one rule. The
workflow runtime gets a six-line change to the skill loader. All other
changes are additive or deletions.

```
Workflow finishes
        │
        ▼
signal accumulator records low-score signals
        │
        ▼
Dispatcher threshold met → classify_signals()
        │
        ▼  (new rule: ≥3 non-empty signals on same task_type → Tier.ONE_A)
        ▼
SelfUpgradeDispatcher._handle_tier1a(signals)
        │
        ▼
Tier1aBuilder.build(signals)
        │
        ├─ resolve matching skill via skill_registry
        │     (no match? → LowConfidence → dispatcher falls through to Tier 3)
        │
        ├─ check skill has ≥ 1 recorded outcome in skill_outcome_store
        │     (none? → LowConfidence → Tier 3)
        │
        ├─ check skill does not already have __v2 sibling
        │     (exists? → LowConfidence "A/B in progress" → Tier 3)
        │
        ├─ skill_generator.draft_refined_content(...)  ← pure function, returns str
        │
        ├─ skill_ab.write_candidate(base, version=2, content=refined)
        │     ← writes SKILL.md + metadata.json
        │     ← updates integrity hash via skill_registry.security
        │     ← registers new sibling in skill_registry
        │
        └─ return Tier1aResult.CandidateWritten(skill_name, v2_path, signal_refs)
                │
                ▼
        DispatchResult.Tier1aQueued(refinement_id, signal_refs)
```

**Read path (every workflow run that loads a skill):**

```
specialist node → skill_loader.load_skills(state, registry)
        │
        ▼
(existing discovery finds matching skill path by task_type)
        │
        ▼
NEW: skill_ab.list_versions_for(base_name, skills_root=skill_path.parent)
        │
        ├─ 1 version → use discovered path (today's behavior, unchanged)
        │
        └─ ≥2 versions → skill_ab.pick_active_version(
                              candidates, run_input=state["session_id"])
                          │
                          ▼
                    sha256(session_id).digest()[0] % 2 → 0 or 1
                          │
                          ▼
                    returns candidates[bucket]
        │
        ▼
state["loaded_skills"] entry carries the VERSIONED name
(either "myCodeSkill" or "myCodeSkill__v2") so downstream
outcome recording is naturally version-aware without any
separate plumbing.
```

**Promotion path (end of workflow, inside skill_cleanup):**

```
skill_cleanup.record_skill_outcomes(state, ...)
        │
        ├─ (existing) for each skill used in run:
        │     outcome_store.record(skill_name, score, ...)
        │     ↑ skill_name here is the versioned name from state
        │
        └─ NEW: skill_ab.maybe_promote_winners(
                    skill_names_in_run,
                    outcome_store,
                    skills_root,
                    registry,
                    K_per_version=10,
                )
                │
                ▼
        For each base_name with ≥2 versions:
            count outcomes per version
            if all versions < 10 outcomes → noop
            if all versions ≥ 10 outcomes:
                winner = version with higher avg(score) (ties → v1)
                loser  = the other
                skill_ab.archive_loser(loser_dir, superseded_by=winner)
                if winner is a __v{N} directory:
                    skill_ab.rename_winner_to_base(winner_dir, registry)
```

## Components

### `agents/self_upgrade/tier1a_builder.py` (new)

Dispatcher-called builder that drafts a v2 candidate for an underperforming
skill. Pure-ish: one LLM call (via `skill_generator.draft_refined_content`),
one filesystem write, one registry update. No network except the LLM.

```python
class Tier1aResult:
    """Tagged union returned by Tier1aBuilder.build()."""

    @dataclass
    class CandidateWritten:
        skill_name: str        # base name, e.g. "myCodeSkill"
        v2_path: Path          # absolute path to the new __v2 directory
        signal_refs: List[str]

    @dataclass
    class LowConfidence:
        reason: str            # specific reason string, used in dispatcher logs
        signal_refs: List[str]

    AnyResult = Union[
        "Tier1aResult.CandidateWritten",
        "Tier1aResult.LowConfidence",
    ]


class Tier1aBuilder:
    """Drafts a v2 refinement candidate for an underperforming skill."""

    def __init__(
        self,
        *,
        skill_generator: "SkillGeneratorNode",
        skill_registry: "SkillRegistry",
        outcome_store: "SkillOutcomeStore",
        skills_root: Path,
    ) -> None: ...

    def build(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str = "",
        author_run_id: str = "",
    ) -> "Tier1aResult.AnyResult":
        """Draft a v2 candidate from a signal cluster.

        Steps:
        1. Determine the target task_type from signals (first signal wins; all
           signals in a Tier 1a cluster share a task_type by classifier rule).
        2. Resolve the matching skill via skill_registry. No match →
           LowConfidence("no matching skill").
        3. Check outcome_store has ≥1 recorded outcome for the base skill
           name. None → LowConfidence("no recorded outcomes").
        4. Check skill_ab.list_versions_for(base) returns only 1 version. If
           a __v2 already exists → LowConfidence("A/B in progress").
        5. Call skill_generator.draft_refined_content(...) with aggregated
           feedback from the signal cluster. Empty result →
           LowConfidence("draft_refined_content returned empty").
        6. Write to the __v2 sibling directory, update integrity hash,
           register with skill_registry.
        7. Return Tier1aResult.CandidateWritten with the signal refs.
        """
```

**Invariants:**

- `build()` never modifies the existing v1 skill content.
- Every `LowConfidence` branch returns a specific reason string, used in
  dispatcher logs and tests.
- Never writes to `~/.vibe/skills/archive/`. Archival is exclusively owned
  by `skill_ab.archive_loser`.
- Feedback aggregation for the LLM prompt concatenates distinct critic
  feedback strings from the signal cluster, deduped (exact string match)
  and truncated to a hard 3000-character cap before being passed to
  `draft_refined_content`.

### `agents/skill_ab.py` (new)

Pure A/B versioning logic. Single source of truth for the `__v{N}` naming
convention, deterministic bucketing, promotion, and archival. No LLM, no
Paperclip, no network.

```python
"""A/B versioning for skill refinements.

All functions are deterministic given their inputs. Testable without a
running workflow.
"""

VERSION_SUFFIX_RE = re.compile(r"^(?P<base>.+?)__v(?P<version>\d+)$")


def is_versioned_name(name: str) -> bool: ...
def base_name(name: str) -> str: ...                    # "x__v2" → "x"
def versioned_name(base: str, version: int) -> str: ... # ("x", 2) → "x__v2"


def list_versions_for(
    base: str,
    *,
    skills_root: Path,
) -> List[Path]:
    """Return all version directories for a base name, sorted by version
    number ascending. The base directory is treated as version 1.

    Returns an empty list if no directories match.
    Never returns entries under the archive/ subdirectory.
    """


def bucket_for_run(run_input: str, num_buckets: int = 2) -> int:
    """sha256(run_input).digest()[0] % num_buckets. Deterministic.

    Does NOT use Python's built-in hash() — PEP 456 process randomization
    would break cross-process determinism.
    """


def pick_active_version(
    candidates: List[Path],
    *,
    run_input: str,
) -> Path:
    """Given ≥1 version directories, pick one deterministically.

    Invariant: same run_input + same candidate list → same result, always.
    Called by skill_loader. Never mutates the filesystem.

    If candidates has only 1 entry, returns it unchanged (no bucketing).
    If run_input is empty, returns candidates[0] (stable fallback for missing
    session_id — keeps the loader from breaking on edge cases).
    """


def write_candidate(
    base: str,
    *,
    version: int,
    content: str,
    parent_dir: Path,
    skill_registry: "SkillRegistry",
) -> Path:
    """Write a new __v{N} sibling directory and register it.

    Writes SKILL.md and a minimal metadata.json. Calls
    skill_registry.security.store_integrity_hash(versioned_name, content)
    and skill_registry._save_index() to make the new sibling discoverable.

    Raises if the target directory already exists.
    """


@dataclass
class PromotionResult:
    base_name: str
    winner_version: int
    loser_version: int
    winner_avg: float
    loser_avg: float
    archived_to: Path


def maybe_promote_winners(
    skill_names_in_run: List[str],
    outcome_store: "SkillOutcomeStore",
    *,
    skills_root: Path,
    skill_registry: "SkillRegistry",
    K_per_version: int = 10,
) -> List[PromotionResult]:
    """For each base name in skill_names_in_run that has ≥2 versions on disk,
    check whether all versions have at least K_per_version recorded outcomes.

    If yes: pick the winner (higher avg, ties → earlier version), archive
    the others via archive_loser, and if the winner is a __v{N} directory,
    rename it back to the base name via rename_winner_to_base.

    If no: return [] for that base name.

    Returns a list of PromotionResult records, one per skill promoted.
    Side effects: filesystem moves, registry index updates.
    """


def archive_loser(
    loser_dir: Path,
    *,
    superseded_by: str,
    archive_root: Path,
    skill_registry: "SkillRegistry",
) -> Path:
    """Move loser_dir to archive_root/{name}__superseded_{YYYYMMDD}/ and
    unregister from skill_registry.

    Returns the final archive path. Atomic: if the registry update fails,
    the directory move is rolled back.
    """


def rename_winner_to_base(
    winner_dir: Path,
    *,
    skill_registry: "SkillRegistry",
) -> Path:
    """Rename myCodeSkill__v2 → myCodeSkill after the loser has been archived.

    Only called when the winner is a __v{N} directory. Atomic: renames the
    directory and updates the registry index in one pass.
    """
```

**Invariants:**

- Every function is testable without a running workflow, without an LLM.
- `pick_active_version` is a pure function of its arguments (no global state).
- `maybe_promote_winners` is the only function that mutates both the
  filesystem and the registry.
- Archival is always `move + rename`, never `delete`. The archive directory
  is never scanned by the loader — version discovery ignores anything under
  `skills_root/archive/`.
- The `__v{N}` naming convention is defined exactly once, in
  `VERSION_SUFFIX_RE`. Any code that wants to parse or construct a versioned
  name imports the helpers from this module.

### `agents/self_upgrade_dispatcher.py` (modified)

**Classifier change.** Add one rule **immediately after** the existing Tier
1b rule (`self_upgrade_dispatcher.py:129–134`). The order matters: Tier 1b
fires on same-detail clusters (cheaper prompt-append fix); Tier 1a fires on
varied-detail clusters on the same task_type (a sign the skill itself needs
refinement, not just an extra instruction). If Tier 1a were evaluated first,
its broader "same task_type, ≥3 signals" rule would intercept same-detail
clusters that belong to Tier 1b — because same-detail is a strict subset of
same-task-type. Tier 1b must have first claim on clusters it can handle.

```python
# Rule: varied-detail cluster on same task_type with ≥3 signals
# → Tier 1a refinement of the matching skill.
# Evaluated AFTER the Tier 1b same-detail rule so that same-detail clusters
# still route to the cheaper prompt-append fix.
task_types = {s.task_type for s in non_empty}
if len(task_types) == 1 and len(non_empty) >= 3:
    return Tier.ONE_A
```

**Constructor change.** Add `tier1a_builder: Optional[Tier1aBuilder] = None`
keyword argument. Stored on `self._tier1a`. Matches the existing pattern for
`tier0_builder` and `tier3_builder`.

**Handler change.** Add `_handle_tier1a`:

```python
def _handle_tier1a(
    self,
    signals: List[UpgradeSignal],
    author_agent_id: str,
    author_run_id: str,
    role: str,
) -> "DispatchResult.AnyResult":
    """Build a v2 refinement candidate via Tier1aBuilder."""
    if self._tier1a is None:
        return DispatchResult.Rejected(
            reason="tier1a dependencies not wired",
            signal_refs=[s.id for s in signals],
        )
    from .self_upgrade.tier1a_builder import Tier1aResult

    result = self._tier1a.build(
        signals,
        author_agent_id=author_agent_id,
        author_run_id=author_run_id,
    )
    if isinstance(result, Tier1aResult.LowConfidence):
        # Fall through to Tier 3 so the signals still produce a
        # human-visible artifact instead of silently disappearing.
        return self._handle_tier3(signals, author_agent_id, role)

    return DispatchResult.Tier1aQueued(
        refinement_id=result.skill_name + "__v2",
        signal_refs=result.signal_refs,
    )
```

**Dispatch change.** In `dispatch()`, add one `if tier == Tier.ONE_A:` branch
calling `_handle_tier1a`. No other changes.

### `agents/skill_generator.py` (modified)

Three changes:

1. **Delete `refine_skill` entirely** (lines 568–627). Its only production
   caller (`skill_cleanup.py:215`) is also being deleted in this PR, so the
   method has no live consumers after the refactor.
2. **Delete `_find_skill_path`** (lines 672–681). Its only caller was
   `refine_skill`.
3. **Promote `_create_refined_skill_content` (line 629) to public
   `draft_refined_content`**. Body unchanged. Drops the leading underscore.
   The method becomes a pure function: takes task_type, specification,
   original_content, feedback, score → returns str.

```python
def draft_refined_content(
    self,
    task_type: str,
    specification: str,
    original_content: str,
    feedback: str,
    score: int,
) -> str:
    """Compute refined SKILL.md content incorporating critic feedback.

    Pure function — no file I/O, no registry writes. Callers are
    responsible for persisting the result and registering it.
    """
    # body from existing _create_refined_skill_content (unchanged)
```

**Rationale for making it public and deleting `refine_skill`:** the v2
candidate is written by `Tier1aBuilder`, not by `skill_generator`. Leaving
`refine_skill` as dead code is worse than deleting it — the next contributor
has to figure out which method is live. `draft_refined_content` as a pure
function has an obvious test surface (input → output) and can be unit-tested
without touching the filesystem.

### `agents/skill_cleanup.py` (modified)

Two changes:

1. **Delete lines 208–227** — the `if score < REFINEMENT_THRESHOLD and feedback:`
   block that calls `refine_skill`. Delete the `refined_count` local and the
   trailing log line at 226–227.
2. **Delete the import** of `SkillGeneratorNode, REFINEMENT_THRESHOLD` at
   line 21 — neither symbol is used after the refactor.
3. **Add one call** at the end of `record_skill_outcomes`, after the
   existing outcome-recording loop:

```python
from . import skill_ab
from .config import get_skills_dir

skill_ab.maybe_promote_winners(
    skill_names_in_run=list(skills_in_use),
    outcome_store=self.outcome_store,
    skills_root=Path(get_skills_dir()),
    skill_registry=self.skill_registry,
    K_per_version=10,
)
```

**Rationale:** skill_cleanup is already the natural post-run housekeeping
location. It already runs on every workflow finish. Adding a promotion check
right next to outcome recording is the smallest possible coupling — one new
call, zero new control flow.

### `agents/skill_loader.py` (modified)

Minimal touch. In the existing load path, after a skill path is resolved by
`_scan_skill_sources` or the registry lookup, and before `SKILL.md` is read,
check for sibling versions:

```python
from . import skill_ab

# skill_path points to the discovered skill's directory
# (this comes from the existing load logic — path unchanged)
base = skill_path.name
versions = skill_ab.list_versions_for(base, skills_root=skill_path.parent)

if len(versions) > 1:
    skill_path = skill_ab.pick_active_version(
        versions,
        run_input=state.get("session_id", ""),
    )
    # The loaded_skills entry constructed below will carry skill_path.name
    # as the skill's name — which is the versioned name if we picked v2,
    # or the base name if we picked v1.
```

**Invariants:**

- The `loaded_skills[i]["name"]` field must be `skill_path.name` *after*
  this block. This is what makes outcome recording naturally version-aware
  — no separate plumbing, no new state fields, no changes to `AgentState`.
- The `discovered_skills` state entry (populated upstream by the router via
  `skill_registry.find_skill`) must carry the **same versioned name** as
  `loaded_skills`. Otherwise `skill_cleanup._build_skill_to_task_type_map`
  will build a map keyed by base name but try to look up entries by
  versioned name (or vice versa), silently returning the `"general"`
  fallback task_type. The cleanest place to enforce this invariant is in
  the skill_loader itself: when it detects a versioned sibling exists, it
  mutates the `skill_info` dict in place (updating both `skill_name` and
  `skill_path` to the versioned values) *before* loading content. The
  updated dict is still the same object in `state["discovered_skills"]`,
  so the router's upstream write and the loader's per-skill mutation stay
  consistent.

**Backwards compatibility:** skills with only one version (`list_versions_for`
returns 1 entry) skip the bucketing block entirely and behave exactly as
today. The only behavior change is for skills that currently have a sibling
`__v2` — which is zero skills today.

## Data flow summary

| Event | Component | Action |
|---|---|---|
| Low-score signal accumulates | `self_upgrade_trigger` | Record signal (existing) |
| Signal cluster exceeds threshold | `SelfUpgradeDispatcher.classify_signals` | Match new Tier.ONE_A rule |
| Dispatcher routes to Tier 1a | `SelfUpgradeDispatcher._handle_tier1a` | Call `Tier1aBuilder.build` |
| Builder drafts v2 | `Tier1aBuilder.build` | LLM call + filesystem write + registry update |
| Workflow loads skill | `skill_loader` | Detect versions → `pick_active_version` |
| Workflow finishes | `skill_cleanup.record_skill_outcomes` | Record outcome under versioned name |
| Promotion check | `skill_ab.maybe_promote_winners` | If K hit, archive loser + rename winner |
| Rollback (manual) | human | `mv archive/{name}__superseded_{date}/ ~/.vibe/skills/temp/{name}/` |

## Error handling

- **`Tier1aBuilder` fails to draft (LLM error, empty response):** returns
  `LowConfidence("draft_refined_content returned empty")`. Dispatcher falls
  through to Tier 3, signals stay accumulating for retry on next heartbeat.
- **`Tier1aBuilder` filesystem write fails:** exception propagates to
  dispatcher, caught by existing error handling in
  `graph_nodes._run_self_upgrade_dispatch`, logged and dropped. Signals stay
  unmarked so the next dispatch retries.
- **`maybe_promote_winners` archival fails mid-operation:** `archive_loser`
  is atomic (move-then-update-registry, rolled back on registry failure);
  `rename_winner_to_base` is similarly atomic. If the second atomic step
  fails after the first succeeded, the base name is missing until the next
  run retries — which is safe because the loader's discovery falls back to
  whichever version is still registered.
- **Loader sees sibling versions but one is corrupted / unreadable:**
  `list_versions_for` filters to directories with a readable `SKILL.md`.
  Missing SKILL.md → not included in the candidate list, loader uses the
  remaining version. (This is a defensive check, not expected in normal
  operation.)
- **Missing `session_id` in state:** `pick_active_version` falls back to
  `candidates[0]` (v1 by sort order). Deterministic, stable, never crashes.

## Security considerations

- **All A/B operations are local to `~/.vibe/skills/`.** No repo commits, no
  branches, no PRs. Blast radius is bounded to the individual install.
- **Tier 1a operations cannot modify any immutable file.** The Tier1aBuilder
  writes only to new sibling directories under the skill's existing parent;
  it never touches anything in `IMMUTABLE_PATHS` or `_ADDITIONAL_IMMUTABLES`.
- **The new files are already pre-registered as immutable.** The spec's
  `_ADDITIONAL_IMMUTABLES` set in `agents/self_upgrade/__init__.py` already
  includes `agents/self_upgrade/tier1a_builder.py`. This PR also adds
  `agents/skill_ab.py` to the same set.
- **Integrity hashes are updated via the existing `skill_registry.security`
  path.** New v2 content gets a hash via `store_integrity_hash(name, content)`
  identical to the hash check applied to all other skills. The loader's
  existing integrity verification runs on v2 exactly as it does on v1.
- **Bucket selection cannot be influenced by untrusted input.** `session_id`
  is generated server-side by the workflow runtime, not supplied by user
  input. Even if it were, `sha256` is cryptographically preimage-resistant —
  an attacker cannot construct a `session_id` that forces their preferred
  bucket.
- **No skill can modify `skill_ab.py` itself via self-upgrade.** This module
  is added to `_ADDITIONAL_IMMUTABLES`. Only human edits can change the A/B
  mechanics.

## Testing

Six new test files plus updates to three existing ones. Every piece is
unit-testable without a running workflow, without an LLM, and without
Paperclip.

### `tests/test_skill_ab.py` (new, ~18 tests)

Pure A/B logic. Deterministic, fast, no fixtures beyond tmpdirs.

- `test_is_versioned_name` — true for `"x__v2"`, false for `"x"`,
  `"x__v"`, `"x__va"`, `"x__v2_extra"`
- `test_base_name_and_versioned_name_roundtrip`
- `test_list_versions_for_single_version_returns_base` — only base
  dir → `[base_dir]`
- `test_list_versions_for_two_versions_sorted` — base + `__v2` →
  returns both, sorted by version
- `test_list_versions_for_ignores_archive_directory` —
  `archive/x__superseded_*/` is never included
- `test_list_versions_for_ignores_dirs_without_skill_md` — directory
  exists but no SKILL.md → excluded
- `test_bucket_for_run_is_deterministic` — same input, same bucket,
  10× in a row
- `test_bucket_for_run_distributes_roughly_evenly` — 1000 random
  inputs, each bucket gets 400–600
- `test_bucket_for_run_independent_of_python_hash_randomization` —
  compare against an independent `hashlib.sha256(x).digest()[0] % 2`
  invocation, must match every time
- `test_pick_active_version_bucket_zero_picks_first`
- `test_pick_active_version_bucket_one_picks_second`
- `test_pick_active_version_single_candidate_returns_it`
- `test_pick_active_version_empty_run_input_falls_back_to_first`
- `test_write_candidate_creates_versioned_directory_with_skill_md`
- `test_write_candidate_updates_integrity_hash`
- `test_write_candidate_raises_if_target_exists`
- `test_maybe_promote_winners_not_enough_outcomes` — 5 outcomes per
  version → returns `[]`, filesystem unchanged
- `test_maybe_promote_winners_v2_wins` — 10 outcomes each, v2 avg > v1
  avg → v1 archived, v2 renamed, returns `PromotionResult`
- `test_maybe_promote_winners_v1_wins_on_tie` — identical avg → v1
  stays, v2 archived (no rename)
- `test_maybe_promote_winners_v1_wins_outright` — v1 higher avg →
  v2 archived, v1 name unchanged
- `test_archive_loser_uses_dated_suffix` — archived path ends in
  `__superseded_YYYYMMDD`
- `test_archive_loser_unregisters_from_registry`
- `test_rename_winner_to_base_updates_registry`

### `tests/test_tier1a_builder.py` (new, ~8 tests)

Mocked `skill_generator.draft_refined_content` (returns canned strings), real
`skill_registry` and `outcome_store` on tmpdirs. No LLM calls.

- `test_build_no_matching_skill_returns_low_confidence` — signal
  cluster for unknown task_type → `LowConfidence("no matching skill")`
- `test_build_skill_has_no_recorded_outcomes_returns_low_confidence`
- `test_build_v2_already_exists_returns_low_confidence` —
  `LowConfidence("A/B in progress")`
- `test_build_happy_path_writes_v2_directory`
- `test_build_happy_path_registers_v2_with_registry`
- `test_build_happy_path_updates_integrity_hash`
- `test_build_empty_draft_returns_low_confidence` —
  `draft_refined_content` returns `""` → `LowConfidence`
- `test_build_result_carries_signal_refs`

### `tests/test_dispatcher_tier1a_classification.py` (new, ~6 tests)

Classifier unit tests, no builder.

- `test_single_signal_stays_tier_zero`
- `test_three_signals_same_detail_same_type_stays_tier1b` — existing
  rule wins, Tier 1a rule does not override
- `test_three_signals_varied_detail_same_type_goes_tier1a`
- `test_three_signals_varied_detail_different_types_goes_tier3`
- `test_two_signals_insufficient_for_tier1a`
- `test_tier1a_rule_ordered_after_tier1b`

### `tests/test_dispatcher_tier1a_handling.py` (new, ~4 tests)

Dispatcher → builder hand-off, mocked `Tier1aBuilder`.

- `test_dispatch_tier1a_no_builder_returns_rejected`
- `test_dispatch_tier1a_builder_low_confidence_falls_through_to_tier3`
- `test_dispatch_tier1a_builder_success_returns_tier1a_queued`
- `test_dispatch_tier1a_queued_refinement_id_matches_skill_name`

### `tests/test_skill_loader_version_selection.py` (new, ~5 tests)

Real `skill_loader` on a tmpdir, explicit `session_id` in state.

- `test_load_single_version_unchanged_behavior` — only base dir exists
  → backwards compatible
- `test_load_picks_v1_when_bucket_is_zero` — use a `session_id` whose
  sha256 byte 0 % 2 == 0 → loader picks v1, `loaded_skills[0]["name"]
  == "myCodeSkill"`
- `test_load_picks_v2_when_bucket_is_one` → `"myCodeSkill__v2"`
- `test_load_deterministic_same_session_id` — call twice, same result
- `test_load_missing_session_id_falls_back_to_v1`

### `tests/test_self_upgrade_invariants.py` (new, ~3 tests)

Lock-in tests for the deletions and immutable registration.

- `test_skill_generator_no_longer_has_refine_skill` — `assert not
  hasattr(SkillGeneratorNode, "refine_skill")`
- `test_skill_cleanup_does_not_reference_refine_threshold` — grep the
  file for `REFINEMENT_THRESHOLD`; if found, fail
- `test_skill_ab_is_in_additional_immutables` — `assert
  "agents/skill_ab.py" in _ADDITIONAL_IMMUTABLES`

### Updates to existing tests

- **`tests/test_skill_reinforcement.py` (lines 316, 334, 349):** three tests
  currently call `gen.refine_skill(...)`. Rewrite to call
  `gen.draft_refined_content(...)` and assert on the returned string. The
  integration-style subtests that verified file overwrites move into
  `test_tier1a_builder.py` (the builder now owns file writes).
- **`tests/test_misc_coverage.py` (line 957):** update the private-method
  reference from `_create_refined_skill_content` to `draft_refined_content`.
- **`tests/test_skill_cleanup.py`:** delete or rewrite tests that verified
  the auto-refine path. Add new tests that verify `maybe_promote_winners`
  fires after outcome recording (use fake outcomes for two versions, call
  `record_skill_outcomes`, assert promotion ran).

**Total: ~44 new tests + 5 existing test updates.**

## Rollout

No feature flag, no env var. The existing `VIBE_SELF_UPGRADE_ENABLED` master
switch covers the entire self-upgrade pipeline including Tier 1a. Adding a
per-tier toggle would multiply configuration surface area and create edge
cases (what if `VIBE_SELF_UPGRADE_ENABLED=true` and `VIBE_TIER1A_ENABLED=false`?).

The ripout of the `skill_cleanup` auto-refine path is a strict
simplification: it removes a silent side effect on every low-scoring run.
There is no fallback path — the dispatcher-gated Tier 1a is the only way
skill refinement happens after this PR lands.

## Dependencies

This spec assumes M0 + M1 from the self-upgrade feasibility plan
(2026-04-06-self-upgrade-feasibility-plan.md) has shipped and merged, which
it has (PR #37 merged 2026-04-08). Specifically:

- `SelfUpgradeDispatcher` exists and is wired into
  `graph_nodes._run_self_upgrade_dispatch`
- `DispatchResult.Tier1aQueued` variant already exists on the tagged union
- `_ADDITIONAL_IMMUTABLES` already pre-registers
  `agents/self_upgrade/tier1a_builder.py`
- `Tier0Result` and `Tier3Result` tagged-union patterns exist as templates
  for `Tier1aResult`

## Open questions

None. All decisions captured in the brainstorming session above.

## Appendix: Decisions summary

| Decision | Value |
|---|---|
| Scope | Tier 1a only; Tier 1b and Tier 2 are separate specs |
| In-place refinement path | Deleted entirely; no fast-path, no fallback |
| Disk layout for versions | Sibling directories with `__v{N}` suffix |
| New-skill-creation case | Out of scope; falls through to Tier 3 |
| Cluster threshold | 3+ non-empty signals on same task_type |
| Skill resolution timing | Inside Tier1aBuilder, not the classifier |
| Eligibility for refinement | Matching skill exists + ≥1 recorded outcome |
| Bucket function | `sha256(session_id).digest()[0] % 2` |
| Promotion threshold K | 20 total (10 per version) |
| Tie-breaking on promotion | Earlier version wins (v1 beats v2) |
| Loser handling | Move to `archive/{name}__superseded_{YYYYMMDD}/` |
| Winner handling | Rename `__v{N}` → base name after archival |
| Outcome store schema | Unchanged; version encoded in `skill_name` |
| Promotion trigger site | Inline at end of `skill_cleanup.record_skill_outcomes` |
| Version selection site | `skill_loader` (6-line touch) via `skill_ab.pick_active_version` |
| A/B logic location | New `agents/skill_ab.py` module |
| Builder location | New `agents/self_upgrade/tier1a_builder.py` |
| Builder result type | `Tier1aResult` tagged union (`CandidateWritten`, `LowConfidence`) |
| `refine_skill` | Deleted |
| `_create_refined_skill_content` | Promoted to public `draft_refined_content` |
| Kill switch | Existing `VIBE_SELF_UPGRADE_ENABLED` covers it |
| New files immutable? | Yes; `skill_ab.py` added to `_ADDITIONAL_IMMUTABLES` |
