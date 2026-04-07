# Self-Upgrade Feasibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the tiered self-upgrade feasibility design from `docs/superpowers/specs/2026-04-06-self-upgrade-feasibility-design.md` across five milestones (M0 dispatcher → M1 memory notes + issue reports → M2 skill refinement → M3 prompt overrides → M4 typed code edits).

**Architecture:** Replace the single "file-diff pipeline" model with a **signal dispatcher** that routes accumulated signals to one of five tier-specific builders based on signal shape. Each tier produces a different artifact (memory notes, skill refinements, prompt overrides, typed code edits, issue reports) with its own validation path. No tier can merge its own work — all code-producing tiers file PRs assigned to the human user.

**Tech Stack:** Python 3.13, pytest, libcst (M4 only), YAML, existing Vibe-Stack infra (memory_store, paperclip_client, heartbeat, critic_nodes).

---

## Pre-flight: plan deviations from spec

Three deviations from the spec were discovered during plan review. Each is intentional and documented here so a reviewer can judge whether they're acceptable.

1. **LessonStore instead of extending memory_store.** The spec says "reuse `memory_store` with a new `MemoryType.LESSON` variant." In reality, `memory_store.py`'s `MemoryStore` class has no `MemoryType` enum — it uses a free-form `source: str` field and a space-separated `tags: str` field. The spec's "reuse" path would require adding 5+ lesson-specific columns (`role`, `task_type`, `status`, `outcome_delta`, `uses`) via ALTER TABLE migrations, polluting `memory_store` with lesson-specific fields that have no meaning for other memory types. **Plan uses a dedicated `agents/lesson_store.py`** — a focused ~250-line SQLite store with exactly the fields needed. Reuses the same storage_backend abstraction, same pluggable pattern, same thread-safety model. Deviation cost: one more file, no conceptual change.

2. **`self_upgrade_dispatcher.py` is a new file, not a refactor of `self_upgrade_trigger.py`.** The spec says "refactor `self_upgrade_trigger.py` into a dispatcher." The cleaner implementation keeps `self_upgrade_trigger.py` as the signal *accumulator* (its current responsibility, plus new `id`/`artifact_ref` fields) and puts the *dispatcher + classifier* in a new file. This matches the spec's conceptual separation between "accumulate signals" and "route signals to tiers" while keeping each file focused. Both files remain in the immutable list.

