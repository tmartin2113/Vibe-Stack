"""
Tests for heuristic output critic — the zero-LLM-cost output evaluator
that can approve good output without calling the LLM critic.
"""

import pytest
from agents.heuristic_critic import heuristic_evaluate_output, _compute_score
from agents.state import AgentState


def _make_state(
    output: str = "",
    task_type: str = "code_generation",
    threshold: int = 70,
) -> AgentState:
    """Build a minimal AgentState for heuristic critic testing."""
    return AgentState(
        user_request="test request",
        session_id="test",
        quality_threshold=85,
        effective_quality_threshold=threshold,
        complexity_tier="fast",
        heuristic_critic_score=0,
        heuristic_critic_passed=False,
        specialist_output=output,
        routed_task_type=task_type,
        iteration_count=0,
        max_iterations=3,
        quality_gate_decision="refine",
        start_time="2024-01-01T00:00:00",
        discovered_skills=[],
        skills_in_use=[],
        skill_quality_scores={},
        loaded_skills=[],
skills_cleaned_up=False,
        specialist_iteration_count=0,
        specialist_max_iterations=3,
        requires_decomposition=False,
        sub_tasks=[],
        current_sub_task_index=0,
        completed_sub_tasks=0,
        parallel_execution=False,
        aggregation_strategy="merge",
        cache_hit=False,
        cache_key="",
        cache_entry_stored=False,
        parallel_execution_errors=[],
        memory_context="",
        conversation_history=[],
        adapters_used=[],
        tool_calls_made=[],
        debug_info={},
    )


# ── _compute_score unit tests ──

class TestComputeScore:
    """Direct tests for the scoring function."""

    def test_empty_output_scores_zero(self):
        assert _compute_score("", "code_generation") == 0

    def test_whitespace_only_scores_zero(self):
        assert _compute_score("   \n\t  ", "code_generation") == 0

    def test_good_code_output_scores_high(self):
        output = """```python
def calculate_total(items):
    return sum(item.price for item in items)
```"""
        score = _compute_score(output, "code_generation")
        assert score >= 85

    def test_short_code_output_penalty(self):
        output = "x = 1"
        score = _compute_score(output, "code_generation")
        assert score < 85  # Short + no code block

    def test_no_code_block_penalty_for_code_task(self):
        output = "def foo():\n    return 42\n"  # No ``` fences
        score = _compute_score(output, "code_generation")
        assert score <= 60  # Penalized for missing code block

    def test_no_code_block_no_penalty_for_text_task(self):
        """Documentation tasks don't require code blocks."""
        output = "This module handles user authentication. " * 5
        score = _compute_score(output, "documentation")
        assert score >= 85

    def test_error_pattern_penalty(self):
        output = """```python
def foo():
    pass
```
Traceback (most recent call last):
  File "test.py", line 1
TypeError: expected str"""
        score = _compute_score(output, "code_generation")
        assert score <= 50

    def test_truncation_penalty(self):
        output = """```python
def foo():
    return bar
```
[truncated]"""
        score = _compute_score(output, "code_generation")
        assert score < 85

    def test_ellipsis_truncation(self):
        output = """```python
def process():
    for item in items:
        ..."""
        score = _compute_score(output, "code_generation")
        assert score < 85

    def test_structural_bonus(self):
        """Output with function defs gets a small bonus."""
        output = """```python
def calculate_total(items):
    total = 0
    for item in items:
        total += item.price
    return total
```"""
        score = _compute_score(output, "code_generation")
        assert score == 90  # 85 base + 5 structural

    def test_score_clamped_to_0(self):
        """Score cannot go below 0 even with multiple penalties."""
        output = "x"  # Very short, no code block, for a code task
        score = _compute_score(output, "code_generation")
        assert score >= 0

    def test_score_clamped_to_100(self):
        """Score cannot exceed 100."""
        output = """```python
def foo():
    return 42
class Bar:
    pass
import os
from sys import argv
```"""
        score = _compute_score(output, "code_generation")
        assert score <= 100

    def test_test_generation_requires_code_block(self):
        output = "You should test the function by calling it."
        score = _compute_score(output, "test_generation")
        assert score < 70

    def test_security_audit_is_code_task(self):
        """Security audit is in _CODE_TASK_TYPES."""
        output = "The code looks secure."
        score = _compute_score(output, "security_audit")
        assert score < 70  # No code block


