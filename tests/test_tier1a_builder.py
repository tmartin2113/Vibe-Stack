"""Tests for agents/self_upgrade/tier1a_builder.py — Tier 1a skill refinement builder."""

import pytest
from pathlib import Path
from dataclasses import is_dataclass
from unittest.mock import MagicMock

from agents.self_upgrade.tier1a_builder import Tier1aBuilder, Tier1aResult
from agents.self_upgrade_trigger import UpgradeSignal


def _make_signal(task_type="code_generation", detail="Missing validation", score=50):
    return UpgradeSignal(
        category="low_score",
        task_type=task_type,
        detail=detail,
        score=score,
        source_node="critic",
    )


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
        builder = Tier1aBuilder(
            skill_generator=MagicMock(),
            skill_registry=MagicMock(),
            outcome_store=MagicMock(),
            skills_root=tmp_path,
        )
        assert builder._skills_root == tmp_path


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
