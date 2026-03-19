"""
Tests to increase code coverage for daemon, router, sandbox client,
skill_loader, and api_key_manager modules.

Covers:
- PaperclipBridge: dedup, message extraction, response chunking, status
- RouterNode: regex classification, decomposition detection, sub-task generation
- SandboxPoolManager: recycle expired, acquire, run_async
- SkillLoaderNode: _extract_description, extract_metadata, format_skills_for_context
- APIKeyManager: _validate_api_key, get_error_message
"""

import asyncio
import json
import os
import queue
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, AsyncMock, PropertyMock

import pytest

os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"


# ====================================================================
# PaperclipBridge (daemon.py)
# ====================================================================


class TestPaperclipBridgeDedup:
    """Test _mark_message_processed and _is_message_processed dedup logic."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.processed_messages = {}
            b.message_lock = threading.Lock()
            return b

    def test_mark_and_check_processed(self, bridge):
        """A message should be detected as processed after marking."""
        from agents.daemon import PaperclipBridge
        bridge._mark_message_processed("msg-1")
        assert bridge._is_message_processed("msg-1") is True

    def test_unprocessed_message_returns_false(self, bridge):
        """An unseen message should not be detected as processed."""
        assert bridge._is_message_processed("never-seen") is False

    def test_expired_dedup_entry_returns_false(self, bridge):
        """Entries older than DEDUP_TTL_SECONDS should be evicted on check."""
        # Insert an entry with a very old timestamp
        bridge.processed_messages["old-msg"] = time.monotonic() - 7200  # 2 hours ago
        # Default TTL is 3600s (1 hour), so this should be evicted
        assert bridge._is_message_processed("old-msg") is False
        # Entry should be removed
        assert "old-msg" not in bridge.processed_messages

    def test_non_expired_entry_returns_true(self, bridge):
        """An entry within the TTL window should still be detected."""
        bridge.processed_messages["fresh-msg"] = time.monotonic() - 10  # 10 seconds ago
        assert bridge._is_message_processed("fresh-msg") is True

    def test_mark_evicts_expired_when_over_max_size(self, bridge):
        """When cache exceeds DEDUP_MAX_SIZE, expired entries are evicted."""
        from agents import daemon
        original_max = daemon.DEDUP_MAX_SIZE
        try:
            daemon.DEDUP_MAX_SIZE = 5  # Set a small max for testing
            now = time.monotonic()
            # Fill with entries: 3 expired, 2 fresh
            bridge.processed_messages = {
                f"expired-{i}": now - 7200 for i in range(3)
            }
            bridge.processed_messages.update({
                f"fresh-{i}": now - 10 for i in range(2)
            })
            # Adding one more should trigger eviction of expired entries
            bridge._mark_message_processed("new-msg")
            # All expired should be gone, fresh + new should remain
            assert "new-msg" in bridge.processed_messages
            for i in range(3):
                assert f"expired-{i}" not in bridge.processed_messages
        finally:
            daemon.DEDUP_MAX_SIZE = original_max

    def test_mark_evicts_oldest_when_still_over_max(self, bridge):
        """When still over DEDUP_MAX_SIZE after TTL eviction, oldest entries popped."""
        from agents import daemon
        original_max = daemon.DEDUP_MAX_SIZE
        try:
            daemon.DEDUP_MAX_SIZE = 3
            now = time.monotonic()
            # All entries are fresh (not expired), but we have 4 which > max
            bridge.processed_messages = {
                f"msg-{i}": now - (100 - i) for i in range(4)
            }
            bridge._mark_message_processed("msg-new")
            # Should be at most DEDUP_MAX_SIZE entries
            assert len(bridge.processed_messages) <= daemon.DEDUP_MAX_SIZE
            # The newest entry should always survive
            assert "msg-new" in bridge.processed_messages
        finally:
            daemon.DEDUP_MAX_SIZE = original_max


class TestExtractRequestFromMessage:
    """Test _extract_request_from_message which strips @mention prefixes."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            return b

    def test_empty_text_returns_none(self, bridge):
        assert bridge._extract_request_from_message({"text": ""}) is None

    def test_missing_text_returns_none(self, bridge):
        assert bridge._extract_request_from_message({}) is None

    def test_whitespace_only_returns_none(self, bridge):
        assert bridge._extract_request_from_message({"text": "   "}) is None

    def test_removes_slack_mention(self, bridge):
        result = bridge._extract_request_from_message(
            {"text": "<@U1234ABCDE> please build a REST API for users"}
        )
        assert result == "please build a REST API for users"

    def test_removes_mattermost_mention(self, bridge):
        result = bridge._extract_request_from_message(
            {"text": "@vibe-bot please build a REST API for users"}
        )
        assert result == "please build a REST API for users"

    def test_removes_multiple_mentions(self, bridge):
        result = bridge._extract_request_from_message(
            {"text": "<@U123> @someone write tests for auth module"}
        )
        assert "write tests for auth module" in result

    def test_too_short_after_stripping_returns_none(self, bridge):
        """Text shorter than 5 chars after stripping mentions should return None."""
        result = bridge._extract_request_from_message(
            {"text": "<@U123> hi"}
        )
        assert result is None

    def test_exactly_5_chars_is_accepted(self, bridge):
        result = bridge._extract_request_from_message(
            {"text": "<@U123> hello"}
        )
        assert result == "hello"

    def test_preserves_content_after_mention(self, bridge):
        result = bridge._extract_request_from_message(
            {"text": "<@U999ZZZ> Deploy the staging environment and run tests"}
        )
        assert "Deploy the staging environment" in result


