# Tier 1a Skill Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the silent in-place skill rewrites in `skill_cleanup` with a dispatcher-gated, versioned, A/B-tested refinement loop, scoped to per-install `~/.vibe/skills/` only.

**Architecture:** New `agents/skill_ab.py` owns all A/B logic (naming, bucketing, promotion, archival). New `agents/self_upgrade/tier1a_builder.py` is the dispatcher-called builder that drafts v2 candidates via a pure `draft_refined_content` call against `skill_generator`. The dispatcher gains one new classifier rule and one new handler. The skill loader gains a six-line touch that swaps in the active version when siblings exist. Workflow cleanup removes the auto-refine path and fires promotion inline.

**Tech Stack:** Python 3.12, pytest, SQLite (existing outcome store, no new persistence), `hashlib.sha256` for bucketing, existing `SkillRegistry` for registration/integrity, existing `SelfUpgradeDispatcher` tagged-union pattern.

**Reference spec:** `docs/superpowers/specs/2026-04-08-tier1a-skill-refinement-design.md`

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `agents/skill_ab.py` | Pure A/B logic: naming helpers, `list_versions_for`, `bucket_for_run`, `pick_active_version`, `write_candidate`, `archive_loser`, `rename_winner_to_base`, `maybe_promote_winners`, `PromotionResult` dataclass |
| `agents/self_upgrade/tier1a_builder.py` | `Tier1aResult` tagged union + `Tier1aBuilder` class. Orchestrates skill resolution → eligibility checks → LLM draft → `skill_ab.write_candidate` |
| `tests/test_skill_ab.py` | Unit tests for every public function in `skill_ab.py`. No LLM, no workflow. |
| `tests/test_tier1a_builder.py` | Unit tests for the builder with mocked LLM, real registry + outcome_store on tmpdirs |
| `tests/test_dispatcher_tier1a_classification.py` | Classifier rule unit tests, no builder |
| `tests/test_dispatcher_tier1a_handling.py` | Dispatcher → builder hand-off tests with mocked builder |
| `tests/test_skill_loader_version_selection.py` | Loader tests that verify version picking under explicit session_ids |
| `tests/test_self_upgrade_invariants.py` | Lock-in tests for `refine_skill` deletion and immutable registration |

### Modified files

| File | Change |
|---|---|
| `agents/self_upgrade/__init__.py` | Add `agents/skill_ab.py` to `_ADDITIONAL_IMMUTABLES` |
| `agents/self_upgrade_dispatcher.py` | Add classifier rule, constructor param, `_handle_tier1a`, wire into `dispatch()` |
| `agents/skill_generator.py` | Delete `refine_skill` + `_find_skill_path`, promote `_create_refined_skill_content` to public `draft_refined_content` |
| `agents/skill_cleanup.py` | Remove auto-refine path, add `skill_ab.maybe_promote_winners` call |
| `agents/skill_loader.py` | Version detection + `pick_active_version` call; mutates `skill_info` in place |
| `agents/skill_registry_lifecycle.py` | Add `unregister_skill(name)` method |
| `tests/test_skill_reinforcement.py` | Update 3 tests (lines 316, 334, 349) to call `draft_refined_content` instead of `refine_skill` |
| `tests/test_misc_coverage.py` | Update 1 test (line 957) for method rename |

---

## Task 1: `skill_ab` — naming helpers + `list_versions_for`

**Files:**
- Create: `agents/skill_ab.py`
- Test: `tests/test_skill_ab.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_skill_ab.py`:

```python
"""Tests for agents/skill_ab.py — A/B versioning logic for skill refinements."""

import pytest
from pathlib import Path

from agents import skill_ab


class TestVersionNaming:
    def test_is_versioned_name_true_for_suffixed(self):
        assert skill_ab.is_versioned_name("myCodeSkill__v2")

    def test_is_versioned_name_false_for_base(self):
        assert not skill_ab.is_versioned_name("myCodeSkill")

    def test_is_versioned_name_false_for_empty_suffix(self):
        assert not skill_ab.is_versioned_name("myCodeSkill__v")

    def test_is_versioned_name_false_for_non_numeric_suffix(self):
        assert not skill_ab.is_versioned_name("myCodeSkill__va")

    def test_base_name_strips_version_suffix(self):
        assert skill_ab.base_name("myCodeSkill__v2") == "myCodeSkill"

    def test_base_name_unchanged_when_unversioned(self):
        assert skill_ab.base_name("myCodeSkill") == "myCodeSkill"

    def test_versioned_name_constructs_suffix(self):
        assert skill_ab.versioned_name("myCodeSkill", 2) == "myCodeSkill__v2"

    def test_versioned_name_roundtrip(self):
        original = "myCodeSkill__v3"
        base = skill_ab.base_name(original)
        version = int(skill_ab.VERSION_SUFFIX_RE.match(original).group("version"))
        assert skill_ab.versioned_name(base, version) == original


class TestListVersionsFor:
    def test_list_versions_single_version_returns_base(self, tmp_path):
        base_dir = tmp_path / "myCodeSkill"
        base_dir.mkdir()
        (base_dir / "SKILL.md").write_text("# test")
        result = skill_ab.list_versions_for("myCodeSkill", skills_root=tmp_path)
        assert result == [base_dir]

    def test_list_versions_two_versions_sorted(self, tmp_path):
        base_dir = tmp_path / "myCodeSkill"
        v2_dir = tmp_path / "myCodeSkill__v2"
        for d in (base_dir, v2_dir):
            d.mkdir()
            (d / "SKILL.md").write_text("# test")
        result = skill_ab.list_versions_for("myCodeSkill", skills_root=tmp_path)
        assert result == [base_dir, v2_dir]

    def test_list_versions_ignores_archive_directory(self, tmp_path):
        base_dir = tmp_path / "myCodeSkill"
        base_dir.mkdir()
        (base_dir / "SKILL.md").write_text("# test")
        archive_dir = tmp_path / "archive" / "myCodeSkill__superseded_20260408"
        archive_dir.mkdir(parents=True)
        (archive_dir / "SKILL.md").write_text("# old")
        result = skill_ab.list_versions_for("myCodeSkill", skills_root=tmp_path)
        assert result == [base_dir]

    def test_list_versions_skips_dirs_without_skill_md(self, tmp_path):
        base_dir = tmp_path / "myCodeSkill"
        base_dir.mkdir()
        (base_dir / "SKILL.md").write_text("# test")
        broken_v2 = tmp_path / "myCodeSkill__v2"
        broken_v2.mkdir()
        # no SKILL.md inside broken_v2
        result = skill_ab.list_versions_for("myCodeSkill", skills_root=tmp_path)
        assert result == [base_dir]

    def test_list_versions_empty_when_no_match(self, tmp_path):
        other = tmp_path / "otherSkill"
        other.mkdir()
        (other / "SKILL.md").write_text("# test")
        result = skill_ab.list_versions_for("myCodeSkill", skills_root=tmp_path)
        assert result == []

    def test_list_versions_does_not_match_prefix_overlap(self, tmp_path):
        # "foo" should not match "foobar"
        foo = tmp_path / "foo"
        foobar = tmp_path / "foobar"
        for d in (foo, foobar):
            d.mkdir()
            (d / "SKILL.md").write_text("# test")
        result = skill_ab.list_versions_for("foo", skills_root=tmp_path)
        assert result == [foo]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_skill_ab.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'agents.skill_ab'`

- [ ] **Step 3: Implement minimal code to make tests pass**

Create `agents/skill_ab.py`:

```python
"""A/B versioning for skill refinements.

Pure A/B logic — no LLM, no Paperclip, no network. All functions are
deterministic given their inputs. This module is the single source of
truth for the ``__v{N}`` naming convention.

Used by Tier1aBuilder (to write v2 candidates), skill_loader (to pick
active versions), and skill_cleanup (to promote winners and archive losers).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .skill_outcome_store import SkillOutcomeStore
    from .skill_registry import SkillRegistry


VERSION_SUFFIX_RE = re.compile(r"^(?P<base>.+?)__v(?P<version>\d+)$")


def is_versioned_name(name: str) -> bool:
    """Return True if ``name`` matches the ``<base>__v<N>`` convention."""
    return VERSION_SUFFIX_RE.match(name) is not None


def base_name(name: str) -> str:
    """Return the base name for a possibly-versioned skill name.

    ``base_name("x__v2") == "x"``; ``base_name("x") == "x"``.
    """
    match = VERSION_SUFFIX_RE.match(name)
    if match:
        return match.group("base")
    return name


def versioned_name(base: str, version: int) -> str:
    """Construct a versioned name from a base and an integer version.

    ``versioned_name("x", 2) == "x__v2"``.
    """
    return f"{base}__v{version}"


def list_versions_for(base: str, *, skills_root: Path) -> List[Path]:
    """Return all version directories for a base name, sorted by version.

    The base directory (no suffix) is treated as version 1 and always
    appears first if it exists. Subsequent versions appear in ascending
    version order.

    Directories under ``skills_root/archive/`` are never included.
    Directories without a readable ``SKILL.md`` are never included.
    Returns an empty list if no matching directories exist.
    """
    if not skills_root.is_dir():
        return []

    matches: List[tuple[int, Path]] = []
    for entry in skills_root.iterdir():
        if entry.name == "archive":
            continue
        if not entry.is_dir():
            continue
        if not (entry / "SKILL.md").is_file():
            continue

        if entry.name == base:
            matches.append((1, entry))
            continue

        m = VERSION_SUFFIX_RE.match(entry.name)
        if m and m.group("base") == base:
            matches.append((int(m.group("version")), entry))

    matches.sort(key=lambda pair: pair[0])
    return [path for _version, path in matches]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_skill_ab.py -v --no-cov`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add agents/skill_ab.py tests/test_skill_ab.py
git commit -m "feat(skill_ab): naming helpers and version directory discovery

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `skill_ab` — `bucket_for_run` + `pick_active_version`

**Files:**
- Modify: `agents/skill_ab.py`
- Test: `tests/test_skill_ab.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_skill_ab.py`:

```python
import hashlib


class TestBucketForRun:
    def test_deterministic_same_input_same_bucket(self):
        inputs = ["run_abc", "run_xyz", "session_42", ""]
        for inp in inputs:
            first = skill_ab.bucket_for_run(inp)
            for _ in range(10):
                assert skill_ab.bucket_for_run(inp) == first

    def test_matches_independent_sha256_computation(self):
        # Pin the bucket to the exact sha256 byte-0 % 2 formula so other
        # processes (e.g. a future Go or Rust implementation) can reproduce
        # bucket assignment without referring back to this Python module.
        run_input = "session_42"
        expected = hashlib.sha256(run_input.encode("utf-8")).digest()[0] % 2
        assert skill_ab.bucket_for_run(run_input) == expected

    def test_distributes_roughly_evenly(self):
        counts = [0, 0]
        for i in range(1000):
            counts[skill_ab.bucket_for_run(f"run_{i}")] += 1
        # Chi-square-ish sanity bound: neither bucket below 400 or above 600
        assert 400 <= counts[0] <= 600, counts
        assert 400 <= counts[1] <= 600, counts

    def test_num_buckets_default_is_two(self):
        for i in range(100):
            assert skill_ab.bucket_for_run(f"run_{i}") in (0, 1)


class TestPickActiveVersion:
    def _make_version_dirs(self, tmp_path, count):
        dirs = []
        for version in range(1, count + 1):
            if version == 1:
                d = tmp_path / "myCodeSkill"
            else:
                d = tmp_path / f"myCodeSkill__v{version}"
            d.mkdir()
            (d / "SKILL.md").write_text(f"# v{version}")
            dirs.append(d)
        return dirs

    def test_single_candidate_returned_unchanged(self, tmp_path):
        dirs = self._make_version_dirs(tmp_path, 1)
        result = skill_ab.pick_active_version(dirs, run_input="anything")
        assert result == dirs[0]

    def test_picks_first_when_bucket_zero(self, tmp_path):
        dirs = self._make_version_dirs(tmp_path, 2)
        # Find a run_input whose sha256 byte 0 % 2 == 0
        bucket_zero_input = None
        for i in range(100):
            if skill_ab.bucket_for_run(f"run_{i}") == 0:
                bucket_zero_input = f"run_{i}"
                break
        assert bucket_zero_input is not None
        assert skill_ab.pick_active_version(dirs, run_input=bucket_zero_input) == dirs[0]

    def test_picks_second_when_bucket_one(self, tmp_path):
        dirs = self._make_version_dirs(tmp_path, 2)
        bucket_one_input = None
        for i in range(100):
            if skill_ab.bucket_for_run(f"run_{i}") == 1:
                bucket_one_input = f"run_{i}"
                break
        assert bucket_one_input is not None
        assert skill_ab.pick_active_version(dirs, run_input=bucket_one_input) == dirs[1]

    def test_empty_run_input_falls_back_to_first(self, tmp_path):
        dirs = self._make_version_dirs(tmp_path, 2)
        assert skill_ab.pick_active_version(dirs, run_input="") == dirs[0]

    def test_empty_candidates_raises(self, tmp_path):
        with pytest.raises(ValueError, match="at least one candidate"):
            skill_ab.pick_active_version([], run_input="run_1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_skill_ab.py::TestBucketForRun tests/test_skill_ab.py::TestPickActiveVersion -v --no-cov`
