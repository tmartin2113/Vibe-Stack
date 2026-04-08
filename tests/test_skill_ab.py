"""Tests for agents/skill_ab.py — A/B versioning logic for skill refinements."""

import datetime
import hashlib
from unittest.mock import MagicMock

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

    def test_list_versions_rejects_leading_zero_version(self, tmp_path):
        # foo__v02 parses to version 2 but is not the canonical form
        # produced by versioned_name(). Reject it to keep ordering
        # deterministic and prevent silent misfires in promotion logic.
        canonical = tmp_path / "foo__v2"
        leading_zero = tmp_path / "foo__v02"
        for d in (canonical, leading_zero):
            d.mkdir()
            (d / "SKILL.md").write_text("# test")
        result = skill_ab.list_versions_for("foo", skills_root=tmp_path)
        assert result == [canonical]


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