class TestFormatResponseForChat:
    """Test _format_response_for_chat which splits long responses."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            return b

    def test_short_response_single_chunk(self, bridge):
        result = bridge._format_response_for_chat("Short message", max_length=100)
        assert len(result) == 1
        assert result[0] == "Short message"

    def test_long_response_split_into_chunks(self, bridge):
        # Create a response that's longer than max_length
        long_text = "\n".join([f"Line {i}: " + "x" * 50 for i in range(20)])
        result = bridge._format_response_for_chat(long_text, max_length=200)
        assert len(result) > 1

    def test_first_chunk_has_continuation_marker(self, bridge):
        long_text = "\n".join([f"Line {i}: " + "x" * 50 for i in range(20)])
        result = bridge._format_response_for_chat(long_text, max_length=200)
        assert "[Continued in next message...]" in result[0]

    def test_last_chunk_has_continued_from_marker(self, bridge):
        long_text = "\n".join([f"Line {i}: " + "x" * 50 for i in range(20)])
        result = bridge._format_response_for_chat(long_text, max_length=200)
        assert "[Continued from previous message]" in result[-1]

    def test_middle_chunks_have_part_markers(self, bridge):
        """Middle chunks should have 'Part X/Y' markers."""
        # Need enough text to create 3+ chunks
        long_text = "\n".join([f"Line {i}: " + "x" * 80 for i in range(50)])
        result = bridge._format_response_for_chat(long_text, max_length=200)
        if len(result) >= 3:
            # Middle chunks (not first, not last)
            for chunk in result[1:-1]:
                assert "Continued - Part" in chunk

    def test_exact_max_length_single_chunk(self, bridge):
        text = "a" * 100
        result = bridge._format_response_for_chat(text, max_length=100)
        assert len(result) == 1

    def test_default_max_length(self, bridge):
        """Default max_length is 4000."""
        text = "a" * 3999
        result = bridge._format_response_for_chat(text)
        assert len(result) == 1


class TestBridgeStatus:
    """Test the status() method that returns daemon metrics."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.running = False
            b.mattermost_client = None
            b.slack_client = None
            b.mattermost_bot_username = None
            b.slack_bot_user_id = None
            b.inflight = {}
            b.inflight_lock = threading.Lock()
            b.request_queue = __import__("queue").Queue()
            b.metrics = {
                "requests_created": 0,
                "requests_completed": 0,
                "requests_failed": 0,
                "start_time": None,
            }
            return b

    def test_status_not_running(self, bridge):
        status = bridge.status()
        assert status["running"] is False
        assert status["uptime"] is None
        assert status["inflight_issues"] == 0
        assert status["queue_size"] == 0

    def test_status_with_uptime(self, bridge):
        bridge.running = True
        bridge.metrics["start_time"] = datetime.now() - timedelta(hours=1)
        status = bridge.status()
        assert status["running"] is True
        assert status["uptime"] is not None

    def test_status_messenger_info(self, bridge):
        status = bridge.status()
        assert status["messengers"]["mattermost"]["enabled"] is False
        assert status["messengers"]["slack"]["enabled"] is False

    def test_status_with_mattermost_enabled(self, bridge):
        bridge.mattermost_client = MagicMock()
        bridge.mattermost_bot_username = "vibe-bot"
        status = bridge.status()
        assert status["messengers"]["mattermost"]["enabled"] is True
        assert status["messengers"]["mattermost"]["bot_username"] == "vibe-bot"

    def test_status_with_inflight(self, bridge):
        bridge.inflight = {"issue-1": {}, "issue-2": {}}
        status = bridge.status()
        assert status["inflight_issues"] == 2

    def test_status_with_queued_items(self, bridge):
        bridge.request_queue.put({"id": "test"})
        status = bridge.status()
        assert status["queue_size"] == 1

    def test_status_metrics_dict(self, bridge):
        bridge.metrics["requests_created"] = 5
        bridge.metrics["requests_completed"] = 3
        bridge.metrics["requests_failed"] = 1
        status = bridge.status()
        assert status["metrics"]["requests_created"] == 5
        assert status["metrics"]["requests_completed"] == 3
        assert status["metrics"]["requests_failed"] == 1


class TestBridgeIsReady:
    """Test _is_ready() readiness check."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.running = False
            b.paperclip_client = None
            b.mattermost_client = None
            b.slack_client = None
            return b

    def test_not_ready_when_not_running(self, bridge):
        assert bridge._is_ready() is False

    def test_not_ready_without_paperclip(self, bridge):
        bridge.running = True
        bridge.mattermost_client = MagicMock()
        assert bridge._is_ready() is False

    def test_not_ready_without_messenger(self, bridge):
        bridge.running = True
        bridge.paperclip_client = MagicMock()
        assert bridge._is_ready() is False

    def test_ready_with_mattermost(self, bridge):
        bridge.running = True
        bridge.paperclip_client = MagicMock()
        bridge.mattermost_client = MagicMock()
        assert bridge._is_ready() is True

    def test_ready_with_slack(self, bridge):
        bridge.running = True
        bridge.paperclip_client = MagicMock()
        bridge.slack_client = MagicMock()
        assert bridge._is_ready() is True


# ====================================================================
# RouterNode (router.py)
# ====================================================================


class TestRouterClassifyTaskRegex:
    """Test _classify_task_regex weighted pattern matching."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        from agents.skill_registry import SkillRegistry
        import tempfile
        tmpdir = tempfile.mkdtemp()
        registry = SkillRegistry(tmpdir)
        registry._enable_remote = False
        return RouterNode(
            skill_registry=registry,
            classification_mode="regex",
        )

    def test_empty_spec_returns_general(self, router):
        task_type, confidence = router._classify_task_regex("")
        assert task_type == "general"

    def test_none_spec_returns_general(self, router):
        task_type, confidence = router._classify_task_regex(None)
        assert task_type == "general"

    def test_test_generation_keywords(self, router):
        # Use many strong keywords to exceed the 0.15 threshold across 19 weighted patterns
        task_type, confidence = router._classify_task_regex(
            "Write unit test cases with pytest, generate test suites and test fixtures, "
            "test coverage for edge cases, add test assertions"
        )
        assert task_type == "test_generation"
        assert confidence > 0.0

    def test_security_audit_keywords(self, router):
        # Heavy security keywords (XSS=3.0, OWASP=3.0, CSRF=3.0, vulnerability=2.0)
        task_type, confidence = router._classify_task_regex(
            "Run a security audit for XSS and CSRF vulnerability, check OWASP compliance, "
            "encrypt secrets and validate input sanitization"
        )
        assert task_type == "security_audit"
        assert confidence > 0.0

    def test_documentation_keywords(self, router):
        task_type, confidence = router._classify_task_regex(
            "Write API documentation and docstrings for the payment module"
        )
        assert task_type == "documentation"
        assert confidence > 0.0

    def test_debugging_keywords(self, router):
        task_type, confidence = router._classify_task_regex(
            "Debug the traceback error and fix the bug, investigate the stack trace"
        )
        assert task_type == "debugging"
        assert confidence > 0.0

    def test_api_development_keywords(self, router):
        # Heavy API keywords (REST=3.0, FastAPI=3.0, endpoint=2.0, CORS=2.5, OpenAPI=2.5)
        task_type, confidence = router._classify_task_regex(
            "Build a REST API with FastAPI endpoints, add CORS middleware, "
            "OpenAPI Swagger docs, webhook route, and rate limit"
        )
        assert task_type == "api_development"
        assert confidence > 0.0

    def test_database_operations_keywords(self, router):
        task_type, confidence = router._classify_task_regex(
            "Write SQL queries for database migration and schema design, "
            "optimize the query, create indexes, manage data model"
        )
        assert task_type == "database_operations"
        assert confidence > 0.0

    def test_low_confidence_falls_to_general(self, router):
        """Ambiguous text with no strong keyword matches should default to general."""
        task_type, confidence = router._classify_task_regex(
            "make it work better somehow"
        )
        # Should either be general or have low confidence
        assert confidence < 0.5 or task_type == "general"

    def test_code_generation_keywords(self, router):
        task_type, confidence = router._classify_task_regex(
            "Create a function to implement a sorting algorithm, write code to build a utility class"
        )
        assert task_type == "code_generation"
        assert confidence > 0.0