Expected: FAIL with `AttributeError: module 'agents.skill_ab' has no attribute 'bucket_for_run'`

- [ ] **Step 3: Implement `bucket_for_run` and `pick_active_version`**

Add to `agents/skill_ab.py`:

```python
import hashlib


def bucket_for_run(run_input: str, num_buckets: int = 2) -> int:
    """Return a deterministic bucket index for a run input.

    Formula: ``sha256(run_input).digest()[0] % num_buckets``.

    Deliberately does NOT use Python's built-in ``hash()`` — PEP 456
    process-level randomization would break cross-process determinism.
    Empty ``run_input`` returns 0 (stable fallback).
    """
    digest = hashlib.sha256(run_input.encode("utf-8")).digest()
    return digest[0] % num_buckets


def pick_active_version(
    candidates: List[Path],
    *,
    run_input: str,
) -> Path:
    """Pick one version directory from a candidate list, deterministically.

    Invariant: same ``run_input`` + same candidate list → same result, always.
    Called by the skill loader during workflow execution. Never mutates the
    filesystem.

    Args:
        candidates: Version directories from ``list_versions_for``, in the
            order returned by that function (ascending by version).
        run_input: The bucketing input — typically ``state["session_id"]``.
            If empty, returns ``candidates[0]`` as a stable fallback.

    Raises:
        ValueError: If ``candidates`` is empty.
    """
    if not candidates:
        raise ValueError("pick_active_version requires at least one candidate")
    if len(candidates) == 1 or not run_input:
        return candidates[0]
    bucket = bucket_for_run(run_input, num_buckets=len(candidates))
    return candidates[bucket]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_skill_ab.py -v --no-cov`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add agents/skill_ab.py tests/test_skill_ab.py
git commit -m "feat(skill_ab): deterministic bucket_for_run and pick_active_version

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `skill_ab` — `write_candidate`

**Files:**
- Modify: `agents/skill_ab.py`
- Test: `tests/test_skill_ab.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_skill_ab.py`:

```python
from unittest.mock import MagicMock


class TestWriteCandidate:
    def test_creates_versioned_directory_with_skill_md(self, tmp_path):
        registry = MagicMock()
        registry.register_skill = MagicMock()

        result = skill_ab.write_candidate(
            base="myCodeSkill",
            version=2,
            content="# myCodeSkill v2\n\nrefined content",
            description="v2 refined",
            task_types=["code_generation"],
            tier="temp",
            parent_dir=tmp_path,
            skill_registry=registry,
        )

        expected = tmp_path / "myCodeSkill__v2"
        assert result == expected
        assert expected.is_dir()
        assert (expected / "SKILL.md").read_text() == "# myCodeSkill v2\n\nrefined content"

    def test_calls_register_skill_with_versioned_name(self, tmp_path):
        registry = MagicMock()
        registry.register_skill = MagicMock()

        skill_ab.write_candidate(
            base="myCodeSkill",
            version=2,
            content="# v2",
            description="v2 refined",
            task_types=["code_generation"],
            tier="temp",
            parent_dir=tmp_path,
            skill_registry=registry,
        )

        registry.register_skill.assert_called_once()
        kwargs = registry.register_skill.call_args.kwargs
        assert kwargs["name"] == "myCodeSkill__v2"
        assert kwargs["description"] == "v2 refined"
        assert kwargs["tier"] == "temp"
        assert kwargs["task_types"] == ["code_generation"]
        assert kwargs["skill_path"] == tmp_path / "myCodeSkill__v2"

    def test_raises_if_target_already_exists(self, tmp_path):
        registry = MagicMock()
        existing = tmp_path / "myCodeSkill__v2"
        existing.mkdir()

        with pytest.raises(FileExistsError, match="already exists"):
            skill_ab.write_candidate(
                base="myCodeSkill",
                version=2,
                content="# v2",
                description="v2",
                task_types=["code_generation"],
                tier="temp",
                parent_dir=tmp_path,
                skill_registry=registry,
            )

    def test_does_not_call_register_if_target_exists(self, tmp_path):
        registry = MagicMock()
        registry.register_skill = MagicMock()
        (tmp_path / "myCodeSkill__v2").mkdir()

        with pytest.raises(FileExistsError):
            skill_ab.write_candidate(
                base="myCodeSkill",
                version=2,
                content="# v2",
                description="v2",
                task_types=["code_generation"],
                tier="temp",
                parent_dir=tmp_path,
                skill_registry=registry,
            )
        registry.register_skill.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_skill_ab.py::TestWriteCandidate -v --no-cov`
Expected: FAIL with `AttributeError: module 'agents.skill_ab' has no attribute 'write_candidate'`

- [ ] **Step 3: Implement `write_candidate`**

Add to `agents/skill_ab.py`:

```python
def write_candidate(
    base: str,
    *,
    version: int,
    content: str,
    description: str,
    task_types: List[str],
    tier: str,
    parent_dir: Path,
    skill_registry: "SkillRegistry",
) -> Path:
    """Write a new ``__v{N}`` sibling directory and register it.

    Writes ``SKILL.md`` to ``parent_dir/<base>__v<version>/`` and then calls
    ``skill_registry.register_skill`` which handles validation, integrity
    hash storage, and index updates.

    Args:
        base: Base skill name (no version suffix).
        version: Integer version number (typically 2).
        content: Full SKILL.md content for the candidate.
        description: Skill description (passed through to register_skill).
        task_types: Task types this skill handles (passed through).
        tier: Skill tier — one of "temp", "local", "official".
        parent_dir: Directory that will contain the new sibling.
        skill_registry: SkillRegistry instance to register with.

    Returns:
        Absolute path to the new version directory.

    Raises:
        FileExistsError: If the target directory already exists. The caller
            is responsible for checking via ``list_versions_for`` before
            calling this function.
    """
    target_dir = parent_dir / versioned_name(base, version)
    if target_dir.exists():
        raise FileExistsError(
            f"Cannot write candidate: {target_dir} already exists"
        )

    target_dir.mkdir(parents=True)
    (target_dir / "SKILL.md").write_text(content, encoding="utf-8")

    skill_registry.register_skill(
        name=versioned_name(base, version),
        description=description,
        tier=tier,
        task_types=task_types,
        skill_path=target_dir,
    )

    return target_dir
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_skill_ab.py::TestWriteCandidate -v --no-cov`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add agents/skill_ab.py tests/test_skill_ab.py
git commit -m "feat(skill_ab): write_candidate creates versioned sibling directory

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Add `unregister_skill` to `SkillRegistry`

**Files:**
- Modify: `agents/skill_registry_lifecycle.py`
- Test: `tests/test_skill_registry.py`

Archival (next task) needs to remove a skill from the registry's index. No existing method does this — the current code only adds and reads. Adding one small public method is cleaner than having `skill_ab` reach into `registry.index["tiers"][...]` directly.

- [ ] **Step 1: Write the failing tests**

Find the existing `tests/test_skill_registry.py` and append:

```python
class TestUnregisterSkill:
    def test_unregister_removes_temp_skill_from_index(self, tmp_path):
        from agents.skill_registry import SkillRegistry

        registry = SkillRegistry(base_dir=tmp_path)
        skill_dir = registry.temp_dir / "myTempSkill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: myTempSkill\n---\n\n# Test"
        )

        registry.register_skill(
            name="myTempSkill",
            description="temp test",
            tier="temp",
            task_types=["general"],
            skill_path=skill_dir,
        )
        assert "myTempSkill" in registry.index["tiers"]["temp"]["skills"]

        registry.unregister_skill("myTempSkill")
        assert "myTempSkill" not in registry.index["tiers"]["temp"]["skills"]

    def test_unregister_noop_for_unknown_skill(self, tmp_path):
        from agents.skill_registry import SkillRegistry

        registry = SkillRegistry(base_dir=tmp_path)
        # No exception should be raised
        registry.unregister_skill("neverExisted")

    def test_unregister_persists_to_index_file(self, tmp_path):
        from agents.skill_registry import SkillRegistry

        registry = SkillRegistry(base_dir=tmp_path)
        skill_dir = registry.temp_dir / "myTempSkill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: myTempSkill\n---\n\n# Test"
        )
        registry.register_skill(
            name="myTempSkill",
            description="test",
            tier="temp",
            task_types=["general"],
            skill_path=skill_dir,
        )
        registry.unregister_skill("myTempSkill")

        # New registry instance reads from disk — should not see the skill
        fresh = SkillRegistry(base_dir=tmp_path)
        assert "myTempSkill" not in fresh.index["tiers"]["temp"]["skills"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_skill_registry.py::TestUnregisterSkill -v --no-cov`
Expected: FAIL with `AttributeError: 'SkillRegistry' object has no attribute 'unregister_skill'`

- [ ] **Step 3: Implement `unregister_skill`**

Add to `agents/skill_registry_lifecycle.py`, immediately after `register_skill` (around line 146):

```python
    def unregister_skill(self, name: str) -> None:
        """Remove a skill from the registry index.

        Searches all tiers for the skill and deletes its entry from the
        first match. Idempotent: silently no-ops if the skill is not
        registered. Persists the change to the index file immediately.

        Does NOT delete the skill's directory from disk — that is the
        caller's responsibility. Used by skill_ab.archive_loser after the
        directory has already been moved to the archive.
        """
        for tier in ("official", "local", "temp"):
            if name in self.index["tiers"][tier]["skills"]:
                del self.index["tiers"][tier]["skills"][name]
                self._save_index()
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_skill_registry.py::TestUnregisterSkill -v --no-cov`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add agents/skill_registry_lifecycle.py tests/test_skill_registry.py
git commit -m "feat(skill_registry): add unregister_skill for archival flows

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `skill_ab` — `archive_loser` + `rename_winner_to_base`

**Files:**
- Modify: `agents/skill_ab.py`
- Test: `tests/test_skill_ab.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_skill_ab.py`:

```python
class TestArchiveLoser:
    def test_moves_loser_to_dated_archive_path(self, tmp_path):
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        loser_dir = tmp_path / "myCodeSkill"
        loser_dir.mkdir()
        (loser_dir / "SKILL.md").write_text("# v1")
        archive_root = tmp_path / "archive"

        result = skill_ab.archive_loser(
            loser_dir,
            superseded_by="myCodeSkill__v2",
            archive_root=archive_root,
            skill_registry=registry,
        )

        assert not loser_dir.exists()
        assert result.exists()
        assert result.parent == archive_root
        assert result.name.startswith("myCodeSkill__superseded_")
        assert (result / "SKILL.md").read_text() == "# v1"

    def test_uses_yyyymmdd_suffix(self, tmp_path):
        import datetime
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        loser_dir = tmp_path / "myCodeSkill"
        loser_dir.mkdir()
        (loser_dir / "SKILL.md").write_text("# v1")

        result = skill_ab.archive_loser(
            loser_dir,
            superseded_by="myCodeSkill__v2",
            archive_root=tmp_path / "archive",
            skill_registry=registry,
        )

        today = datetime.date.today().strftime("%Y%m%d")
        assert result.name == f"myCodeSkill__superseded_{today}"

    def test_calls_unregister_with_loser_name(self, tmp_path):
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        loser_dir = tmp_path / "myCodeSkill__v2"
        loser_dir.mkdir()
        (loser_dir / "SKILL.md").write_text("# v2")

        skill_ab.archive_loser(
            loser_dir,
            superseded_by="myCodeSkill",
            archive_root=tmp_path / "archive",
            skill_registry=registry,
        )

        registry.unregister_skill.assert_called_once_with("myCodeSkill__v2")

    def test_creates_archive_root_if_missing(self, tmp_path):
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        loser_dir = tmp_path / "myCodeSkill"
        loser_dir.mkdir()
        (loser_dir / "SKILL.md").write_text("# v1")
        archive_root = tmp_path / "archive"
        assert not archive_root.exists()

        skill_ab.archive_loser(
            loser_dir,
            superseded_by="myCodeSkill__v2",
            archive_root=archive_root,
            skill_registry=registry,
        )

        assert archive_root.is_dir()

    def test_archive_collision_uses_counter_suffix(self, tmp_path):
        import datetime
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        today = datetime.date.today().strftime("%Y%m%d")
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        # Pre-create the normal-suffix target so the fallback path runs
        (archive_root / f"myCodeSkill__superseded_{today}").mkdir()

        loser_dir = tmp_path / "myCodeSkill"
        loser_dir.mkdir()
        (loser_dir / "SKILL.md").write_text("# v1")

        result = skill_ab.archive_loser(
            loser_dir,
            superseded_by="myCodeSkill__v2",
            archive_root=archive_root,
            skill_registry=registry,
        )

        assert result.name == f"myCodeSkill__superseded_{today}_1"


class TestRenameWinnerToBase:
    def test_renames_v2_to_base_and_updates_registry(self, tmp_path):
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        registry.register_skill = MagicMock()
        v2_dir = tmp_path / "myCodeSkill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text("# v2 content")
        (v2_dir / "metadata.json").write_text('{"description": "v2"}')

        result = skill_ab.rename_winner_to_base(
            v2_dir,
            description="promoted v2",
            task_types=["code_generation"],
            tier="temp",
            skill_registry=registry,
        )

        assert result == tmp_path / "myCodeSkill"
        assert result.is_dir()
        assert not v2_dir.exists()
        assert (result / "SKILL.md").read_text() == "# v2 content"

        registry.unregister_skill.assert_called_once_with("myCodeSkill__v2")
        registry.register_skill.assert_called_once()
        assert registry.register_skill.call_args.kwargs["name"] == "myCodeSkill"
        assert registry.register_skill.call_args.kwargs["skill_path"] == result

    def test_raises_if_base_name_already_exists(self, tmp_path):
        registry = MagicMock()
        v2_dir = tmp_path / "myCodeSkill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text("# v2")
        conflict = tmp_path / "myCodeSkill"
        conflict.mkdir()
        (conflict / "SKILL.md").write_text("# conflict")

        with pytest.raises(FileExistsError, match="already exists"):
            skill_ab.rename_winner_to_base(
                v2_dir,
                description="v2",
                task_types=["code_generation"],
                tier="temp",
                skill_registry=registry,
            )

    def test_raises_if_source_not_versioned(self, tmp_path):
        registry = MagicMock()
        base_dir = tmp_path / "myCodeSkill"
        base_dir.mkdir()
        (base_dir / "SKILL.md").write_text("# base")

        with pytest.raises(ValueError, match="not a versioned"):
            skill_ab.rename_winner_to_base(
                base_dir,
                description="base",
                task_types=["code_generation"],
                tier="temp",
                skill_registry=registry,
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_skill_ab.py::TestArchiveLoser tests/test_skill_ab.py::TestRenameWinnerToBase -v --no-cov`
Expected: FAIL with `AttributeError: module 'agents.skill_ab' has no attribute 'archive_loser'`

