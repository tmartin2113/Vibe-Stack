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