class TestRouterRequiresDecomposition:
    """Test _requires_decomposition which checks task complexity."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        from agents.skill_registry import SkillRegistry
        import tempfile
        tmpdir = tempfile.mkdtemp()
        registry = SkillRegistry(tmpdir)
        registry._enable_remote = False
        r = RouterNode(
            skill_registry=registry,
            classification_mode="regex",
        )
        # Ensure clean state
        r._last_secondary_categories = []
        return r

    def test_empty_spec_no_decomposition(self, router):
        assert router._requires_decomposition("") is False
        assert router._requires_decomposition(None) is False

    def test_single_concern_no_decomposition(self, router):
        """A single-concern spec should not require decomposition."""
        assert router._requires_decomposition(
            "Write a function to sort a list"
        ) is False

    def test_explicit_multi_specialist_indicator(self, router):
        """Explicit multi-concern phrasing should trigger decomposition."""
        result = router._requires_decomposition(
            "Write tests and also document the API endpoints"
        )
        assert result is True

    def test_multi_specialist_security_and_tests(self, router):
        result = router._requires_decomposition(
            "security audit and testing for the payment module"
        )
        assert result is True

    def test_llm_secondary_categories_trigger(self, router):
        """When LLM mode has secondary categories, decomposition is enabled."""
        router.classification_mode = "hybrid"
        router._last_secondary_categories = ["test_generation", "documentation"]
        result = router._requires_decomposition("any spec")
        assert result is True

    def test_no_secondary_categories_no_trigger(self, router):
        """When LLM mode has empty secondary categories, regex fallback is used."""
        router.classification_mode = "hybrid"
        router._last_secondary_categories = []
        result = router._requires_decomposition("Write a function to sort a list")
        assert result is False


class TestRouterDecomposeIntoSubtasks:
    """Test _decompose_into_subtasks with mock LLM response."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        from agents.skill_registry import SkillRegistry
        import tempfile
        tmpdir = tempfile.mkdtemp()
        registry = SkillRegistry(tmpdir)
        registry._enable_remote = False
        return RouterNode(
            skill_registry=registry,
            classification_mode="regex",
        )

    def test_regex_decomposition_creates_subtasks(self, router):
        """Regex decomposition should create sub-tasks for matching types."""
        state = {
            "specification": "Write pytest tests for the REST API and generate documentation",
            "routed_task_type": "test_generation",
            "debug_info": {},
        }
        router._last_secondary_categories = []
        result = router._decompose_into_subtasks(state)

        assert "sub_tasks" in result
        assert len(result["sub_tasks"]) >= 1
        assert result["current_sub_task_index"] == 0
        assert "aggregation_strategy" in result

    def test_llm_decomposition_uses_secondary_categories(self, router):
        """LLM decomposition should use primary + secondary categories."""
        router.classification_mode = "hybrid"
        router._last_secondary_categories = ["documentation", "security_audit"]

        state = {
            "specification": "Build an API with tests and docs",
            "routed_task_type": "api_development",
            "debug_info": {},
        }
        result = router._decompose_into_subtasks(state)

        task_types = [t["task_type"] for t in result["sub_tasks"]]
        assert "api_development" in task_types
        assert "documentation" in task_types

    def test_subtasks_limited_to_five(self, router):
        """Even with many matching types, subtasks should be capped at 5."""
        router.classification_mode = "hybrid"
        router._last_secondary_categories = [
            "test_generation", "documentation", "security_audit",
            "performance_optimization", "debugging", "refactoring",
        ]
        state = {
            "specification": "do everything",
            "routed_task_type": "code_generation",
            "debug_info": {},
        }
        result = router._decompose_into_subtasks(state)
        assert len(result["sub_tasks"]) <= 5

    def test_debug_info_records_decomposition(self, router):
        """Debug info should record decomposition details."""
        state = {
            "specification": "Write pytest tests for the REST API and generate documentation",
            "routed_task_type": "test_generation",
            "debug_info": {},
        }
        router._last_secondary_categories = []
        result = router._decompose_into_subtasks(state)

        debug = result["debug_info"]["router_decision"]
        assert debug["decomposed"] is True
        assert "num_subtasks" in debug
        assert "task_types" in debug


class TestRouterDecompositionRules:
    """Test decomposition rule management."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        from agents.skill_registry import SkillRegistry
        import tempfile
        tmpdir = tempfile.mkdtemp()
        registry = SkillRegistry(tmpdir)
        registry._enable_remote = False
        return RouterNode(
            skill_registry=registry,
            classification_mode="regex",
        )

    def test_add_decomposition_rule(self, router):
        initial_count = len(router.decomposition_rules)
        router.add_decomposition_rule({
            "name": "ml_workflow",
            "condition": lambda types: "machine_learning" in types,
            "execution": "sequential",
            "aggregation": "report",
            "order": lambda types: types,
            "priority": 3,
        })
        assert len(router.decomposition_rules) == initial_count + 1

    def test_add_rule_missing_fields_raises(self, router):
        with pytest.raises(ValueError, match="must contain fields"):
            router.add_decomposition_rule({"name": "incomplete"})

    def test_add_rule_invalid_execution_raises(self, router):
        with pytest.raises(ValueError, match="execution"):
            router.add_decomposition_rule({
                "name": "bad_exec",
                "condition": lambda t: True,
                "execution": "invalid",
                "aggregation": "merge",
                "order": lambda t: t,
                "priority": 1,
            })

    def test_add_rule_invalid_aggregation_raises(self, router):
        with pytest.raises(ValueError, match="aggregation"):
            router.add_decomposition_rule({
                "name": "bad_agg",
                "condition": lambda t: True,
                "execution": "sequential",
                "aggregation": "invalid",
                "order": lambda t: t,
                "priority": 1,
            })

    def test_add_rule_replaces_existing_name(self, router):
        rule = {
            "name": "code_with_analysis",  # Existing rule name
            "condition": lambda t: True,
            "execution": "parallel",
            "aggregation": "report",
            "order": lambda t: t,
            "priority": 99,
        }
        router.add_decomposition_rule(rule)
        names = [r["name"] for r in router.decomposition_rules]
        assert names.count("code_with_analysis") == 1

    def test_remove_decomposition_rule(self, router):
        initial_count = len(router.decomposition_rules)
        router.remove_decomposition_rule("code_with_analysis")
        assert len(router.decomposition_rules) == initial_count - 1

    def test_remove_nonexistent_rule(self, router):
        initial_count = len(router.decomposition_rules)
        router.remove_decomposition_rule("nonexistent_rule")
        assert len(router.decomposition_rules) == initial_count

    def test_get_decomposition_rules_sorted(self, router):
        rules = router.get_decomposition_rules()
        priorities = [r["priority"] for r in rules]
        assert priorities == sorted(priorities)

    def test_get_available_specialists(self, router):
        specialists = router.get_available_specialists()
        assert isinstance(specialists, list)
        assert len(specialists) > 0


class TestRouterMultiSpecialistIndicators:
    """Test multi-specialist indicator detection patterns."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        from agents.skill_registry import SkillRegistry
        import tempfile
        tmpdir = tempfile.mkdtemp()
        registry = SkillRegistry(tmpdir)
        registry._enable_remote = False
        r = RouterNode(skill_registry=registry, classification_mode="regex")
        r._last_secondary_categories = []
        return r

    def test_tests_and_security(self, router):
        assert router._requires_decomposition(
            "testing and security audit for the payment module"
        ) is True

    def test_documentation_and_testing(self, router):
        assert router._requires_decomposition(
            "documentation and testing for the API"
        ) is True

    def test_write_tests_and_document(self, router):
        assert router._requires_decomposition(
            "write tests and document the user service"
        ) is True

    def test_generate_tests_and_docs(self, router):
        assert router._requires_decomposition(
            "generate tests and documentation for the auth module"
        ) is True

    def test_audit_and_fix(self, router):
        assert router._requires_decomposition(
            "audit the codebase and fix the security vulnerabilities"
        ) is True