- [ ] **Step 3: Implement `archive_loser` and `rename_winner_to_base`**

Add to `agents/skill_ab.py`:

```python
import datetime
import shutil


def archive_loser(
    loser_dir: Path,
    *,
    superseded_by: str,
    archive_root: Path,
    skill_registry: "SkillRegistry",
) -> Path:
    """Move the losing version directory into the archive and unregister it.

    Archive path format: ``archive_root/<loser_name>__superseded_YYYYMMDD/``.
    If that path already exists (multiple archivals of the same name on the
    same day), appends ``_1``, ``_2``, ... as a counter suffix.

    Args:
        loser_dir: Current location of the losing version directory.
        superseded_by: Name of the winner, included in logs but not
            currently used in the archive path. Reserved for future
            metadata capture.
        archive_root: Root directory for archives. Created if missing.
        skill_registry: Registry to unregister the loser from.

    Returns:
        Final path of the archived directory.

    Raises:
        OSError: If the move fails for any reason (filesystem error,
            permission, etc.). The registry is only unregistered after a
            successful move.
    """
    archive_root.mkdir(parents=True, exist_ok=True)

    loser_name = loser_dir.name
    today = datetime.date.today().strftime("%Y%m%d")
    target = archive_root / f"{loser_name}__superseded_{today}"

    counter = 1
    while target.exists():
        target = archive_root / f"{loser_name}__superseded_{today}_{counter}"
        counter += 1

    shutil.move(str(loser_dir), str(target))
    skill_registry.unregister_skill(loser_name)
    return target


def rename_winner_to_base(
    winner_dir: Path,
    *,
    description: str,
    task_types: List[str],
    tier: str,
    skill_registry: "SkillRegistry",
) -> Path:
    """Rename a ``__v{N}`` winner directory to its base name.

    Only called when the winner is a versioned directory (i.e. v2 won and
    v1 has already been archived). Uses unregister + re-register rather
    than attempting to mutate the registry index in place, because
    ``register_skill`` handles integrity hashes and validation.

    Args:
        winner_dir: Path to the winning ``__v{N}`` directory.
        description: Description to carry to the re-registered skill.
        task_types: Task types to carry to the re-registered skill.
        tier: Tier to re-register under ("temp", "local", "official").
        skill_registry: Registry to update.

    Returns:
        New path (the base-named directory).

    Raises:
        ValueError: If ``winner_dir`` is not a versioned directory.
        FileExistsError: If the base-name target already exists on disk.
    """
    if not is_versioned_name(winner_dir.name):
        raise ValueError(
            f"rename_winner_to_base: {winner_dir.name} is not a versioned name"
        )

    base = base_name(winner_dir.name)
    target = winner_dir.parent / base
    if target.exists():
        raise FileExistsError(
            f"Cannot rename winner: {target} already exists"
        )

    old_name = winner_dir.name
    winner_dir.rename(target)
    skill_registry.unregister_skill(old_name)
    skill_registry.register_skill(
        name=base,
        description=description,
        tier=tier,
        task_types=task_types,
        skill_path=target,
    )
    return target
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_skill_ab.py::TestArchiveLoser tests/test_skill_ab.py::TestRenameWinnerToBase -v --no-cov`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add agents/skill_ab.py tests/test_skill_ab.py
git commit -m "feat(skill_ab): archive_loser and rename_winner_to_base

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `skill_ab` — `maybe_promote_winners` + `PromotionResult`

**Files:**
- Modify: `agents/skill_ab.py`
- Test: `tests/test_skill_ab.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_skill_ab.py`:

```python
class TestMaybePromoteWinners:
    def _setup_two_versions(self, tmp_path):
        """Create v1 and v2 directories with metadata for a test skill."""
        for name in ("myCodeSkill", "myCodeSkill__v2"):
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"# {name}")
        return tmp_path

    def _make_outcome_store_with_outcomes(self, tmp_path, outcomes_by_name):
        from agents.skill_outcome_store import SkillOutcomeStore

        store_path = tmp_path / "outcomes.jsonl"
        store = SkillOutcomeStore(store_path=str(store_path))

        # SkillOutcomeStore dedups on (skill_name, is_positive) band. To
        # simulate N distinct outcomes per skill we monkey-patch _read_all to
        # return raw records without going through the dedup logic.
        flat_entries = []
        for name, scores in outcomes_by_name.items():
            for score in scores:
                flat_entries.append({
                    "skill_name": name,
                    "task_type": "code_generation",
                    "specification_summary": "",
                    "skill_content": "# stub",
                    "score": score,
                    "feedback": "",
                    "is_positive": score >= 70,
                    "timestamp": "2026-04-08T00:00:00Z",
                })
        store._read_all = lambda: list(flat_entries)
        return store

    def test_not_enough_outcomes_noop(self, tmp_path):
        self._setup_two_versions(tmp_path)
        store = self._make_outcome_store_with_outcomes(
            tmp_path,
            {
                "myCodeSkill": [75, 80, 72],          # only 3 outcomes
                "myCodeSkill__v2": [85, 88, 80],      # only 3 outcomes
            },
        )
        registry = MagicMock()

        result = skill_ab.maybe_promote_winners(
            skill_names_in_run=["myCodeSkill", "myCodeSkill__v2"],
            outcome_store=store,
            skills_root=tmp_path,
            skill_registry=registry,
            K_per_version=10,
        )

        assert result == []
        assert (tmp_path / "myCodeSkill").exists()
        assert (tmp_path / "myCodeSkill__v2").exists()

    def test_v2_wins_archives_v1_and_renames_v2(self, tmp_path):
        self._setup_two_versions(tmp_path)
        store = self._make_outcome_store_with_outcomes(
            tmp_path,
            {
                "myCodeSkill": [70] * 10,
                "myCodeSkill__v2": [85] * 10,
            },
        )
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        registry.register_skill = MagicMock()
        registry.index = {
            "tiers": {
                "temp": {"skills": {
                    "myCodeSkill": {"description": "v1", "task_types": ["code_generation"]},
                    "myCodeSkill__v2": {"description": "v2", "task_types": ["code_generation"]},
                }},
                "local": {"skills": {}},
                "official": {"skills": {}},
            }
        }

        results = skill_ab.maybe_promote_winners(
            skill_names_in_run=["myCodeSkill", "myCodeSkill__v2"],
            outcome_store=store,
            skills_root=tmp_path,
            skill_registry=registry,
            K_per_version=10,
        )

        assert len(results) == 1
        r = results[0]
        assert r.base_name == "myCodeSkill"
        assert r.winner_version == 2
        assert r.loser_version == 1
        assert r.winner_avg == 85.0
        assert r.loser_avg == 70.0

        # v2 dir moved to base name; old base dir gone (archived)
        assert (tmp_path / "myCodeSkill").exists()
        assert not (tmp_path / "myCodeSkill__v2").exists()
        assert (tmp_path / "archive").is_dir()

    def test_v1_wins_archives_v2_no_rename(self, tmp_path):
        self._setup_two_versions(tmp_path)
        store = self._make_outcome_store_with_outcomes(
            tmp_path,
            {
                "myCodeSkill": [90] * 10,
                "myCodeSkill__v2": [60] * 10,
            },
        )
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        registry.register_skill = MagicMock()
        registry.index = {
            "tiers": {
                "temp": {"skills": {
                    "myCodeSkill": {"description": "v1", "task_types": ["code_generation"]},
                    "myCodeSkill__v2": {"description": "v2", "task_types": ["code_generation"]},
                }},
                "local": {"skills": {}},
                "official": {"skills": {}},
            }
        }

        results = skill_ab.maybe_promote_winners(
            skill_names_in_run=["myCodeSkill"],
            outcome_store=store,
            skills_root=tmp_path,
            skill_registry=registry,
            K_per_version=10,
        )

        assert len(results) == 1
        assert results[0].winner_version == 1
        assert results[0].loser_version == 2
        assert (tmp_path / "myCodeSkill").exists()
        assert not (tmp_path / "myCodeSkill__v2").exists()

    def test_tie_v1_wins(self, tmp_path):
        self._setup_two_versions(tmp_path)
        store = self._make_outcome_store_with_outcomes(
            tmp_path,
            {
                "myCodeSkill": [80] * 10,
                "myCodeSkill__v2": [80] * 10,
            },
        )
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        registry.register_skill = MagicMock()
        registry.index = {
            "tiers": {
                "temp": {"skills": {
                    "myCodeSkill": {"description": "v1", "task_types": ["code_generation"]},
                    "myCodeSkill__v2": {"description": "v2", "task_types": ["code_generation"]},
                }},
                "local": {"skills": {}},
                "official": {"skills": {}},
            }
        }

        results = skill_ab.maybe_promote_winners(
            skill_names_in_run=["myCodeSkill"],
            outcome_store=store,
            skills_root=tmp_path,
            skill_registry=registry,
            K_per_version=10,
        )

        assert results[0].winner_version == 1
        assert results[0].loser_version == 2

    def test_single_version_noop(self, tmp_path):
        # Only v1 exists
        d = tmp_path / "myCodeSkill"
        d.mkdir()
        (d / "SKILL.md").write_text("# v1")

        store = self._make_outcome_store_with_outcomes(
            tmp_path,
            {"myCodeSkill": [80] * 20},
        )
        registry = MagicMock()

        result = skill_ab.maybe_promote_winners(
            skill_names_in_run=["myCodeSkill"],
            outcome_store=store,
            skills_root=tmp_path,
            skill_registry=registry,
            K_per_version=10,
        )

        assert result == []
        assert (tmp_path / "myCodeSkill").exists()

    def test_deduplicates_base_names_in_input(self, tmp_path):
        # If both "myCodeSkill" and "myCodeSkill__v2" appear in the input
        # list (possible when both versions were used in the same run),
        # the promotion check should run exactly once for the base.
        self._setup_two_versions(tmp_path)
        store = self._make_outcome_store_with_outcomes(
            tmp_path,
            {
                "myCodeSkill": [70] * 10,
                "myCodeSkill__v2": [85] * 10,
            },
        )
        registry = MagicMock()
        registry.unregister_skill = MagicMock()
        registry.register_skill = MagicMock()
        registry.index = {
            "tiers": {
                "temp": {"skills": {
                    "myCodeSkill": {"description": "v1", "task_types": ["code_generation"]},
                    "myCodeSkill__v2": {"description": "v2", "task_types": ["code_generation"]},
                }},
                "local": {"skills": {}},
                "official": {"skills": {}},
            }
        }

        results = skill_ab.maybe_promote_winners(
            skill_names_in_run=["myCodeSkill", "myCodeSkill__v2", "myCodeSkill"],
            outcome_store=store,
            skills_root=tmp_path,
            skill_registry=registry,
            K_per_version=10,
        )

        # Should produce exactly one promotion, not three
        assert len(results) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_skill_ab.py::TestMaybePromoteWinners -v --no-cov`
