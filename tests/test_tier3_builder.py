"""Tests for Tier3Builder: LLM draft + self-critique gating."""
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


def test_builder_drops_on_malformed_json_from_llm():
    """LLM draft that isn't valid JSON should be dropped cleanly."""
    llm = MagicMock()
    llm.generate.return_value = "not json at all, just prose"

    builder = Tier3Builder(llm=llm)
    signals = [UpgradeSignal(category="low_score", task_type="t", detail="d", score=40)]

    result = builder.build(signals, author_agent_id="", author_role="")
    assert isinstance(result, Tier3Result.Dropped)
    assert "json" in result.reason.lower() or "parse" in result.reason.lower()