class TestRouterGenerateSubTaskSpec:
    """Test _generate_sub_task_spec seed specification generation."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        from agents.skill_registry import SkillRegistry
        import tempfile
        tmpdir = tempfile.mkdtemp()
        registry = SkillRegistry(tmpdir)
        registry._enable_remote = False
        return RouterNode(skill_registry=registry, classification_mode="regex")

    def test_generates_scoped_spec(self, router):
        result = router._generate_sub_task_spec(
            main_spec="Build a REST API with tests",
            task_type="test_generation",
            sibling_types=["api_development"],
            index=0,
            is_sequential=False,
        )
        assert "TEST GENERATION" in result or "test generation" in result.lower()
        assert "Build a REST API with tests" in result

    def test_includes_sibling_awareness(self, router):
        result = router._generate_sub_task_spec(
            main_spec="Build it",
            task_type="test_generation",
            sibling_types=["api_development", "documentation"],
            index=0,
            is_sequential=False,
        )
        assert "Other specialists" in result
        assert "do not duplicate" in result.lower()

    def test_no_sibling_note_when_empty(self, router):
        result = router._generate_sub_task_spec(
            main_spec="Build it",
            task_type="code_generation",
            sibling_types=[],
            index=0,
            is_sequential=False,
        )
        assert "Other specialists" not in result

    def test_dependency_context_sequential(self, router):
        result = router._generate_sub_task_spec(
            main_spec="Build it",
            task_type="test_generation",
            sibling_types=["code_generation", "documentation"],
            index=1,
            is_sequential=True,
        )
        assert "runs after" in result.lower()
        assert "available" in result.lower()

    def test_no_dependency_context_for_first_task(self, router):
        result = router._generate_sub_task_spec(
            main_spec="Build it",
            task_type="code_generation",
            sibling_types=["test_generation"],
            index=0,
            is_sequential=True,
        )
        assert "runs after" not in result.lower()


class TestRouterClassifyTask:
    """Test _classify_task routing through modes."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        from agents.skill_registry import SkillRegistry
        import tempfile
        tmpdir = tempfile.mkdtemp()
        registry = SkillRegistry(tmpdir)
        registry._enable_remote = False
        return RouterNode(skill_registry=registry, classification_mode="regex")

    def test_force_regex_ignores_mode(self, router):
        """force_regex should use regex even if mode is hybrid."""
        router.classification_mode = "hybrid"
        router.llm_classifier = MagicMock()
        spec = "Write unit test cases with pytest, generate test suites and test fixtures"
        task_type, conf = router._classify_task(spec, force_regex=True)
        router.llm_classifier.classify_task.assert_not_called()

    def test_regex_mode(self, router):
        spec = "Write unit test cases with pytest, generate test suites and test fixtures"
        task_type, conf = router._classify_task(spec)
        assert task_type == "test_generation"

    def test_llm_mode_without_classifier_falls_back(self, router):
        """LLM mode without classifier should fallback to regex."""
        router.classification_mode = "llm"
        router.llm_classifier = None
        spec = "Write unit test cases with pytest, generate test suites and test fixtures"
        task_type, conf = router._classify_task(spec)
        # Falls back to regex
        assert task_type == "test_generation"

    def test_unknown_mode_falls_back_to_regex(self, router):
        router.classification_mode = "unknown_mode"
        spec = "Write unit test cases with pytest, generate test suites and test fixtures"
        task_type, conf = router._classify_task(spec)
        assert task_type == "test_generation"

    def test_clears_secondary_categories(self, router):
        """Each classify call should clear stale secondary categories."""
        router._last_secondary_categories = ["old_category"]
        router._classify_task("Write a function")
        assert router._last_secondary_categories == []


class TestRouterExecute:
    """Test router execute() with pre-set task type."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        from agents.skill_registry import SkillRegistry
        import tempfile
        tmpdir = tempfile.mkdtemp()
        registry = SkillRegistry(tmpdir)
        registry._enable_remote = False
        return RouterNode(skill_registry=registry, classification_mode="regex")

    def test_preset_task_type_skips_classification(self, router):
        state = {
            "specification": "anything",
            "user_request": "",
            "routed_task_type": "custom_type",
            "debug_info": {},
            "discovered_skills": [],
            "skills_in_use": [],
            "skill_quality_scores": {},
        }
        result = router.execute(state)
        assert result["routed_task_type"] == "custom_type"
        assert result["routing_confidence"] == 1.0
        assert result["requires_decomposition"] is False
        assert result["debug_info"]["router_decision"]["classification_mode"] == "pre_set"

    def test_empty_spec_uses_user_request(self, router):
        state = {
            "specification": "",
            "user_request": "Write unit test cases with pytest, generate test suites and test fixtures",
            "routed_task_type": "",
            "debug_info": {},
            "discovered_skills": [],
            "skills_in_use": [],
            "skill_quality_scores": {},
        }
        result = router.execute(state)
        assert result["routed_task_type"] == "test_generation"


# ====================================================================
# LLMClassifier (router.py)
# ====================================================================


class TestLLMClassifier:
    """Test LLMClassifier's classification and parsing logic."""

    @pytest.fixture
    def classifier(self):
        from agents.router import LLMClassifier
        mock_model = MagicMock()
        return LLMClassifier(mock_model)

    def test_empty_spec_returns_general(self, classifier):
        task, conf, secondary = classifier.classify_task("")
        assert task == "general"
        assert conf == 0.3

    def test_none_spec_returns_general(self, classifier):
        task, conf, secondary = classifier.classify_task(None)
        assert task == "general"

    def test_valid_json_response_parsed(self, classifier):
        classifier.model.generate.return_value = json.dumps({
            "primary_category": "test_generation",
            "confidence": 0.95,
            "reasoning": "The task asks to write tests",
            "secondary_categories": ["documentation"],
        })
        task, conf, secondary = classifier.classify_task("Write tests for auth")
        assert task == "test_generation"
        assert conf == 0.95
        assert secondary == ["documentation"]

    def test_json_response_with_extra_text(self, classifier):
        classifier.model.generate.return_value = (
            'Here is my classification:\n'
            '{"primary_category": "debugging", "confidence": 0.8, "reasoning": "bug fix"}'
        )
        task, conf, secondary = classifier.classify_task("Fix the login bug")
        assert task == "debugging"
        assert conf == 0.8

    def test_cache_hit(self, classifier):
        classifier.model.generate.return_value = json.dumps({
            "primary_category": "code_generation",
            "confidence": 0.9,
            "reasoning": "code task",
        })
        # First call
        classifier.classify_task("Write a sort function")
        # Second call should use cache
        classifier.classify_task("Write a sort function")
        # Model should only be called once
        assert classifier.model.generate.call_count == 1

    def test_cache_eviction_at_max_size(self, classifier):
        classifier.cache_size = 2
        classifier.model.generate.return_value = json.dumps({
            "primary_category": "general",
            "confidence": 0.5,
            "reasoning": "generic",
        })
        classifier.classify_task("task 1")
        classifier.classify_task("task 2")
        classifier.classify_task("task 3")  # Should evict oldest
        assert len(classifier.classification_cache) <= 2

    def test_exception_returns_general(self, classifier):
        classifier.model.generate.side_effect = RuntimeError("model error")
        task, conf, secondary = classifier.classify_task("anything")
        assert task == "general"
        assert conf == 0.3

    def test_custom_task_descriptions(self):
        from agents.router import LLMClassifier
        mock_model = MagicMock()
        custom = {"ml_training": "Machine learning model training"}
        clf = LLMClassifier(mock_model, task_descriptions=custom)
        assert "ml_training" in clf.task_descriptions

    def test_fallback_extraction_no_match(self, classifier):
        result = classifier._fallback_extraction("completely unrelated text")
        assert result["primary_category"] == "general"
        assert result["confidence"] == 0.3

    def test_fallback_extraction_finds_task_type(self, classifier):
        result = classifier._fallback_extraction("this is about debugging issues")
        assert result["primary_category"] == "debugging"
        assert result["confidence"] == 0.5

    def test_parse_response_missing_secondary_categories(self, classifier):
        result = classifier._parse_classification_response(
            '{"primary_category": "code_generation", "confidence": 0.8}'
        )
        assert result["secondary_categories"] == []

    def test_parse_response_invalid_secondary_categories_type(self, classifier):
        result = classifier._parse_classification_response(
            '{"primary_category": "code_generation", "confidence": 0.8, '
            '"secondary_categories": "not a list"}'
        )
        assert result["secondary_categories"] == []