# ── heuristic_evaluate_output integration tests ──

class TestHeuristicEvaluateOutput:
    """Full node function tests."""

    def test_passes_good_output(self):
        output = """```python
def add(a, b):
    return a + b
```"""
        state = _make_state(output=output, threshold=70)
        result = heuristic_evaluate_output(state)
        assert result["heuristic_critic_passed"] is True
        assert result["heuristic_critic_score"] >= 70

    def test_fails_empty_output(self):
        state = _make_state(output="", threshold=70)
        result = heuristic_evaluate_output(state)
        assert result["heuristic_critic_passed"] is False
        assert result["heuristic_critic_score"] == 0

    def test_fails_below_threshold(self):
        state = _make_state(output="bad", task_type="code_generation", threshold=70)
        result = heuristic_evaluate_output(state)
        assert result["heuristic_critic_passed"] is False

    def test_threshold_boundary_exact(self):
        """Output scoring exactly at threshold passes."""
        output = """```python
def calculate_total(items):
    total = sum(item.price for item in items)
    return total
```"""
        # This scores 90 (85 base + 5 structural, length > 50)
        state = _make_state(output=output, threshold=90)
        result = heuristic_evaluate_output(state)
        assert result["heuristic_critic_passed"] is True

    def test_writes_both_fields(self):
        state = _make_state(output="something")
        result = heuristic_evaluate_output(state)
        assert "heuristic_critic_score" in result
        assert "heuristic_critic_passed" in result

    def test_preserves_existing_state(self):
        state = _make_state(output="x")
        state["user_request"] = "original request"
        result = heuristic_evaluate_output(state)
        assert result["user_request"] == "original request"

    def test_documentation_text_passes(self):
        """Long documentation text should pass for documentation task type."""
        output = "This module provides authentication utilities. " * 10
        state = _make_state(output=output, task_type="documentation", threshold=70)
        result = heuristic_evaluate_output(state)
        assert result["heuristic_critic_passed"] is True

    def test_high_threshold_stricter(self):
        """Higher threshold should fail borderline outputs."""
        output = "def foo(): pass"  # Short, no code block
        state_low = _make_state(output=output, threshold=30)
        state_high = _make_state(output=output, threshold=80)
        heuristic_evaluate_output(state_low)
        heuristic_evaluate_output(state_high)
        # Low threshold might pass where high fails
        assert state_high["heuristic_critic_passed"] is False


# ── should_use_llm_critic decision function tests ──

class TestShouldUseLlmCritic:
    """Test the decision function that gates heuristic → LLM critic."""

    def test_approve_when_heuristic_passed(self):
        from agents.decision_functions import should_use_llm_critic
        state = _make_state()
        state["heuristic_critic_passed"] = True
        state["heuristic_critic_score"] = 90
        result = should_use_llm_critic(state)
        assert result == "approve"
        assert state["output_critic_score"] == 90

    def test_critic_output_when_heuristic_failed(self):
        from agents.decision_functions import should_use_llm_critic
        state = _make_state()
        state["heuristic_critic_passed"] = False
        state["heuristic_critic_score"] = 40
        result = should_use_llm_critic(state)
        assert result == "critic_output"

    def test_default_false_when_field_missing(self):
        from agents.decision_functions import should_use_llm_critic
        state = _make_state()
        # Don't set heuristic_critic_passed
        del state["heuristic_critic_passed"]
        result = should_use_llm_critic(state)
        assert result == "critic_output"