Expected: FAIL with `AttributeError: module 'agents.skill_ab' has no attribute 'maybe_promote_winners'`

- [ ] **Step 3: Implement `maybe_promote_winners` and `PromotionResult`**

Add to `agents/skill_ab.py`:

```python
from dataclasses import dataclass


@dataclass
class PromotionResult:
    """Outcome of a single A/B promotion decision."""

    base_name: str
    winner_version: int
    loser_version: int
    winner_avg: float
    loser_avg: float
    archived_to: Path


def _read_outcomes_for(
    outcome_store: "SkillOutcomeStore",
    name: str,
) -> List[int]:
    """Return all recorded scores for a skill name from the raw store."""
    entries = outcome_store._read_all()
    return [e["score"] for e in entries if e.get("skill_name") == name]


def _lookup_skill_metadata(
    skill_registry: "SkillRegistry",
    name: str,
) -> Optional[dict]:
    """Return {description, task_types, tier} for a registered skill, or None."""
    for tier in ("official", "local", "temp"):
        entry = skill_registry.index["tiers"][tier]["skills"].get(name)
        if entry is not None:
            return {
                "description": entry.get("description", ""),
                "task_types": entry.get("task_types", []),
                "tier": tier,
            }
    return None


def maybe_promote_winners(
    skill_names_in_run: List[str],
    outcome_store: "SkillOutcomeStore",
    *,
    skills_root: Path,
    skill_registry: "SkillRegistry",
    K_per_version: int = 10,
    archive_root: Optional[Path] = None,
) -> List[PromotionResult]:
    """Promote A/B winners for any base skill that has hit the outcome quota.

    For each base name derived from ``skill_names_in_run``:
    1. Discover its versions on disk via ``list_versions_for``.
    2. If fewer than 2 versions exist, skip (no A/B in progress).
    3. If any version has fewer than ``K_per_version`` recorded outcomes
       in the ``outcome_store``, skip (not enough data yet).
    4. Pick the winner (highest avg score; ties → earliest version).
    5. Archive all losers via ``archive_loser``.
    6. If the winner is a versioned directory, rename it to the base name
       via ``rename_winner_to_base``.
    7. Append a ``PromotionResult`` to the return list.

    Args:
        skill_names_in_run: Names (possibly already versioned) of skills
            used in the just-finished run. Duplicates and cross-version
            entries are deduped internally via ``base_name``.
        outcome_store: Outcome store to count per-version scores against.
        skills_root: Parent directory containing version directories.
        skill_registry: Registry for unregister/register operations.
        K_per_version: Minimum outcomes required per version before a
            promotion can fire. Default 10.
        archive_root: Archive location. Defaults to ``skills_root/archive``.

    Returns:
        List of PromotionResult, one per skill promoted in this call.
    """
    if archive_root is None:
        archive_root = skills_root / "archive"

    results: List[PromotionResult] = []
    seen_bases: set = set()

    for raw_name in skill_names_in_run:
        base = base_name(raw_name)
        if base in seen_bases:
            continue
        seen_bases.add(base)

        versions = list_versions_for(base, skills_root=skills_root)
        if len(versions) < 2:
            continue

        # Collect per-version scores by parsing the version number from
        # each directory name.
        per_version_scores: dict = {}
        for version_dir in versions:
            if version_dir.name == base:
                version_num = 1
                store_name = base
            else:
                m = VERSION_SUFFIX_RE.match(version_dir.name)
                version_num = int(m.group("version"))
                store_name = version_dir.name
            scores = _read_outcomes_for(outcome_store, store_name)
            per_version_scores[version_num] = scores

        # Quota check
        if any(len(s) < K_per_version for s in per_version_scores.values()):
            continue

        # Pick winner (highest avg, ties → earliest version number)
        avgs = {v: sum(s) / len(s) for v, s in per_version_scores.items()}
        sorted_versions = sorted(avgs.keys())
        winner_version = max(sorted_versions, key=lambda v: (avgs[v], -v))
        loser_version = next(v for v in sorted_versions if v != winner_version)

        winner_dir = next(
            v for v in versions
            if (winner_version == 1 and v.name == base)
            or (winner_version > 1 and v.name == versioned_name(base, winner_version))
        )
        loser_dir = next(
            v for v in versions
            if (loser_version == 1 and v.name == base)
            or (loser_version > 1 and v.name == versioned_name(base, loser_version))
        )

        # Capture winner metadata before any registry mutation
        winner_meta = _lookup_skill_metadata(skill_registry, winner_dir.name)

        # Archive loser first (must succeed before we touch winner)
        archived = archive_loser(
            loser_dir,
            superseded_by=winner_dir.name,
            archive_root=archive_root,
            skill_registry=skill_registry,
        )

        # Rename winner to base if it's a versioned directory
        if winner_version > 1 and winner_meta is not None:
            rename_winner_to_base(
                winner_dir,
                description=winner_meta["description"],
                task_types=winner_meta["task_types"],
                tier=winner_meta["tier"],
                skill_registry=skill_registry,
            )

        results.append(PromotionResult(
            base_name=base,
            winner_version=winner_version,
            loser_version=loser_version,
            winner_avg=avgs[winner_version],
            loser_avg=avgs[loser_version],
            archived_to=archived,
        ))

    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_skill_ab.py -v --no-cov`
Expected: PASS (all tests in test_skill_ab.py)

- [ ] **Step 5: Commit**

```bash
git add agents/skill_ab.py tests/test_skill_ab.py
git commit -m "feat(skill_ab): maybe_promote_winners with PromotionResult

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Register `skill_ab` as immutable

**Files:**
- Modify: `agents/self_upgrade/__init__.py:117-125`
- Test: `tests/test_self_upgrade_invariants.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_self_upgrade_invariants.py`:

```python
"""Lock-in tests for Tier 1a: prevent regressions on deletions and
immutable-set membership for self-upgrade mechanics."""

from agents.self_upgrade import _ADDITIONAL_IMMUTABLES, is_path_immutable


def test_skill_ab_is_immutable():
    assert "agents/skill_ab.py" in _ADDITIONAL_IMMUTABLES
    assert is_path_immutable("agents/skill_ab.py")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_self_upgrade_invariants.py::test_skill_ab_is_immutable -v --no-cov`
Expected: FAIL with `AssertionError: assert 'agents/skill_ab.py' in ...`

- [ ] **Step 3: Add `skill_ab.py` to `_ADDITIONAL_IMMUTABLES`**

In `agents/self_upgrade/__init__.py`, modify the `_ADDITIONAL_IMMUTABLES` frozenset (lines 117-125). Change:

```python
_ADDITIONAL_IMMUTABLES = frozenset({
    "agents/lesson_store.py",       # M1 — cannot modify lesson persistence
    "agents/self_upgrade/tier0_builder.py",   # M1
    "agents/self_upgrade/tier3_builder.py",   # M1
    "agents/self_upgrade/tier1a_builder.py",  # M2
    "agents/self_upgrade/tier1b_builder.py",  # M3
    "agents/self_upgrade/tier2_builder.py",   # M4
    "agents/self_upgrade/ast_verifier.py",    # M4
})
```

to:

```python
_ADDITIONAL_IMMUTABLES = frozenset({
    "agents/lesson_store.py",       # M1 — cannot modify lesson persistence
    "agents/self_upgrade/tier0_builder.py",   # M1
    "agents/self_upgrade/tier3_builder.py",   # M1
    "agents/self_upgrade/tier1a_builder.py",  # M2 (Tier 1a)
    "agents/skill_ab.py",                     # M2 (Tier 1a) — A/B mechanics
    "agents/self_upgrade/tier1b_builder.py",  # M3
    "agents/self_upgrade/tier2_builder.py",   # M4
    "agents/self_upgrade/ast_verifier.py",    # M4
})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_self_upgrade_invariants.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agents/self_upgrade/__init__.py tests/test_self_upgrade_invariants.py
git commit -m "feat(self_upgrade): register skill_ab.py as immutable

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Promote `_create_refined_skill_content` to public `draft_refined_content`

**Files:**
- Modify: `agents/skill_generator.py:629-670`
- Modify: `tests/test_misc_coverage.py:942-970`

- [ ] **Step 1: Update the existing test to use the new public name**

Find the test at `tests/test_misc_coverage.py:942` (`test_create_refined_skill_content_anchor_missing` or similar). Change both the comment and the call.

Open `tests/test_misc_coverage.py` and locate the test (around line 942). Change:

```python
    """Cover line 643: _create_refined_skill_content anchor missing."""
```

to:

```python
    """Cover line 643: draft_refined_content anchor missing."""
```

And change (around line 957):

```python
        result = node._create_refined_skill_content(
```

to:

```python
        result = node.draft_refined_content(
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_misc_coverage.py -v --no-cov -k "refined"`
Expected: FAIL with `AttributeError: 'SkillGeneratorNode' object has no attribute 'draft_refined_content'`

- [ ] **Step 3: Rename the method in `skill_generator.py`**

In `agents/skill_generator.py`, find `_create_refined_skill_content` (line 629). Rename to `draft_refined_content` (drop the leading underscore). The body is unchanged; only the name and docstring change.

Current:

```python
    def _create_refined_skill_content(
        self,
        task_type: str,
        specification: str,
        original_content: str,
        feedback: str,
        score: int,
    ) -> str:
        """
        Create a refined SKILL.md that incorporates critic feedback.

        Takes the base template + learned patterns and adds a
        "Refinement Directives" section that encodes the critic's
        specific complaints as hard requirements.
        """
```

