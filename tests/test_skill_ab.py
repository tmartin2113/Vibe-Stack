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