# ====================================================================
# SandboxPoolManager (sandbox/client.py)
# ====================================================================


class TestSandboxHandle:
    """Test SandboxHandle dataclass."""

    def test_age_seconds(self):
        from agents.sandbox.client import SandboxHandle
        handle = SandboxHandle(
            sandbox_id="test-1",
            sandbox=MagicMock(),
            created_at=time.monotonic() - 60,
        )
        assert handle.age_seconds >= 59  # At least 59 seconds old

    def test_touch_updates_last_used(self):
        from agents.sandbox.client import SandboxHandle
        handle = SandboxHandle(
            sandbox_id="test-1",
            sandbox=MagicMock(),
        )
        old_last_used = handle.last_used
        time.sleep(0.01)
        handle.touch()
        assert handle.last_used > old_last_used


class TestSandboxPoolRecycleExpired:
    """Test _recycle_expired TTL-based sandbox cleanup."""

    @pytest.fixture
    def pool(self):
        from agents.sandbox.client import SandboxPoolManager, SandboxHandle
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig(sandbox_timeout=60, pool_size=3)
        mgr = SandboxPoolManager.__new__(SandboxPoolManager)
        mgr.config = config
        mgr._pool = queue.Queue()
        mgr._all_handles = []
        mgr._lock = threading.Lock()
        mgr._stop_event = threading.Event()
        mgr._event_loop = None
        mgr._loop_thread = None
        mgr._started = False
        mgr._warmed = False
        return mgr

    def test_recycle_removes_expired(self, pool):
        from agents.sandbox.client import SandboxHandle

        mock_sandbox = MagicMock()
        mock_sandbox.kill = AsyncMock()

        # Create expired handle (older than timeout)
        expired_handle = SandboxHandle(
            sandbox_id="expired-1",
            sandbox=mock_sandbox,
            created_at=time.monotonic() - 120,  # 120s > 60s timeout
        )
        # Create fresh handle
        fresh_handle = SandboxHandle(
            sandbox_id="fresh-1",
            sandbox=MagicMock(),
            created_at=time.monotonic() - 10,
        )

        pool._pool.put(expired_handle)
        pool._pool.put(fresh_handle)
        pool._all_handles = [expired_handle, fresh_handle]

        # Mock _kill_sandbox to avoid async calls
        pool._kill_sandbox = MagicMock()

        pool._recycle_expired()

        # Fresh handle should be back in the pool
        assert pool._pool.qsize() == 1
        remaining = pool._pool.get()
        assert remaining.sandbox_id == "fresh-1"

        # Expired handle should have been killed
        pool._kill_sandbox.assert_called_once_with(expired_handle)
        assert expired_handle not in pool._all_handles

    def test_recycle_no_expired(self, pool):
        from agents.sandbox.client import SandboxHandle

        fresh = SandboxHandle(
            sandbox_id="fresh",
            sandbox=MagicMock(),
            created_at=time.monotonic() - 5,
        )
        pool._pool.put(fresh)
        pool._all_handles = [fresh]
        pool._kill_sandbox = MagicMock()

        pool._recycle_expired()

        assert pool._pool.qsize() == 1
        pool._kill_sandbox.assert_not_called()

    def test_recycle_empty_pool(self, pool):
        pool._kill_sandbox = MagicMock()
        pool._recycle_expired()
        assert pool._pool.qsize() == 0
        pool._kill_sandbox.assert_not_called()


class TestSandboxPoolAcquire:
    """Test _acquire pool acquisition logic."""

    @pytest.fixture
    def pool(self):
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig(sandbox_timeout=60, pool_size=2)
        mgr = SandboxPoolManager.__new__(SandboxPoolManager)
        mgr.config = config
        mgr._pool = queue.Queue()
        mgr._all_handles = []
        mgr._lock = threading.Lock()
        mgr._stop_event = threading.Event()
        mgr._warmed = True  # Skip warm-up
        mgr._event_loop = None
        mgr._loop_thread = None
        return mgr

    def test_acquire_returns_fresh_handle(self, pool):
        from agents.sandbox.client import SandboxHandle
        handle = SandboxHandle(
            sandbox_id="fresh",
            sandbox=MagicMock(),
            created_at=time.monotonic() - 10,
        )
        pool._pool.put(handle)
        result = pool._acquire()
        assert result.sandbox_id == "fresh"

    def test_acquire_replaces_expired_handle(self, pool):
        from agents.sandbox.client import SandboxHandle

        expired = SandboxHandle(
            sandbox_id="expired",
            sandbox=MagicMock(),
            created_at=time.monotonic() - 120,  # Older than timeout
        )
        pool._pool.put(expired)
        pool._all_handles = [expired]

        new_handle = SandboxHandle(
            sandbox_id="new",
            sandbox=MagicMock(),
        )
        pool._kill_sandbox = MagicMock()
        pool._create_sandbox = MagicMock(return_value=new_handle)

        result = pool._acquire()
        assert result.sandbox_id == "new"
        pool._kill_sandbox.assert_called_once_with(expired)

    def test_acquire_on_empty_pool_creates_new(self, pool):
        from agents.sandbox.client import SandboxHandle

        new_handle = SandboxHandle(
            sandbox_id="on-demand",
            sandbox=MagicMock(),
        )
        pool._create_sandbox = MagicMock(return_value=new_handle)

        result = pool._acquire()
        assert result.sandbox_id == "on-demand"
        pool._create_sandbox.assert_called_once()

    def test_acquire_triggers_lazy_warm(self):
        from agents.sandbox.client import SandboxPoolManager, SandboxHandle
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig(sandbox_timeout=60, pool_size=1)
        mgr = SandboxPoolManager.__new__(SandboxPoolManager)
        mgr.config = config
        mgr._pool = queue.Queue()
        mgr._all_handles = []
        mgr._lock = threading.Lock()
        mgr._stop_event = threading.Event()
        mgr._warmed = False  # Not warmed yet
        mgr._event_loop = None
        mgr._loop_thread = None

        new_handle = SandboxHandle(
            sandbox_id="warmed",
            sandbox=MagicMock(),
        )
        mgr._warm_pool = MagicMock()
        mgr._create_sandbox = MagicMock(return_value=new_handle)

        result = mgr._acquire()
        mgr._warm_pool.assert_called_once()


class TestSandboxPoolRelease:
    """Test _release method."""

    @pytest.fixture
    def pool(self):
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig()
        mgr = SandboxPoolManager.__new__(SandboxPoolManager)
        mgr.config = config
        mgr._pool = queue.Queue()
        mgr._stop_event = threading.Event()
        return mgr

    def test_release_returns_to_pool(self, pool):
        from agents.sandbox.client import SandboxHandle
        handle = SandboxHandle(sandbox_id="test", sandbox=MagicMock())
        pool._release(handle)
        assert pool._pool.qsize() == 1

    def test_release_kills_during_shutdown(self, pool):
        from agents.sandbox.client import SandboxHandle
        handle = SandboxHandle(sandbox_id="test", sandbox=MagicMock())
        pool._stop_event.set()
        pool._kill_sandbox = MagicMock()
        pool._release(handle)
        pool._kill_sandbox.assert_called_once_with(handle)
        assert pool._pool.qsize() == 0