New:

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
        responsible for persisting the result (see
        ``agents.self_upgrade.tier1a_builder.Tier1aBuilder``) or discarding
        it if the refinement is not worth keeping.

        Inserts a "Refinement Directives" section before "## Context" in the
        newly-generated base skill content, encoding the critic's complaints
        as hard requirements the refined skill must address.
        """
```

Also find the internal call on line 645 inside the old `refine_skill` (which we'll delete in the next task, but it currently still calls this method):

```python
        refined = self._create_refined_skill_content(
```

Change to:

```python
        refined = self.draft_refined_content(
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_misc_coverage.py -v --no-cov -k "refined"`
Expected: PASS

Also run the broader skill_reinforcement tests to verify nothing else broke:

Run: `python3 -m pytest tests/test_skill_reinforcement.py tests/test_misc_coverage.py -q --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agents/skill_generator.py tests/test_misc_coverage.py
git commit -m "refactor(skill_generator): rename _create_refined_skill_content to draft_refined_content

Public pure function used by the new Tier1aBuilder. Behavior unchanged.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Delete `refine_skill` and `_find_skill_path`

**Files:**
- Modify: `agents/skill_generator.py:568-627, 672-681`
- Modify: `tests/test_skill_reinforcement.py:316, 334, 349`
- Modify: `agents/skill_cleanup.py:21, 208-227` (prepare, full delete in Task 16)

**Note:** This task breaks `skill_cleanup.py:215` which calls `refine_skill`. That caller is being deleted in Task 16. To keep the tree green in-between, we remove the auto-refine path from `skill_cleanup` in this same commit as a preparatory step (it just stops firing — the promotion call is added in Task 16).

- [ ] **Step 1: Rewrite the existing `refine_skill` tests**

In `tests/test_skill_reinforcement.py`, find the three tests that call `gen.refine_skill(...)` around lines 316, 334, and 349. Each test currently checks that `refine_skill` returns the refined content or None.

For lines 316, 334, 349, change:

```python
        refined = gen.refine_skill(
            skill_name="test-skill",
            task_type="test_generation",
            original_content=original,
            score=50,
            feedback="Missing error handling",
            specification=spec,
        )
```

to:

```python
        refined = gen.draft_refined_content(
            task_type="test_generation",
            specification=spec,
            original_content=original,
            feedback="Missing error handling",
            score=50,
        )
```

All three tests use similar call patterns — update each to drop the `skill_name` argument and switch to `draft_refined_content`.

The tests at lines 334 and 349 may have assertion changes: if they check behavior like "returns None when score >= threshold", that logic lived inside `refine_skill` (checking `REFINEMENT_THRESHOLD`), not inside `_create_refined_skill_content`. Those tests need to be changed to test the *new* behavior — `draft_refined_content` is a pure function that always returns content regardless of score. If a test was specifically verifying the threshold gate, delete it — that gate now lives inside `Tier1aBuilder.build`, covered by Task 11.

Specifically, if you find a test like:

```python
    def test_refine_skill_returns_none_above_threshold(self, tmp_path):
        ...
        result = gen.refine_skill(..., score=90, ...)
        assert result is None
```

Delete this test — the threshold check is no longer in the generator.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_skill_reinforcement.py -v --no-cov -k "refine"`
Expected: FAIL — `refine_skill` still exists but the tests now call `draft_refined_content` with different signatures; some failures may come from threshold-gate tests that should be deleted.

Act: delete the above threshold-gate test if present. Rerun:

Run: `python3 -m pytest tests/test_skill_reinforcement.py -v --no-cov -k "refine"`
Expected: PASS for the rewritten tests (they call the existing `draft_refined_content` method which was added in Task 8)

- [ ] **Step 3: Delete `refine_skill` and `_find_skill_path` from `skill_generator.py`**

In `agents/skill_generator.py`:

1. Delete the `refine_skill` method entirely — lines 568 through 627 (the full method from `def refine_skill(` through its final `return refined`).

2. Delete `_find_skill_path` entirely — lines 672 through 681 (the full method).

3. Leave `draft_refined_content` (renamed in Task 8) in place — it's still the pure content-generation helper.

- [ ] **Step 4: Prepare `skill_cleanup.py` by deleting the auto-refine call**

In `agents/skill_cleanup.py`:

1. On line 21, delete the import of `SkillGeneratorNode, REFINEMENT_THRESHOLD`:

```python
from .skill_generator import SkillGeneratorNode, REFINEMENT_THRESHOLD
```

Delete this entire line.

2. In `_record_outcomes_and_refine`, delete lines 208–227 (the entire `refined_count` logic block):

```python
            # Self-refinement: if score is below threshold and we have
            # feedback, regenerate with critic directives
            if score < REFINEMENT_THRESHOLD and feedback:
                generator = SkillGeneratorNode(
                    self.skill_registry, self.outcome_store,
                    base_model=self.base_model,
                )
                refined = generator.refine_skill(
                    skill_name=skill_name,
                    task_type=task_type,
                    original_content=skill_content,
                    score=score,
                    feedback=feedback,
                    specification=specification,
                )
                if refined:
                    refined_count += 1

        if refined_count > 0:
            logger.info(f"🔄 Refined {refined_count} low-scoring skill(s)")
```

Also delete the `refined_count = 0` initialization at line 170. The `_record_outcomes_and_refine` method now only records outcomes.

3. Update the class docstring at lines 27–38 to remove the "trigger self-refinement" bullet:

Current:

```python
    """
    Handles TTL-based skill cache eviction, final usage tracking, outcome
    recording, and self-refinement of low-scoring skills.

    This node should be called at the end of a session to:
    1. Track final quality scores for all used skills
    2. Record skill outcomes in the outcome store (reinforcement memory)
    3. Trigger self-refinement for skills below REFINEMENT_THRESHOLD
    4. Evict stale temp skills (> 7 days unused) while retaining recent ones
    5. Evict stale official skills (> 30 days unused) — re-fetchable from GitHub
    6. Update usage statistics in the index
    """
```

New:

```python
    """
    Handles TTL-based skill cache eviction, final usage tracking, outcome
    recording, and A/B promotion of skill refinements.

    This node should be called at the end of a session to:
    1. Track final quality scores for all used skills
    2. Record skill outcomes in the outcome store (reinforcement memory)
    3. Evict stale temp skills (> 7 days unused) while retaining recent ones
    4. Evict stale official skills (> 30 days unused) — re-fetchable from GitHub
    5. Update usage statistics in the index

    Note: Skill refinement is no longer triggered here. Refinements now run
    through the self-upgrade dispatcher (Tier 1a), which writes versioned
    candidates; this node promotes winners once K outcomes per version are
    recorded (added in a separate task).
    """
```

Also update the module docstring at lines 1–14 to remove the mention of refinement.

- [ ] **Step 5: Run the tests to verify nothing is broken**

Run: `python3 -m pytest tests/test_skill_reinforcement.py tests/test_skill_cleanup.py tests/test_misc_coverage.py -q --no-cov`
Expected: PASS (all remaining tests, with no references to `refine_skill` or `REFINEMENT_THRESHOLD`)

Run: `python3 -m pytest tests/ -q --no-cov --tb=short -x --ignore=tests/test_self_upgrade_invariants.py 2>&1 | tail -20`
Expected: PASS — no test in the full suite should reference the deleted symbols. If something does, find and fix it.

- [ ] **Step 6: Commit**

```bash
git add agents/skill_generator.py agents/skill_cleanup.py tests/test_skill_reinforcement.py
git commit -m "refactor: delete refine_skill and remove auto-refine from cleanup

Tier 1a refinement now runs through the self-upgrade dispatcher. The
in-place rewrite path in skill_cleanup is deleted; the dispatcher-driven
path (Tier1aBuilder) is added in subsequent commits.

- Delete SkillGeneratorNode.refine_skill (58 lines)
- Delete SkillGeneratorNode._find_skill_path (10 lines, only caller)
- Delete auto-refine loop from SkillCleanupNode._record_outcomes_and_refine
- Remove stale REFINEMENT_THRESHOLD import from skill_cleanup

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: `Tier1aResult` + `Tier1aBuilder.__init__`

**Files:**
- Create: `agents/self_upgrade/tier1a_builder.py`
- Test: `tests/test_tier1a_builder.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tier1a_builder.py`:

```python
"""Tests for agents/self_upgrade/tier1a_builder.py — Tier 1a skill refinement builder."""

import pytest
from pathlib import Path
from dataclasses import is_dataclass

from agents.self_upgrade.tier1a_builder import Tier1aBuilder, Tier1aResult


class TestTier1aResult:
    def test_candidate_written_is_dataclass(self):
        assert is_dataclass(Tier1aResult.CandidateWritten)

    def test_low_confidence_is_dataclass(self):
        assert is_dataclass(Tier1aResult.LowConfidence)

    def test_candidate_written_has_expected_fields(self):
        result = Tier1aResult.CandidateWritten(
            skill_name="myCodeSkill",
            v2_path=Path("/tmp/myCodeSkill__v2"),
            signal_refs=["sig_1", "sig_2"],
        )
        assert result.skill_name == "myCodeSkill"
        assert result.v2_path == Path("/tmp/myCodeSkill__v2")
        assert result.signal_refs == ["sig_1", "sig_2"]

    def test_low_confidence_has_expected_fields(self):
        result = Tier1aResult.LowConfidence(
            reason="no matching skill",
            signal_refs=["sig_1"],
        )
        assert result.reason == "no matching skill"
        assert result.signal_refs == ["sig_1"]


class TestTier1aBuilderInit:
    def test_builder_accepts_required_dependencies(self, tmp_path):
        from unittest.mock import MagicMock
        builder = Tier1aBuilder(
            skill_generator=MagicMock(),
            skill_registry=MagicMock(),
            outcome_store=MagicMock(),
            skills_root=tmp_path,
        )
        assert builder._skills_root == tmp_path
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tier1a_builder.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'agents.self_upgrade.tier1a_builder'`

- [ ] **Step 3: Implement `Tier1aResult` and `Tier1aBuilder.__init__`**

Create `agents/self_upgrade/tier1a_builder.py`:

```python
"""Tier 1a builder — drafts a v2 refinement candidate for an underperforming skill.

Called by SelfUpgradeDispatcher when classify_signals() returns Tier.ONE_A.
Resolves the matching skill, checks eligibility, drafts refined content via
SkillGeneratorNode.draft_refined_content (pure function), and writes the
result to a new __v2 sibling directory via skill_ab.write_candidate.

Never modifies the existing v1 content. Never touches the archive directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union, TYPE_CHECKING

from ..self_upgrade_trigger import UpgradeSignal

if TYPE_CHECKING:
    from ..skill_generator import SkillGeneratorNode
    from ..skill_outcome_store import SkillOutcomeStore
    from ..skill_registry import SkillRegistry

logger = logging.getLogger(__name__)

# Hard cap on aggregated feedback text passed to the LLM (per spec).
_FEEDBACK_CHAR_CAP = 3000


class Tier1aResult:
    """Tagged union of Tier1aBuilder.build() outcomes."""

    @dataclass
    class CandidateWritten:
        skill_name: str        # base name, e.g. "myCodeSkill"
        v2_path: Path          # absolute path to the new __v2 directory
        signal_refs: List[str]

    @dataclass
    class LowConfidence:
        reason: str            # specific reason string used in dispatcher logs
        signal_refs: List[str]

    AnyResult = Union["Tier1aResult.CandidateWritten", "Tier1aResult.LowConfidence"]


class Tier1aBuilder:
    """Drafts a v2 refinement candidate for an underperforming skill."""

    def __init__(
        self,
        *,
        skill_generator: "SkillGeneratorNode",
        skill_registry: "SkillRegistry",
        outcome_store: "SkillOutcomeStore",
        skills_root: Path,
    ) -> None:
        self._skill_generator = skill_generator
        self._skill_registry = skill_registry
        self._outcome_store = outcome_store
        self._skills_root = skills_root
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tier1a_builder.py -v --no-cov`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add agents/self_upgrade/tier1a_builder.py tests/test_tier1a_builder.py
git commit -m "feat(tier1a): Tier1aResult tagged union and Tier1aBuilder skeleton

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: `Tier1aBuilder.build` — `LowConfidence` paths

**Files:**
- Modify: `agents/self_upgrade/tier1a_builder.py`
- Test: `tests/test_tier1a_builder.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tier1a_builder.py`:

```python
from unittest.mock import MagicMock
from agents.self_upgrade_trigger import UpgradeSignal


def _make_signal(task_type="code_generation", detail="Missing validation", score=50):
    return UpgradeSignal(
        category="low_score",
        task_type=task_type,
        detail=detail,
        score=score,
        source_node="critic",
    )