3. **Milestones 2–4 use task-level granularity, not step-level.** The spec covers all 5 milestones but the plan-writing skill recommends bite-sized TDD steps for each. Writing bite-sized steps for M2–M4 would produce stale prescriptions (we'd be planning libcst code 3+ months before it ships). Plan provides **full bite-sized TDD steps for M0 and M1** and **task-level outlines for M2–M4** — each M2–M4 task names files, tests, and acceptance criteria but does not pre-specify exact code. An explicit re-plan pass is required before M2–M4 execution.

---

## File structure

### Milestone 0 — Dispatcher rewiring

**New files:**
- `agents/self_upgrade_dispatcher.py` — `SelfUpgradeDispatcher` class with `dispatch()`, `classify_signals()`, and stub tier routing
- `scripts/migrate_upgrade_signals.py` — one-shot migration wiping stale signals and rewriting the file with `id`+`artifact_ref` fields on any remaining entries
- `tests/test_self_upgrade_dispatcher.py` — dispatcher + classifier tests
- `tests/test_signal_migration.py` — migration tests
- `tests/test_signal_store_new_fields.py` — `id`/`artifact_ref` persistence tests

**Modified files:**
- `agents/self_upgrade_trigger.py` — add `id` + `artifact_ref` fields to `UpgradeSignal` dataclass and `_persist_signals`/`_load_persisted_signals` methods; rename `SelfUpgradeTrigger` → `SignalAccumulator` (keep file name, keep in immutable list)
- `agents/self_upgrade.py` — narrow `SelfUpgradePipeline` → `Tier2Pipeline`; remove `UpgradeProposal(files=dict)` interface entirely; keep `IMMUTABLE_PATHS` (expand), `MAX_DIFF_LINES`, `_run_tests`, `_run_bandit`, `_apply_and_commit` (called selectively by edit type later)
- `agents/heartbeat.py` — replace any `SelfUpgradePipeline.execute()` call with `SelfUpgradeDispatcher.dispatch()`

### Milestone 1 — Tier 0 + Tier 3

**New files:**
- `agents/lesson_store.py` — dedicated LessonStore with schema, `add()`, `list_by_scope()`, `record_use()`, `compute_outcome_delta()`, `decay_check()`
- `agents/self_upgrade/__init__.py` — package marker
- `agents/self_upgrade/tier0_builder.py` — `Tier0Builder.build(signals) -> Lesson | None`
- `agents/self_upgrade/tier3_builder.py` — `Tier3Builder.build(signals) -> IssueReport | None` + self-critique path
- `agents/self_upgrade/reports.py` — `IssueReport`, `EvidenceRow` dataclasses + rendering
- `agents/memory_note_node.py` — workflow node that calls `Tier0Builder` at end of run
- `tests/test_lesson_store.py`
- `tests/test_tier0_builder.py`
- `tests/test_tier3_builder.py`
- `tests/test_memory_note_node.py`
- `tests/test_lesson_injection.py` — integration: lesson → inject → new run → scoring
- `tests/test_paperclip_client_assignee.py`

**Modified files:**
- `agents/paperclip_client.py` — extend `create_issue()` with `assignee_user_id: Optional[str]` param, pass through as `assigneeUserId` in request body
- `agents/config.py` — add `VIBE_HUMAN_TRIAGE_USER_ID` env var + matching `SystemConfig` field
- `agents/critic_nodes.py` — at the end of `evaluate_output`, set a new state flag `lesson_eligible: bool = (score < 85 and feedback.strip() != "")`
- `agents/graph.py` — register `memory_note_node` as a conditional next step after critic when `lesson_eligible` is True
- `agents/nodes.py` — export `memory_note_node` if the graph uses a central node registry
- `agents/heartbeat_context.py` — new helper `_load_lessons_for_run(role, task_type)` that queries `lesson_store.list_by_scope()` and appends a "## Lessons from past runs" block to `user_request`; call from `_build_user_request`
- `agents/state.py` — add `lesson_eligible: bool` and `injected_lesson_ids: list[str]` to AgentState
- `agents/self_upgrade_dispatcher.py` — replace Tier 3 stub with real `Tier3Builder` call; add Tier 0 routing

### Milestone 2 — Tier 1a skill refinement (task-level, re-plan before execution)

**New files:**
- `agents/self_upgrade/tier1a_builder.py`
- `tests/test_tier1a_builder.py`
- `tests/test_skill_refinement_ab.py`

**Modified files:**
- `agents/skill_generator.py` — add `refine(existing_skill, accumulated_feedback) -> Skill`
- `agents/skill_outcome_store.py` — add `version` field to outcome records; add `pick_active_version(skill_name) -> str`
- `agents/skill_loader.py` — call `pick_active_version()` when loading a skill with multiple versions
- `agents/self_upgrade_dispatcher.py` — wire Tier 1a routing

### Milestone 3 — Tier 1b prompt overrides (task-level, re-plan before execution)

**New files:**
- `agents/prompt_library/__init__.py`
- `agents/prompt_library/overrides.yaml` — empty initial file with `overrides: []`
- `agents/prompt_library/loader.py` — `OverrideLoader.load_all()`, scope matching
- `agents/self_upgrade/tier1b_builder.py`
- `agents/self_upgrade/tier1b_pipeline.py` — schema validation + append-only diff check + prompt-critic + canonical smoke + commit
- `agents/self_upgrade/canonical_smoke.py` — runs canonical fixtures against the prompt override
- `agents/canonical_harvester.py` — hooks end-of-run to capture high-scoring outputs as canonical fixtures
- `tests/canonical/README.md` — explains the fixture format
- `tests/test_prompt_override_loader.py`
- `tests/test_tier1b_builder.py`
- `tests/test_tier1b_pipeline.py`
- `tests/test_canonical_harvester.py`
- `tests/test_canonical_smoke.py`

**Modified files:**
- `agents/adapters.py` — `AdapterRegistry.__init__` loads overrides; `get_or_create` merges matching appends
- `agents/critic_nodes.py` — hook `canonical_harvester.maybe_capture()` at end of `evaluate_output` when score ≥ some threshold
- `agents/self_upgrade_dispatcher.py` — wire Tier 1b routing

### Milestone 4 — Tier 2 typed code edits (task-level, re-plan before execution)

**New files:**
- `agents/self_upgrade/typed_edits.py` — `TypedEdit` + `PromptConstantEdit` / `ThresholdEdit` / `DictListAppendEdit` / `DocstringEdit` / `NewTestFileEdit` dataclasses
- `agents/self_upgrade/ast_verifier.py` — `TypedEditValidator` using libcst
- `agents/self_upgrade/tier2_builder.py` — LLM-driven proposal generator
- `agents/self_upgrade/signal_replay.py` — replays `SignalAccumulator.analyse()` over historical signals for `threshold_tweak` gate
- `agents/self_upgrade/allowlists.py` — per-edit-type file allowlists (static data)
- `tests/test_typed_edits.py`
- `tests/test_ast_verifier_docstring.py`
- `tests/test_ast_verifier_threshold.py`
- `tests/test_ast_verifier_dict_list_append.py`
- `tests/test_ast_verifier_prompt_constant.py`
- `tests/test_ast_verifier_new_test_file.py`
- `tests/test_tier2_pipeline_docstring.py`
- `tests/test_tier2_pipeline_threshold.py`
- `tests/test_signal_replay.py`

**Modified files:**
- `agents/self_upgrade.py` — expand `Tier2Pipeline.execute()` to accept `TypedEdit`, dispatch to per-type gate
- `pyproject.toml` — add `libcst>=1.0` to deps
- `requirements-production.lock` — add libcst pin
- `agents/self_upgrade_dispatcher.py` — wire Tier 2 routing

---

# MILESTONE 0 — Dispatcher rewiring

Refactor the signal pipeline so it has a real routing layer. No new tiers active at the end of M0 — all classified signals fall through to a stub Tier 3 path that logs and no-ops. This milestone proves the wiring without requiring Paperclip, memory_store, or any tier-specific logic.

## Task 1: Wipe stale signals + migration script

**Files:**
- Create: `scripts/migrate_upgrade_signals.py`
- Create: `tests/test_signal_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signal_migration.py
import json
from pathlib import Path

from scripts.migrate_upgrade_signals import migrate_signals_file


def test_migration_wipes_legacy_signals(tmp_path):
    legacy = tmp_path / "upgrade_signals.jsonl"
    # Seed with a typical stale entry (no id, no artifact_ref)
    legacy.write_text(json.dumps({
        "category": "low_score",
        "task_type": "code_generation",
        "detail": "Score 40/100",
        "score": 40,
        "source_node": "critic",
        "timestamp": "2026-03-28T13:47:53.116358Z",
    }) + "\n")

    result = migrate_signals_file(legacy, wipe=True)

    assert result.wiped_count == 1
    assert result.remaining_count == 0
    assert legacy.read_text() == ""


def test_migration_preserves_new_format_entries(tmp_path):
    legacy = tmp_path / "upgrade_signals.jsonl"
    legacy.write_text(json.dumps({
        "id": "sig_01HZ123",
        "artifact_ref": None,
        "category": "low_score",
        "task_type": "code_generation",
        "detail": "Score 40/100",
        "score": 40,
        "source_node": "critic",
        "timestamp": "2026-04-06T00:00:00Z",
    }) + "\n")

    result = migrate_signals_file(legacy, wipe=False)

    assert result.wiped_count == 0
    assert result.remaining_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_signal_migration.py -v
```

Expected: `ModuleNotFoundError: No module named 'scripts.migrate_upgrade_signals'`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/migrate_upgrade_signals.py
"""One-shot migration for ~/.vibe/skills/upgrade_signals.jsonl.

Wipes stale test data from the March 28 burst and ensures any remaining entries
have the new `id` + `artifact_ref` fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MigrationResult:
    wiped_count: int
    remaining_count: int
    path: Path


def _is_legacy(entry: dict) -> bool:
    """Return True for any entry missing the new id+artifact_ref fields."""
    return "id" not in entry or "artifact_ref" not in entry


def migrate_signals_file(path: Path, wipe: bool = True) -> MigrationResult:
    """Read the JSONL file, optionally wipe legacy entries, rewrite in-place.

    Args:
        path:  Path to the upgrade_signals.jsonl file.
        wipe:  If True (default), legacy entries are dropped. If False, legacy
               entries are back-filled with generated ids and null artifact_ref.

    Returns:
        MigrationResult with counts.
    """
    if not path.exists():
        return MigrationResult(wiped_count=0, remaining_count=0, path=path)

    wiped = 0
    kept: list[dict] = []

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # Corrupt line — drop it
            wiped += 1
            continue

        if _is_legacy(entry):
            if wipe:
                wiped += 1
                continue
            # Backfill mode
            entry["id"] = f"sig_backfill_{len(kept)}"
            entry["artifact_ref"] = None

        kept.append(entry)

    path.write_text("".join(json.dumps(e) + "\n" for e in kept))

    return MigrationResult(
        wiped_count=wiped,
        remaining_count=len(kept),
        path=path,
    )


def main() -> None:
    default_path = Path.home() / ".vibe" / "skills" / "upgrade_signals.jsonl"
    result = migrate_signals_file(default_path, wipe=True)
    print(
        f"Migration complete: wiped={result.wiped_count} "
        f"remaining={result.remaining_count} path={result.path}"
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_signal_migration.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add scripts/migrate_upgrade_signals.py tests/test_signal_migration.py
git commit -m "feat(self-upgrade): add signal migration script"
```

## Task 2: Run migration against real stale signal file

**Files:**
- Modify: `~/.vibe/skills/upgrade_signals.jsonl` (not in repo)

- [ ] **Step 1: Confirm the stale file still exists**

```bash
wc -l ~/.vibe/skills/upgrade_signals.jsonl
```

Expected: `170 /home/prime/.vibe/skills/upgrade_signals.jsonl` (or similar count).

- [ ] **Step 2: Run the migration**

```bash
cd ~/Repos/Vibe-Stack && python scripts/migrate_upgrade_signals.py
```

Expected output:
```
Migration complete: wiped=170 remaining=0 path=/home/prime/.vibe/skills/upgrade_signals.jsonl
```

- [ ] **Step 3: Verify the file is empty**

```bash
wc -l ~/.vibe/skills/upgrade_signals.jsonl
```

Expected: `0 /home/prime/.vibe/skills/upgrade_signals.jsonl`

- [ ] **Step 4: No commit — this is a data operation, not a code change**

## Task 3: Add id + artifact_ref fields to UpgradeSignal

**Files:**
- Modify: `agents/self_upgrade_trigger.py` (UpgradeSignal dataclass + persistence)
- Create: `tests/test_signal_store_new_fields.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signal_store_new_fields.py
import json
import tempfile
from pathlib import Path

import pytest

from agents.self_upgrade_trigger import SelfUpgradeTrigger, UpgradeSignal


def test_new_signals_get_id_and_null_artifact_ref(tmp_path):
    store = tmp_path / "signals.jsonl"
    trigger = SelfUpgradeTrigger(signal_store_path=str(store))

    sig = UpgradeSignal(
        category="low_score",
        task_type="code_generation",
        detail="test",
        score=40,
        source_node="critic",
    )
    trigger._persist_signals([sig], "code_generation")

    entries = [json.loads(line) for line in store.read_text().splitlines() if line]
    assert len(entries) == 1
    assert entries[0]["id"].startswith("sig_")
    assert entries[0]["artifact_ref"] is None


def test_load_persisted_signals_tolerates_missing_new_fields(tmp_path):
    store = tmp_path / "signals.jsonl"
    # Legacy-format entry (wipe should have caught these, but load should be tolerant)
    store.write_text(json.dumps({
        "category": "low_score",
        "task_type": "code_generation",
        "detail": "legacy",
        "score": 40,
        "source_node": "critic",
        "timestamp": "2026-03-28T00:00:00Z",
    }) + "\n")

    trigger = SelfUpgradeTrigger(signal_store_path=str(store))
    assert trigger.get_signal_count("code_generation") == 1


def test_mark_signal_with_artifact_ref(tmp_path):
    store = tmp_path / "signals.jsonl"
    trigger = SelfUpgradeTrigger(signal_store_path=str(store))
    trigger._persist_signals([
        UpgradeSignal(category="low_score", task_type="t", detail="d", score=40),
    ], "t")

    # Grab the first signal id
    entries = [json.loads(line) for line in store.read_text().splitlines() if line]
    sig_id = entries[0]["id"]

    trigger.mark_artifact_ref([sig_id], artifact_ref="note_abc")

    entries = [json.loads(line) for line in store.read_text().splitlines() if line]
    assert entries[0]["artifact_ref"] == "note_abc"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_signal_store_new_fields.py -v
```

Expected: `AttributeError: 'SelfUpgradeTrigger' object has no attribute 'mark_artifact_ref'` (and id/artifact_ref missing from persisted entries).

- [ ] **Step 3: Modify UpgradeSignal + _persist_signals**

In `agents/self_upgrade_trigger.py`:

Add `id` and `artifact_ref` fields to `UpgradeSignal`:
```python
# Replace the existing UpgradeSignal dataclass
from uuid import uuid4

@dataclass
class UpgradeSignal:
    """A single signal that something should be upgraded."""
    category: str
    task_type: str
    detail: str
    score: int = 0
    source_node: str = ""
    id: str = field(default_factory=lambda: f"sig_{uuid4().hex[:12]}")
    artifact_ref: Optional[str] = None
```

Update `_persist_signals` to include the new fields:
```python
def _persist_signals(self, signals: List[UpgradeSignal], task_type: str) -> None:
    with self._lock:
        try:
            with open(self._signal_store_path, "a", encoding="utf-8") as f:
                for s in signals:
                    entry = {
                        "id": s.id,
                        "artifact_ref": s.artifact_ref,
                        "category": s.category,
                        "task_type": s.task_type,
                        "detail": s.detail[:300],
                        "score": s.score,
                        "source_node": s.source_node,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }
                    f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.debug("Failed to persist upgrade signals: %s", e)
```

Update `_load_persisted_signals` to tolerate missing fields (legacy entries):
```python
# Inside the parse loop of _load_persisted_signals:
signal = UpgradeSignal(
    category=entry.get("category", ""),
    task_type=entry.get("task_type", ""),
    detail=entry.get("detail", ""),
    score=entry.get("score", 0),
    source_node=entry.get("source_node", ""),
    id=entry.get("id", f"sig_legacy_{uuid4().hex[:12]}"),
    artifact_ref=entry.get("artifact_ref"),
)
```

Add `mark_artifact_ref` method:
```python
def mark_artifact_ref(self, signal_ids: List[str], artifact_ref: str) -> int:
    """Update the given signal entries in the store with the artifact_ref.

    Returns the number of entries updated.
    """
    if not self._signal_store_path.exists():
        return 0

    updated = 0
    with self._lock:
        lines = self._signal_store_path.read_text().splitlines()
        out: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                out.append(line)
                continue
            if entry.get("id") in signal_ids:
                entry["artifact_ref"] = artifact_ref
                updated += 1
            out.append(json.dumps(entry))
        self._signal_store_path.write_text("".join(l + "\n" for l in out))

        # Also update in-memory history so the dispatcher sees the change
        for history in self._signal_history.values():
            for sig in history:
                if sig.id in signal_ids:
                    sig.artifact_ref = artifact_ref

    return updated
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_signal_store_new_fields.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Run existing self_upgrade_trigger tests to check for regressions**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -k "upgrade_trigger or self_upgrade" -v
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/self_upgrade_trigger.py tests/test_signal_store_new_fields.py
git commit -m "feat(self-upgrade): add id and artifact_ref to UpgradeSignal"
```

## Task 4: Dispatcher skeleton with classifier

**Files:**
- Create: `agents/self_upgrade_dispatcher.py`
- Create: `tests/test_self_upgrade_dispatcher.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_self_upgrade_dispatcher.py
from agents.self_upgrade_dispatcher import (
    DispatchResult,
    SelfUpgradeDispatcher,
    Tier,
)
from agents.self_upgrade_trigger import UpgradeSignal


def _make_signal(category="low_score", task_type="code_generation", detail="", score=40):
    return UpgradeSignal(
        category=category, task_type=task_type, detail=detail,
        score=score, source_node="critic",
    )


def test_classifier_routes_single_actionable_low_score_to_tier0():
    dispatcher = SelfUpgradeDispatcher()
    signals = [_make_signal(detail="Missing error handling around DB calls")]
    tier = dispatcher.classify_signals(signals)
    assert tier == Tier.ZERO


def test_classifier_routes_repeated_pattern_to_tier1b():
    dispatcher = SelfUpgradeDispatcher()
    signals = [
        _make_signal(detail="Missing request validation"),
        _make_signal(detail="Missing request validation"),
        _make_signal(detail="Missing request validation"),
    ]
    tier = dispatcher.classify_signals(signals)
    assert tier == Tier.ONE_B


def test_classifier_routes_empty_feedback_to_tier3():
    dispatcher = SelfUpgradeDispatcher()
    signals = [_make_signal(detail=""), _make_signal(detail=""), _make_signal(detail="")]
    tier = dispatcher.classify_signals(signals)
    assert tier == Tier.THREE


def test_dispatch_stub_returns_rejected_for_every_tier_in_m0():
    """In M0, all builders are stubs — every dispatch returns Rejected."""
    dispatcher = SelfUpgradeDispatcher()
    signals = [_make_signal()]
    result = dispatcher.dispatch(signals)
    assert isinstance(result, DispatchResult.Rejected)
    assert "stub" in result.reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_dispatcher.py -v
```

Expected: `ModuleNotFoundError: No module named 'agents.self_upgrade_dispatcher'`

- [ ] **Step 3: Write minimal implementation**

```python
# agents/self_upgrade_dispatcher.py
"""Self-upgrade dispatcher — routes classified signals to tier-specific builders.

M0: all tiers are stubs. Classifier runs for real but every dispatch returns
DispatchResult.Rejected with a "stub" reason. Real tier routing is added in M1+.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Union

from .self_upgrade_trigger import UpgradeSignal

logger = logging.getLogger(__name__)


class Tier(str, Enum):
    ZERO = "0"
    ONE_A = "1a"
    ONE_B = "1b"
    TWO = "2"
    THREE = "3"


class DispatchResult:
    """Tagged union of dispatcher outcomes."""

    @dataclass
    class Tier0Written:
        lesson_id: str
        signal_refs: List[str]

    @dataclass
    class Tier1aQueued:
        refinement_id: str
        signal_refs: List[str]

    @dataclass
    class Tier1bCommitted:
        branch: str
        commit: str
        pr_url: str
        issue_id: str
        signal_refs: List[str]

    @dataclass
    class Tier2Committed:
        branch: str
        commit: str
        pr_url: str
        issue_id: str
        edit_type: str
        signal_refs: List[str]

    @dataclass
    class Tier3Filed:
        issue_id: str
        signal_refs: List[str]

    @dataclass
    class Rejected:
        reason: str
        signal_refs: List[str]

    Union = Union[
        "DispatchResult.Tier0Written",
        "DispatchResult.Tier1aQueued",
        "DispatchResult.Tier1bCommitted",
        "DispatchResult.Tier2Committed",
        "DispatchResult.Tier3Filed",
        "DispatchResult.Rejected",
    ]


class SelfUpgradeDispatcher:
    """Routes accumulated signals to tier-specific builders."""

    def __init__(self) -> None:
        pass

    def classify_signals(self, signals: List[UpgradeSignal]) -> Tier:
        """Heuristic classifier. No LLM call.

        Rules are evaluated in order, first match wins. See spec §"Dispatch logic".
        """
        if not signals:
            return Tier.THREE

        non_empty = [s for s in signals if s.detail and s.detail.strip()]

        # Rule: all-empty-feedback signal cluster → Tier 3 (can't draft a lesson
        # or override from empty feedback)
        if not non_empty:
            return Tier.THREE

        # Rule: single actionable signal, no repeats → Tier 0 memory note
        if len(non_empty) == 1:
            return Tier.ZERO

        # Rule: repeated same-pattern feedback on same task type → Tier 1b
        # (same-detail clusters suggest a missing prompt instruction)
        details = {s.detail for s in non_empty}
        task_types = {s.task_type for s in non_empty}
        if len(details) == 1 and len(task_types) == 1 and len(non_empty) >= 3:
            return Tier.ONE_B

        # TODO (M2+): skill-cluster rule for Tier 1a
        # TODO (M4): threshold / registry / tool-failure rules for Tier 2

        # Fallback: Tier 3 report
        return Tier.THREE

    def dispatch(self, signals: List[UpgradeSignal]) -> "DispatchResult.Union":
        """Classify the signals and route to the appropriate builder.

        M0: every tier is a stub and returns Rejected("stub not implemented").
        """
        tier = self.classify_signals(signals)
        sig_refs = [s.id for s in signals]

        logger.info(
            "Self-upgrade dispatcher classified %d signal(s) to tier %s",
            len(signals), tier.value,
        )

        # M0: every tier is a stub
        return DispatchResult.Rejected(
            reason=f"stub: tier {tier.value} builder not implemented in M0",
            signal_refs=sig_refs,
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_dispatcher.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/self_upgrade_dispatcher.py tests/test_self_upgrade_dispatcher.py
git commit -m "feat(self-upgrade): dispatcher skeleton with heuristic classifier"
```

## Task 5: Narrow SelfUpgradePipeline to Tier2Pipeline (dormant)

**Files:**
- Modify: `agents/self_upgrade.py`
- Modify: existing tests that exercise `SelfUpgradePipeline.execute(proposal, critic_fn)` — these need to be updated or removed since the old `UpgradeProposal(files=dict)` interface is deleted

- [ ] **Step 1: Find existing callers of the old interface**

```bash
cd ~/Repos/Vibe-Stack && grep -rn "UpgradeProposal\|SelfUpgradePipeline" agents/ tests/ --include="*.py"
```

Record the list. Any callers outside `self_upgrade.py` itself need to be updated.

- [ ] **Step 2: Write a placeholder test for the narrowed pipeline**

```python
# Add to tests/test_self_upgrade_pipeline.py (or create if missing)

import pytest
from agents.self_upgrade import Tier2Pipeline, is_self_upgrade_enabled


def test_tier2_pipeline_rejects_when_disabled(monkeypatch):
    monkeypatch.setenv("VIBE_SELF_UPGRADE_ENABLED", "false")
    pipeline = Tier2Pipeline()
    # TypedEdit isn't implemented until M4. Passing None should be a clean rejection.
    result = pipeline.execute(None)
    assert result.success is False
    assert any("not enabled" in e.lower() for e in result.errors)


def test_tier2_pipeline_rejects_none_proposal_when_enabled(monkeypatch):
    monkeypatch.setenv("VIBE_SELF_UPGRADE_ENABLED", "true")
    pipeline = Tier2Pipeline()
    result = pipeline.execute(None)
    assert result.success is False
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_pipeline.py -v
```

Expected: `ImportError: cannot import name 'Tier2Pipeline' from 'agents.self_upgrade'`

- [ ] **Step 4: Narrow SelfUpgradePipeline**

In `agents/self_upgrade.py`:

Rename the class (keep the module-level `SelfUpgradePipeline = Tier2Pipeline` alias for one release to avoid breaking imports), delete the `UpgradeProposal` dataclass entirely, and make `execute()` accept `Optional[TypedEdit]` (forward reference) that returns Rejected for any non-None input until M4.

Key changes:
1. Delete `UpgradeProposal` dataclass.
2. Rename `class SelfUpgradePipeline:` → `class Tier2Pipeline:`
3. Add alias: `SelfUpgradePipeline = Tier2Pipeline` (M0 only — remove after M4)
4. Change `execute(self, proposal, critic_fn=None)` → `execute(self, typed_edit=None)`.
5. Return `UpgradeResult(success=False, errors=["Tier2Pipeline dormant until M4"])` for any non-None typed_edit until M4.
6. Expand `IMMUTABLE_PATHS` to the full "Never" set from the spec.
7. Keep `_run_tests`, `_run_bandit`, `_apply_and_commit`, `_generate_diff_text` as private helpers for M4 use.

Expanded `IMMUTABLE_PATHS`:

```python
IMMUTABLE_PATHS = frozenset({
    # Current immutables
    "agents/self_upgrade.py",
    "agents/self_upgrade_trigger.py",
    "agents/self_upgrade_dispatcher.py",
    "agents/skill_security.py",
    "agents/config.py",
    ".env",
    ".env.example",
    # Workflow core
    "agents/graph.py",
    "agents/graph_engine.py",
    "agents/graph_runners.py",
    "agents/graph_nodes.py",
    "agents/nodes.py",
    "agents/state.py",
    "agents/specialist_nodes.py",
    "agents/output_nodes.py",
    # LLM plumbing (llm_retry.py intentionally absent — threshold_tweak allowlist)
    "agents/llm_backend.py",
    "agents/backend_pool.py",
    # Storage
    "agents/message_store.py",
    "agents/memory_store.py",
    "agents/artifact_store.py",
    "agents/spending_tracker.py",
    "agents/session_store.py",
    "agents/embedder.py",
    # Heartbeat
    "agents/heartbeat.py",
    "agents/heartbeat_context.py",
    "agents/heartbeat_progress.py",
    "agents/heartbeat_signals.py",
    "agents/heartbeat_spending.py",
    "agents/heartbeat_formatting.py",
    "agents/workflow_factory.py",
    # Skill subsystem plumbing
    "agents/skill_loader.py",
    "agents/skill_generator.py",
    "agents/skill_outcome_store.py",
    "agents/skill_cleanup.py",
    "agents/skill_search.py",
    "agents/skill_remote.py",
    # External clients
    "agents/paperclip_client.py",
    "agents/ws_client.py",
    "agents/messenger_client.py",
    "agents/api_key_manager.py",
    # Resource layer
    "agents/resource_discovery.py",
    "agents/resource_allocator.py",
    # Orchestrator + main
    "agents/main.py",
    "agents/orchestrator.py",
    "agents/daemon.py",
    "agents/cancellation.py",
    "agents/intent_classifier.py",
})
```

(The `agents/storage/*`, `agents/sandbox/*`, `vibe/backends/*`, `agents/skill_registry*`.py, and `agents/lesson_store.py` can be added as glob prefix checks rather than listed explicitly — TODO in Task 6 below.)

- [ ] **Step 5: Update any existing callers**

For each caller found in Step 1 outside `self_upgrade.py`:
- If the caller is a test exercising the old `UpgradeProposal(files=dict)` interface → delete or port the test.
- If the caller is production code (e.g. `heartbeat.py`) → replace with a stub call to the dispatcher (to be wired up in Task 7).

- [ ] **Step 6: Run the new test**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_pipeline.py -v
```

Expected: 2 passed.

- [ ] **Step 7: Run the full self_upgrade test suite**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -k "self_upgrade" -v
```

Expected: all pass. Any remaining test exercising the old `UpgradeProposal(files=dict)` signature should have been updated or deleted in Step 5.

- [ ] **Step 8: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/self_upgrade.py tests/test_self_upgrade_pipeline.py
git commit -m "refactor(self-upgrade): narrow pipeline to Tier2Pipeline (dormant until M4)"
```

## Task 6: Expand immutable path check to directory prefixes

**Files:**
- Modify: `agents/self_upgrade.py` (`validate_paths` method)
- Modify: `tests/test_self_upgrade_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_self_upgrade_pipeline.py

def test_immutable_directory_prefixes_blocked():
    """Files under immutable directories (storage/, sandbox/, skill_registry*) are blocked."""
    from agents.self_upgrade import is_path_immutable

    # File-level (already covered)
    assert is_path_immutable("agents/self_upgrade.py") is True

    # Directory prefixes
    assert is_path_immutable("agents/storage/sqlite.py") is True
    assert is_path_immutable("agents/sandbox/docker.py") is True
    assert is_path_immutable("vibe/backends/vllm.py") is True

    # Skill registry pattern
    assert is_path_immutable("agents/skill_registry.py") is True
    assert is_path_immutable("agents/skill_registry_index.py") is True

    # Lesson store (M1 will add it — must be immutable at rest)
    assert is_path_immutable("agents/lesson_store.py") is True

    # Allowed
    assert is_path_immutable("agents/tools/web_search.py") is False
    assert is_path_immutable("agents/heuristic_critic.py") is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_pipeline.py::test_immutable_directory_prefixes_blocked -v
```

Expected: `ImportError` or `AssertionError` on the directory prefix lines.

- [ ] **Step 3: Add is_path_immutable helper**

In `agents/self_upgrade.py`:

```python
# Module-level directory prefixes (match if rel_path startswith any)
IMMUTABLE_DIR_PREFIXES = (
    "agents/storage/",
    "agents/sandbox/",
    "vibe/backends/",
)

# Module-level filename patterns (match if filename startswith any)
IMMUTABLE_FILE_PREFIXES = (
    "agents/skill_registry",
)

# Additional explicit files added since the original IMMUTABLE_PATHS was defined
_ADDITIONAL_IMMUTABLES = frozenset({
    "agents/lesson_store.py",       # M1 — cannot modify lesson persistence
    "agents/self_upgrade/tier0_builder.py",   # M1
    "agents/self_upgrade/tier3_builder.py",   # M1
    "agents/self_upgrade/tier1a_builder.py",  # M2
    "agents/self_upgrade/tier1b_builder.py",  # M3
    "agents/self_upgrade/tier2_builder.py",   # M4
    "agents/self_upgrade/ast_verifier.py",    # M4
})


def is_path_immutable(rel_path: str) -> bool:
    """Return True if rel_path is forbidden for any self-upgrade operation."""
    if rel_path in IMMUTABLE_PATHS:
        return True
    if rel_path in _ADDITIONAL_IMMUTABLES:
        return True
    if any(rel_path.startswith(p) for p in IMMUTABLE_DIR_PREFIXES):
        return True
    if any(rel_path.startswith(p) for p in IMMUTABLE_FILE_PREFIXES):
        return True
    return False
```

Update `Tier2Pipeline` path validation to use `is_path_immutable()`.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_pipeline.py::test_immutable_directory_prefixes_blocked -v
```

Expected: passed.

- [ ] **Step 5: Run full self_upgrade test suite**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -k "self_upgrade" -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/self_upgrade.py tests/test_self_upgrade_pipeline.py
git commit -m "feat(self-upgrade): expand immutable path check to directory prefixes"
```

## Task 7: Wire dispatcher into heartbeat

**Files:**
- Modify: `agents/heartbeat.py`
- Create: `tests/test_heartbeat_dispatcher_integration.py`

- [ ] **Step 1: Find where the old pipeline was called from heartbeat**

```bash
cd ~/Repos/Vibe-Stack && grep -n "self_upgrade\|SelfUpgradePipeline\|SelfUpgradeTrigger" agents/heartbeat.py
```

Record the line numbers. The integration point is typically at the end of a run, after the critic has finished and the result has been posted.

- [ ] **Step 2: Write a failing integration test**

```python
# tests/test_heartbeat_dispatcher_integration.py
from unittest.mock import MagicMock, patch

from agents.self_upgrade_dispatcher import DispatchResult
from agents.self_upgrade_trigger import SelfUpgradeTrigger, UpgradeSignal


def test_heartbeat_calls_dispatcher_with_accumulated_signals(tmp_path):
    """At end-of-run, heartbeat should invoke the dispatcher and log the result."""
    from agents.heartbeat import _run_self_upgrade_dispatch

    fake_trigger = MagicMock(spec=SelfUpgradeTrigger)
    fake_trigger.get_accumulated_signals.return_value = [
        UpgradeSignal(
            category="low_score", task_type="t", detail="d", score=40,
        ),
    ]

    with patch("agents.heartbeat.SelfUpgradeDispatcher") as dispatcher_cls:
        dispatcher_cls.return_value.dispatch.return_value = DispatchResult.Rejected(
            reason="stub", signal_refs=["sig_1"],
        )

        result = _run_self_upgrade_dispatch(fake_trigger, task_type="t")

        dispatcher_cls.return_value.dispatch.assert_called_once()
        assert isinstance(result, DispatchResult.Rejected)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_heartbeat_dispatcher_integration.py -v
```

Expected: `ImportError: cannot import name '_run_self_upgrade_dispatch' from 'agents.heartbeat'`

- [ ] **Step 4: Add get_accumulated_signals to SelfUpgradeTrigger**

In `agents/self_upgrade_trigger.py`:

```python
def get_accumulated_signals(self, task_type: str) -> List[UpgradeSignal]:
    """Return all undispatched (artifact_ref is None) signals for a task type."""
    history = self._signal_history.get(task_type, [])
    return [s for s in history if s.artifact_ref is None]
```

- [ ] **Step 5: Add _run_self_upgrade_dispatch to heartbeat**

In `agents/heartbeat.py`:

```python
from .self_upgrade_dispatcher import DispatchResult, SelfUpgradeDispatcher
from .self_upgrade_trigger import SelfUpgradeTrigger

def _run_self_upgrade_dispatch(
    trigger: SelfUpgradeTrigger,
    task_type: str,
) -> "DispatchResult.Union":
    """End-of-run hook: invoke the dispatcher with any undispatched signals.

    Logs the result. On success tiers, marks the contributing signals with
    the artifact_ref so they aren't re-dispatched.
    """
    signals = trigger.get_accumulated_signals(task_type)
    if not signals:
        logger.debug("Self-upgrade dispatch: no accumulated signals")
        return DispatchResult.Rejected(reason="no signals", signal_refs=[])

    dispatcher = SelfUpgradeDispatcher()
    result = dispatcher.dispatch(signals)

    logger.info(
        "Self-upgrade dispatch result: %s",
        type(result).__name__,
    )

    # Mark signals as dispatched when the result carries an artifact_ref
    artifact_ref: Optional[str] = None
    if isinstance(result, DispatchResult.Tier0Written):
        artifact_ref = result.lesson_id
    elif isinstance(result, DispatchResult.Tier3Filed):
        artifact_ref = result.issue_id
    # Tier 1a/1b/2 cases added in later milestones

    if artifact_ref:
        trigger.mark_artifact_ref(
            [s.id for s in signals],
            artifact_ref,
        )

    return result
```

Also find the existing end-of-run code path (after the critic posts results) and add a call to `_run_self_upgrade_dispatch(self._trigger, task_type)`.

- [ ] **Step 6: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_heartbeat_dispatcher_integration.py -v
```

Expected: passed.

- [ ] **Step 7: Run full heartbeat tests for regressions**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -k "heartbeat" -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/heartbeat.py agents/self_upgrade_trigger.py tests/test_heartbeat_dispatcher_integration.py
git commit -m "feat(self-upgrade): wire dispatcher into heartbeat end-of-run"
```

## Milestone 0 complete

At this point:
- Dispatcher skeleton is in place
- Signals have `id` + `artifact_ref` fields
- `SelfUpgradePipeline` is narrowed to dormant `Tier2Pipeline`
- Heartbeat invokes the dispatcher at end-of-run
- All current tests pass
- Every real dispatch returns `Rejected("stub ...")` — no visible artifacts yet

Next: Milestone 1 replaces the Tier 0 and Tier 3 stubs with real implementations.

---

# MILESTONE 1 — Tier 0 memory notes + Tier 3 issue reports

The real MVP. After this ships: memory notes start accumulating, injection into future runs works, and real Paperclip issues start appearing in the Improvements tab when signals indicate a problem the agent can't patch.

## Task 8: LessonStore schema and store() method

**Files:**
- Create: `agents/lesson_store.py`
- Create: `tests/test_lesson_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lesson_store.py
from datetime import datetime

from agents.lesson_store import Lesson, LessonStore


def test_store_and_retrieve_by_scope(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    lesson_id = store.add(
        role="backend_engineer",
        task_type="code_generation",
        tag="fastapi",
        lesson="When generating FastAPI endpoints, always include Pydantic request validation.",
        author_agent_id="agent_123",
        author_run_id="run_456",
    )

    assert lesson_id.startswith("lesson_")

    matches = store.list_by_scope(
        role="backend_engineer",
        task_type="code_generation",
    )
    assert len(matches) == 1
    assert matches[0].lesson.startswith("When generating")
    assert matches[0].uses == 0
    assert matches[0].outcome_delta is None
    assert matches[0].status == "active"


def test_list_by_scope_matches_wildcards(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    # Role-specific
    store.add(role="backend_engineer", task_type="code_generation",
              tag="", lesson="A", author_agent_id="", author_run_id="")
    # Role-wildcard (applies to any role)
    store.add(role="*", task_type="code_generation",
              tag="", lesson="B", author_agent_id="", author_run_id="")
    # Different task type
    store.add(role="backend_engineer", task_type="research",
              tag="", lesson="C", author_agent_id="", author_run_id="")

    matches = store.list_by_scope(
        role="backend_engineer",
        task_type="code_generation",
    )
    assert len(matches) == 2
    lessons = {m.lesson for m in matches}
    assert lessons == {"A", "B"}


def test_list_by_scope_respects_status_filter(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    lesson_id = store.add(role="*", task_type="*", tag="",
                          lesson="X", author_agent_id="", author_run_id="")
    store.set_status(lesson_id, "decayed")

    assert store.list_by_scope(role="r", task_type="t", status="active") == []
    assert len(store.list_by_scope(role="r", task_type="t", status="decayed")) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_lesson_store.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal LessonStore implementation**

```python
# agents/lesson_store.py
"""Dedicated store for Tier 0 "lessons learned" memory notes.

Scoped to (role, task_type, tag) so the read path can cheaply filter by exact
match. Distinct from memory_store because lessons have different metadata
requirements (outcome_delta, uses, status, decay) that don't make sense for
general-purpose memories.

Thread-safe with per-call SQLite connections (matching session_store.py pattern).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_DEFAULT_DB_DIR = Path.home() / ".vibe"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "lessons.db"


@dataclass
class Lesson:
    lesson_id: str
    role: str                   # "*" for role-agnostic
    task_type: str              # "*" for type-agnostic
    tag: str
    lesson: str
    author_agent_id: str
    author_run_id: str
    created_at: str
    uses: int = 0
    outcome_delta: Optional[float] = None
    last_used_at: Optional[str] = None
    status: str = "active"      # active | decayed | superseded


class LessonStore:
    """SQLite-backed lesson store."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS lessons (
            lesson_id       TEXT PRIMARY KEY,
            role            TEXT NOT NULL,
            task_type       TEXT NOT NULL,
            tag             TEXT NOT NULL DEFAULT '',
            lesson          TEXT NOT NULL,
            author_agent_id TEXT NOT NULL DEFAULT '',
            author_run_id   TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL,
            uses            INTEGER NOT NULL DEFAULT 0,
            outcome_delta   REAL,
            last_used_at    TEXT,
            status          TEXT NOT NULL DEFAULT 'active'
        );
        CREATE INDEX IF NOT EXISTS idx_lessons_scope
            ON lessons(role, task_type, status);
        CREATE INDEX IF NOT EXISTS idx_lessons_outcome
            ON lessons(outcome_delta DESC);

        CREATE TABLE IF NOT EXISTS lesson_uses (
            lesson_id    TEXT NOT NULL,
            run_id       TEXT NOT NULL,
            run_score    INTEGER NOT NULL,
            used_at      TEXT NOT NULL,
            PRIMARY KEY (lesson_id, run_id)
        );
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(self._SCHEMA)

    def add(
        self,
        *,
        role: str,
        task_type: str,
        tag: str,
        lesson: str,
        author_agent_id: str,
        author_run_id: str,
    ) -> str:
        """Insert a new lesson. Returns its lesson_id."""
        lesson_id = f"lesson_{uuid4().hex[:12]}"
        now = datetime.utcnow().isoformat() + "Z"

        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO lessons (lesson_id, role, task_type, tag, lesson, "
                "author_agent_id, author_run_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (lesson_id, role, task_type, tag, lesson,
                 author_agent_id, author_run_id, now),
            )

        return lesson_id

    def list_by_scope(
        self,
        *,
        role: str,
        task_type: str,
        status: str = "active",
        limit: int = 5,
    ) -> List[Lesson]:
        """List lessons matching (role, task_type) with status filter.

        Wildcard matching: lessons with role="*" match any role, and lessons
        with task_type="*" match any task_type. Results are ordered by
        outcome_delta DESC (best-performing first).
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lessons "
                "WHERE (role = ? OR role = '*') "
                "AND (task_type = ? OR task_type = '*') "
                "AND status = ? "
                "ORDER BY outcome_delta DESC NULLS LAST, created_at DESC "
                "LIMIT ?",
                (role, task_type, status, limit),
            ).fetchall()

        return [self._row_to_lesson(r) for r in rows]

    def set_status(self, lesson_id: str, status: str) -> None:
        """Update a lesson's status (active/decayed/superseded)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE lessons SET status = ? WHERE lesson_id = ?",
                (status, lesson_id),
            )

    def _row_to_lesson(self, row: sqlite3.Row) -> Lesson:
        return Lesson(
            lesson_id=row["lesson_id"],
            role=row["role"],
            task_type=row["task_type"],
            tag=row["tag"],
            lesson=row["lesson"],
            author_agent_id=row["author_agent_id"],
            author_run_id=row["author_run_id"],
            created_at=row["created_at"],
            uses=row["uses"],
            outcome_delta=row["outcome_delta"],
            last_used_at=row["last_used_at"],
            status=row["status"],
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_lesson_store.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/lesson_store.py tests/test_lesson_store.py
git commit -m "feat(lesson-store): initial schema + add/list_by_scope"
```

## Task 9: LessonStore use tracking and outcome_delta scoring

**Files:**
- Modify: `agents/lesson_store.py`
- Modify: `tests/test_lesson_store.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_lesson_store.py

def test_record_use_and_compute_outcome_delta(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="lesson", author_agent_id="", author_run_id="")

    # Record 3 runs that used this lesson with scores 80, 85, 90 (avg 85)
    store.record_use(lesson_id, run_id="run_1", run_score=80)
    store.record_use(lesson_id, run_id="run_2", run_score=85)
    store.record_use(lesson_id, run_id="run_3", run_score=90)

    # Baseline is passed explicitly in M1 (M2+ may compute lazily)
    delta = store.recompute_outcome_delta(lesson_id, baseline_score=70.0)

    # avg(80, 85, 90) = 85, baseline 70 → delta 15
    assert delta == 15.0

    lessons = store.list_by_scope(role="r", task_type="t")
    assert lessons[0].uses == 3
    assert lessons[0].outcome_delta == 15.0


def test_record_use_is_idempotent_per_run(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))
    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="lesson", author_agent_id="", author_run_id="")

    store.record_use(lesson_id, run_id="run_1", run_score=80)
    store.record_use(lesson_id, run_id="run_1", run_score=80)  # duplicate

    lessons = store.list_by_scope(role="r", task_type="t")
    assert lessons[0].uses == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_lesson_store.py::test_record_use_and_compute_outcome_delta -v
```

Expected: `AttributeError: 'LessonStore' object has no attribute 'record_use'`

- [ ] **Step 3: Add record_use and recompute_outcome_delta**

```python
# In agents/lesson_store.py, add to LessonStore:

def record_use(self, lesson_id: str, run_id: str, run_score: int) -> None:
    """Record that a run used this lesson, with the run's final score.

    Idempotent per (lesson_id, run_id) — duplicate calls have no effect.
    """
    now = datetime.utcnow().isoformat() + "Z"
    with self._lock, self._connect() as conn:
        try:
            conn.execute(
                "INSERT INTO lesson_uses (lesson_id, run_id, run_score, used_at) "
                "VALUES (?, ?, ?, ?)",
                (lesson_id, run_id, run_score, now),
            )
        except sqlite3.IntegrityError:
            # Duplicate (lesson_id, run_id) — idempotent no-op
            return

        # Update denormalized uses count + last_used_at
        conn.execute(
            "UPDATE lessons SET uses = (SELECT COUNT(*) FROM lesson_uses "
            "WHERE lesson_id = ?), last_used_at = ? WHERE lesson_id = ?",
            (lesson_id, now, lesson_id),
        )

def recompute_outcome_delta(
    self, lesson_id: str, baseline_score: float,
) -> Optional[float]:
    """Recompute and persist outcome_delta for a lesson.

    outcome_delta = avg(run_scores for uses of this lesson) - baseline_score

    Returns the new delta, or None if the lesson has no uses yet.
    """
    with self._lock, self._connect() as conn:
        row = conn.execute(
            "SELECT AVG(run_score) AS avg_score, COUNT(*) AS n "
            "FROM lesson_uses WHERE lesson_id = ?",
            (lesson_id,),
        ).fetchone()

        if row is None or row["n"] == 0:
            return None

        delta = float(row["avg_score"]) - float(baseline_score)
        conn.execute(
            "UPDATE lessons SET outcome_delta = ? WHERE lesson_id = ?",
            (delta, lesson_id),
        )
        return delta
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_lesson_store.py -v
```

Expected: all 5 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/lesson_store.py tests/test_lesson_store.py
git commit -m "feat(lesson-store): use tracking and outcome_delta scoring"
```

## Task 10: LessonStore decay check

**Files:**
- Modify: `agents/lesson_store.py`
- Modify: `tests/test_lesson_store.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_lesson_store.py

def test_decay_check_marks_underperforming_lessons(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))
    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="bad", author_agent_id="", author_run_id="")

    # 10 uses, all scoring 50, baseline 70 → delta -20
    for i in range(10):
        store.record_use(lesson_id, run_id=f"run_{i}", run_score=50)
    store.recompute_outcome_delta(lesson_id, baseline_score=70.0)

    decayed = store.decay_check(min_uses=10)
    assert lesson_id in decayed

    lessons = store.list_by_scope(role="r", task_type="t", status="active")
    assert len(lessons) == 0  # It's been decayed

    decayed_list = store.list_by_scope(role="r", task_type="t", status="decayed")
    assert len(decayed_list) == 1


def test_decay_check_preserves_good_lessons(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))
    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="good", author_agent_id="", author_run_id="")

    for i in range(10):
        store.record_use(lesson_id, run_id=f"run_{i}", run_score=90)
    store.recompute_outcome_delta(lesson_id, baseline_score=70.0)

    decayed = store.decay_check(min_uses=10)
    assert lesson_id not in decayed


def test_decay_check_ignores_low_use_lessons(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))
    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="early", author_agent_id="", author_run_id="")

    # Only 5 uses — not enough to judge
    for i in range(5):
        store.record_use(lesson_id, run_id=f"run_{i}", run_score=50)
    store.recompute_outcome_delta(lesson_id, baseline_score=70.0)

    decayed = store.decay_check(min_uses=10)
    assert lesson_id not in decayed
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_lesson_store.py -v -k "decay"
```

Expected: `AttributeError: 'LessonStore' object has no attribute 'decay_check'`

- [ ] **Step 3: Implement decay_check**

```python
# In agents/lesson_store.py, add to LessonStore:

def decay_check(self, min_uses: int = 10) -> List[str]:
    """Mark underperforming lessons as decayed.

    A lesson is decayed when it has at least `min_uses` recorded uses AND
    its outcome_delta is negative. Returns the list of lesson_ids that were
    decayed this call.
    """
    with self._lock, self._connect() as conn:
        rows = conn.execute(
            "SELECT lesson_id FROM lessons "
            "WHERE status = 'active' "
            "AND uses >= ? "
            "AND outcome_delta IS NOT NULL "
            "AND outcome_delta < 0",
            (min_uses,),
        ).fetchall()

        decayed = [r["lesson_id"] for r in rows]
        if decayed:
            conn.executemany(
                "UPDATE lessons SET status = 'decayed' WHERE lesson_id = ?",
                [(lid,) for lid in decayed],
            )

    return decayed
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_lesson_store.py -v
```

Expected: all passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/lesson_store.py tests/test_lesson_store.py
git commit -m "feat(lesson-store): decay check for underperforming lessons"
```

## Task 11: Tier0Builder draws a lesson from a signal cluster

**Files:**
- Create: `agents/self_upgrade/__init__.py`
- Create: `agents/self_upgrade/tier0_builder.py`
- Create: `tests/test_tier0_builder.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tier0_builder.py
from unittest.mock import MagicMock

from agents.self_upgrade.tier0_builder import Tier0Builder, Tier0Result
from agents.self_upgrade_trigger import UpgradeSignal


def test_builder_drafts_lesson_from_signal_cluster():
    fake_llm = MagicMock()
    fake_llm.generate.return_value = (
        "When generating FastAPI endpoints, always include Pydantic request validation."
    )

    builder = Tier0Builder(llm=fake_llm)
    signals = [
        UpgradeSignal(
            category="low_score",
            task_type="code_generation",
            detail="Missing request validation in the endpoint",
            score=60,
        ),
    ]

    result = builder.build(
        signals,
        author_agent_id="agent_1",
        author_run_id="run_1",
        role="backend_engineer",
    )

    assert isinstance(result, Tier0Result.LessonDrafted)
    assert result.role == "backend_engineer"
    assert result.task_type == "code_generation"
    assert "FastAPI" in result.lesson
    assert fake_llm.generate.called


def test_builder_returns_empty_when_llm_returns_nothing():
    fake_llm = MagicMock()
    fake_llm.generate.return_value = ""

    builder = Tier0Builder(llm=fake_llm)
    result = builder.build(
        [UpgradeSignal(category="low_score", task_type="t", detail="d", score=60)],
        author_agent_id="", author_run_id="", role="r",
    )

    assert isinstance(result, Tier0Result.Empty)


def test_builder_returns_empty_when_no_signals():
    fake_llm = MagicMock()
    builder = Tier0Builder(llm=fake_llm)
    result = builder.build(
        [], author_agent_id="", author_run_id="", role="r",
    )
    assert isinstance(result, Tier0Result.Empty)
    assert not fake_llm.generate.called
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_tier0_builder.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write Tier0Builder**

```python
# agents/self_upgrade/__init__.py
"""Self-upgrade tier builders and pipelines."""
```

```python
# agents/self_upgrade/tier0_builder.py
"""Tier 0 builder — drafts a memory note from a signal cluster."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Protocol, Union

from ..self_upgrade_trigger import UpgradeSignal

logger = logging.getLogger(__name__)


class _LLMProtocol(Protocol):
    def generate(self, prompt: str, max_tokens: int = 200) -> str: ...


class Tier0Result:
    @dataclass
    class LessonDrafted:
        lesson: str
        role: str
        task_type: str
        tag: str
        signal_refs: List[str]

    @dataclass
    class Empty:
        reason: str

    Union = Union["Tier0Result.LessonDrafted", "Tier0Result.Empty"]


_TIER0_PROMPT_TEMPLATE = """\
You are summarising a lesson for future agent runs to avoid a recurring mistake.

Context:
- Agent role: {role}
- Task type: {task_type}
- Recent signal(s):
{signals}

Write ONE lesson in ≤3 sentences that a future agent could apply next time to
avoid this mistake. The lesson should be concrete, actionable, and specific to
the role and task type. Return ONLY the lesson text, no preamble.
"""


class Tier0Builder:
    """Drafts a Lesson from a signal cluster using the critic adapter."""

    def __init__(self, llm: _LLMProtocol) -> None:
        self._llm = llm

    def build(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str,
        author_run_id: str,
        role: str,
    ) -> "Tier0Result.Union":
        if not signals:
            return Tier0Result.Empty(reason="no signals to draft from")

        # Use the first signal's task_type; all signals in a cluster share it
        task_type = signals[0].task_type

        signals_text = "\n".join(
            f"  - {s.category}: {s.detail} (score={s.score})"
            for s in signals[:5]  # Cap to avoid overlong prompts
        )

        prompt = _TIER0_PROMPT_TEMPLATE.format(
            role=role,
            task_type=task_type,
            signals=signals_text,
        )

        lesson = self._llm.generate(prompt, max_tokens=200).strip()
        if not lesson:
            return Tier0Result.Empty(reason="llm returned empty string")

        return Tier0Result.LessonDrafted(
            lesson=lesson,
            role=role,
            task_type=task_type,
            tag="",                      # M2+ may add auto-tagging
            signal_refs=[s.id for s in signals],
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_tier0_builder.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/self_upgrade/__init__.py agents/self_upgrade/tier0_builder.py tests/test_tier0_builder.py
git commit -m "feat(tier0): builder drafts lessons from signal clusters"
```

## Task 12: memory_note_node workflow node

**Files:**
- Create: `agents/memory_note_node.py`
- Create: `tests/test_memory_note_node.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_note_node.py
from unittest.mock import MagicMock, patch

from agents.memory_note_node import memory_note_node
from agents.self_upgrade.tier0_builder import Tier0Result


def test_memory_note_node_skips_when_not_eligible():
    state = {
        "lesson_eligible": False,
        "output_critic_score": 90,
        "output_critic_feedback": "all good",
    }

    result_state = memory_note_node(state, lesson_store=MagicMock(), tier0_builder=MagicMock())

    assert "lesson_written_id" not in result_state or result_state["lesson_written_id"] is None


def test_memory_note_node_writes_lesson_when_eligible():
    fake_store = MagicMock()
    fake_store.add.return_value = "lesson_xyz"

    fake_builder = MagicMock()
    fake_builder.build.return_value = Tier0Result.LessonDrafted(
        lesson="use validation",
        role="backend",
        task_type="code_generation",
        tag="",
        signal_refs=[],
    )

    state = {
        "lesson_eligible": True,
        "output_critic_score": 60,
        "output_critic_feedback": "missing validation",
        "routed_task_type": "code_generation",
        "agent_role": "backend",
        "agent_id": "agent_1",
        "run_id": "run_1",
        "accumulated_signals": [],
    }

    result_state = memory_note_node(
        state, lesson_store=fake_store, tier0_builder=fake_builder,
    )

    assert result_state["lesson_written_id"] == "lesson_xyz"
    fake_store.add.assert_called_once()


def test_memory_note_node_no_op_when_builder_returns_empty():
    fake_store = MagicMock()
    fake_builder = MagicMock()
    fake_builder.build.return_value = Tier0Result.Empty(reason="llm empty")

    state = {
        "lesson_eligible": True,
        "output_critic_score": 60,
        "output_critic_feedback": "x",
        "routed_task_type": "t",
        "agent_role": "r",
        "agent_id": "",
        "run_id": "",
        "accumulated_signals": [],
    }

    result_state = memory_note_node(
        state, lesson_store=fake_store, tier0_builder=fake_builder,
    )

    assert result_state.get("lesson_written_id") is None
    fake_store.add.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_memory_note_node.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement memory_note_node**

```python
# agents/memory_note_node.py
"""Workflow node — writes a Tier 0 lesson at the end of a run if eligible.

Runs after the critic. Only fires when `state["lesson_eligible"]` is True
(set by the critic when score < 85 AND feedback is non-empty).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .lesson_store import LessonStore
from .self_upgrade.tier0_builder import Tier0Builder, Tier0Result

logger = logging.getLogger(__name__)


def memory_note_node(
    state: Dict[str, Any],
    *,
    lesson_store: LessonStore,
    tier0_builder: Tier0Builder,
) -> Dict[str, Any]:
    """Optionally write a lesson to the lesson store.

    Passes through `state` unchanged except for adding `lesson_written_id` when
    a lesson is successfully written.
    """
    if not state.get("lesson_eligible"):
        return state

    # The critic populated these flags; reading them for the builder
    signals = state.get("accumulated_signals", [])
    role = state.get("agent_role", "*")
    task_type = state.get("routed_task_type", "*")

    result = tier0_builder.build(
        signals,
        author_agent_id=state.get("agent_id", ""),
        author_run_id=state.get("run_id", ""),
        role=role,
    )

    if isinstance(result, Tier0Result.Empty):
        logger.debug("memory_note_node: builder empty (%s)", result.reason)
        return state

    assert isinstance(result, Tier0Result.LessonDrafted)
    lesson_id = lesson_store.add(
        role=result.role,
        task_type=result.task_type,
        tag=result.tag,
        lesson=result.lesson,
        author_agent_id=state.get("agent_id", ""),
        author_run_id=state.get("run_id", ""),
    )

    logger.info("memory_note_node: wrote lesson %s", lesson_id)
    state["lesson_written_id"] = lesson_id
    return state
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_memory_note_node.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/memory_note_node.py tests/test_memory_note_node.py
git commit -m "feat(tier0): memory_note_node writes lessons at end of run"
```

## Task 13: Critic sets lesson_eligible flag

**Files:**
- Modify: `agents/critic_nodes.py`
- Modify: `agents/state.py`
- Create: `tests/test_critic_lesson_eligible.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_critic_lesson_eligible.py
from agents.critic_nodes import _compute_lesson_eligible


def test_lesson_eligible_true_when_score_low_and_feedback_nonempty():
    assert _compute_lesson_eligible(score=60, feedback="needs validation") is True
    assert _compute_lesson_eligible(score=84, feedback="x") is True
    assert _compute_lesson_eligible(score=0, feedback="complete failure") is True


def test_lesson_eligible_false_when_score_high():
    assert _compute_lesson_eligible(score=85, feedback="good") is False
    assert _compute_lesson_eligible(score=100, feedback="perfect") is False


def test_lesson_eligible_false_when_feedback_empty():
    assert _compute_lesson_eligible(score=40, feedback="") is False
    assert _compute_lesson_eligible(score=40, feedback="   ") is False
    assert _compute_lesson_eligible(score=40, feedback=None) is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_critic_lesson_eligible.py -v
```

Expected: `ImportError: cannot import name '_compute_lesson_eligible'`

- [ ] **Step 3: Add _compute_lesson_eligible helper + wire into evaluate_output**

In `agents/critic_nodes.py`:

```python
def _compute_lesson_eligible(score: int, feedback: Optional[str]) -> bool:
    """Return True if this run should emit a memory note (per spec §Tier 0 Write path).

    Gating: score < 85 AND feedback non-empty after stripping.
    """
    if score >= 85:
        return False
    if feedback is None:
        return False
    return bool(feedback.strip())
```

At the end of `evaluate_output`, before returning state:

```python
# Flag for the memory_note_node downstream
state["lesson_eligible"] = _compute_lesson_eligible(
    score=state.get("output_critic_score", 0),
    feedback=state.get("output_critic_feedback", ""),
)
```

In `agents/state.py`, add the new state fields (append to the appropriate group TypedDict):

```python
# Inside OutputCriticState or an adjacent group:
lesson_eligible: NotRequired[bool]
lesson_written_id: NotRequired[Optional[str]]
injected_lesson_ids: NotRequired[List[str]]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_critic_lesson_eligible.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Run existing critic tests for regressions**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -k "critic" -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/critic_nodes.py agents/state.py tests/test_critic_lesson_eligible.py
git commit -m "feat(critic): set lesson_eligible flag for memory_note_node"
```

## Task 14: Wire memory_note_node into the workflow graph

**Files:**
- Modify: `agents/graph.py`
- Modify: `agents/workflow_factory.py` (if it's where builder instances are wired — check)
- Create: `tests/test_graph_memory_note_integration.py`

- [ ] **Step 1: Find where the critic node connects to downstream nodes in graph.py**

```bash
cd ~/Repos/Vibe-Stack && grep -n "critic\|OutputCritic\|evaluate_output\|add_node\|add_edge" agents/graph.py | head -40
```

- [ ] **Step 2: Write integration test**

```python
# tests/test_graph_memory_note_integration.py
from unittest.mock import MagicMock

from agents.graph import build_workflow_graph


def test_graph_includes_memory_note_node_after_critic():
    """The compiled graph should route the critic → memory_note_node for lesson-eligible runs."""
    graph = build_workflow_graph()
    node_names = set(graph.get_node_names())
    assert "memory_note_node" in node_names

    # memory_note_node should have the critic (or its parent) as an in-edge
    in_edges = graph.get_in_edges("memory_note_node")
    assert any("critic" in n.lower() or "evaluate_output" in n for n in in_edges)
```

(Adjust to the actual `build_workflow_graph()` / `graph_engine` API in-repo — check `graph_engine.py` for the real method names.)

- [ ] **Step 3: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_graph_memory_note_integration.py -v
```

Expected: fails — `memory_note_node` not in the graph yet.

- [ ] **Step 4: Register memory_note_node in the graph**

In `agents/graph.py`, after the critic node registration:

```python
from .memory_note_node import memory_note_node
from .lesson_store import LessonStore
from .self_upgrade.tier0_builder import Tier0Builder

# Singleton instances (wired through workflow_factory in production)
_lesson_store = LessonStore()

def _memory_note_wrapper(state):
    """Lazy init of builder — pulls the critic adapter's LLM from workflow factory."""
    from .workflow_factory import get_adapter
    critic_llm = get_adapter("CRITIC").backend
    builder = Tier0Builder(llm=critic_llm)
    return memory_note_node(state, lesson_store=_lesson_store, tier0_builder=builder)

workflow.add_node("memory_note_node", _memory_note_wrapper)
workflow.add_edge("output_critic", "memory_note_node")
workflow.add_edge("memory_note_node", "output_formatter")  # or whatever the next node is
```

(Adjust to the actual graph API — this is psuedocode matching the probable shape of `graph.py`.)

- [ ] **Step 5: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_graph_memory_note_integration.py -v
```

Expected: passed.

- [ ] **Step 6: Run full graph tests for regressions**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -k "graph" -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/graph.py tests/test_graph_memory_note_integration.py
git commit -m "feat(tier0): wire memory_note_node into workflow graph"
```

## Task 15: heartbeat_context injects lessons into user_request

**Files:**
- Modify: `agents/heartbeat_context.py`
- Create: `tests/test_heartbeat_context_lessons.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_heartbeat_context_lessons.py
from unittest.mock import MagicMock, patch

from agents.heartbeat_context import _load_lessons_for_run


def test_load_lessons_returns_empty_when_no_matches(tmp_path):
    store = MagicMock()
    store.list_by_scope.return_value = []

    lessons = _load_lessons_for_run(
        lesson_store=store,
        role="backend_engineer",
        task_type="code_generation",
    )
    assert lessons == []


def test_load_lessons_formats_matching_lessons_for_injection(tmp_path):
    from agents.lesson_store import Lesson

    store = MagicMock()
    store.list_by_scope.return_value = [
        Lesson(
            lesson_id="lesson_1", role="backend_engineer",
            task_type="code_generation", tag="",
            lesson="Always include Pydantic validation.",
            author_agent_id="", author_run_id="",
            created_at="2026-04-06T00:00:00Z",
        ),
    ]

    lessons = _load_lessons_for_run(
        lesson_store=store,
        role="backend_engineer",
        task_type="code_generation",
    )

    assert len(lessons) == 1
    assert "Pydantic" in lessons[0]
    store.list_by_scope.assert_called_once_with(
        role="backend_engineer",
        task_type="code_generation",
        status="active",
        limit=5,
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_heartbeat_context_lessons.py -v
```

Expected: `ImportError: cannot import name '_load_lessons_for_run'`

- [ ] **Step 3: Add _load_lessons_for_run and injection hook**

In `agents/heartbeat_context.py`:

```python
from .lesson_store import LessonStore

def _load_lessons_for_run(
    *,
    lesson_store: LessonStore,
    role: str,
    task_type: str,
) -> List[str]:
    """Return a list of formatted lesson strings to inject into the specialist context."""
    matches = lesson_store.list_by_scope(
        role=role,
        task_type=task_type,
        status="active",
        limit=5,
    )
    return [
        f"- ({m.lesson_id}) {m.lesson}"
        for m in matches
    ]
```

In `_build_user_request` (or equivalent), after the existing context block is assembled:

```python
lessons = _load_lessons_for_run(
    lesson_store=lesson_store,
    role=agent_role,
    task_type=task_type,
)
if lessons:
    user_request += "\n\n## Lessons from past runs\n" + "\n".join(lessons)
    # Record which lessons were injected so the node can attribute scoring back
    state["injected_lesson_ids"] = [l.split(")")[0].strip("(- ") for l in lessons]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_heartbeat_context_lessons.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run heartbeat test suite**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -k "heartbeat" -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/heartbeat_context.py tests/test_heartbeat_context_lessons.py
git commit -m "feat(tier0): heartbeat_context injects matching lessons into context"
```

## Task 16: Record lesson use at end of run

**Files:**
- Modify: `agents/memory_note_node.py` (add a second hook: record_uses_node)
- Modify: `agents/graph.py`
- Create: `tests/test_record_lesson_uses.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_record_lesson_uses.py
from unittest.mock import MagicMock

from agents.memory_note_node import record_lesson_uses_node


def test_record_uses_writes_one_use_per_injected_lesson():
    fake_store = MagicMock()

    state = {
        "injected_lesson_ids": ["lesson_1", "lesson_2"],
        "run_id": "run_abc",
        "output_critic_score": 88,
    }

    record_lesson_uses_node(state, lesson_store=fake_store)

    assert fake_store.record_use.call_count == 2
    fake_store.record_use.assert_any_call(
        "lesson_1", run_id="run_abc", run_score=88,
    )
    fake_store.record_use.assert_any_call(
        "lesson_2", run_id="run_abc", run_score=88,
    )


def test_record_uses_noop_when_no_injected_lessons():
    fake_store = MagicMock()
    state = {"injected_lesson_ids": [], "run_id": "run_abc", "output_critic_score": 80}
    record_lesson_uses_node(state, lesson_store=fake_store)
    fake_store.record_use.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_record_lesson_uses.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Add record_lesson_uses_node**

```python
# Append to agents/memory_note_node.py

def record_lesson_uses_node(
    state: Dict[str, Any],
    *,
    lesson_store: LessonStore,
) -> Dict[str, Any]:
    """Record that this run used each of the injected lessons, with final score."""
    injected = state.get("injected_lesson_ids", [])
    if not injected:
        return state

    run_id = state.get("run_id", "")
    score = int(state.get("output_critic_score", 0))

    for lesson_id in injected:
        lesson_store.record_use(lesson_id, run_id=run_id, run_score=score)

    return state
```

Wire it into the graph after memory_note_node:

```python
# In agents/graph.py
from .memory_note_node import memory_note_node, record_lesson_uses_node

def _record_uses_wrapper(state):
    return record_lesson_uses_node(state, lesson_store=_lesson_store)

workflow.add_node("record_lesson_uses", _record_uses_wrapper)
workflow.add_edge("memory_note_node", "record_lesson_uses")
workflow.add_edge("record_lesson_uses", "output_formatter")  # whatever was after memory_note
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_record_lesson_uses.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/memory_note_node.py agents/graph.py tests/test_record_lesson_uses.py
git commit -m "feat(tier0): record lesson uses at end of run for outcome_delta"
```

## Task 17: Extend paperclip_client.create_issue with assignee_user_id

**Files:**
- Modify: `agents/paperclip_client.py`
- Create: `tests/test_paperclip_client_assignee.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paperclip_client_assignee.py
from unittest.mock import MagicMock, patch

from agents.paperclip_client import PaperclipClient


def test_create_issue_passes_assignee_user_id_to_server():
    client = PaperclipClient(
        api_url="http://test/api",
        company_id="co_1",
        agent_id="agent_1",
        agent_jwt_secret="secret",
    )

    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = {
            "id": "iss_1", "identifier": "TST-1",
            "title": "t", "description": "", "status": "todo",
            "priority": "medium", "labels": [],
        }
        client.create_issue(
            title="Test",
            description="body",
            labels=["self-upgrade"],
            assignee_user_id="user_abc",
        )

        # Inspect the body passed to _request
        _, kwargs = mock_request.call_args
        body = kwargs.get("json_body") or mock_request.call_args[0][2] if len(mock_request.call_args[0]) > 2 else {}
        assert body["assigneeUserId"] == "user_abc"
        assert body["labels"] == ["self-upgrade"]


def test_create_issue_omits_assignee_user_id_when_none():
    client = PaperclipClient(
        api_url="http://test/api",
        company_id="co_1",
        agent_id="agent_1",
        agent_jwt_secret="secret",
    )

    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = {
            "id": "iss_1", "identifier": "TST-1",
            "title": "t", "description": "", "status": "todo",
            "priority": "medium", "labels": [],
        }
        client.create_issue(title="Test")

        _, kwargs = mock_request.call_args
        body = kwargs.get("json_body", {})
        assert "assigneeUserId" not in body
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_paperclip_client_assignee.py -v
```

Expected: fails — `create_issue` doesn't accept `assignee_user_id`.

- [ ] **Step 3: Extend create_issue**

In `agents/paperclip_client.py`, modify the `create_issue` method:

```python
def create_issue(
    self,
    title: str,
    description: str = "",
    priority: str = "medium",
    labels: Optional[List[str]] = None,
    assignee_user_id: Optional[str] = None,
) -> Issue:
    """POST /api/companies/{companyId}/issues — create a top-level issue.

    Args:
        title: Issue title.
        description: Issue body (markdown).
        priority: low/medium/high.
        labels: List of label names to attach.
        assignee_user_id: If set, assigns the issue to a human user rather
            than an agent. Use this for self-upgrade reports so the server-side
            label-based routing at heartbeat.ts doesn't auto-process them.
    """
    body: Dict[str, Any] = {
        "title": title,
        "description": description,
        "priority": priority,
    }
    if labels:
        body["labels"] = labels
    if assignee_user_id:
        body["assigneeUserId"] = assignee_user_id

    data = self._request(
        "POST",
        f"/api/companies/{self.company_id}/issues",
        json_body=body,
    )
    return _parse_issue(data)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_paperclip_client_assignee.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Verify server-side support**

The Paperclip server endpoint must accept `assigneeUserId` in the request body. Check:

```bash
cd ~/Repos/paperclip && grep -n "assigneeUserId\|assignee_user_id" server/src/routes/issues.ts server/src/services/issues.ts 2>/dev/null
```

**If the server doesn't accept `assigneeUserId`:** this task is blocked. Open a separate Paperclip PR to add server support before continuing. The Paperclip PR scope: accept `assigneeUserId` on `POST /api/companies/:id/issues` and persist it to the `assignee_user_id` column on the `issues` table. Verify with a manual curl test after the Paperclip PR lands.

**If the server already accepts it:** proceed to Step 6.

- [ ] **Step 6: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/paperclip_client.py tests/test_paperclip_client_assignee.py
git commit -m "feat(paperclip-client): add assignee_user_id to create_issue"
```

## Task 18: Add VIBE_HUMAN_TRIAGE_USER_ID to config

**Files:**
- Modify: `agents/config.py`
- Modify: `tests/test_config.py` (or equivalent)

- [ ] **Step 1: Find the config test file**

```bash
cd ~/Repos/Vibe-Stack && ls tests/test_config*.py tests/test_system_config*.py 2>&1 | head -5
```

- [ ] **Step 2: Write the failing test**

```python
# Add to tests/test_config.py (or wherever SystemConfig is tested)
import os
from unittest.mock import patch

from agents.config import SystemConfig


def test_human_triage_user_id_read_from_env():
    with patch.dict(os.environ, {"VIBE_HUMAN_TRIAGE_USER_ID": "user_prime_123"}):
        cfg = SystemConfig.from_env()
        assert cfg.human_triage_user_id == "user_prime_123"


def test_human_triage_user_id_defaults_to_empty():
    with patch.dict(os.environ, {}, clear=True):
        cfg = SystemConfig.from_env()
        assert cfg.human_triage_user_id == ""
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_config.py -v -k "human_triage"
```

Expected: `AttributeError: 'SystemConfig' object has no attribute 'human_triage_user_id'`

- [ ] **Step 4: Add the field**

In `agents/config.py`:

1. Add `human_triage_user_id: str = ""` to the relevant `SystemConfig` (or `SelfUpgradeConfig`) dataclass.
2. In the `from_env()` or equivalent factory, read `VIBE_HUMAN_TRIAGE_USER_ID` with a default of `""`.

- [ ] **Step 5: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_config.py -v -k "human_triage"
```

Expected: 2 passed.

- [ ] **Step 6: Update .env.example**

```bash
cd ~/Repos/Vibe-Stack && grep -n "VIBE_" .env.example | head -5
```

Append to `.env.example`:

```bash
# Self-upgrade Tier 3 reports and Tier 1b/2 companion issues are assigned to
# this user (not an agent) so they stay out of agent queues and land in the
# Improvements tab for human triage. Set to the Paperclip user ID of whoever
# owns self-upgrade triage.
VIBE_HUMAN_TRIAGE_USER_ID=
```

- [ ] **Step 7: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/config.py tests/test_config.py .env.example
git commit -m "feat(config): VIBE_HUMAN_TRIAGE_USER_ID for self-upgrade triage"
```

## Task 19: IssueReport and EvidenceRow dataclasses + renderer

**Files:**
- Create: `agents/self_upgrade/reports.py`
- Create: `tests/test_self_upgrade_reports.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_self_upgrade_reports.py
from agents.self_upgrade.reports import (
    EvidenceRow, IssueReport, render_report,
)


def test_render_report_produces_markdown_with_yaml_frontmatter():
    report = IssueReport(
        report_id="report_01",
        title="Critic can't score empty feedback",
        signal_refs=["sig_1", "sig_2"],
        evidence=[
            EvidenceRow(
                run_id="run_abc", task_type="code_generation",
                score=40, excerpt="Score 40/100",
            ),
        ],
        hypothesis="heuristic_critic returns 40 when feedback is empty",
        suggested_change="Return None + skip persistence when no actionable feedback",
        suggested_change_kind="code",
        confidence=0.75,
        author_agent_id="agent_1",
        author_role="backend_engineer",
        created_at="2026-04-06T00:00:00Z",
    )

    rendered = render_report(report)

    # YAML frontmatter block
    assert rendered.startswith("---\n")
    assert "report_id: report_01" in rendered
    assert "tier: 3" in rendered
    assert "kind: code" in rendered
    assert "confidence: 0.75" in rendered
    # Body sections
    assert "## Hypothesis" in rendered
    assert "## Evidence" in rendered
    assert "## Suggested change" in rendered
    assert "run_abc" in rendered
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_reports.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write the dataclasses + renderer**

```python
# agents/self_upgrade/reports.py
"""Tier 3 issue report data model and markdown renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal


@dataclass
class EvidenceRow:
    run_id: str
    task_type: str
    score: int
    excerpt: str  # <= 500 chars


@dataclass
class IssueReport:
    report_id: str
    title: str
    signal_refs: List[str]
    evidence: List[EvidenceRow]
    hypothesis: str
    suggested_change: str
    suggested_change_kind: Literal["code", "config", "infra", "prompt", "data", "external"]
    confidence: float
    author_agent_id: str
    author_role: str
    created_at: str


def render_report(report: IssueReport) -> str:
    """Render an IssueReport as markdown with a YAML frontmatter block."""
    frontmatter_lines = [
        "---",
        f"report_id: {report.report_id}",
        "tier: 3",
        f"kind: {report.suggested_change_kind}",
        f"confidence: {report.confidence}",
        f"author_agent_id: {report.author_agent_id}",
        f"author_role: {report.author_role}",
        f"created_at: {report.created_at}",
        "signal_refs:",
    ]
    for sid in report.signal_refs:
        frontmatter_lines.append(f"  - {sid}")
    frontmatter_lines.append("---")

    body_lines = [
        "",
        "## Hypothesis",
        "",
        report.hypothesis,
        "",
        "## Suggested change",
        "",
        report.suggested_change,
        "",
        "## Evidence",
        "",
    ]
    for ev in report.evidence:
        body_lines.append(
            f"- **run {ev.run_id}** (task: {ev.task_type}, score: {ev.score})"
        )
        body_lines.append(f"  > {ev.excerpt}")

    return "\n".join(frontmatter_lines + body_lines)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_reports.py -v
```

Expected: passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/self_upgrade/reports.py tests/test_self_upgrade_reports.py
git commit -m "feat(tier3): IssueReport dataclass and markdown renderer"
```

## Task 20: Tier3Builder with LLM draft + self-critique

**Files:**
- Create: `agents/self_upgrade/tier3_builder.py`
- Create: `tests/test_tier3_builder.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tier3_builder.py
import json
from unittest.mock import MagicMock

from agents.self_upgrade.tier3_builder import Tier3Builder, Tier3Result
from agents.self_upgrade_trigger import UpgradeSignal


def _fake_llm_returning(draft_json: dict, critique_score: int = 80):
    """Fake LLM that returns a draft on first call and a critique on second."""
    llm = MagicMock()
    llm.generate.side_effect = [
        json.dumps(draft_json),
        json.dumps({"score": critique_score, "feedback": "ok"}),
    ]
    return llm


def test_builder_produces_report_on_self_critique_pass():
    llm = _fake_llm_returning(
        draft_json={
            "title": "Critic scores are empty",
            "hypothesis": "Feedback strings are missing",
            "suggested_change": "Return None from heuristic_critic when no feedback",
            "suggested_change_kind": "code",
            "confidence": 0.7,
        },
        critique_score=80,
    )

    builder = Tier3Builder(llm=llm)
    signals = [
        UpgradeSignal(
            category="low_score", task_type="code_generation",
            detail="Score 40/100", score=40, source_node="critic",
        ),
    ]

    result = builder.build(
        signals,
        author_agent_id="agent_1",
        author_role="backend_engineer",
    )

    assert isinstance(result, Tier3Result.ReportDrafted)
    assert result.report.title == "Critic scores are empty"
    assert result.report.suggested_change_kind == "code"
    assert len(result.report.evidence) == 1


def test_builder_drops_on_self_critique_fail():
    llm = _fake_llm_returning(
        draft_json={
            "title": "vague",
            "hypothesis": "?",
            "suggested_change": "fix it",
            "suggested_change_kind": "code",
            "confidence": 0.4,
        },
        critique_score=60,  # below 70 threshold
    )

    builder = Tier3Builder(llm=llm)
    signals = [
        UpgradeSignal(
            category="low_score", task_type="t", detail="d", score=40,
        ),
    ]

    result = builder.build(
        signals, author_agent_id="", author_role="",
    )
    assert isinstance(result, Tier3Result.Dropped)
    assert "self-critique" in result.reason


def test_builder_returns_empty_on_no_signals():
    builder = Tier3Builder(llm=MagicMock())
    result = builder.build([], author_agent_id="", author_role="")
    assert isinstance(result, Tier3Result.Dropped)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_tier3_builder.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write Tier3Builder**

```python
# agents/self_upgrade/tier3_builder.py
"""Tier 3 builder — drafts an IssueReport and self-critiques before filing."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Protocol, Union
from uuid import uuid4

from ..self_upgrade_trigger import UpgradeSignal
from .reports import EvidenceRow, IssueReport

logger = logging.getLogger(__name__)

SELF_CRITIQUE_THRESHOLD = 70


class _LLMProtocol(Protocol):
    def generate(self, prompt: str, max_tokens: int = 500) -> str: ...


class Tier3Result:
    @dataclass
    class ReportDrafted:
        report: IssueReport

    @dataclass
    class Dropped:
        reason: str
        signal_refs: List[str]

    Union = Union["Tier3Result.ReportDrafted", "Tier3Result.Dropped"]


_DRAFT_PROMPT = """\
You are drafting a structured bug report from accumulated signal evidence.

Signals:
{signals_block}

Produce a JSON object with EXACTLY these fields:
- title: short, PR-style title (≤ 80 chars)
- hypothesis: one paragraph explaining what you think is wrong
- suggested_change: one paragraph describing what to change (prose, not code)
- suggested_change_kind: one of "code" | "config" | "infra" | "prompt" | "data" | "external"
- confidence: float 0.0-1.0

Return ONLY the JSON object, no preamble or commentary.
"""

_CRITIQUE_PROMPT = """\
Evaluate the following draft bug report on three axes:
- evidence quality (is there concrete evidence, not just hand-waving?)
- clarity (is the hypothesis clear and specific?)
- actionability (is the suggested change something a human could act on?)

Draft:
{draft}

Return ONLY a JSON object: {{"score": <0-100>, "feedback": "<one sentence>"}}.
"""


class Tier3Builder:
    def __init__(self, llm: _LLMProtocol) -> None:
        self._llm = llm

    def build(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str,
        author_role: str,
    ) -> "Tier3Result.Union":
        if not signals:
            return Tier3Result.Dropped(reason="no signals", signal_refs=[])

        # Draft the report
        draft = self._draft(signals)
        if draft is None:
            return Tier3Result.Dropped(
                reason="failed to parse LLM draft as JSON",
                signal_refs=[s.id for s in signals],
            )

        # Self-critique
        critique_score = self._self_critique(draft)
        if critique_score < SELF_CRITIQUE_THRESHOLD:
            return Tier3Result.Dropped(
                reason=f"self-critique scored {critique_score} < {SELF_CRITIQUE_THRESHOLD}",
                signal_refs=[s.id for s in signals],
            )

        # Build the final report
        report = IssueReport(
            report_id=f"report_{uuid4().hex[:12]}",
            title=draft["title"],
            signal_refs=[s.id for s in signals],
            evidence=[
                EvidenceRow(
                    run_id=s.source_node or "unknown",
                    task_type=s.task_type,
                    score=s.score,
                    excerpt=s.detail[:500],
                )
                for s in signals[:10]
            ],
            hypothesis=draft["hypothesis"],
            suggested_change=draft["suggested_change"],
            suggested_change_kind=draft["suggested_change_kind"],
            confidence=float(draft.get("confidence", 0.5)),
            author_agent_id=author_agent_id,
            author_role=author_role,
            created_at=datetime.utcnow().isoformat() + "Z",
        )

        return Tier3Result.ReportDrafted(report=report)

    def _draft(self, signals: List[UpgradeSignal]) -> dict | None:
        signals_block = "\n".join(
            f"- [{s.category}] task={s.task_type} score={s.score}: {s.detail}"
            for s in signals[:10]
        )
        prompt = _DRAFT_PROMPT.format(signals_block=signals_block)
        raw = self._llm.generate(prompt, max_tokens=500).strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Tier3Builder: LLM draft was not valid JSON: %s", raw[:200])
            return None

    def _self_critique(self, draft: dict) -> int:
        prompt = _CRITIQUE_PROMPT.format(draft=json.dumps(draft, indent=2))
        raw = self._llm.generate(prompt, max_tokens=200).strip()

        try:
            parsed = json.loads(raw)
            return int(parsed.get("score", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            return 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_tier3_builder.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/self_upgrade/tier3_builder.py tests/test_tier3_builder.py
git commit -m "feat(tier3): builder with LLM draft and self-critique gate"
```

## Task 21: Wire Tier 0 and Tier 3 builders into dispatcher

**Files:**
- Modify: `agents/self_upgrade_dispatcher.py`
- Modify: `tests/test_self_upgrade_dispatcher.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_self_upgrade_dispatcher.py
from unittest.mock import MagicMock

from agents.self_upgrade.reports import IssueReport, EvidenceRow
from agents.self_upgrade.tier0_builder import Tier0Result
from agents.self_upgrade.tier3_builder import Tier3Result


def test_dispatch_tier0_writes_lesson_via_store():
    fake_store = MagicMock()
    fake_store.add.return_value = "lesson_xyz"

    fake_tier0 = MagicMock()
    fake_tier0.build.return_value = Tier0Result.LessonDrafted(
        lesson="x", role="r", task_type="t", tag="",
        signal_refs=["sig_1"],
    )

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=fake_store,
        tier0_builder=fake_tier0,
        tier3_builder=MagicMock(),
        paperclip_client=MagicMock(),
        human_triage_user_id="user_prime",
    )

    signals = [_make_signal(detail="single actionable")]
    result = dispatcher.dispatch(
        signals, author_agent_id="a", author_run_id="r", role="backend",
    )

    assert isinstance(result, DispatchResult.Tier0Written)
    assert result.lesson_id == "lesson_xyz"
    fake_store.add.assert_called_once()


def test_dispatch_tier3_files_paperclip_issue():
    fake_client = MagicMock()
    fake_client.create_issue.return_value = MagicMock(id="iss_42")

    fake_report = IssueReport(
        report_id="report_1", title="T", signal_refs=["sig_1", "sig_2", "sig_3"],
        evidence=[EvidenceRow(run_id="", task_type="t", score=0, excerpt="")],
        hypothesis="", suggested_change="", suggested_change_kind="code",
        confidence=0.8, author_agent_id="", author_role="", created_at="",
    )
    fake_tier3 = MagicMock()
    fake_tier3.build.return_value = Tier3Result.ReportDrafted(report=fake_report)

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=MagicMock(),
        tier0_builder=MagicMock(),
        tier3_builder=fake_tier3,
        paperclip_client=fake_client,
        human_triage_user_id="user_prime",
    )

    # 3 empty-feedback signals → classifier picks Tier 3
    signals = [_make_signal(detail="") for _ in range(3)]
    result = dispatcher.dispatch(
        signals, author_agent_id="", author_run_id="", role="",
    )

    assert isinstance(result, DispatchResult.Tier3Filed)
    fake_client.create_issue.assert_called_once()
    _, call_kwargs = fake_client.create_issue.call_args
    assert call_kwargs["assignee_user_id"] == "user_prime"
    assert "self-upgrade" in call_kwargs["labels"]
    assert "tier-3" in call_kwargs["labels"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_dispatcher.py -v
```

Expected: the existing stub test still passes, but the new tests fail because `SelfUpgradeDispatcher.__init__` doesn't accept those kwargs yet.

- [ ] **Step 3: Extend SelfUpgradeDispatcher to wire Tier 0 and Tier 3**

In `agents/self_upgrade_dispatcher.py`:

```python
# Replace SelfUpgradeDispatcher class:

from typing import Optional

from .lesson_store import LessonStore
from .paperclip_client import PaperclipClient
from .self_upgrade.reports import render_report
from .self_upgrade.tier0_builder import Tier0Builder, Tier0Result
from .self_upgrade.tier3_builder import Tier3Builder, Tier3Result


class SelfUpgradeDispatcher:
    def __init__(
        self,
        *,
        lesson_store: Optional[LessonStore] = None,
        tier0_builder: Optional[Tier0Builder] = None,
        tier3_builder: Optional[Tier3Builder] = None,
        paperclip_client: Optional[PaperclipClient] = None,
        human_triage_user_id: str = "",
    ) -> None:
        self._lesson_store = lesson_store
        self._tier0 = tier0_builder
        self._tier3 = tier3_builder
        self._paperclip = paperclip_client
        self._human_triage_user_id = human_triage_user_id

    def classify_signals(self, signals: List[UpgradeSignal]) -> Tier:
        # unchanged from M0
        ...

    def dispatch(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str = "",
        author_run_id: str = "",
        role: str = "*",
    ) -> "DispatchResult.Union":
        tier = self.classify_signals(signals)
        sig_refs = [s.id for s in signals]

        if tier == Tier.ZERO:
            return self._handle_tier0(signals, author_agent_id, author_run_id, role)
        if tier == Tier.THREE:
            return self._handle_tier3(signals, author_agent_id, role)

        # Tier 1a/1b/2 still stubs in M1
        return DispatchResult.Rejected(
            reason=f"tier {tier.value} not implemented yet",
            signal_refs=sig_refs,
        )

    def _handle_tier0(
        self,
        signals: List[UpgradeSignal],
        author_agent_id: str,
        author_run_id: str,
        role: str,
    ) -> "DispatchResult.Union":
        if self._tier0 is None or self._lesson_store is None:
            return DispatchResult.Rejected(
                reason="tier0 dependencies not wired",
                signal_refs=[s.id for s in signals],
            )

        result = self._tier0.build(
            signals,
            author_agent_id=author_agent_id,
            author_run_id=author_run_id,
            role=role,
        )

        if isinstance(result, Tier0Result.Empty):
            return DispatchResult.Rejected(
                reason=f"tier0 builder empty: {result.reason}",
                signal_refs=[s.id for s in signals],
            )

        assert isinstance(result, Tier0Result.LessonDrafted)
        lesson_id = self._lesson_store.add(
            role=result.role,
            task_type=result.task_type,
            tag=result.tag,
            lesson=result.lesson,
            author_agent_id=author_agent_id,
            author_run_id=author_run_id,
        )
        return DispatchResult.Tier0Written(
            lesson_id=lesson_id,
            signal_refs=result.signal_refs,
        )

    def _handle_tier3(
        self,
        signals: List[UpgradeSignal],
        author_agent_id: str,
        role: str,
    ) -> "DispatchResult.Union":
        if self._tier3 is None or self._paperclip is None:
            return DispatchResult.Rejected(
                reason="tier3 dependencies not wired",
                signal_refs=[s.id for s in signals],
            )

        result = self._tier3.build(
            signals,
            author_agent_id=author_agent_id,
            author_role=role,
        )

        if isinstance(result, Tier3Result.Dropped):
            return DispatchResult.Rejected(
                reason=f"tier3 dropped: {result.reason}",
                signal_refs=[s.id for s in signals],
            )

        assert isinstance(result, Tier3Result.ReportDrafted)
        report = result.report

        issue = self._paperclip.create_issue(
            title=f"[self-report] {report.title}",
            description=render_report(report),
            labels=[
                "self-upgrade",
                "auto-generated",
                "tier-3",
                f"kind:{report.suggested_change_kind}",
            ],
            assignee_user_id=self._human_triage_user_id or None,
        )

        return DispatchResult.Tier3Filed(
            issue_id=issue.id,
            signal_refs=report.signal_refs,
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_self_upgrade_dispatcher.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add agents/self_upgrade_dispatcher.py tests/test_self_upgrade_dispatcher.py
git commit -m "feat(self-upgrade): wire Tier 0 and Tier 3 builders into dispatcher"
```

## Task 22: End-to-end Milestone 1 integration test

**Files:**
- Create: `tests/test_milestone1_e2e.py`

- [ ] **Step 1: Write a full integration test**

```python
# tests/test_milestone1_e2e.py
"""End-to-end test: simulated signal accumulation → dispatcher → real LessonStore + fake Paperclip client."""

from unittest.mock import MagicMock
import pytest

from agents.lesson_store import LessonStore
from agents.self_upgrade.tier0_builder import Tier0Builder
from agents.self_upgrade.tier3_builder import Tier3Builder
from agents.self_upgrade_dispatcher import (
    DispatchResult,
    SelfUpgradeDispatcher,
)
from agents.self_upgrade_trigger import UpgradeSignal


def _signal(**overrides):
    defaults = dict(
        category="low_score",
        task_type="code_generation",
        detail="Missing Pydantic validation on request body",
        score=60,
        source_node="critic",
    )
    defaults.update(overrides)
    return UpgradeSignal(**defaults)


def test_single_actionable_signal_writes_lesson(tmp_path):
    lesson_store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    fake_llm = MagicMock()
    fake_llm.generate.return_value = "Include Pydantic validation for FastAPI endpoints."

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=lesson_store,
        tier0_builder=Tier0Builder(llm=fake_llm),
        tier3_builder=MagicMock(),
        paperclip_client=MagicMock(),
        human_triage_user_id="user_prime",
    )

    result = dispatcher.dispatch(
        [_signal()],
        author_agent_id="agent_1",
        author_run_id="run_1",
        role="backend_engineer",
    )

    assert isinstance(result, DispatchResult.Tier0Written)

    # Lesson is retrievable
    lessons = lesson_store.list_by_scope(
        role="backend_engineer",
        task_type="code_generation",
    )
    assert len(lessons) == 1
    assert "Pydantic" in lessons[0].lesson


def test_empty_feedback_cluster_files_tier3_report(tmp_path):
    import json

    lesson_store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    fake_llm = MagicMock()
    fake_llm.generate.side_effect = [
        json.dumps({
            "title": "Critic returns empty feedback",
            "hypothesis": "heuristic_critic falls through without feedback",
            "suggested_change": "Log a warning and skip signal persistence when feedback is empty",
            "suggested_change_kind": "code",
            "confidence": 0.8,
        }),
        json.dumps({"score": 85, "feedback": "clear and actionable"}),
    ]

    fake_client = MagicMock()
    fake_client.create_issue.return_value = MagicMock(id="iss_42")

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=lesson_store,
        tier0_builder=MagicMock(),
        tier3_builder=Tier3Builder(llm=fake_llm),
        paperclip_client=fake_client,
        human_triage_user_id="user_prime",
    )

    signals = [_signal(detail="") for _ in range(3)]
    result = dispatcher.dispatch(
        signals, author_agent_id="a", author_run_id="r", role="*",
    )

    assert isinstance(result, DispatchResult.Tier3Filed)
    assert result.issue_id == "iss_42"
    fake_client.create_issue.assert_called_once()
    _, kwargs = fake_client.create_issue.call_args
    assert "self-upgrade" in kwargs["labels"]
    assert "tier-3" in kwargs["labels"]
    assert kwargs["assignee_user_id"] == "user_prime"
```

- [ ] **Step 2: Run the integration test**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/test_milestone1_e2e.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Run the full self_upgrade and related test subset**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -k "self_upgrade or lesson or tier0 or tier3 or dispatcher" -v
```

Expected: all pass.

- [ ] **Step 4: Run the whole test suite to check for regressions**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -x -m "not e2e" --no-header -q
```

Expected: all pass (or same count of failures as before this milestone).

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack && git add tests/test_milestone1_e2e.py
git commit -m "test(self-upgrade): end-to-end M1 integration test"
```

## Milestone 1 complete

At this point:
- `LessonStore` persists memory notes per-install
- `Tier0Builder` drafts lessons from signal clusters via an LLM call
- `memory_note_node` writes lessons when the critic flags a run as lesson-eligible
- `heartbeat_context.py` injects matching lessons into future runs
- Lesson uses are tracked, `outcome_delta` is computed, decay check archives underperformers
- `PaperclipClient.create_issue()` supports `assignee_user_id`
- `Tier3Builder` drafts reports with a self-critique gate
- Dispatcher routes Tier 0 and Tier 3 to real implementations
- Filed reports appear in the Improvements tab of the Paperclip UI (via the pre-existing label filter)
- Everything is reversible: delete a lesson row, close a Paperclip issue

The first real agent run after M1 ships should produce visible artifacts. If none appear after a dozen runs, debug the classifier and end-of-run dispatcher hook before assuming the plumbing is broken — most likely cause is that `critic_nodes.py` isn't producing non-empty feedback strings (which means `lesson_eligible` never becomes True).

---

# MILESTONE 2 — Tier 1a skill refinement

> **Note:** M2 tasks are task-level, not bite-sized. **Re-plan this milestone with `writing-plans` skill before execution.** The file paths and test files below are concrete, but the exact code will depend on the state of `skill_generator.py` and `skill_outcome_store.py` at the time of execution.

## Task 23: Add version field to skill_outcome_store

**Files:**
- Modify: `agents/skill_outcome_store.py`
- Create: `tests/test_skill_version_field.py`

**Acceptance:** each outcome record carries a `version: str` field (default `"v1"`). `list_outcomes(skill_name, version=None)` filters. Schema migration preserves existing outcomes as `v1`. TDD: write the test that stores two outcomes with different versions and retrieves them separately.

**Commit:** `feat(skill-outcome): add version field for A/B cooldown`

## Task 24: pick_active_version with deterministic bucket

**Files:**
- Modify: `agents/skill_outcome_store.py`
- Create: `tests/test_pick_active_version.py`

**Acceptance:** `pick_active_version(skill_name, run_id)` returns `"v1"` or `"v2"` based on `hash(run_id) % 2` when both versions exist. Returns the only version when one exists. Returns the higher-avg version with uses ≥ K (default 10) when the cooldown is complete.

**Commit:** `feat(skill-outcome): A/B picker with deterministic bucket`

## Task 25: skill_generator.refine() mode

**Files:**
- Modify: `agents/skill_generator.py`
- Create: `tests/test_skill_generator_refine.py`

**Acceptance:** `refine(existing_skill, accumulated_feedback) -> Skill` prompts the LLM with the existing skill content + feedback, receives an improved SKILL.md as output, validates its structure, returns a new `Skill` with `version="v2"`. Fails cleanly if the LLM output doesn't parse as valid SKILL.md.

**Commit:** `feat(skill-generator): add refine() mode for Tier 1a`

## Task 26: skill_loader picks active version on load

**Files:**
- Modify: `agents/skill_loader.py`
- Create: `tests/test_skill_loader_version.py`

**Acceptance:** when loading a skill that has multiple versions, `skill_loader.load(skill_name, run_id)` calls `skill_outcome_store.pick_active_version()` and returns the right one. Single-version skills unchanged.

**Commit:** `feat(skill-loader): support A/B version picking`

## Task 27: Tier1aBuilder

**Files:**
- Create: `agents/self_upgrade/tier1a_builder.py`
- Create: `tests/test_tier1a_builder.py`

**Acceptance:** `Tier1aBuilder.build(signals, skill_loader, skill_generator) -> SkillRefinementRequest | Empty`. Checks if the signal cluster's task type has a poorly-scoring skill. If yes, calls `skill_generator.refine()` and persists v2. If no skill exists, returns Empty (the existing ephemeral generation path handles new-skill creation).

**Commit:** `feat(tier1a): skill refinement builder`

## Task 28: Wire Tier 1a into dispatcher

**Files:**
- Modify: `agents/self_upgrade_dispatcher.py`
- Modify: `tests/test_self_upgrade_dispatcher.py`

**Acceptance:** the classifier routes "low_score cluster on task_type with existing poorly-scoring skill" to Tier 1a. The dispatcher calls `Tier1aBuilder.build()`, persists the v2 skill, returns `DispatchResult.Tier1aQueued(refinement_id, cooldown_until)`.

**Commit:** `feat(self-upgrade): wire Tier 1a into dispatcher`

## Milestone 2 complete

At this point, signal clusters indicating poor skill performance trigger an A/B refinement cycle. After the cooldown (default 10 uses per version), the winner is kept and the loser is archived.

---

# MILESTONE 3 — Tier 1b prompt overrides

> **Note:** M3 tasks are task-level. **Re-plan this milestone with `writing-plans` skill before execution.** This milestone includes the most new infrastructure (canonical fixtures, prompt-critic, loader, smoke harness) and is the most likely to benefit from a fresh planning pass.

## Task 29: prompt_library package + empty overrides.yaml

**Files:**
- Create: `agents/prompt_library/__init__.py`
- Create: `agents/prompt_library/overrides.yaml` (initial content: `overrides: []`)
- Create: `tests/test_prompt_library_init.py`

**Acceptance:** the package is importable, the YAML file parses, and an empty file is a valid "no overrides" state.

**Commit:** `feat(prompt-library): initial package structure`

## Task 30: OverrideLoader with scope matching

**Files:**
- Create: `agents/prompt_library/loader.py`
- Create: `tests/test_prompt_override_loader.py`

**Acceptance:** `OverrideLoader.load_all()` reads overrides.yaml, parses entries, filters by `status=active`. `get_matching(adapter_type, task_type, tag)` returns the list of matching appends. Wildcard matching on task_type and tag. Validates schema (required fields, `append` ≤ 500 chars, status ∈ known values).

**Commit:** `feat(prompt-library): override loader with scope matching`

## Task 31: AdapterRegistry merges overrides on get_or_create

**Files:**
- Modify: `agents/adapters.py`
- Create: `tests/test_adapter_registry_overrides.py`

**Acceptance:** `AdapterRegistry.__init__` instantiates `OverrideLoader` and loads overrides once. `get_or_create(adapter_type, context)` appends matching overrides to the base prompt. Unchanged when no overrides match. Caches the merged result per (adapter_type, scope) tuple.

**Commit:** `feat(adapters): merge prompt_library overrides at get_or_create`

## Task 32: canonical_harvester captures high-scoring runs as fixtures

**Files:**
- Create: `agents/canonical_harvester.py`
- Modify: `agents/critic_nodes.py`
- Create: `tests/canonical/README.md`
- Create: `tests/test_canonical_harvester.py`

**Acceptance:** at end-of-run, when `critic_score ≥ 90`, `canonical_harvester.maybe_capture(adapter_type, state)` stores the task prompt + expected keywords + baseline score as a JSON fixture under `tests/canonical/{adapter_type}/task_{N}.json`. Caps at 10 fixtures per adapter type. Updates `tests/canonical/{adapter_type}/baseline.json` with the running average baseline score.

**Commit:** `feat(canonical): harvester captures high-scoring runs as fixtures`

## Task 33: canonical_smoke runs fixtures against a proposed override

**Files:**
- Create: `agents/self_upgrade/canonical_smoke.py`
- Create: `tests/test_canonical_smoke.py`

**Acceptance:** `run_smoke_test(adapter_type, override_yaml) -> SmokeResult` loads fixtures, runs each through the adapter with the override applied, scores via the critic, compares to baseline. Returns `SmokeResult(passed: bool, scores: list[int], regressions: list[str])`. Tolerance: average score must not be more than 5 points below baseline average.

**Commit:** `feat(canonical-smoke): run fixtures against proposed prompt overrides`

## Task 34: Tier1bPipeline with schema + append-only + prompt-critic + smoke

**Files:**
- Create: `agents/self_upgrade/tier1b_pipeline.py`
- Create: `tests/test_tier1b_pipeline.py`

**Acceptance:** `Tier1bPipeline.execute(override: PromptOverride) -> Tier1bResult`. Gates in order: schema validation → append-only diff check → prompt-critic ≥ 80 → canonical smoke. On pass, applies the override to `overrides.yaml`, commits to `vibe/self-upgrade/prompt-override-{id}`, opens a PR. On fail, returns Rejected with the gate that failed.

**Commit:** `feat(tier1b): pipeline with schema + append-only + prompt-critic + smoke`

## Task 35: Tier1bBuilder

**Files:**
- Create: `agents/self_upgrade/tier1b_builder.py`
- Create: `tests/test_tier1b_builder.py`

**Acceptance:** `Tier1bBuilder.build(signals, adapter_type, scope) -> PromptOverride | Empty`. Uses the LLM (critic adapter) to draft an `append` string from the signal cluster. Returns Empty on LLM failure. The builder does NOT run the pipeline — it just drafts.

**Commit:** `feat(tier1b): builder drafts prompt overrides from signal clusters`

## Task 36: Wire Tier 1b into dispatcher + file companion issue

**Files:**
- Modify: `agents/self_upgrade_dispatcher.py`
- Modify: `tests/test_self_upgrade_dispatcher.py`

**Acceptance:** classifier routes "repeated same critic_pattern, same task_type" to Tier 1b. Dispatcher calls `Tier1bBuilder.build()` → `Tier1bPipeline.execute()`. On success, files a companion Paperclip issue via `create_issue()` with branch/commit/gate outputs in a YAML frontmatter block (per spec §Paperclip Integration → Companion issue body format) and `assignee_user_id=human_triage_user_id`. Returns `DispatchResult.Tier1bCommitted(branch, commit, pr_url, issue_id)`.

**Commit:** `feat(self-upgrade): wire Tier 1b with companion issue filing`

## Milestone 3 complete

At this point, signal patterns suggesting "the prompt is silent about X" trigger the proposal of an append-only prompt override, which gates through a canonical smoke test and lands as a PR + companion issue in the Improvements tab for your review.

---

# MILESTONE 4 — Tier 2 typed code edits

> **Note:** M4 tasks are task-level. **Re-plan this milestone with `writing-plans` skill before execution.** The `libcst` AST work is the most uncertain — the actual API surface we need will depend on libcst's current version and could benefit from a quick proof-of-concept spike before writing step-level tests.

## Task 37: Add libcst dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements-production.lock`

**Acceptance:** `libcst>=1.0` added to deps. Lock file regenerated. `python -c "import libcst"` succeeds.

**Commit:** `chore(deps): add libcst for Tier 2 AST verification`

## Task 38: TypedEdit dataclasses

**Files:**
- Create: `agents/self_upgrade/typed_edits.py`
- Create: `tests/test_typed_edits.py`

**Acceptance:** `TypedEdit` + 5 per-type edit dataclasses (`PromptConstantEdit`, `ThresholdEdit`, `DictListAppendEdit`, `DocstringEdit`, `NewTestFileEdit`) exactly as specified in spec §Tier 2 Proposal format. Schema validation method `TypedEdit.validate() -> list[str]` returns per-field errors.

**Commit:** `feat(tier2): TypedEdit dataclasses for 5 edit types`

## Task 39: Allowlists module

**Files:**
- Create: `agents/self_upgrade/allowlists.py`
- Create: `tests/test_allowlists.py`

**Acceptance:** module exports `ALLOWLIST_BY_EDIT_TYPE: dict[str, set[str]]` matching spec §Tier 2 The 5 edit types table. Helper `is_allowed(edit_type, rel_path) -> bool` with glob support for `new_test_file`.

**Commit:** `feat(tier2): file allowlists per edit type`

## Task 40: AST verifier — docstring (cheapest first)

**Files:**
- Create: `agents/self_upgrade/ast_verifier.py` (initial, docstring only)
- Create: `tests/test_ast_verifier_docstring.py`

**Acceptance:** `TypedEditValidator.verify(typed_edit: TypedEdit, current_file_content: str) -> VerifyResult`. For `edit_type="docstring"`: parses file with libcst, locates the named symbol, verifies only its docstring changed, no other node in the tree touched. Rejects edits that try to change more than the docstring.

**Commit:** `feat(tier2): AST verifier for docstring edits`

## Task 41: AST verifier — new_test_file

**Files:**
- Modify: `agents/self_upgrade/ast_verifier.py`
- Create: `tests/test_ast_verifier_new_test_file.py`

**Acceptance:** for `edit_type="new_test_file"`: verifies the target path is under `tests/`, doesn't exist, and the content parses as a valid Python module whose top-level statements are only imports and `test_*` function definitions. Rejects modules with top-level side effects.

**Commit:** `feat(tier2): AST verifier for new_test_file`

## Task 42: AST verifier — dict_list_append

**Files:**
- Modify: `agents/self_upgrade/ast_verifier.py`
- Create: `tests/test_ast_verifier_dict_list_append.py`

**Acceptance:** for `edit_type="dict_list_append"`: locates the named container, verifies every existing element is preserved byte-for-byte, verifies the diff is purely insertions, verifies new entries match the inferred element type.

**Commit:** `feat(tier2): AST verifier for dict_list_append`

## Task 43: AST verifier — threshold_tweak

**Files:**
- Modify: `agents/self_upgrade/ast_verifier.py`
- Create: `tests/test_ast_verifier_threshold.py`

**Acceptance:** for `edit_type="threshold_tweak"`: locates the named constant, verifies the only change is the RHS literal, verifies `old_value` matches current RHS and `new_value` matches the same literal type.

**Commit:** `feat(tier2): AST verifier for threshold_tweak`

## Task 44: AST verifier — prompt_constant

**Files:**
- Modify: `agents/self_upgrade/ast_verifier.py`
- Create: `tests/test_ast_verifier_prompt_constant.py`

**Acceptance:** for `edit_type="prompt_constant"`: locates the named constant in `agents/adapters.py`, verifies only the RHS changed, verifies RHS remains a string literal.

**Commit:** `feat(tier2): AST verifier for prompt_constant`

## Task 45: signal_replay for threshold_tweak gate

**Files:**
- Create: `agents/self_upgrade/signal_replay.py`
- Create: `tests/test_signal_replay.py`

**Acceptance:** `replay_signals(path_to_signals, old_threshold, new_threshold) -> ReplayResult`. Loads historical signals, re-runs the classification function with both thresholds, computes the classification counts. Returns pass/fail based on spec §Tier 2 signal_replay tolerance (new value must not produce more than 2× the signal volume or fewer than half).

**Commit:** `feat(tier2): signal replay gate for threshold_tweak`

## Task 46: Tier2Pipeline dispatches to per-type gates

**Files:**
- Modify: `agents/self_upgrade.py` (Tier2Pipeline)
- Create: `tests/test_tier2_pipeline_docstring.py`
- Create: `tests/test_tier2_pipeline_threshold.py`

**Acceptance:** `Tier2Pipeline.execute(typed_edit: TypedEdit) -> UpgradeResult`. Dispatches to per-type gate sequence per spec §Tier 2 Gates per type table. For each edit type, the appropriate gates run in order. On full pass, commits to `vibe/self-upgrade/{edit_type}-{id}` and returns success. Full docstring→pytest-free path tested; full threshold_tweak→targeted pytest + signal replay path tested.

**Commit:** `feat(tier2): Tier2Pipeline with per-type gate dispatch`

## Task 47: Tier2Builder drafts TypedEdits from signals

**Files:**
- Create: `agents/self_upgrade/tier2_builder.py`
- Create: `tests/test_tier2_builder.py`

**Acceptance:** `Tier2Builder.build(signals, classifier_hint) -> TypedEdit | LowConfidence`. Uses the LLM to draft a typed edit based on the signals + classifier hint (which edit type was suggested). Returns `LowConfidence` when the LLM output doesn't parse as a valid TypedEdit. The dispatcher's fallback logic converts `LowConfidence` to Tier 3.

**Commit:** `feat(tier2): builder drafts TypedEdits with confidence fallback`

## Task 48: Wire Tier 2 into dispatcher + companion issue

**Files:**
- Modify: `agents/self_upgrade_dispatcher.py`
- Modify: `tests/test_self_upgrade_dispatcher.py`

**Acceptance:** classifier routes signal types listed in spec §Trigger rewiring → Classification rules to Tier 2. Dispatcher calls `Tier2Builder.build()` → `Tier2Pipeline.execute()`. On success, files companion Paperclip issue. On `LowConfidence` from builder, falls back to Tier 3.

**Commit:** `feat(self-upgrade): wire Tier 2 with companion issue and Tier 3 fallback`

## Milestone 4 complete

All 5 tiers live. The self-upgrade loop can now genuinely improve the codebase — proposing lessons, skill refinements, prompt overrides, typed code edits, or reports — with per-tier validation scaled to risk.

---

# Self-review

Running through the spec section by section to verify coverage.

**Spec coverage check:**

| Spec section | Covered by |
|---|---|
| Problem, Goal, Success criteria | Implicit throughout; no task required |
| Tier 0 data model + read/write/scoring/decay | Tasks 8-16 |
| Tier 1a skill refinement + A/B cooldown | Tasks 23-28 |
| Tier 1b overrides.yaml + gates + loader + AdapterRegistry merge | Tasks 29-36 |
| Tier 2 5 edit types + AST verifier + gates + Never set expansion | Tasks 5, 6, 37-48 |
| Tier 3 IssueReport + filing + assignee_user_id | Tasks 17, 19, 20, 21 |
| Trigger rewiring + signal format + migration | Tasks 1, 2, 3, 4 |
| Dispatcher model + DispatchResult + classifier | Tasks 4, 21, 28, 36, 48 |
| Paperclip integration (labels + assignee + companion issue format) | Tasks 17, 21, 36, 48 |
| Canonical fixtures + harvester | Tasks 32, 33 |
| Milestone 0 code surface | Tasks 1-7 |
| Milestone 1 code surface | Tasks 8-22 |
| Milestone 2 code surface | Tasks 23-28 |
| Milestone 3 code surface | Tasks 29-36 |
| Milestone 4 code surface | Tasks 37-48 |
| Expanded immutable/Never set | Task 5, 6 |
| DeerFlow self_upgrade_agent future work | Explicitly out of scope (spec) |

**Placeholder scan:** No TBDs in M0/M1 tasks. M2-M4 tasks use task-level granularity with explicit "re-plan before execution" notes — this is the negotiated scope deviation, not a placeholder.

**Type consistency check:**
- `Tier0Result.LessonDrafted` fields: `lesson`, `role`, `task_type`, `tag`, `signal_refs` — consistent across Task 11 and 12 and 21.
- `DispatchResult.Tier0Written(lesson_id, signal_refs)` — consistent across Task 4, 7, 21.
- `LessonStore.add()` kwargs: `role`, `task_type`, `tag`, `lesson`, `author_agent_id`, `author_run_id` — consistent across Tasks 8, 9, 12, 21, 22.
- `PaperclipClient.create_issue(assignee_user_id=...)` — consistent across Task 17 and 21.
- `UpgradeSignal.id` / `.artifact_ref` — consistent across Tasks 3, 4, 7, 21.

**Known gaps / follow-ups not in this plan:**
- Milestone -1 (container config) is explicitly out of scope — its own separate spec.
- DeerFlow subagent integration is explicitly out of scope — future work noted in spec.
- Cross-install skill sharing is noted in spec as future work.
- Memory note → prompt override promotion is noted in spec as future work.

---

# Execution handoff

**Plan complete and saved to `docs/superpowers/plans/2026-04-06-self-upgrade-feasibility-plan.md`.**

## Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Best for M0 and M1 where each task is bite-sized and review cycles are short.

**2. Inline Execution** — Execute tasks in this session using executing-plans skill, batch execution with checkpoints for review. Better when you want to watch the whole thing happen sequentially.

**My recommendation: Subagent-Driven for M0 + M1 (tasks 1-22). Stop after M1 and re-plan M2-M4 individually before executing them.** The M2-M4 task-level outlines are deliberately under-specified and would benefit from a fresh bite-sized planning pass when you're ready to ship each one.

**Which approach?**