class TestSandboxRunAsync:
    """Test _run_async event loop threading."""

    def test_run_async_without_event_loop(self):
        """When no event loop is set, should use asyncio.run()."""
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        mgr = SandboxPoolManager.__new__(SandboxPoolManager)
        mgr._event_loop = None

        async def sample_coro():
            return 42

        result = mgr._run_async(sample_coro())
        assert result == 42

    def test_run_async_with_stopped_event_loop(self):
        """When event loop exists but is not running, should use asyncio.run()."""
        from agents.sandbox.client import SandboxPoolManager

        mgr = SandboxPoolManager.__new__(SandboxPoolManager)
        loop = asyncio.new_event_loop()
        # Loop is not running
        mgr._event_loop = loop

        async def sample_coro():
            return 99

        result = mgr._run_async(sample_coro())
        assert result == 99
        loop.close()

    def test_run_async_with_running_event_loop(self):
        """When event loop is running, should use run_coroutine_threadsafe."""
        from agents.sandbox.client import SandboxPoolManager

        mgr = SandboxPoolManager.__new__(SandboxPoolManager)
        loop = asyncio.new_event_loop()

        # Start the loop in a background thread
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()

        try:
            mgr._event_loop = loop

            async def sample_coro():
                return 77

            result = mgr._run_async(sample_coro())
            assert result == 77
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=5)
            loop.close()


class TestSandboxReplenishPool:
    """Test _replenish_pool creates new sandboxes when below target."""

    @pytest.fixture
    def pool(self):
        from agents.sandbox.client import SandboxPoolManager
        from agents.sandbox.config import SandboxConfig

        config = SandboxConfig(pool_size=3)
        mgr = SandboxPoolManager.__new__(SandboxPoolManager)
        mgr.config = config
        mgr._pool = queue.Queue()
        mgr._all_handles = []
        mgr._lock = threading.Lock()
        return mgr

    def test_replenish_creates_needed_sandboxes(self, pool):
        from agents.sandbox.client import SandboxHandle
        new_handle = SandboxHandle(sandbox_id="new", sandbox=MagicMock())
        pool._create_sandbox = MagicMock(return_value=new_handle)

        pool._replenish_pool()

        assert pool._create_sandbox.call_count == 3
        assert pool._pool.qsize() == 3

    def test_replenish_skips_when_at_target(self, pool):
        from agents.sandbox.client import SandboxHandle
        for i in range(3):
            pool._pool.put(SandboxHandle(sandbox_id=f"h-{i}", sandbox=MagicMock()))

        pool._create_sandbox = MagicMock()
        pool._replenish_pool()
        pool._create_sandbox.assert_not_called()

    def test_replenish_handles_creation_failure(self, pool):
        pool._create_sandbox = MagicMock(side_effect=RuntimeError("creation failed"))
        # Should not raise
        pool._replenish_pool()
        assert pool._pool.qsize() == 0


# ====================================================================
# SkillLoaderNode (skill_loader.py)
# ====================================================================


class TestSkillLoaderExtractDescription:
    """Test _extract_description which parses SKILL.md frontmatter."""

    def test_no_frontmatter_returns_first_line(self):
        from agents.skill_loader import SkillLoaderNode
        result = SkillLoaderNode._extract_description("# My Skill\n\nDoes stuff.")
        assert result == "# My Skill"

    def test_frontmatter_with_description(self):
        from agents.skill_loader import SkillLoaderNode
        content = """---
name: my-skill
description: A fantastic skill for testing
---

# My Skill
"""
        result = SkillLoaderNode._extract_description(content)
        assert result == "A fantastic skill for testing"

    def test_frontmatter_with_quoted_description(self):
        from agents.skill_loader import SkillLoaderNode
        content = """---
name: my-skill
description: "Quoted description here"
---

# My Skill
"""
        result = SkillLoaderNode._extract_description(content)
        assert result == "Quoted description here"

    def test_frontmatter_with_single_quoted_description(self):
        from agents.skill_loader import SkillLoaderNode
        content = """---
name: my-skill
description: 'Single quoted'
---

# Body
"""
        result = SkillLoaderNode._extract_description(content)
        assert result == "Single quoted"

    def test_frontmatter_no_description_falls_to_body(self):
        from agents.skill_loader import SkillLoaderNode
        content = """---
name: my-skill
license: MIT
---

# The Body Starts Here
"""
        result = SkillLoaderNode._extract_description(content)
        assert result == "# The Body Starts Here"

    def test_frontmatter_no_closing_returns_first_line(self):
        from agents.skill_loader import SkillLoaderNode
        content = """---
name: my-skill
description: unclosed frontmatter"""
        result = SkillLoaderNode._extract_description(content)
        assert "---" in result or "name" in result.lower() or len(result) > 0

    def test_long_description_truncated(self):
        from agents.skill_loader import SkillLoaderNode
        long_desc = "x" * 300
        content = f"""---
description: {long_desc}
---

# Body
"""
        result = SkillLoaderNode._extract_description(content)
        assert len(result) <= 200

    def test_empty_content(self):
        from agents.skill_loader import SkillLoaderNode
        result = SkillLoaderNode._extract_description("")
        assert result == ""

    def test_frontmatter_empty_body(self):
        from agents.skill_loader import SkillLoaderNode
        content = """---
name: empty-body
---

"""
        result = SkillLoaderNode._extract_description(content)
        assert result == ""


class TestSkillLoaderExtractMetadata:
    """Test extract_metadata which reads SKILL.md headers."""

    def test_no_skill_md_returns_name_only(self, tmp_path):
        from agents.skill_loader import SkillLoaderNode
        skill_path = tmp_path / "my-skill"
        skill_path.mkdir()
        result = SkillLoaderNode.extract_metadata("my-skill", skill_path)
        assert result["name"] == "my-skill"
        assert result["description"] == ""

    def test_skill_md_with_frontmatter(self, tmp_path):
        from agents.skill_loader import SkillLoaderNode
        skill_path = tmp_path / "test-skill"
        skill_path.mkdir()
        (skill_path / "SKILL.md").write_text("""---
name: Test Skill
description: A skill for testing purposes
---

# Test Skill

Details here.
""")
        result = SkillLoaderNode.extract_metadata("test-skill", skill_path)
        assert result["name"] == "Test Skill"
        assert result["description"] == "A skill for testing purposes"

    def test_skill_md_read_error(self, tmp_path):
        from agents.skill_loader import SkillLoaderNode
        skill_path = tmp_path / "bad-skill"
        skill_path.mkdir()
        skill_md = skill_path / "SKILL.md"
        skill_md.mkdir()  # Create a directory instead of a file to cause OSError
        result = SkillLoaderNode.extract_metadata("bad-skill", skill_path)
        assert result["name"] == "bad-skill"
        assert result["description"] == ""