class TestTier1aBuilderLowConfidence:
    def test_no_matching_skill_returns_low_confidence(self, tmp_path):
        registry = MagicMock()
        registry.find_skill = MagicMock(return_value=("none", None, None))

        builder = Tier1aBuilder(
            skill_generator=MagicMock(),
            skill_registry=registry,
            outcome_store=MagicMock(),
            skills_root=tmp_path,
        )

        signals = [_make_signal(), _make_signal(detail="Another"), _make_signal(detail="Third")]
        result = builder.build(signals)

        assert isinstance(result, Tier1aResult.LowConfidence)
        assert result.reason == "no matching skill"
        assert result.signal_refs == [s.id for s in signals]

    def test_no_recorded_outcomes_returns_low_confidence(self, tmp_path):
        base_dir = tmp_path / "myCodeSkill"
        base_dir.mkdir()
        (base_dir / "SKILL.md").write_text("# v1")

        registry = MagicMock()
        registry.find_skill = MagicMock(
            return_value=("temp", "myCodeSkill", base_dir)
        )

        outcome_store = MagicMock()
        outcome_store._read_all = MagicMock(return_value=[])

        builder = Tier1aBuilder(
            skill_generator=MagicMock(),
            skill_registry=registry,
            outcome_store=outcome_store,
            skills_root=tmp_path,
        )

        signals = [_make_signal(), _make_signal(detail="Another"), _make_signal(detail="Third")]
        result = builder.build(signals)

        assert isinstance(result, Tier1aResult.LowConfidence)
        assert result.reason == "no recorded outcomes"

    def test_v2_already_exists_returns_low_confidence(self, tmp_path):
        base_dir = tmp_path / "myCodeSkill"
        base_dir.mkdir()
        (base_dir / "SKILL.md").write_text("# v1")
        v2_dir = tmp_path / "myCodeSkill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text("# v2 already here")

        registry = MagicMock()
        registry.find_skill = MagicMock(
            return_value=("temp", "myCodeSkill", base_dir)
        )

        outcome_store = MagicMock()
        outcome_store._read_all = MagicMock(return_value=[
            {"skill_name": "myCodeSkill", "score": 60, "is_positive": False,
             "feedback": "bad", "task_type": "code_generation"},
        ])

        builder = Tier1aBuilder(
            skill_generator=MagicMock(),
            skill_registry=registry,
            outcome_store=outcome_store,
            skills_root=tmp_path,
        )

        signals = [_make_signal(), _make_signal(detail="Another"), _make_signal(detail="Third")]
        result = builder.build(signals)

        assert isinstance(result, Tier1aResult.LowConfidence)
        assert result.reason == "A/B in progress"

    def test_empty_draft_returns_low_confidence(self, tmp_path):
        base_dir = tmp_path / "myCodeSkill"
        base_dir.mkdir()
        (base_dir / "SKILL.md").write_text("# v1 original")

        registry = MagicMock()
        registry.find_skill = MagicMock(
            return_value=("temp", "myCodeSkill", base_dir)
        )
        registry.index = {
            "tiers": {
                "temp": {"skills": {"myCodeSkill": {
                    "description": "test",
                    "task_types": ["code_generation"],
                }}},
                "local": {"skills": {}},
                "official": {"skills": {}},
            }
        }

        outcome_store = MagicMock()
        outcome_store._read_all = MagicMock(return_value=[
            {"skill_name": "myCodeSkill", "score": 60, "is_positive": False,
             "feedback": "bad", "task_type": "code_generation"},
        ])

        generator = MagicMock()
        generator.draft_refined_content = MagicMock(return_value="")

        builder = Tier1aBuilder(
            skill_generator=generator,
            skill_registry=registry,
            outcome_store=outcome_store,
            skills_root=tmp_path,
        )

        signals = [_make_signal(), _make_signal(detail="Another"), _make_signal(detail="Third")]
        result = builder.build(signals)

        assert isinstance(result, Tier1aResult.LowConfidence)
        assert result.reason == "draft_refined_content returned empty"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tier1a_builder.py::TestTier1aBuilderLowConfidence -v --no-cov`
Expected: FAIL with `AttributeError: 'Tier1aBuilder' object has no attribute 'build'`

- [ ] **Step 3: Implement `Tier1aBuilder.build` with LowConfidence paths**

Add the `build` method to `agents/self_upgrade/tier1a_builder.py`:

```python
    def build(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str = "",
        author_run_id: str = "",
    ) -> "Tier1aResult.AnyResult":
        """Draft a v2 refinement candidate from a signal cluster.

        Steps:
        1. Determine target task_type (all signals share one by classifier rule)
        2. Resolve matching skill via skill_registry.find_skill
        3. Check outcome_store has ≥1 recorded outcome for the base skill
        4. Check no __v2 already exists (only one A/B at a time)
        5. Aggregate feedback and call draft_refined_content
        6. Write the v2 sibling via skill_ab.write_candidate
        """
        from .. import skill_ab

        signal_refs = [s.id for s in signals]

        # Step 1: task_type from the first signal (classifier guarantees
        # all cluster members share a task_type).
        task_type = signals[0].task_type

        # Step 2: resolve the matching skill
        tier, skill_name, skill_path = self._skill_registry.find_skill(task_type)
        if not skill_name or not skill_path:
            return Tier1aResult.LowConfidence(
                reason="no matching skill",
                signal_refs=signal_refs,
            )

        # Step 3: ≥1 recorded outcome proves the skill has been exercised
        all_outcomes = self._outcome_store._read_all()
        base_outcomes = [
            o for o in all_outcomes if o.get("skill_name") == skill_name
        ]
        if len(base_outcomes) < 1:
            return Tier1aResult.LowConfidence(
                reason="no recorded outcomes",
                signal_refs=signal_refs,
            )

        # Step 4: no A/B already in progress
        skill_path = Path(skill_path)
        parent_dir = skill_path.parent
        existing_versions = skill_ab.list_versions_for(
            skill_name, skills_root=parent_dir
        )
        if len(existing_versions) > 1:
            return Tier1aResult.LowConfidence(
                reason="A/B in progress",
                signal_refs=signal_refs,
            )

        # Step 5: draft the refined content
        original_content = (skill_path / "SKILL.md").read_text(
            encoding="utf-8", errors="replace"
        )
        aggregated_feedback = self._aggregate_feedback(signals, base_outcomes)
        worst_score = min((s.score for s in signals if s.score is not None),
                          default=50)

        refined = self._skill_generator.draft_refined_content(
            task_type=task_type,
            specification="",  # spec not available at dispatch time
            original_content=original_content,
            feedback=aggregated_feedback,
            score=worst_score,
        )

        if not refined or not refined.strip():
            return Tier1aResult.LowConfidence(
                reason="draft_refined_content returned empty",
                signal_refs=signal_refs,
            )

        # Step 6 happens in the next task — for now, placeholder
        return Tier1aResult.LowConfidence(
            reason="write path not yet implemented",
            signal_refs=signal_refs,
        )

    @staticmethod
    def _aggregate_feedback(
        signals: List[UpgradeSignal],
        outcomes: List[dict],
    ) -> str:
        """Concatenate distinct critic feedback strings, dedupe, cap at 3000 chars.

        Pulls feedback from both the signal cluster (current-run critic) and
        prior recorded outcomes (historical critic feedback for the same
        skill). Deduplication uses exact string match.
        """
        pieces: List[str] = []
        seen: set = set()

        for s in signals:
            if s.detail and s.detail not in seen:
                seen.add(s.detail)
                pieces.append(s.detail)

        for o in outcomes:
            fb = o.get("feedback", "")
            if fb and fb not in seen:
                seen.add(fb)
                pieces.append(fb)

        combined = "\n\n".join(pieces)
        if len(combined) > _FEEDBACK_CHAR_CAP:
            combined = combined[:_FEEDBACK_CHAR_CAP]
        return combined
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tier1a_builder.py::TestTier1aBuilderLowConfidence -v --no-cov`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add agents/self_upgrade/tier1a_builder.py tests/test_tier1a_builder.py
git commit -m "feat(tier1a): Tier1aBuilder.build LowConfidence paths

Covers no-matching-skill, no-outcomes, A/B-in-progress, and empty-draft
rejections. Happy path write added in the next commit.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: `Tier1aBuilder.build` — happy path (CandidateWritten)

**Files:**
- Modify: `agents/self_upgrade/tier1a_builder.py`
- Test: `tests/test_tier1a_builder.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tier1a_builder.py`:

```python
class TestTier1aBuilderHappyPath:
    def _setup(self, tmp_path):
        base_dir = tmp_path / "myCodeSkill"
        base_dir.mkdir()
        (base_dir / "SKILL.md").write_text("# v1 original\n\n## Context\n\nuse this")

        registry = MagicMock()
        registry.find_skill = MagicMock(
            return_value=("temp", "myCodeSkill", base_dir)
        )
        registry.index = {
            "tiers": {
                "temp": {"skills": {"myCodeSkill": {
                    "description": "Test skill",
                    "task_types": ["code_generation"],
                    "path": str(base_dir),
                }}},
                "local": {"skills": {}},
                "official": {"skills": {}},
            }
        }
        registry.register_skill = MagicMock()

        outcome_store = MagicMock()
        outcome_store._read_all = MagicMock(return_value=[
            {"skill_name": "myCodeSkill", "score": 60, "is_positive": False,
             "feedback": "Missing validation", "task_type": "code_generation"},
        ])

        generator = MagicMock()
        generator.draft_refined_content = MagicMock(return_value="# v2 refined\n\nbetter content")

        return base_dir, registry, outcome_store, generator

    def test_happy_path_returns_candidate_written(self, tmp_path):
        base_dir, registry, outcome_store, generator = self._setup(tmp_path)
        builder = Tier1aBuilder(
            skill_generator=generator,
            skill_registry=registry,
            outcome_store=outcome_store,
            skills_root=tmp_path,
        )

        signals = [
            _make_signal(detail="Bad error handling"),
            _make_signal(detail="Missing tests"),
            _make_signal(detail="Unclear naming"),
        ]
        result = builder.build(signals)

        assert isinstance(result, Tier1aResult.CandidateWritten)
        assert result.skill_name == "myCodeSkill"
        assert result.v2_path == tmp_path / "myCodeSkill__v2"
        assert result.signal_refs == [s.id for s in signals]

    def test_happy_path_writes_v2_directory(self, tmp_path):
        base_dir, registry, outcome_store, generator = self._setup(tmp_path)
        builder = Tier1aBuilder(
            skill_generator=generator,
            skill_registry=registry,
            outcome_store=outcome_store,
            skills_root=tmp_path,
        )

        signals = [
            _make_signal(detail="Bad error handling"),
            _make_signal(detail="Missing tests"),
            _make_signal(detail="Unclear naming"),
        ]
        builder.build(signals)

        v2_dir = tmp_path / "myCodeSkill__v2"
        assert v2_dir.is_dir()
        assert (v2_dir / "SKILL.md").read_text() == "# v2 refined\n\nbetter content"

    def test_happy_path_registers_v2_with_versioned_name(self, tmp_path):
        base_dir, registry, outcome_store, generator = self._setup(tmp_path)
        builder = Tier1aBuilder(
            skill_generator=generator,
            skill_registry=registry,
            outcome_store=outcome_store,
            skills_root=tmp_path,
        )

        signals = [
            _make_signal(detail="Bad error handling"),
            _make_signal(detail="Missing tests"),
            _make_signal(detail="Unclear naming"),
        ]
        builder.build(signals)

        registry.register_skill.assert_called_once()
        kwargs = registry.register_skill.call_args.kwargs
        assert kwargs["name"] == "myCodeSkill__v2"
        assert kwargs["tier"] == "temp"
        assert kwargs["task_types"] == ["code_generation"]

    def test_happy_path_calls_draft_with_aggregated_feedback(self, tmp_path):
        base_dir, registry, outcome_store, generator = self._setup(tmp_path)
        builder = Tier1aBuilder(
            skill_generator=generator,
            skill_registry=registry,
            outcome_store=outcome_store,
            skills_root=tmp_path,
        )

        signals = [
            _make_signal(detail="Bad error handling"),
            _make_signal(detail="Missing tests"),
            _make_signal(detail="Unclear naming"),
        ]
        builder.build(signals)

        generator.draft_refined_content.assert_called_once()
        kwargs = generator.draft_refined_content.call_args.kwargs
        assert "Bad error handling" in kwargs["feedback"]
        assert "Missing tests" in kwargs["feedback"]
        assert "Unclear naming" in kwargs["feedback"]
        assert "Missing validation" in kwargs["feedback"]  # from outcome store
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_tier1a_builder.py::TestTier1aBuilderHappyPath -v --no-cov`
Expected: FAIL — current implementation returns `LowConfidence("write path not yet implemented")` at the end of `build`.

- [ ] **Step 3: Replace the placeholder with the `write_candidate` call**

In `agents/self_upgrade/tier1a_builder.py`, find the final block:

```python
        # Step 6 happens in the next task — for now, placeholder
        return Tier1aResult.LowConfidence(
            reason="write path not yet implemented",
            signal_refs=signal_refs,
        )
