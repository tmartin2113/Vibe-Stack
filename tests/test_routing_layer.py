"""
Tests for the routing layer: intent classifier.

Covers:
- IntentClassifier: pattern matching, confidence scoring, state integration
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ["GENESIA_DISABLE_REMOTE_SKILLS"] = "1"

from agents.intent_classifier import IntentClassifier, classify_intent


# ====================================================================
# IntentClassifier tests
# ====================================================================


class TestIntentClassifierPatterns:
    """Test that the right patterns trigger the right intents."""

    @pytest.fixture
    def classifier(self):
        return IntentClassifier()

    # --- Conversational ---

    @pytest.mark.parametrize("user_input", [
        "What is dependency injection?",
        "Explain how async/await works",
        "Compare React vs Vue",
        "What are the differences between REST and GraphQL?",
        "How does garbage collection work in Python?",
        "Describe the observer pattern",
        "Tell me about SOLID principles",
    ])
    def test_conversational_intent(self, classifier, user_input):
        intent, confidence = classifier.classify(user_input)
        assert intent == "conversational", f"Expected conversational for: {user_input}"
        assert confidence > 0.0

    # --- Code generation ---

    @pytest.mark.parametrize("user_input", [
        "Write a function to sort a list",
        "Implement a binary search tree",
        "Create a REST API for user management",
        "Fix the authentication bug in login.py",
        "Refactor the payment module",
        "Add input validation to the signup form",
        "Debug the memory leak in the worker process",
    ])
    def test_code_generation_intent(self, classifier, user_input):
        intent, confidence = classifier.classify(user_input)
        assert intent == "code_generation", f"Expected code_generation for: {user_input}"

    # --- Planning ---

    @pytest.mark.parametrize("user_input", [
        "Design an architecture for a microservices system",
        "Should I use PostgreSQL or MongoDB?",
        "Recommend a technology stack for a real-time chat app",
        "Choose between monolith and microservices",
    ])
    def test_planning_intent(self, classifier, user_input):
        intent, confidence = classifier.classify(user_input)
        assert intent == "planning", f"Expected planning for: {user_input}"

    def test_ambiguous_planning_vs_conversational(self, classifier):
        """'What are the pros and cons of X?' matches both conversational (^what are)
        and planning (pros.*cons). Conversational wins because ^what are has
        higher weight (3.0 vs 2.5). This is expected behavior."""
        intent, _ = classifier.classify("What are the pros and cons of serverless?")
        # Conversational wins the weight tie — this documents the behavior
        assert intent == "conversational"

    # --- Research ---

    @pytest.mark.parametrize("user_input", [
        "Research the official documentation for FastAPI",
        "Find docs for the Stripe API",
        "Look up the latest version of React",
    ])
    def test_research_intent(self, classifier, user_input):
        intent, confidence = classifier.classify(user_input)
        assert intent == "research", f"Expected research for: {user_input}"


class TestIntentClassifierEdgeCases:
    """Test edge cases and fallback behavior."""

    @pytest.fixture
    def classifier(self):
        return IntentClassifier()

    def test_empty_request_defaults_to_code(self, classifier):
        intent, confidence = classifier.classify("")
        assert intent == "code_generation"

    def test_none_request_defaults_to_code(self, classifier):
        intent, confidence = classifier.classify(None)
        assert intent == "code_generation"

    def test_whitespace_only_defaults_to_code(self, classifier):
        intent, confidence = classifier.classify("   ")
        assert intent == "code_generation"

    def test_ambiguous_request_has_confidence(self, classifier):
        """An ambiguous request should still return a valid intent + confidence."""
        intent, confidence = classifier.classify("help me with this thing")
        assert intent in ("conversational", "code_generation", "research", "planning")
        assert 0.0 <= confidence <= 1.0

    def test_case_insensitive(self, classifier):
        """Classification should be case-insensitive."""
        intent1, _ = classifier.classify("WHAT IS dependency injection?")
        intent2, _ = classifier.classify("what is dependency injection?")
        assert intent1 == intent2 == "conversational"


class TestIntentClassifierStateIntegration:
    """Test the execute() method that updates AgentState."""

    def test_execute_sets_state_fields(self):
        classifier = IntentClassifier()
        state = {"user_request": "What is a closure?"}
        result = classifier.execute(state)

        assert result["intent"] == "conversational"
        assert "intent_confidence" in result
        assert result["debug_info"]["intent_classification"]["intent"] == "conversational"

    def test_execute_missing_user_request(self):
        classifier = IntentClassifier()
        state = {}
        result = classifier.execute(state)
        assert result["intent"] == "code_generation"

    def test_classify_intent_convenience_function(self):
        state = {"user_request": "Write a test for the auth module"}
        result = classify_intent(state)
        assert result["intent"] == "code_generation"


class TestIntentBinaryRouting:
    """Test how intents map to the binary code/conversational routing decision."""

    def test_research_routes_to_conversational(self):
        """Research intent maps to conversational (non-code) path."""
        classifier = IntentClassifier()
        intent, _ = classifier.classify("Research the official docs for FastAPI")
        # In should_generate_code, anything != code_generation -> "conversational"
        assert intent != "code_generation"

    def test_planning_routes_to_conversational(self):
        """Planning intent maps to conversational (non-code) path."""
        classifier = IntentClassifier()
        intent, _ = classifier.classify("Design an architecture for microservices")
        assert intent != "code_generation"


# ====================================================================
# Dead code removal verification
# ====================================================================


class TestDeadCodeRemoved:
    """Verify that dead routing infrastructure has been removed."""

    def test_router_analytics_removed(self):
        """router_analytics.py should no longer exist."""
        import importlib
        with pytest.raises(ImportError):
            importlib.import_module("agents.router_analytics")

    def test_semantic_router_removed(self):
        """semantic_router.py should no longer exist."""
        import importlib
        with pytest.raises(ImportError):
            importlib.import_module("agents.semantic_router")

    def test_confidence_assessor_removed(self):
        """confidence_assessor.py should no longer exist (enhancement removed)."""
        import importlib
        with pytest.raises(ImportError):
            importlib.import_module("agents.confidence_assessor")

    def test_state_has_no_routing_decision_obj(self):
        """routing_decision_obj should be removed from state type hints."""
        from agents.state import AgentState
        # AgentState is a TypedDict — check its annotations
        annotations = AgentState.__annotations__
        assert "routing_decision_obj" not in annotations
        assert "routing_method" not in annotations

    def test_state_still_has_routing_confidence(self):
        """routing_confidence should still exist (it's used)."""
        from agents.state import AgentState
        annotations = AgentState.__annotations__
        assert "routing_confidence" in annotations