class TestSkillLoaderFormatSkillsForContext:
    """Test format_skills_for_context progressive disclosure."""

    @pytest.fixture
    def loader(self):
        from agents.skill_loader import SkillLoaderNode
        mock_registry = MagicMock()
        return SkillLoaderNode(skill_registry=mock_registry)

    def test_empty_skills_returns_empty(self, loader):
        assert loader.format_skills_for_context([]) == ""

    def test_single_skill_full_content(self, loader):
        skills = [{
            "name": "python-testing",
            "content": "Write pytest tests for Python code.\n\n## Details\nMore content here.",
            "tier": "official",
        }]
        result = loader.format_skills_for_context(skills)
        assert "python-testing" in result
        assert "Write pytest tests" in result

    def test_single_skill_truncated(self, loader):
        skills = [{
            "name": "long-skill",
            "content": "A" * 3000,
            "tier": "official",
        }]
        result = loader.format_skills_for_context(skills, max_length=100)
        assert "[...content truncated...]" in result

    def test_multiple_skills_progressive_disclosure(self, loader):
        skills = [
            {
                "name": "primary-skill",
                "content": """---
description: Primary skill desc
---

# Primary Skill

Full primary content here.""",
                "tier": "official",
            },
            {
                "name": "secondary-skill",
                "content": """---
description: Secondary skill desc
---

# Secondary Skill

Secondary content.""",
                "tier": "local",
            },
        ]
        result = loader.format_skills_for_context(skills, max_length=2000)
        # Primary skill gets full content
        assert "Primary Skill" in result
        # Secondary skill gets summary only
        assert "Additional Skills" in result
        assert "secondary-skill" in result

    def test_primary_gets_70_percent_budget(self, loader):
        long_content = "B" * 3000
        skills = [
            {"name": "primary", "content": long_content, "tier": "official"},
            {"name": "secondary", "content": "---\ndescription: Short\n---\n# S\nOk.", "tier": "local"},
        ]
        result = loader.format_skills_for_context(skills, max_length=2000)
        # Primary content should be truncated to ~70% of 2000 = 1400
        assert "[...content truncated...]" in result


class TestSkillLoaderGetSkillsForTask:
    """Test get_skills_for_task filtering."""

    @pytest.fixture
    def loader(self):
        from agents.skill_loader import SkillLoaderNode
        mock_registry = MagicMock()
        return SkillLoaderNode(skill_registry=mock_registry)

    def test_filters_by_task_type(self, loader):
        state = {
            "loaded_skills": [
                {"name": "s1", "task_type": "test_generation", "content": "..."},
                {"name": "s2", "task_type": "code_generation", "content": "..."},
                {"name": "s3", "task_type": "test_generation", "content": "..."},
            ]
        }
        result = loader.get_skills_for_task(state, "test_generation")
        assert len(result) == 2
        assert all(s["task_type"] == "test_generation" for s in result)

    def test_empty_loaded_skills(self, loader):
        state = {"loaded_skills": []}
        result = loader.get_skills_for_task(state, "any")
        assert result == []

    def test_no_matching_task_type(self, loader):
        state = {
            "loaded_skills": [
                {"name": "s1", "task_type": "code_generation", "content": "..."},
            ]
        }
        result = loader.get_skills_for_task(state, "security_audit")
        assert result == []


class TestSkillLoaderExecuteSkillScript:
    """Test execute_skill_script."""

    def test_nonexistent_script_returns_none(self, tmp_path):
        from agents.skill_loader import SkillLoaderNode
        result = SkillLoaderNode.execute_skill_script(tmp_path, "missing.py")
        assert result is None

    def test_no_sandbox_pool_returns_none(self, tmp_path):
        from agents.skill_loader import SkillLoaderNode
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "test.py").write_text("print('hello')")
        result = SkillLoaderNode.execute_skill_script(tmp_path, "test.py", sandbox_pool=None)
        assert result is None

    def test_sandbox_pool_execution(self, tmp_path):
        from agents.skill_loader import SkillLoaderNode
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "test.py").write_text("print('hello')")

        mock_handle = MagicMock()
        mock_handle.sandbox.run.return_value.stdout = "hello\n"
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_handle

        result = SkillLoaderNode.execute_skill_script(tmp_path, "test.py", sandbox_pool=mock_pool)
        assert result == "hello\n"
        mock_pool.release.assert_called_once_with(mock_handle)

    def test_sandbox_pool_exception(self, tmp_path):
        from agents.skill_loader import SkillLoaderNode
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "test.py").write_text("print('hello')")

        mock_pool = MagicMock()
        mock_pool.acquire.side_effect = RuntimeError("pool error")

        result = SkillLoaderNode.execute_skill_script(tmp_path, "test.py", sandbox_pool=mock_pool)
        assert result is None


# ====================================================================
# APIKeyManager (api_key_manager.py)
# ====================================================================


