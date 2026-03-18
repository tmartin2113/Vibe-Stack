"""
Tests for complexity triage — the zero-LLM-cost router that assigns
requests to fast / standard / full pipeline tiers.
"""

import pytest
from agents.complexity_triage import classify_complexity
from agents.state import AgentState


def _make_state(request: str, intent: str = "code_generation", threshold: int = 85) -> AgentState:
    """Build a minimal AgentState for triage testing."""
    return AgentState(
        user_request=request,
        intent=intent,
        intent_confidence=0.9,
        quality_threshold=threshold,
        iteration_count=0,
        max_iterations=3,
        quality_gate_decision="refine",
        session_id="test",
        start_time="2024-01-01T00:00:00",
        complexity_tier="",
        effective_quality_threshold=threshold,
        heuristic_critic_score=0,
        heuristic_critic_passed=False,
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


# ── Fast-path classification ──

class TestFastPath:
    """Tasks that should classify as 'fast' — short, specific, single action."""

    def test_add_docstring(self):
        state = _make_state("Add a docstring to `calculate_total` in utils.py")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"
        assert result["effective_quality_threshold"] == 70

    def test_write_pytest(self):
        state = _make_state("Write a pytest for calculate_total()")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"

    def test_fix_function(self):
        state = _make_state("Fix the bug in `parse_config` in config.py")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"

    def test_rename_variable(self):
        state = _make_state("Rename `getData` to `get_data` in api_client.py")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"

    def test_add_type_hints(self):
        state = _make_state("Add type hints to `process_items` in worker.py")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"

    def test_refactor_method(self):
        state = _make_state("Refactor `handle_request()` to use async/await")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"

    def test_create_simple_class(self):
        state = _make_state("Create a `UserValidator` class in validators.py")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"

    def test_implement_function(self):
        state = _make_state("Implement `merge_sorted_lists()` in algorithms.py")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"


# ── Standard-path classification ──

class TestStandardPath:
    """Tasks that should classify as 'standard' — moderate complexity."""

    def test_create_rest_api(self):
        state = _make_state("Create a REST API endpoint for user authentication")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "standard"
        assert result["effective_quality_threshold"] == 75

    def test_no_specificity_keywords(self):
        """Generic request without function names or file paths."""
        state = _make_state("Write some code to handle user input")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "standard"

    def test_ambiguous_request(self):
        """Ambiguity markers push out of fast path."""
        state = _make_state("Maybe fix `get_user` — not sure what's wrong")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "standard"

    def test_medium_length(self):
        """Requests between 40-80 words fall to standard."""
        words = " ".join(["word"] * 50)
        state = _make_state(f"Write a function in utils.py that {words}")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "standard"

    def test_non_code_intent(self):
        """Non-code intents cannot be fast."""
        state = _make_state("Add a docstring to `foo` in bar.py", intent="conversational")
        result = classify_complexity(state)
        assert result["complexity_tier"] != "fast"

    def test_planning_intent(self):
        state = _make_state("Write a pytest for `calc`", intent="planning")
        result = classify_complexity(state)
        assert result["complexity_tier"] != "fast"

    def test_multi_intent_conjunction(self):
        """'and also' blocks fast path."""
        state = _make_state("Fix `parse()` in parser.py and also update the tests")
        result = classify_complexity(state)
        assert result["complexity_tier"] != "fast"

    def test_data_pipeline(self):
        state = _make_state("Write a data pipeline to process CSV files and output JSON")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "standard"


# ── Full-path classification ──

class TestFullPath:
    """Tasks that should classify as 'full' — complex, multi-deliverable."""

    def test_production_ready(self):
        state = _make_state("Build a production-ready authentication microservice")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"
        assert result["effective_quality_threshold"] == 85

    def test_comprehensive(self):
        state = _make_state("Write a comprehensive test suite covering all edge cases")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"

    def test_complete_system(self):
        state = _make_state("Design a complete system for real-time notifications")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"

    def test_multi_specialist_pattern(self):
        state = _make_state("Write tests and security audit for the API module")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"

    def test_long_request(self):
        """Requests over 80 words → full."""
        words = " ".join(["word"] * 81)
        state = _make_state(f"Create {words}")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"

    def test_well_tested(self):
        state = _make_state("Build a well-tested data processing pipeline")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"

    def test_robust(self):
        state = _make_state("Create a robust error handling system for the application")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"

    def test_enterprise_keyword(self):
        state = _make_state("Build an enterprise logging framework")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"

    def test_scalable(self):
        state = _make_state("Create a scalable message queue consumer")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "full"


# ── Edge cases ──

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_request(self):
        state = _make_state("")
        result = classify_complexity(state)
        # Empty request has 1 word ('') — not fast (no specificity, no verb)
        assert result["complexity_tier"] in ("standard", "full")

    def test_single_word(self):
        state = _make_state("help")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "standard"

    def test_state_fields_written(self):
        """Verify that both tier and threshold are written to state."""
        state = _make_state("Fix `foo` in bar.py")
        result = classify_complexity(state)
        assert "complexity_tier" in result
        assert "effective_quality_threshold" in result
        assert isinstance(result["effective_quality_threshold"], int)

    def test_preserves_existing_state(self):
        """Triage should not clobber other state fields."""
        state = _make_state("Add a docstring to `foo()` in bar.py")
        state["specialist_output"] = "existing output"
        result = classify_complexity(state)
        assert result["specialist_output"] == "existing output"

    def test_threshold_mapping_fast(self):
        state = _make_state("Fix `get()` in client.py")
        result = classify_complexity(state)
        if result["complexity_tier"] == "fast":
            assert result["effective_quality_threshold"] == 70

    def test_threshold_mapping_standard(self):
        state = _make_state("Create a REST API for user management")
        result = classify_complexity(state)
        if result["complexity_tier"] == "standard":
            assert result["effective_quality_threshold"] == 75

    def test_threshold_mapping_full(self):
        state = _make_state("Build a production-ready microservice")
        result = classify_complexity(state)
        assert result["effective_quality_threshold"] == 85

    def test_backtick_identifier_is_specific(self):
        """Backtick-quoted identifiers should count as specificity."""
        state = _make_state("Fix `calculateTotalRevenue` function")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"

    def test_camelcase_is_specific(self):
        """CamelCase identifiers should count as specificity."""
        state = _make_state("Refactor UserProfileManager to use dependency injection")
        result = classify_complexity(state)
        assert result["complexity_tier"] == "fast"