```

Replace with:

```python
        # Step 6: persist the candidate
        # Look up metadata for the v2 entry (inherits from v1)
        skill_meta = self._skill_registry.index["tiers"][tier]["skills"].get(
            skill_name, {}
        )
        description = skill_meta.get("description", f"Refined v2 of {skill_name}")
        task_types_list = skill_meta.get("task_types", [task_type])

        try:
            v2_path = skill_ab.write_candidate(
                base=skill_name,
                version=2,
                content=refined,
                description=description,
                task_types=task_types_list,
                tier=tier,
                parent_dir=parent_dir,
                skill_registry=self._skill_registry,
            )
        except FileExistsError:
            return Tier1aResult.LowConfidence(
                reason="A/B in progress",
                signal_refs=signal_refs,
            )

        logger.info(
            "Tier1a: wrote v2 candidate for %s at %s (author=%s, run=%s)",
            skill_name, v2_path, author_agent_id, author_run_id,
        )
        return Tier1aResult.CandidateWritten(
            skill_name=skill_name,
            v2_path=v2_path,
            signal_refs=signal_refs,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tier1a_builder.py -v --no-cov`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add agents/self_upgrade/tier1a_builder.py tests/test_tier1a_builder.py
git commit -m "feat(tier1a): Tier1aBuilder.build writes v2 candidate on happy path

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: Dispatcher classifier — add Tier 1a rule

**Files:**
- Modify: `agents/self_upgrade_dispatcher.py:110-140`
- Create: `tests/test_dispatcher_tier1a_classification.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dispatcher_tier1a_classification.py`:

```python
"""Classifier tests for the Tier 1a rule in SelfUpgradeDispatcher."""

from agents.self_upgrade_dispatcher import SelfUpgradeDispatcher, Tier
from agents.self_upgrade_trigger import UpgradeSignal


def _sig(task_type="code_generation", detail="some feedback", score=50):
    return UpgradeSignal(
        category="low_score",
        task_type=task_type,
        detail=detail,
        score=score,
        source_node="critic",
    )


class TestTier1aClassification:
    def test_single_signal_stays_tier_zero(self):
        d = SelfUpgradeDispatcher()
        result = d.classify_signals([_sig()])
        assert result == Tier.ZERO

    def test_three_signals_same_detail_same_type_stays_tier1b(self):
        d = SelfUpgradeDispatcher()
        signals = [_sig(detail="identical") for _ in range(3)]
        result = d.classify_signals(signals)
        assert result == Tier.ONE_B

    def test_three_signals_varied_detail_same_type_goes_tier1a(self):
        d = SelfUpgradeDispatcher()
        signals = [
            _sig(detail="Missing validation"),
            _sig(detail="Bad error handling"),
            _sig(detail="Unclear naming"),
        ]
        result = d.classify_signals(signals)
        assert result == Tier.ONE_A

    def test_three_signals_varied_detail_different_types_goes_tier3(self):
        d = SelfUpgradeDispatcher()
        signals = [
            _sig(task_type="code_generation", detail="a"),
            _sig(task_type="test_generation", detail="b"),
            _sig(task_type="code_generation", detail="c"),
        ]
        result = d.classify_signals(signals)
        assert result == Tier.THREE

    def test_two_signals_insufficient_for_tier1a(self):
        d = SelfUpgradeDispatcher()
        signals = [
            _sig(detail="a"),
            _sig(detail="b"),
        ]
        result = d.classify_signals(signals)
        assert result == Tier.THREE  # falls through

    def test_tier1b_ordered_before_tier1a(self):
        # Cluster that matches both rules: same-detail subset of same-type.
        # Tier 1b should win because it's checked first.
        d = SelfUpgradeDispatcher()
        signals = [_sig(detail="x") for _ in range(5)]
        result = d.classify_signals(signals)
        assert result == Tier.ONE_B, \
            "Tier 1b must be evaluated before Tier 1a for same-detail clusters"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dispatcher_tier1a_classification.py -v --no-cov`
Expected: FAIL — `test_three_signals_varied_detail_same_type_goes_tier1a` fails because the current classifier returns `Tier.THREE` for this input.

- [ ] **Step 3: Add the Tier 1a classifier rule**

In `agents/self_upgrade_dispatcher.py`, find the `classify_signals` method (lines 110-140). Locate the existing Tier 1b rule:

```python
        # Rule: repeated same-pattern feedback on same task type → Tier 1b
        # (same-detail clusters suggest a missing prompt instruction)
        details = {s.detail for s in non_empty}
        task_types = {s.task_type for s in non_empty}
        if len(details) == 1 and len(task_types) == 1 and len(non_empty) >= 3:
            return Tier.ONE_B
```

Immediately after this block (and before the `# TODO (M2+)` comment), insert:

```python
        # Rule: varied-detail cluster on same task_type with ≥3 signals
        # → Tier 1a refinement of the matching skill. Evaluated AFTER the
        # Tier 1b same-detail rule so that same-detail clusters still route
        # to the cheaper prompt-append fix (Tier 1b).
        if len(task_types) == 1 and len(non_empty) >= 3:
            return Tier.ONE_A
```

Delete the now-irrelevant TODO comment:

```python
        # TODO (M2+): skill-cluster rule for Tier 1a
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dispatcher_tier1a_classification.py tests/test_self_upgrade_dispatcher.py -v --no-cov`
Expected: PASS (all classification tests + existing dispatcher tests)

- [ ] **Step 5: Commit**

```bash
git add agents/self_upgrade_dispatcher.py tests/test_dispatcher_tier1a_classification.py
git commit -m "feat(dispatcher): classify varied-detail clusters as Tier 1a

New rule fires on ≥3 non-empty signals sharing a task_type with varied
critic feedback. Evaluated after the Tier 1b same-detail rule so that
same-detail clusters still route to the cheaper prompt-append fix.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: Dispatcher — wire `Tier1aBuilder` into `dispatch`

**Files:**
- Modify: `agents/self_upgrade_dispatcher.py:86-172`
- Create: `tests/test_dispatcher_tier1a_handling.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dispatcher_tier1a_handling.py`:

```python
"""Dispatcher → Tier1aBuilder hand-off tests with a mocked builder."""

from pathlib import Path
from unittest.mock import MagicMock

from agents.self_upgrade_dispatcher import (
    SelfUpgradeDispatcher, DispatchResult, Tier,
)
from agents.self_upgrade_trigger import UpgradeSignal
from agents.self_upgrade.tier1a_builder import Tier1aResult


def _varied_cluster():
    return [
        UpgradeSignal(
            category="low_score",
            task_type="code_generation",
            detail=f"feedback {i}",
            score=50,
            source_node="critic",
        )
        for i in range(3)
    ]


class TestDispatcherTier1aHandling:
    def test_no_builder_returns_rejected(self):
        d = SelfUpgradeDispatcher()  # no tier1a_builder wired
        result = d.dispatch(_varied_cluster())
        assert isinstance(result, DispatchResult.Rejected)
        assert "tier1a dependencies not wired" in result.reason

    def test_builder_low_confidence_falls_through_to_tier3(self):
        tier1a = MagicMock()
        tier1a.build = MagicMock(return_value=Tier1aResult.LowConfidence(
            reason="no matching skill",
            signal_refs=["sig_1", "sig_2", "sig_3"],
        ))

        tier3 = MagicMock()
        tier3.build = MagicMock()
        paperclip = MagicMock()
        paperclip.create_issue = MagicMock()

        d = SelfUpgradeDispatcher(
            tier1a_builder=tier1a,
            tier3_builder=tier3,
            paperclip_client=paperclip,
            human_triage_user_id="user_1",
        )

        signals = _varied_cluster()
        result = d.dispatch(signals)

        # Either Tier3Filed or Rejected — point is that tier3 builder got called
        tier3.build.assert_called_once()

    def test_builder_success_returns_tier1a_queued(self, tmp_path):
        v2_path = tmp_path / "myCodeSkill__v2"
        tier1a = MagicMock()
        tier1a.build = MagicMock(return_value=Tier1aResult.CandidateWritten(
            skill_name="myCodeSkill",
            v2_path=v2_path,
            signal_refs=["sig_1", "sig_2", "sig_3"],
        ))

        d = SelfUpgradeDispatcher(tier1a_builder=tier1a)

        signals = _varied_cluster()
        result = d.dispatch(signals)

        assert isinstance(result, DispatchResult.Tier1aQueued)
        assert result.refinement_id == "myCodeSkill__v2"
        assert result.signal_refs == ["sig_1", "sig_2", "sig_3"]

    def test_builder_called_with_author_ids(self, tmp_path):
        tier1a = MagicMock()
        tier1a.build = MagicMock(return_value=Tier1aResult.CandidateWritten(
            skill_name="myCodeSkill",
            v2_path=tmp_path / "myCodeSkill__v2",
            signal_refs=[],
        ))

        d = SelfUpgradeDispatcher(tier1a_builder=tier1a)
        d.dispatch(
            _varied_cluster(),
            author_agent_id="agent_x",
            author_run_id="run_y",
        )

        tier1a.build.assert_called_once()
        kwargs = tier1a.build.call_args.kwargs
        assert kwargs["author_agent_id"] == "agent_x"
        assert kwargs["author_run_id"] == "run_y"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_dispatcher_tier1a_handling.py -v --no-cov`
Expected: FAIL — `TypeError: SelfUpgradeDispatcher.__init__() got an unexpected keyword argument 'tier1a_builder'`

- [ ] **Step 3: Wire `Tier1aBuilder` into the dispatcher**

In `agents/self_upgrade_dispatcher.py`:

1. Extend the `TYPE_CHECKING` imports (around line 16–20) to include Tier1aBuilder:

```python
if TYPE_CHECKING:
    from .lesson_store import LessonStore
    from .paperclip_client import PaperclipClient
    from .self_upgrade.tier0_builder import Tier0Builder
    from .self_upgrade.tier1a_builder import Tier1aBuilder
    from .self_upgrade.tier3_builder import Tier3Builder
```

2. Add the constructor parameter (around line 95-108):

```python
    def __init__(
        self,
        *,
        lesson_store: "Optional[LessonStore]" = None,
        tier0_builder: "Optional[Tier0Builder]" = None,
        tier1a_builder: "Optional[Tier1aBuilder]" = None,
        tier3_builder: "Optional[Tier3Builder]" = None,
        paperclip_client: "Optional[PaperclipClient]" = None,
        human_triage_user_id: str = "",
    ) -> None:
        self._lesson_store = lesson_store
        self._tier0 = tier0_builder
        self._tier1a = tier1a_builder
        self._tier3 = tier3_builder
        self._paperclip = paperclip_client
        self._human_triage_user_id = human_triage_user_id
```

3. Add the Tier 1a branch to `dispatch` (around line 163-172). Find:

```python
        if tier == Tier.ZERO:
            return self._handle_tier0(signals, author_agent_id, author_run_id, role)
        if tier == Tier.THREE:
            return self._handle_tier3(signals, author_agent_id, role)

        # Tier 1a/1b/2 still stubs in M1
        return DispatchResult.Rejected(
            reason=f"tier {tier.value} not implemented yet",
            signal_refs=sig_refs,
        )
```

Replace with:

```python
        if tier == Tier.ZERO:
            return self._handle_tier0(signals, author_agent_id, author_run_id, role)
        if tier == Tier.ONE_A:
            return self._handle_tier1a(signals, author_agent_id, author_run_id, role)
        if tier == Tier.THREE:
            return self._handle_tier3(signals, author_agent_id, role)

        # Tier 1b/2 still stubs
        return DispatchResult.Rejected(
            reason=f"tier {tier.value} not implemented yet",
            signal_refs=sig_refs,
        )
```

4. Add the `_handle_tier1a` method. Insert it between `_handle_tier0` and `_handle_tier3`:

```python
    def _handle_tier1a(
        self,
        signals: List[UpgradeSignal],
        author_agent_id: str,
        author_run_id: str,
        role: str,
    ) -> "DispatchResult.AnyResult":
        """Build a v2 refinement candidate via Tier1aBuilder.

        Falls through to Tier 3 on LowConfidence so signals still produce a
        human-visible artifact instead of silently disappearing.
        """
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
            # Fall through to Tier 3 — let the signals surface as a human issue
            logger.info(
                "Tier 1a returned low confidence (%s); falling through to Tier 3",
                result.reason,
            )
            return self._handle_tier3(signals, author_agent_id, role)

        return DispatchResult.Tier1aQueued(
            refinement_id=result.skill_name + "__v2",
            signal_refs=result.signal_refs,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_dispatcher_tier1a_handling.py tests/test_self_upgrade_dispatcher.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agents/self_upgrade_dispatcher.py tests/test_dispatcher_tier1a_handling.py
git commit -m "feat(dispatcher): wire Tier1aBuilder and add _handle_tier1a

Dispatcher routes Tier.ONE_A classifications to Tier1aBuilder.build and
returns DispatchResult.Tier1aQueued on success. LowConfidence results fall
through to Tier 3 so the signals still produce a human-visible artifact.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: `skill_loader` — version detection and `pick_active_version`

**Files:**
- Modify: `agents/skill_loader.py:60-138`
- Create: `tests/test_skill_loader_version_selection.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_skill_loader_version_selection.py`:

```python
"""Tests for version-aware skill loading in agents/skill_loader.py."""

import hashlib
import pytest
from pathlib import Path
from unittest.mock import MagicMock


def _bucket_for(session_id: str) -> int:
    return hashlib.sha256(session_id.encode("utf-8")).digest()[0] % 2


def _session_id_for_bucket(target: int) -> str:
    for i in range(1000):
        sid = f"session_{i}"
        if _bucket_for(sid) == target:
            return sid
    raise RuntimeError("unreachable")


@pytest.fixture
def registry_with_skill(tmp_path):
    """Create a real SkillRegistry with one skill registered in temp tier."""
    from agents.skill_registry import SkillRegistry

    registry = SkillRegistry(base_dir=tmp_path)
    skill_dir = registry.temp_dir / "myCodeSkill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: myCodeSkill\n---\n\n# v1 content"
    )
    registry.register_skill(
        name="myCodeSkill",
        description="test skill",
        tier="temp",
        task_types=["code_generation"],
        skill_path=skill_dir,
    )
    return registry, skill_dir


def _load_state(discovered_name, skill_path, session_id):
    return {
        "session_id": session_id,
        "discovered_skills": [{
            "skill_name": discovered_name,
            "skill_path": str(skill_path),
            "task_type": "code_generation",
            "tier": "temp",
        }],
        "loaded_skills": [],
    }


class TestSkillLoaderVersionSelection:
    def test_single_version_loads_base(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        loader = SkillLoaderNode(skill_registry=registry)
        state = _load_state("myCodeSkill", skill_dir, "session_any")

        result = loader.execute(state)
        assert len(result["loaded_skills"]) == 1
        assert result["loaded_skills"][0]["name"] == "myCodeSkill"

    def test_picks_v1_when_bucket_zero(self, registry_with_skill, tmp_path):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        # Create v2 sibling and register it
        v2_dir = skill_dir.parent / "myCodeSkill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text(
            "---\nname: myCodeSkill__v2\n---\n\n# v2 content"
        )
        registry.register_skill(
            name="myCodeSkill__v2",
            description="v2 refined",
            tier="temp",
            task_types=["code_generation"],
            skill_path=v2_dir,
        )

        loader = SkillLoaderNode(skill_registry=registry)
        state = _load_state("myCodeSkill", skill_dir, _session_id_for_bucket(0))

        result = loader.execute(state)
        assert result["loaded_skills"][0]["name"] == "myCodeSkill"
        assert result["loaded_skills"][0]["content"] == "---\nname: myCodeSkill\n---\n\n# v1 content"

    def test_picks_v2_when_bucket_one(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        v2_dir = skill_dir.parent / "myCodeSkill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text(
            "---\nname: myCodeSkill__v2\n---\n\n# v2 content"
        )
        registry.register_skill(
            name="myCodeSkill__v2",
            description="v2 refined",
            tier="temp",
            task_types=["code_generation"],
            skill_path=v2_dir,
        )

        loader = SkillLoaderNode(skill_registry=registry)
        state = _load_state("myCodeSkill", skill_dir, _session_id_for_bucket(1))

        result = loader.execute(state)
        assert result["loaded_skills"][0]["name"] == "myCodeSkill__v2"
        assert "v2 content" in result["loaded_skills"][0]["content"]

    def test_version_choice_deterministic(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        v2_dir = skill_dir.parent / "myCodeSkill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text(
            "---\nname: myCodeSkill__v2\n---\n\n# v2"
        )
        registry.register_skill(
            name="myCodeSkill__v2",
            description="v2",
            tier="temp",
            task_types=["code_generation"],
            skill_path=v2_dir,
        )

        loader = SkillLoaderNode(skill_registry=registry)
        session_id = "session_42"

        names = []
        for _ in range(3):
            state = _load_state("myCodeSkill", skill_dir, session_id)
            result = loader.execute(state)
            names.append(result["loaded_skills"][0]["name"])

        assert len(set(names)) == 1

    def test_discovered_skills_updated_with_versioned_name(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        v2_dir = skill_dir.parent / "myCodeSkill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text(
            "---\nname: myCodeSkill__v2\n---\n\n# v2"
        )
        registry.register_skill(
            name="myCodeSkill__v2",
            description="v2",
            tier="temp",
            task_types=["code_generation"],
            skill_path=v2_dir,
        )

        loader = SkillLoaderNode(skill_registry=registry)
        state = _load_state("myCodeSkill", skill_dir, _session_id_for_bucket(1))
        result = loader.execute(state)

        # Invariant: discovered_skills entry mutated in place to the
        # versioned name so cleanup's task_type map lookup succeeds.
        assert result["discovered_skills"][0]["skill_name"] == "myCodeSkill__v2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_skill_loader_version_selection.py -v --no-cov`
Expected: FAIL — `test_single_version_loads_base` may pass, but the version-selection tests fail because the loader does not yet check for siblings.

- [ ] **Step 3: Add version detection to `skill_loader.execute`**

In `agents/skill_loader.py`, find the loop body around lines 66-73 (inside `execute`, the `for skill_info in discovered_skills:` loop). Before the existing `skill_content = self.skill_registry.load_skill(skill_name)` call (line 74), insert the version selection block:

```python
        for skill_info in discovered_skills:
            skill_name = skill_info.get("skill_name")

            # Skip ephemeral skills that haven't been generated yet
            if not skill_name:
                continue

            # ── Version-aware selection ──────────────────────────────
            # If a __v{N} sibling exists for this skill (produced by a
            # Tier 1a refinement), deterministically bucket the current
            # run to v1 or v2 via skill_ab.pick_active_version. The
            # chosen directory's name becomes the effective skill_name
            # for the rest of this load and for outcome recording.
            raw_path = skill_info.get("skill_path")
            if raw_path:
                from . import skill_ab
                base = skill_ab.base_name(skill_name)
                skill_path = Path(raw_path)
                versions = skill_ab.list_versions_for(
                    base, skills_root=skill_path.parent
                )
                if len(versions) > 1:
                    chosen = skill_ab.pick_active_version(
                        versions, run_input=state.get("session_id", "")
                    )
                    # Mutate skill_info in place so downstream state
                    # (discovered_skills + loaded_skills) agree on the
                    # versioned name.
                    if chosen.name != skill_name:
                        skill_name = chosen.name
                        skill_info["skill_name"] = chosen.name
                        skill_info["skill_path"] = str(chosen)

            # Load skill content
            skill_content = self.skill_registry.load_skill(skill_name)
```

Add `from pathlib import Path` to the file's imports if not already present (it should be — skill_loader already uses Path in several places).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_skill_loader_version_selection.py tests/test_skill_loader.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agents/skill_loader.py tests/test_skill_loader_version_selection.py
git commit -m "feat(skill_loader): version-aware loading via skill_ab.pick_active_version

When a __v{N} sibling exists alongside a discovered skill, deterministically
bucket to one version via sha256(session_id) % 2 and mutate the skill_info
dict in place so downstream state fields agree on the versioned name.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 16: `skill_cleanup` — wire `maybe_promote_winners`

**Files:**
- Modify: `agents/skill_cleanup.py`
- Test: `tests/test_skill_cleanup.py`

- [ ] **Step 1: Write the failing tests**

Find `tests/test_skill_cleanup.py` and append:

```python
class TestPromotionOnCleanup:
    def test_maybe_promote_winners_called_after_recording(self, tmp_path):
        from unittest.mock import MagicMock, patch
        from agents.skill_cleanup import SkillCleanupNode
        from agents.skill_registry import SkillRegistry
        from agents.skill_outcome_store import SkillOutcomeStore

        registry = SkillRegistry(base_dir=tmp_path)
        outcome_store = SkillOutcomeStore(
            store_path=str(tmp_path / "outcomes.jsonl")
        )
        cleanup = SkillCleanupNode(
            skill_registry=registry,
            outcome_store=outcome_store,
        )

        state = {
            "skills_in_use": ["myCodeSkill"],
            "discovered_skills": [{
                "skill_name": "myCodeSkill",
                "task_type": "code_generation",
            }],
            "loaded_skills": [{
                "name": "myCodeSkill",
                "content": "# content",
                "task_type": "code_generation",
            }],
            "output_critic_score": 80,
            "output_critic_feedback": "",
            "sub_tasks": [],
            "specification": "test spec",
        }

        with patch("agents.skill_ab.maybe_promote_winners") as mock_promote:
            mock_promote.return_value = []
            cleanup.execute(state)

        mock_promote.assert_called_once()
        kwargs = mock_promote.call_args.kwargs
        assert "myCodeSkill" in kwargs["skill_names_in_run"]
        assert kwargs["outcome_store"] is outcome_store
        assert kwargs["skill_registry"] is registry
        assert kwargs["K_per_version"] == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_skill_cleanup.py::TestPromotionOnCleanup -v --no-cov`
Expected: FAIL — `mock_promote.assert_called_once()` fails because `skill_cleanup` does not yet call `maybe_promote_winners`.

- [ ] **Step 3: Add the `maybe_promote_winners` call**

In `agents/skill_cleanup.py`:

1. Add the import at the top of the file (after existing imports):

```python
from pathlib import Path
from . import skill_ab
from .config import get_skills_dir
```

2. In the `execute` method, find the existing `if skills_in_use:` block (around lines 73-91). After the `_record_outcomes_and_refine` call and before the `else:` branch, insert the promotion call. The current code:

```python
        if skills_in_use:
            # Build shared lookups once (Bug #5 fix: avoid redundant computation)
            subtask_scores = self._build_subtask_score_map(state)
            subtask_feedback = self._build_subtask_feedback_map(state)
            fallback_score = state.get("output_critic_score") or 0
            fallback_feedback = state.get("output_critic_feedback", "")

            # Track usage for all skills that were used
            self._track_skill_usage(
                state, skills_in_use, subtask_scores, fallback_score
            )

            # Record outcomes and trigger refinement (reinforcement loop)
            self._record_outcomes_and_refine(
                state, skills_in_use, subtask_scores, subtask_feedback,
                fallback_score, fallback_feedback,
            )
        else:
            logger.info("No skills were used in this session")
```

Becomes:

```python
        if skills_in_use:
            # Build shared lookups once (Bug #5 fix: avoid redundant computation)
            subtask_scores = self._build_subtask_score_map(state)
            subtask_feedback = self._build_subtask_feedback_map(state)
            fallback_score = state.get("output_critic_score") or 0
            fallback_feedback = state.get("output_critic_feedback", "")

            # Track usage for all skills that were used
            self._track_skill_usage(
                state, skills_in_use, subtask_scores, fallback_score
            )

            # Record outcomes in the outcome store (reinforcement loop).
            # Refinement itself now runs via the self-upgrade dispatcher
            # (Tier 1a); this node only records + promotes.
            self._record_outcomes_and_refine(
                state, skills_in_use, subtask_scores, subtask_feedback,
                fallback_score, fallback_feedback,
            )

            # Tier 1a promotion: check whether any A/B'd skill in this
            # run has hit K per-version outcomes; promote winners and
            # archive losers inline.
            if self.outcome_store is not None:
                try:
                    promotions = skill_ab.maybe_promote_winners(
                        skill_names_in_run=list(skills_in_use),
                        outcome_store=self.outcome_store,
                        skills_root=Path(get_skills_dir()),
                        skill_registry=self.skill_registry,
                        K_per_version=10,
                    )
                    for p in promotions:
                        logger.info(
                            "🎯 Tier 1a promoted %s: v%d beat v%d (avg %.1f vs %.1f)",
                            p.base_name, p.winner_version, p.loser_version,
                            p.winner_avg, p.loser_avg,
                        )
                except Exception as e:  # pragma: no cover — defensive
                    logger.warning(
                        "Tier 1a promotion check failed: %s", e,
                    )
        else:
            logger.info("No skills were used in this session")
```

Also update the method name reference if the test still calls it `_record_outcomes_and_refine` — the method no longer does refinement but keeping the name avoids an extra rename touch. If desired, rename to `_record_outcomes` in a follow-up.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_skill_cleanup.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agents/skill_cleanup.py tests/test_skill_cleanup.py
git commit -m "feat(skill_cleanup): wire skill_ab.maybe_promote_winners

After outcome recording, check each base skill in the run for A/B
promotion eligibility. Winners take the canonical name; losers move to
the archive directory. Defensive try/except so a promotion failure never
crashes cleanup.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 17: Lock-in invariant tests

**Files:**
- Modify: `tests/test_self_upgrade_invariants.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_self_upgrade_invariants.py`:

```python
from agents.skill_generator import SkillGeneratorNode


def test_skill_generator_no_longer_has_refine_skill():
    """Lock-in: refine_skill was deleted in favor of draft_refined_content + Tier1aBuilder."""
    assert not hasattr(SkillGeneratorNode, "refine_skill"), (
        "refine_skill must not exist — the in-place rewrite path was deleted "
        "in favor of dispatcher-driven Tier 1a refinement via Tier1aBuilder. "
        "If this test fails, either the deletion was reverted or a new caller "
        "was added; both are regressions."
    )


def test_skill_generator_has_draft_refined_content():
    """Lock-in: draft_refined_content is the public pure-function replacement."""
    assert hasattr(SkillGeneratorNode, "draft_refined_content"), (
        "draft_refined_content must exist as the public pure-function "
        "replacement for refine_skill, used by Tier1aBuilder."
    )


def test_skill_cleanup_no_longer_imports_refinement_threshold():
    """Lock-in: REFINEMENT_THRESHOLD import was removed when auto-refine was ripped out."""
    import agents.skill_cleanup as sc
    source = Path(sc.__file__).read_text()
    assert "REFINEMENT_THRESHOLD" not in source, (
        "REFINEMENT_THRESHOLD must not appear in skill_cleanup.py — the "
        "auto-refine path was removed in favor of dispatcher-driven Tier 1a."
    )
    assert "refine_skill" not in source, (
        "refine_skill must not be referenced in skill_cleanup.py — the "
        "in-place rewrite path was removed."
    )


def test_skill_cleanup_calls_maybe_promote_winners():
    """Lock-in: promotion is wired in skill_cleanup after outcome recording."""
    import agents.skill_cleanup as sc
    source = Path(sc.__file__).read_text()
    assert "maybe_promote_winners" in source, (
        "skill_cleanup must call skill_ab.maybe_promote_winners after "
        "outcome recording to complete the Tier 1a A/B loop."
    )
```

At the top of the file, add:

```python
from pathlib import Path
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_self_upgrade_invariants.py -v --no-cov`
Expected: PASS (5 tests total, including the Task 7 `test_skill_ab_is_immutable`)

- [ ] **Step 3: Run the full affected test suite to verify nothing regressed**

Run: `python3 -m pytest tests/test_skill_ab.py tests/test_tier1a_builder.py tests/test_dispatcher_tier1a_classification.py tests/test_dispatcher_tier1a_handling.py tests/test_skill_loader_version_selection.py tests/test_self_upgrade_invariants.py tests/test_self_upgrade_dispatcher.py tests/test_skill_reinforcement.py tests/test_skill_cleanup.py tests/test_skill_registry.py tests/test_misc_coverage.py -q --no-cov`
Expected: PASS (all tests)

- [ ] **Step 4: Run the full test suite**

Run: `python3 -m pytest tests/ -q --no-cov --tb=short 2>&1 | tail -20`
Expected: PASS (full suite). Coverage threshold failure is expected for per-file runs but the `-q` full-suite run should report `N passed, M skipped`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_self_upgrade_invariants.py
git commit -m "test(self_upgrade): lock-in invariants for Tier 1a deletions and wiring

- refine_skill / _find_skill_path deleted
- draft_refined_content is the public pure replacement
- skill_cleanup no longer imports REFINEMENT_THRESHOLD or calls refine_skill
- skill_cleanup calls maybe_promote_winners
- skill_ab is in _ADDITIONAL_IMMUTABLES

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