class TestAPIKeyValidation:
    """Test _validate_api_key format validation for different providers."""

    @pytest.fixture
    def manager(self, tmp_path):
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = tmp_path / "api_keys.json"
        return mgr

    # Generic validation
    def test_valid_generic_key(self, manager):
        valid, err = manager._validate_api_key("SOME_SERVICE_KEY", "a" * 20)
        assert valid is True
        assert err == ""

    def test_too_short_key(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", "short")
        assert valid is False
        assert "too short" in err

    def test_non_ascii_key(self, manager):
        valid, err = manager._validate_api_key("KEY", "key-with-emojis-1234")
        # This key is actually ASCII, so it passes basic check
        # Let's test actual non-ASCII
        valid, err = manager._validate_api_key("KEY", "key-with-\u00e9moji-1234")
        assert valid is False
        assert "non-ASCII" in err

    def test_whitespace_in_key(self, manager):
        valid, err = manager._validate_api_key("KEY", "has space in key12")
        assert valid is False
        assert "whitespace" in err

    def test_leading_whitespace(self, manager):
        valid, err = manager._validate_api_key("KEY", " leading-space123")
        assert valid is False
        assert "whitespace" in err

    def test_trailing_whitespace(self, manager):
        valid, err = manager._validate_api_key("KEY", "trailing-space12 ")
        assert valid is False

    def test_tab_in_key(self, manager):
        valid, err = manager._validate_api_key("KEY", "has\ttab-char12")
        assert valid is False

    def test_newline_in_key(self, manager):
        valid, err = manager._validate_api_key("KEY", "has\nnewline-12")
        assert valid is False

    # OpenAI
    def test_openai_valid(self, manager):
        valid, _ = manager._validate_api_key("OPENAI_API_KEY", "sk-" + "a" * 45)
        assert valid is True

    def test_openai_wrong_prefix(self, manager):
        valid, err = manager._validate_api_key("OPENAI_API_KEY", "wrong-" + "a" * 45)
        assert valid is False
        assert "sk-" in err

    def test_openai_too_short(self, manager):
        valid, err = manager._validate_api_key("OPENAI_API_KEY", "sk-" + "a" * 10)
        assert valid is False
        assert "too short" in err

    # Anthropic
    def test_anthropic_valid(self, manager):
        valid, _ = manager._validate_api_key("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 50)
        assert valid is True

    def test_anthropic_wrong_prefix(self, manager):
        valid, err = manager._validate_api_key("ANTHROPIC_API_KEY", "sk-" + "a" * 50)
        assert valid is False
        assert "sk-ant-" in err

    def test_anthropic_too_short(self, manager):
        valid, err = manager._validate_api_key("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 10)
        assert valid is False
        assert "too short" in err

    def test_claude_key_uses_anthropic_rules(self, manager):
        valid, err = manager._validate_api_key("CLAUDE_API_KEY", "sk-" + "a" * 50)
        assert valid is False
        assert "sk-ant-" in err

    # HuggingFace
    def test_huggingface_valid(self, manager):
        valid, _ = manager._validate_api_key("HUGGINGFACE_API_KEY", "hf_" + "a" * 20)
        assert valid is True

    def test_huggingface_wrong_prefix(self, manager):
        valid, err = manager._validate_api_key("HF_TOKEN", "wrong-" + "a" * 20)
        assert valid is False
        assert "hf_" in err

    # Google / Gemini
    def test_google_valid(self, manager):
        valid, _ = manager._validate_api_key("GOOGLE_API_KEY", "a" * 39)
        assert valid is True

    def test_google_too_short(self, manager):
        valid, err = manager._validate_api_key("GOOGLE_API_KEY", "a" * 20)
        assert valid is False
        assert "too short" in err

    def test_gemini_uses_google_rules(self, manager):
        valid, err = manager._validate_api_key("GEMINI_API_KEY", "a" * 20)
        assert valid is False
        assert "too short" in err


class TestAPIKeyGetErrorMessage:
    """Test get_error_message formatting."""

    @pytest.fixture
    def manager(self, tmp_path):
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = tmp_path / "api_keys.json"
        return mgr

    def test_basic_error_message(self, manager):
        msg = manager.get_error_message("OPENAI_API_KEY")
        assert "OPENAI_API_KEY" in msg
        assert "export OPENAI_API_KEY" in msg
        assert "api_keys.json" in msg

    def test_messenger_integration_mentioned_when_enabled(self, manager):
        mock_config = MagicMock()
        mock_config.mattermost.enabled = True
        manager.config = mock_config
        msg = manager.get_error_message("SOME_KEY")
        assert "messenger" in msg.lower() or "Interactive" in msg

    def test_no_messenger_when_disabled(self, manager):
        msg = manager.get_error_message("SOME_KEY")
        assert "Interactive prompting" not in msg

    def test_key_name_in_instructions(self, manager):
        msg = manager.get_error_message("ANTHROPIC_API_KEY")
        assert "ANTHROPIC_API_KEY" in msg
        assert "Anthropic Api Key" in msg


class TestAPIKeySaveKey:
    """Test _save_key secure storage."""

    @pytest.fixture
    def manager(self, tmp_path):
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = tmp_path / ".vibe" / "api_keys.json"
        return mgr

    def test_save_creates_directory(self, manager):
        manager._save_key("TEST_KEY", "test-value-1234567890")
        assert manager.storage_path.parent.exists()

    def test_save_writes_json(self, manager):
        manager._save_key("TEST_KEY", "test-value-1234567890")
        with open(manager.storage_path) as f:
            data = json.load(f)
        assert data["TEST_KEY"] == "test-value-1234567890"

    def test_save_updates_cache(self, manager):
        manager._save_key("TEST_KEY", "test-value-1234567890")
        assert manager.cache["TEST_KEY"] == "test-value-1234567890"

    def test_save_preserves_existing_keys(self, manager):
        manager.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with open(manager.storage_path, 'w') as f:
            json.dump({"EXISTING_KEY": "existing-value"}, f)

        manager._save_key("NEW_KEY", "new-value-12345678901")
        with open(manager.storage_path) as f:
            data = json.load(f)
        assert data["EXISTING_KEY"] == "existing-value"
        assert data["NEW_KEY"] == "new-value-12345678901"

    def test_save_sets_file_permissions(self, manager):
        import stat
        manager._save_key("TEST_KEY", "test-value-1234567890")
        mode = manager.storage_path.stat().st_mode
        assert mode & 0o777 == 0o600


class TestAPIKeyGetApiKey:
    """Test get_api_key fallback chain."""

    @pytest.fixture
    def manager(self, tmp_path):
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = tmp_path / "api_keys.json"
        return mgr

    def test_env_var_takes_priority(self, manager):
        with patch.dict(os.environ, {"MY_API_KEY": "from-env-1234567890"}):
            result = manager.get_api_key("MY_API_KEY", prompt_user=False)
        assert result == "from-env-1234567890"

    def test_cache_fallback(self, manager):
        manager.cache["CACHED_KEY"] = "from-cache-1234567890"
        result = manager.get_api_key("CACHED_KEY", prompt_user=False)
        assert result == "from-cache-1234567890"

    def test_storage_fallback(self, manager):
        manager.storage_path.write_text(json.dumps({"STORED_KEY": "from-disk-1234567890"}))
        result = manager.get_api_key("STORED_KEY", prompt_user=False)
        assert result == "from-disk-1234567890"

    def test_not_found_returns_none(self, manager):
        result = manager.get_api_key("MISSING_KEY", prompt_user=False)
        assert result is None

    def test_prompt_user_called_when_enabled(self, manager):
        manager._prompt_user_for_key = MagicMock(return_value=None)
        manager.get_api_key("MISSING_KEY", prompt_user=True)
        manager._prompt_user_for_key.assert_called_once_with("MISSING_KEY")


class TestAPIKeyLoadStoredKeys:
    """Test _load_stored_keys from disk."""

    def test_load_nonexistent_file(self, tmp_path):
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = tmp_path / "nonexistent.json"
        mgr._load_stored_keys()
        assert mgr.cache == {}

    def test_load_valid_file(self, tmp_path):
        from agents.api_key_manager import APIKeyManager
        storage = tmp_path / "keys.json"
        storage.write_text(json.dumps({"KEY1": "val1", "KEY2": "val2"}))
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = storage
        mgr._load_stored_keys()
        assert mgr.cache["KEY1"] == "val1"
        assert mgr.cache["KEY2"] == "val2"

    def test_load_corrupt_file(self, tmp_path):
        from agents.api_key_manager import APIKeyManager
        storage = tmp_path / "keys.json"
        storage.write_text("not valid json {{{")
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = storage
        mgr._load_stored_keys()
        # Should not crash, cache remains empty
        assert mgr.cache == {}


class TestAPIKeyPromptUser:
    """Test _prompt_user_for_key routing."""

    @pytest.fixture
    def manager(self, tmp_path):
        from agents.api_key_manager import APIKeyManager
        mgr = APIKeyManager.__new__(APIKeyManager)
        mgr.config = None
        mgr.cache = {}
        mgr.storage_path = tmp_path / "api_keys.json"
        return mgr

    def test_no_messenger_returns_none(self, manager):
        result = manager._prompt_user_for_key("ANY_KEY")
        assert result is None

    def test_mattermost_attempted_when_configured(self, manager):
        mock_config = MagicMock()
        mock_config.mattermost.enabled = True
        mock_config.mattermost.bot_enabled = True
        manager.config = mock_config
        manager._prompt_via_mattermost = MagicMock(return_value=None)

        result = manager._prompt_user_for_key("ANY_KEY")
        manager._prompt_via_mattermost.assert_called_once_with("ANY_KEY")

    def test_slack_attempted_when_configured(self, manager):
        with patch.dict(os.environ, {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_USER_ID": "U12345",
        }):
            manager._prompt_via_slack = MagicMock(return_value=None)
            result = manager._prompt_user_for_key("ANY_KEY")
            manager._prompt_via_slack.assert_called_once_with("ANY_KEY")

    def test_exception_returns_none(self, manager):
        mock_config = MagicMock()
        mock_config.mattermost.enabled = True
        mock_config.mattermost.bot_enabled = True
        manager.config = mock_config
        manager._prompt_via_mattermost = MagicMock(side_effect=RuntimeError("fail"))

        result = manager._prompt_user_for_key("ANY_KEY")
        assert result is None


class TestAPIKeyManagerGlobal:
    """Test get_api_key_manager global singleton."""

    def test_returns_instance(self):
        from agents import api_key_manager
        # Reset global
        api_key_manager._api_key_manager = None
        with patch.object(api_key_manager.APIKeyManager, '__init__', return_value=None):
            mgr = api_key_manager.get_api_key_manager()
            assert mgr is not None
            # Second call returns same instance
            mgr2 = api_key_manager.get_api_key_manager()
            assert mgr is mgr2
        # Clean up
        api_key_manager._api_key_manager = None
