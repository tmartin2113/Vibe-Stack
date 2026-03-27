"""
Tests for the self-upgrade trigger module.
"""

import os
from unittest.mock import patch

import pytest

os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

from agents.self_upgrade_trigger import (
    MIN_SIGNALS_TO_PROPOSE,
    POOR_SCORE_THRESHOLD,
    TOOL_FAILURE_THRESHOLD,
    SelfUpgradeTrigger,
    TriggerAnalysis,
    UpgradeSignal,
    analyse_for_upgrade,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_state(**overrides):
    """Build a minimal AgentState dict with sane defaults."""
    state = {
        "routed_task_type": "code_generation",
        "output_critic_score": 85,
        "output_critic_feedback": "",
        "tool_calls_made": [],
        "iteration_count": 1,
        "max_iterations": 3,
    }
    state.update(overrides)
    return state


@pytest.fixture
def trigger():
    return SelfUpgradeTrigger()


# ── No signals for healthy workflows ─────────────────────────────────


class TestHealthyWorkflow:

    def test_good_score_no_signals(self, trigger):
        state = _make_state(output_critic_score=90)
        result = trigger.analyse(state)
        assert result.signals == []
        assert result.should_propose is False

    def test_unevaluated_score_no_signals(self, trigger):
        state = _make_state(output_critic_score=0)
        result = trigger.analyse(state)
        assert result.signals == []

    def test_no_tool_failures_no_signals(self, trigger):
        state = _make_state(
            output_critic_score=85,
            tool_calls_made=[
                {"tool": "file_reader", "result": {"success": True}},
                {"tool": "pytest_runner", "result": {"success": True}},
            ],
        )
        result = trigger.analyse(state)
        assert not any(s.category == "tool_failure" for s in result.signals)


# ── Low score detection ──────────────────────────────────────────────


class TestLowScoreDetection:

    def test_low_score_generates_signal(self, trigger):
        state = _make_state(output_critic_score=40)
        result = trigger.analyse(state)
        assert any(s.category == "low_score" for s in result.signals)

    def test_score_at_threshold_no_signal(self, trigger):
        state = _make_state(output_critic_score=POOR_SCORE_THRESHOLD)
        result = trigger.analyse(state)
        assert not any(s.category == "low_score" for s in result.signals)

    def test_score_just_below_threshold(self, trigger):
        state = _make_state(output_critic_score=POOR_SCORE_THRESHOLD - 1)
        result = trigger.analyse(state)
        assert any(s.category == "low_score" for s in result.signals)

    def test_feedback_included_in_signal(self, trigger):
        state = _make_state(
            output_critic_score=30,
            output_critic_feedback="Missing error handling and no tests provided",
        )
        result = trigger.analyse(state)
        signal = next(s for s in result.signals if s.category == "low_score")
        assert "Missing error handling" in signal.detail


# ── Tool failure detection ───────────────────────────────────────────


class TestToolFailureDetection:

    def test_multiple_failures_generates_signal(self, trigger):
        state = _make_state(
            tool_calls_made=[
                {"tool": "file_writer", "result": {"success": False}},
                {"tool": "pytest_runner", "result": {"success": False}},
                {"tool": "file_reader", "result": {"success": True}},
            ],
        )
        result = trigger.analyse(state)
        assert any(s.category == "tool_failure" for s in result.signals)

    def test_single_failure_no_signal(self, trigger):
        state = _make_state(
            tool_calls_made=[
                {"tool": "file_writer", "result": {"success": False}},
                {"tool": "file_reader", "result": {"success": True}},
            ],
        )
        result = trigger.analyse(state)
        assert not any(s.category == "tool_failure" for s in result.signals)

    def test_empty_tool_calls_no_signal(self, trigger):
        state = _make_state(tool_calls_made=[])
        result = trigger.analyse(state)
        assert not any(s.category == "tool_failure" for s in result.signals)

    def test_tool_names_in_detail(self, trigger):
        state = _make_state(
            tool_calls_made=[
                {"tool": "pytest_runner", "result": {"success": False}},
                {"tool": "bandit", "result": {"success": False}},
            ],
        )
        result = trigger.analyse(state)
        signal = next(s for s in result.signals if s.category == "tool_failure")
        assert "pytest_runner" in signal.detail or "bandit" in signal.detail


# ── Iteration exhaustion detection ───────────────────────────────────


class TestIterationExhaustion:

    def test_max_iterations_low_score(self, trigger):
        state = _make_state(
            iteration_count=3,
            max_iterations=3,
            output_critic_score=65,
        )
        result = trigger.analyse(state)
        assert any(s.category == "iteration_exhaustion" for s in result.signals)

    def test_max_iterations_good_score_no_signal(self, trigger):
        state = _make_state(
            iteration_count=3,
            max_iterations=3,
            output_critic_score=85,
        )
        result = trigger.analyse(state)
        assert not any(s.category == "iteration_exhaustion" for s in result.signals)

    def test_not_max_iterations_no_signal(self, trigger):
        state = _make_state(
            iteration_count=1,
            max_iterations=3,
            output_critic_score=50,
        )
        result = trigger.analyse(state)
        assert not any(s.category == "iteration_exhaustion" for s in result.signals)


# ── Critic pattern detection ─────────────────────────────────────────


class TestCriticPatterns:

    def test_missing_error_handling(self, trigger):
        state = _make_state(
            output_critic_feedback="The code is missing error handling for edge cases",
        )
        result = trigger.analyse(state)
        assert any(s.category == "critic_pattern" for s in result.signals)

    def test_incomplete_implementation(self, trigger):
        state = _make_state(
            output_critic_feedback="This is an incomplete implementation of the required feature",
        )
        result = trigger.analyse(state)
        assert any(
            s.category == "critic_pattern" and "incomplete" in s.detail.lower()
            for s in result.signals
        )

    def test_security_concern(self, trigger):
        state = _make_state(
            output_critic_feedback="Found a security vulnerability in input handling",
        )
        result = trigger.analyse(state)
        assert any(s.category == "critic_pattern" for s in result.signals)

    def test_no_pattern_match(self, trigger):
        state = _make_state(
            output_critic_feedback="Good work overall, minor formatting suggestions",
        )
        result = trigger.analyse(state)
        assert not any(s.category == "critic_pattern" for s in result.signals)

    def test_empty_feedback_no_signal(self, trigger):
        state = _make_state(output_critic_feedback="")
        result = trigger.analyse(state)
        assert not any(s.category == "critic_pattern" for s in result.signals)


# ── Signal accumulation and proposal ─────────────────────────────────


class TestSignalAccumulation:

    def test_single_run_no_proposal(self, trigger):
        """One bad run should not trigger a proposal."""
        state = _make_state(output_critic_score=30)
        result = trigger.analyse(state)
        assert len(result.signals) > 0
        assert result.should_propose is False

    def test_accumulated_signals_trigger_proposal(self, trigger):
        """Multiple bad runs should eventually trigger a proposal."""
        for _ in range(MIN_SIGNALS_TO_PROPOSE):
            state = _make_state(output_critic_score=40)
            result = trigger.analyse(state)

        assert result.should_propose is True
        assert result.proposal_description != ""
        assert result.proposal_rationale != ""

    def test_different_task_types_separate(self, trigger):
        """Signals for different task types shouldn't cross-contaminate."""
        # Two bad runs for code_generation
        for _ in range(2):
            trigger.analyse(_make_state(
                routed_task_type="code_generation",
                output_critic_score=40,
            ))

        # One bad run for test_generation — total signals < threshold
        result = trigger.analyse(_make_state(
            routed_task_type="test_generation",
            output_critic_score=40,
        ))
        assert result.should_propose is False

        # But code_generation should now have 3+ signals
        result = trigger.analyse(_make_state(
            routed_task_type="code_generation",
            output_critic_score=40,
        ))
        assert result.should_propose is True

    def test_clear_signals(self, trigger):
        """Clearing signals should reset accumulation."""
        for _ in range(MIN_SIGNALS_TO_PROPOSE):
            trigger.analyse(_make_state(output_critic_score=40))

        trigger.clear_signals("code_generation")
        assert trigger.get_signal_count("code_generation") == 0

        result = trigger.analyse(_make_state(output_critic_score=40))
        assert result.should_propose is False

    def test_get_signal_count(self, trigger):
        trigger.analyse(_make_state(output_critic_score=40))
        assert trigger.get_signal_count("code_generation") >= 1
        assert trigger.get_signal_count("nonexistent") == 0


# ── Proposal construction ────────────────────────────────────────────


class TestProposalConstruction:

    def test_proposal_has_description(self, trigger):
        for _ in range(MIN_SIGNALS_TO_PROPOSE):
            trigger.analyse(_make_state(output_critic_score=40))

        result = trigger.analyse(_make_state(output_critic_score=40))
        assert "code_generation" in result.proposal_description

    def test_proposal_has_rationale(self, trigger):
        for _ in range(MIN_SIGNALS_TO_PROPOSE):
            trigger.analyse(_make_state(output_critic_score=40))

        result = trigger.analyse(_make_state(output_critic_score=40))
        assert "signal" in result.proposal_rationale.lower()
        assert "low_score" in result.proposal_rationale

    def test_target_files_identified(self, trigger):
        for _ in range(MIN_SIGNALS_TO_PROPOSE):
            trigger.analyse(_make_state(output_critic_score=40))

        result = trigger.analyse(_make_state(output_critic_score=40))
        assert len(result.target_files) > 0
        assert all(f.startswith("agents/") for f in result.target_files)

    def test_tool_failure_targets(self, trigger):
        for _ in range(MIN_SIGNALS_TO_PROPOSE):
            trigger.analyse(_make_state(
                tool_calls_made=[
                    {"tool": "a", "result": {"success": False}},
                    {"tool": "b", "result": {"success": False}},
                ],
            ))

        result = trigger.analyse(_make_state(
            tool_calls_made=[
                {"tool": "a", "result": {"success": False}},
                {"tool": "b", "result": {"success": False}},
            ],
        ))
        assert "agents/tools/registry.py" in result.target_files


# ── Convenience function ─────────────────────────────────────────────


class TestAnalyseForUpgrade:

    def test_without_trigger_instance(self):
        state = _make_state(output_critic_score=40)
        result = analyse_for_upgrade(state)
        assert isinstance(result, TriggerAnalysis)
        assert len(result.signals) > 0

    def test_with_shared_trigger(self):
        shared = SelfUpgradeTrigger()
        for _ in range(MIN_SIGNALS_TO_PROPOSE):
            analyse_for_upgrade(_make_state(output_critic_score=40), trigger=shared)

        result = analyse_for_upgrade(
            _make_state(output_critic_score=40), trigger=shared,
        )
        assert result.should_propose is True


# ── Multiple signal types in one run ─────────────────────────────────


class TestMultipleSignals:

    def test_multiple_signals_single_run(self, trigger):
        """A single terrible run can generate multiple signal types."""
        state = _make_state(
            output_critic_score=30,
            output_critic_feedback="This is an incomplete implementation with security issues",
            tool_calls_made=[
                {"tool": "a", "result": {"success": False}},
                {"tool": "b", "result": {"success": False}},
            ],
            iteration_count=3,
            max_iterations=3,
        )
        result = trigger.analyse(state)
        categories = {s.category for s in result.signals}
        assert "low_score" in categories
        assert "tool_failure" in categories
        # iteration_exhaustion requires score < 70, which 30 satisfies
        assert "iteration_exhaustion" in categories
        assert "critic_pattern" in categories

    def test_many_signals_fast_proposal(self, trigger):
        """A single run with 3+ signals should trigger proposal immediately."""
        state = _make_state(
            output_critic_score=30,
            output_critic_feedback="The code is missing error handling and is an incomplete solution",
            tool_calls_made=[
                {"tool": "a", "result": {"success": False}},
                {"tool": "b", "result": {"success": False}},
            ],
            iteration_count=3,
            max_iterations=3,
        )
        result = trigger.analyse(state)
        # Should have multiple signals, potentially >= MIN_SIGNALS_TO_PROPOSE
        assert len(result.signals) >= MIN_SIGNALS_TO_PROPOSE
        assert result.should_propose is True
