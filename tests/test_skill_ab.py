"""Tests for agents/skill_ab.py — A/B versioning logic for skill refinements."""

import hashlib

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
