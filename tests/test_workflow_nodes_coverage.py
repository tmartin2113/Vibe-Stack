"""
Tests targeting uncovered lines in workflow node modules.

Covers:
- agents/nodes.py: _safe_split_after, _safe_split_before, _safe_split_between, _safe_find_line_with
- agents/state.py: _summarize_feedback, get_context_for_node, finalize_state
- agents/output_nodes.py: post_to_mattermost (bot API path, webhook path, missing message)
- agents/critic_nodes.py: _parse_critic_output fallback paths, _build_refinement_history,
      evaluate_sub_specification, evaluate_sub_output, evaluate_aggregated_output
- agents/specialist_nodes.py: _resolve_skill_generation_config, plan_refinement
"""

import os

os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from agents.state import (
    AgentState,
    create_initial_state,
    _summarize_feedback,
    get_context_for_node,
    finalize_state,
)
from agents.nodes import AgentNodes
from agents.adapters import PromptAdapter, AdapterRegistry
from agents.tools import create_default_tool_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CRITIC_WELL_FORMED = """\
SCORES:
Completeness: 88
Accuracy: 91
Quality: 85
Clarity: 90
Coherence: 87
Helpfulness: 92
Overall: 89

REASONING:
Good output. The solution is thorough and well-structured."""

CRITIC_NO_SCORES_SECTION = """\
The output looks great. I would rate it 75/100 overall.
Some areas could be improved but it meets the requirements."""

CRITIC_NO_PARSEABLE_SCORES = """\
This is a review of the output. It is generally acceptable
but lacks detail in some areas. Could be more thorough."""


def _make_registry(responses=None):
    """Create an AdapterRegistry with mock adapters."""
    responses = responses or {}
    registry = AdapterRegistry()
    for name in ["vibe", "critic", "refinement", "code_expert", "general"]:
        resp = responses.get(name, f"Default response from {name}")
        model = MagicMock()
        if isinstance(resp, list):
            model.generate.side_effect = resp
        else:
            model.generate.return_value = resp
        adapter = PromptAdapter(
            name=name,
            system_prompt=f"You are {name}.",
            base_model=model,
        )
        registry.register(adapter)
    return registry


def _make_nodes(responses=None, config=None):
    """Create an AgentNodes instance with mock adapters."""
    registry = _make_registry(responses)
    tool_reg = create_default_tool_registry(sandbox_pool=MagicMock())
    return AgentNodes(registry, tool_reg, config=config)


# ===========================================================================
# 1. agents/nodes.py  -- safe text splitting utilities (lines 99, 107, 114-120, 125-128)
# ===========================================================================


class TestSafeSplitAfter:
    """Tests for AgentNodes._safe_split_after."""

    def test_delimiter_present(self):
        result = AgentNodes._safe_split_after("REASONING: good output", "REASONING:", "")
        assert result == "good output"

    def test_delimiter_missing_returns_default(self):
        result = AgentNodes._safe_split_after("no delimiter here", "REASONING:", "fallback")
        assert result == "fallback"

    def test_empty_text_returns_default(self):
        result = AgentNodes._safe_split_after("", "REASONING:", "fallback")
        assert result == "fallback"

    def test_delimiter_at_end_empty_after(self):
        """When delimiter is at the end with nothing after, returns default."""
        result = AgentNodes._safe_split_after("REASONING:   ", "REASONING:", "default_val")
        assert result == "default_val"

    def test_multiline_text(self):
        text = "SCORES:\nOverall: 85\nREASONING:\nLine 1\nLine 2"
        result = AgentNodes._safe_split_after(text, "REASONING:", "")
        assert "Line 1" in result


class TestSafeSplitBefore:
    """Tests for AgentNodes._safe_split_before."""

    def test_delimiter_present(self):
        result = AgentNodes._safe_split_before("header text REASONING: rest", "REASONING:", "")
        assert result == "header text"

    def test_delimiter_missing_returns_default(self):
        result = AgentNodes._safe_split_before("no delimiter here", "REASONING:", "fallback")
        assert result == "fallback"

    def test_empty_text_returns_default(self):
        result = AgentNodes._safe_split_before("", "REASONING:", "fallback")
        assert result == "fallback"

    def test_delimiter_at_start_empty_before(self):
        """When delimiter is at the start with nothing before, returns default."""
        result = AgentNodes._safe_split_before("REASONING: some text", "REASONING:", "default_val")
        assert result == "default_val"


class TestSafeSplitBetween:
    """Tests for AgentNodes._safe_split_between."""

    def test_both_delimiters_present(self):
        text = "START:hello world:END rest"
        result = AgentNodes._safe_split_between(text, "START:", ":END", "")
        assert result == "hello world"

    def test_start_delimiter_missing(self):
        result = AgentNodes._safe_split_between("no start :END", "START:", ":END", "fallback")
        assert result == "fallback"

    def test_end_delimiter_missing(self):
        result = AgentNodes._safe_split_between("START: no end", "START:", ":END", "fallback")
        assert result == "fallback"

    def test_both_delimiters_missing(self):
        result = AgentNodes._safe_split_between("plain text", "START:", ":END", "fallback")
        assert result == "fallback"

    def test_empty_text_returns_default(self):
        result = AgentNodes._safe_split_between("", "START:", ":END", "fallback")
        assert result == "fallback"

    def test_empty_content_between_delimiters(self):
        """When delimiters are adjacent with only whitespace between, returns default."""
        result = AgentNodes._safe_split_between("START:   :END", "START:", ":END", "fallback")
        assert result == "fallback"

    def test_multiline_between(self):
        text = "BEGIN>>>line1\nline2<<<END"
        result = AgentNodes._safe_split_between(text, "BEGIN>>>", "<<<END", "")
        assert "line1" in result
        assert "line2" in result


class TestSafeFindLineWith:
    """Tests for AgentNodes._safe_find_line_with."""

    def test_finds_matching_line(self):
        text = "first line\nOverall: 85\nlast line"
        result = AgentNodes._safe_find_line_with(text, "Overall:", "")
        assert result == "Overall: 85"

    def test_returns_first_matching_line(self):
        text = "Overall: 50\nOverall: 90"
        result = AgentNodes._safe_find_line_with(text, "Overall:", "")
        assert result == "Overall: 50"

    def test_no_matching_line_returns_default(self):
        text = "line1\nline2\nline3"
        result = AgentNodes._safe_find_line_with(text, "Overall:", "fallback")
        assert result == "fallback"

    def test_empty_text_returns_default(self):
        result = AgentNodes._safe_find_line_with("", "Overall:", "fallback")
        assert result == "fallback"

    def test_strips_leading_trailing_whitespace(self):
        text = "  Overall: 85  \nother line"
        result = AgentNodes._safe_find_line_with(text, "Overall:", "")
        assert result == "Overall: 85"


# ===========================================================================
# 2. agents/state.py  -- _summarize_feedback, get_context_for_node, finalize_state
#    (lines 265-269, 287-341, 357-368)
# ===========================================================================


class TestSummarizeFeedback:
    """Tests for _summarize_feedback."""

    def test_short_feedback_returned_as_is(self):
        result = _summarize_feedback("Short feedback.", max_length=150)
        assert result == "Short feedback."

    def test_long_feedback_truncated_to_first_sentence(self):
        feedback = (
            "First sentence is important. Second sentence has more details. "
            "Third sentence is also long enough to exceed max length when combined."
        )
        result = _summarize_feedback(feedback, max_length=50)
        assert result == "First sentence is important..."

    def test_exact_max_length_not_truncated(self):
        feedback = "x" * 150
        result = _summarize_feedback(feedback, max_length=150)
        assert result == feedback

    def test_long_single_sentence_no_period_truncates(self):
        """When there is no period-space, the split returns a single element,
        which still gets '...' appended since the first 'sentence' equals
        the original feedback (minus the split)."""
        feedback = "A" * 200
        result = _summarize_feedback(feedback, max_length=150)
        # The function splits on '. ' -- no split found means sentences[0] == full text
        # Then it appends '...'
        assert result.endswith("...")


class TestGetContextForNode:
    """Tests for get_context_for_node (lines 287-341)."""

    def _base_state(self):
        state = create_initial_state("Build a web API", max_iterations=3)
        state["specification"] = "REST API spec..."
        state["task_type"] = "code"
        state["routed_task_type"] = "code_generation"
        state["specialist_adapter"] = "code_expert"
        state["routing_confidence"] = 0.92
        state["spec_critic_score"] = 88
        state["loaded_skills"] = [{"name": "fastapi", "content": "Use FastAPI..."}]
        state["specialist_output"] = "def handler(): ..."
        state["output_critic_feedback"] = "Needs error handling"
        state["output_critic_score"] = 72
        state["specialist_iteration_count"] = 1
        state["specialist_output"] = "def handler(): ..."
        state["output_critic_score"] = 72
        state["output_critic_scores"] = {"completeness": 70, "quality": 74}
        state["output_critic_feedback"] = "Needs error handling"
        state["sub_tasks"] = [{"task_type": "code"}]
        state["aggregation_strategy"] = "merge"
        return state

    def test_base_context_always_present(self):
        state = self._base_state()
        ctx = get_context_for_node(state, "unknown_node")
        assert ctx["user_request"] == "Build a web API"
        assert ctx["session_id"] == state["session_id"]
        assert "iteration" in ctx

    def test_executor_context(self):
        state = self._base_state()
        ctx = get_context_for_node(state, "executor")
        assert ctx["specification"] == "REST API spec..."
        assert ctx["task_type"] == "code"

    def test_specialist_context_first_iteration(self):
        state = self._base_state()
        state["specialist_iteration_count"] = 0
        ctx = get_context_for_node(state, "specialist")
        assert ctx["specification"] == "REST API spec..."
        assert ctx["routed_task_type"] == "code_generation"
        assert ctx["specialist_adapter"] == "code_expert"
        assert ctx["loaded_skills"] == state["loaded_skills"]
        # On first iteration, no previous output/feedback
        assert "specialist_output" not in ctx

    def test_specialist_context_refinement_iteration(self):
        state = self._base_state()
        state["specialist_iteration_count"] = 1
        ctx = get_context_for_node(state, "specialist")
        # On refinement iteration, includes previous output/feedback
        assert "specialist_output" in ctx
        assert "output_critic_feedback" in ctx
        assert "output_critic_score" in ctx

    def test_critic_context(self):
        state = self._base_state()
        ctx = get_context_for_node(state, "critic")
        assert ctx["specification"] == "REST API spec..."
        assert ctx["generated_output"] == "def handler(): ..."
        assert ctx["routed_task_type"] == "code_generation"

    def test_refinement_context(self):
        state = self._base_state()
        ctx = get_context_for_node(state, "refinement")
        assert ctx["output_critic_score"] == 72
        assert ctx["output_critic_scores"] == {"completeness": 70, "quality": 74}
        assert ctx["output_critic_feedback"] == "Needs error handling"
        assert ctx["specialist_output"] == "def handler(): ..."
        assert ctx["specialist_adapter"] == "code_expert"
        assert "iterations_remaining" in ctx

    def test_router_context(self):
        state = self._base_state()
        ctx = get_context_for_node(state, "router")
        assert ctx["specification"] == "REST API spec..."
        assert ctx["task_type"] == "code"
        assert ctx["spec_critic_score"] == 88

    def test_aggregator_context(self):
        state = self._base_state()
        ctx = get_context_for_node(state, "aggregator")
        assert ctx["sub_tasks"] == [{"task_type": "code"}]
        assert ctx["aggregation_strategy"] == "merge"
        assert ctx["specification"] == "REST API spec..."


class TestFinalizeState:
    """Tests for finalize_state (lines 357-368)."""

    def test_sets_end_time(self):
        state = create_initial_state("test")
        result = finalize_state(state)
        assert "end_time" in result
        assert result["end_time"] is not None

    def test_calculates_total_time(self):
        state = create_initial_state("test")
        result = finalize_state(state)
        assert "total_time_seconds" in result
        assert result["total_time_seconds"] >= 0

    def test_sets_final_output_from_specialist_output(self):
        state = create_initial_state("test")
        state["specialist_output"] = "the final answer"
        result = finalize_state(state)
        assert result["final_output"] == "the final answer"

    def test_sets_final_score_from_output_critic_score(self):
        state = create_initial_state("test")
        state["output_critic_score"] = 91
        result = finalize_state(state)
        assert result["final_score"] == 91

    def test_defaults_when_no_output_or_score(self):
        state = create_initial_state("test")
        result = finalize_state(state)
        assert result["final_output"] == ""
        assert result["final_score"] == 0

    def test_end_time_after_start_time(self):
        state = create_initial_state("test")
        result = finalize_state(state)
        start = datetime.fromisoformat(result["start_time"])
        end = datetime.fromisoformat(result["end_time"])
        assert end >= start


# ===========================================================================
# 3. agents/output_nodes.py  -- post_to_mattermost
#    (lines 66-67, 74-124)
# ===========================================================================


class TestPostToMattermost:
    """Tests for OutputNodesMixin.post_to_mattermost."""

    def test_no_message_skips_post(self):
        """When mattermost_message is empty, post is skipped."""
        nodes = _make_nodes()
        state = create_initial_state("test")
        state["mattermost_message"] = ""
        result = nodes.post_to_mattermost(state)
        assert result is state  # State returned unchanged

    def test_mattermost_disabled_skips_post(self):
        """When Mattermost is disabled in config, post is skipped."""
        from agents.config import SystemConfig
        config = SystemConfig()
        config.mattermost.enabled = False
        nodes = _make_nodes(config=config)
        state = create_initial_state("test")
        state["mattermost_message"] = "Hello World"
        result = nodes.post_to_mattermost(state)
        # Should return state without mattermost_message_id
        assert "mattermost_message_id" not in result or result.get("mattermost_message_id") is None

    def test_no_config_skips_post(self):
        """When config is None, post is skipped."""
        nodes = _make_nodes(config=None)
        state = create_initial_state("test")
        state["mattermost_message"] = "Hello World"
        result = nodes.post_to_mattermost(state)
        assert result is state

    @patch("agents.output_nodes.MattermostClient", create=True)
    def test_bot_api_path_success(self, _mock_client_cls):
        """Bot API path: successful post stores message_id."""
        from agents.config import SystemConfig

        config = SystemConfig()
        config.mattermost.enabled = True
        config.mattermost.bot_token = "fake-bot-token"
        config.mattermost.mattermost_url = "http://mm.local"
        config.mattermost.webhook_url = None
        config.mattermost.default_channel = "test-channel"

        mock_client_instance = MagicMock()
        mock_client_instance.send_channel_message.return_value = "post_12345"

        nodes = _make_nodes(config=config)

        # Patch the import inside post_to_mattermost
        with patch.dict("sys.modules", {"agents.messenger_client": MagicMock()}):
            with patch("agents.output_nodes.OutputNodesMixin.post_to_mattermost") as mock_post:
                # Instead, let's directly mock the messenger_client import
                pass

        # More direct approach: patch the module import within post_to_mattermost
        state = create_initial_state("test")
        state["mattermost_message"] = "Hello from bot"

        with patch("agents.messenger_client.MattermostClient") as MockMmClient:
            MockMmClient.return_value = mock_client_instance
            result = nodes.post_to_mattermost(state)

        assert result.get("mattermost_message_id") == "post_12345"

    @patch("agents.messenger_client.MattermostClient")
    def test_bot_api_path_returns_no_post_id(self, MockMmClient):
        """Bot API path: when post returns None, logs error."""
        from agents.config import SystemConfig

        config = SystemConfig()
        config.mattermost.enabled = True
        config.mattermost.bot_token = "fake-bot-token"
        config.mattermost.mattermost_url = "http://mm.local"
        config.mattermost.webhook_url = None

        mock_client_instance = MagicMock()
        mock_client_instance.send_channel_message.return_value = None
        MockMmClient.return_value = mock_client_instance

        nodes = _make_nodes(config=config)
        state = create_initial_state("test")
        state["mattermost_message"] = "Hello from bot"

        result = nodes.post_to_mattermost(state)
        # Should return state (early return after bot API path)
        assert result is state

    @patch("agents.messenger_client.MattermostClient")
    def test_bot_api_exception_falls_through_to_webhook(self, MockMmClient):
        """Bot API path: when bot API throws, falls through to webhook."""
        from agents.config import SystemConfig

        config = SystemConfig()
        config.mattermost.enabled = True
        config.mattermost.bot_token = "fake-bot-token"
        config.mattermost.mattermost_url = "http://mm.local"
        config.mattermost.webhook_url = "http://mm.local/hooks/abc"
        config.mattermost.default_channel = "test-channel"
        config.mattermost.username = "TestBot"

        MockMmClient.side_effect = Exception("Connection refused")

        nodes = _make_nodes(config=config)
        state = create_initial_state("test")
        state["mattermost_message"] = "Hello from bot"

        with patch("requests.post") as mock_requests_post:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_requests_post.return_value = mock_resp
            result = nodes.post_to_mattermost(state)

        mock_requests_post.assert_called_once()
        call_kwargs = mock_requests_post.call_args
        assert call_kwargs[1]["json"]["text"] == "Hello from bot"

    def test_webhook_path(self):
        """Webhook path: sends POST to webhook_url."""
        from agents.config import SystemConfig

        config = SystemConfig()
        config.mattermost.enabled = True
        config.mattermost.bot_token = None
        config.mattermost.mattermost_url = None
        config.mattermost.webhook_url = "http://mm.local/hooks/xyz"
        config.mattermost.default_channel = "test-channel"
        config.mattermost.username = "TestBot"

        nodes = _make_nodes(config=config)
        state = create_initial_state("test")
        state["mattermost_message"] = "Hello via webhook"

        with patch("requests.post") as mock_requests_post:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_requests_post.return_value = mock_resp
            result = nodes.post_to_mattermost(state)

        mock_requests_post.assert_called_once_with(
            "http://mm.local/hooks/xyz",
            json={
                "text": "Hello via webhook",
                "channel": "test-channel",
                "username": "TestBot",
            },
            timeout=10,
        )

    def test_webhook_exception_handled(self):
        """Webhook path: exception is caught and logged."""
        from agents.config import SystemConfig

        config = SystemConfig()
        config.mattermost.enabled = True
        config.mattermost.bot_token = None
        config.mattermost.mattermost_url = None
        config.mattermost.webhook_url = "http://mm.local/hooks/xyz"

        nodes = _make_nodes(config=config)
        state = create_initial_state("test")
        state["mattermost_message"] = "Hello"

        with patch("requests.post", side_effect=Exception("timeout")):
            # Should not raise
            result = nodes.post_to_mattermost(state)

        assert result is state

    def test_no_credentials_logs_warning(self):
        """When enabled but no bot_token and no webhook_url, logs warning."""
        from agents.config import SystemConfig

        config = SystemConfig()
        config.mattermost.enabled = True
        config.mattermost.bot_token = None
        config.mattermost.mattermost_url = None
        config.mattermost.webhook_url = None

        nodes = _make_nodes(config=config)
        state = create_initial_state("test")
        state["mattermost_message"] = "Hello"

        result = nodes.post_to_mattermost(state)
        assert result is state


# ===========================================================================
# 4. agents/critic_nodes.py  -- _parse_critic_output, _build_refinement_history,
#    evaluate_sub_specification, evaluate_sub_output, evaluate_aggregated_output
#    (lines 32-34, 181, 282-289, 294-299, 305, 331-350, 354, 365-405, 413-469, 477-543)
# ===========================================================================


class TestParseCriticOutput:
    """Tests for CriticNodesMixin._parse_critic_output."""

    def test_well_formed_output(self):
        """Parses standard SCORES + REASONING format correctly."""
        nodes = _make_nodes()
        scores, feedback = nodes._parse_critic_output(CRITIC_WELL_FORMED)
        assert scores["overall"] == 89
        assert scores["completeness"] == 88
        assert scores["accuracy"] == 91
        assert "thorough" in feedback.lower() or "well-structured" in feedback.lower()

    def test_fallback_scan_entire_output(self):
        """When no SCORES section, falls back to scanning entire output."""
        nodes = _make_nodes()
        text = "Completeness: 80\nAccuracy: 75\nOverall: 78\nThe output is decent."
        scores, feedback = nodes._parse_critic_output(text)
        assert scores["completeness"] == 80
        assert scores["accuracy"] == 75
        assert scores["overall"] == 78

    def test_last_resort_n_over_100_pattern(self):
        """When no dimension labels match, finds N/100 pattern."""
        nodes = _make_nodes()
        text = "The output is acceptable. Rating: 73/100"
        scores, feedback = nodes._parse_critic_output(text)
        assert scores["overall"] == 73

    def test_no_parseable_scores_defaults_to_50(self):
        """When nothing is parseable, all scores default to 50."""
        nodes = _make_nodes()
        scores, feedback = nodes._parse_critic_output(CRITIC_NO_PARSEABLE_SCORES)
        assert scores["overall"] == 50
        assert scores["completeness"] == 50

    def test_partial_parse_keeps_defaults_for_missing(self):
        """When only some dimensions are found, others keep default."""
        nodes = _make_nodes()
        text = "SCORES:\nOverall: 85\nREASONING:\nPartial output."
        scores, feedback = nodes._parse_critic_output(text)
        assert scores["overall"] == 85
        # Other dimensions should still be at default (50)
        assert scores["accuracy"] == 50

    def test_score_clamped_to_100(self):
        """Scores above 100 are clamped to 100."""
        nodes = _make_nodes()
        text = "SCORES:\nOverall: 150\nREASONING:\nOverflow test."
        scores, feedback = nodes._parse_critic_output(text)
        assert scores["overall"] == 100

    def test_score_clamped_to_0(self):
        """Negative-like scores (from regex) clamped to 0."""
        nodes = _make_nodes()
        text = "SCORES:\nOverall: 0\nREASONING:\nZero test."
        scores, feedback = nodes._parse_critic_output(text)
        assert scores["overall"] == 0

    def test_slash_100_format(self):
        """Scores in 'N/100' format are parsed correctly."""
        nodes = _make_nodes()
        text = "SCORES:\nCompleteness: 82/100\nOverall: 79/100\nREASONING:\nOK."
        scores, feedback = nodes._parse_critic_output(text)
        assert scores["completeness"] == 82
        assert scores["overall"] == 79

    def test_multiple_n_over_100_last_wins_for_overall(self):
        """Last resort: multiple N/100 patterns -- last one used as overall."""
        nodes = _make_nodes()
        text = "First: 60/100 Second: 70/100 Third: 85/100"
        scores, feedback = nodes._parse_critic_output(text)
        assert scores["overall"] == 85


class TestBuildRefinementHistory:
    """Tests for CriticNodesMixin._build_refinement_history."""

    def test_empty_history(self):
        state = {"conversation_history": []}
        turns = AgentNodes._build_refinement_history(state)
        assert turns == []

    def test_no_history_key(self):
        state = {}
        turns = AgentNodes._build_refinement_history(state)
        assert turns == []

    def test_single_entry_with_output_and_feedback(self):
        state = {
            "conversation_history": [
                {
                    "iteration": 1,
                    "output": "def foo(): pass",
                    "score": 70,
                    "feedback_summary": "Needs docstrings.",
                }
            ]
        }
        turns = AgentNodes._build_refinement_history(state)
        assert len(turns) == 2
        assert turns[0]["role"] == "assistant"
        assert "def foo(): pass" in turns[0]["content"]
        assert turns[1]["role"] == "user"
        assert "Critic" in turns[1]["content"]
        assert "70" in turns[1]["content"]

    def test_multiple_entries(self):
        state = {
            "conversation_history": [
                {"iteration": 1, "output": "v1", "score": 60, "feedback_summary": "Too short."},
                {"iteration": 2, "output": "v2", "score": 80, "feedback_summary": "Almost there."},
            ]
        }
        turns = AgentNodes._build_refinement_history(state)
        assert len(turns) == 4  # 2 entries * 2 turns each

    def test_entry_missing_output(self):
        """Entry without output skips the assistant turn."""
        state = {
            "conversation_history": [
                {"iteration": 1, "output": "", "score": 50, "feedback_summary": "No output produced."},
            ]
        }
        turns = AgentNodes._build_refinement_history(state)
        # Empty string is falsy, so no assistant turn
        assert len(turns) == 1
        assert turns[0]["role"] == "user"

    def test_entry_missing_feedback(self):
        """Entry without feedback skips the user turn."""
        state = {
            "conversation_history": [
                {"iteration": 1, "output": "some code", "score": 50, "feedback_summary": ""},
            ]
        }
        turns = AgentNodes._build_refinement_history(state)
        # Empty feedback_summary is falsy, so no user turn
        assert len(turns) == 1
        assert turns[0]["role"] == "assistant"


class TestEvaluateSubSpecification:
    """Tests for CriticNodesMixin.evaluate_sub_specification."""

    def test_evaluates_current_subtask(self):
        """Evaluates the current sub-task specification via critic."""
        nodes = _make_nodes(responses={"critic": CRITIC_WELL_FORMED})
        state = create_initial_state("Build a full-stack app")
        state["sub_tasks"] = [
            {
                "task_type": "code_generation",
                "specification": "Build the REST API endpoints",
                "status": "pending",
            },
            {
                "task_type": "test_generation",
                "specification": "Write tests for the API",
                "status": "pending",
            },
        ]
        state["current_sub_task_index"] = 0

        result = nodes.evaluate_sub_specification(state)

        sub = result["sub_tasks"][0]
        assert sub["spec_score"] > 0
        assert sub["spec_feedback"] != ""
        assert sub["status"] == "spec_evaluated"

    def test_index_out_of_range_returns_state(self):
        """When current_sub_task_index >= len(sub_tasks), returns state unchanged."""
        nodes = _make_nodes()
        state = create_initial_state("test")
        state["sub_tasks"] = [{"task_type": "code_generation", "specification": "x"}]
        state["current_sub_task_index"] = 5  # out of range

        result = nodes.evaluate_sub_specification(state)
        assert result is state

    def test_empty_subtasks_returns_state(self):
        """Empty sub_tasks list returns state unchanged."""
        nodes = _make_nodes()
        state = create_initial_state("test")
        state["sub_tasks"] = []
        state["current_sub_task_index"] = 0

        result = nodes.evaluate_sub_specification(state)
        assert result is state


class TestEvaluateSubOutput:
    """Tests for CriticNodesMixin.evaluate_sub_output."""

    def test_evaluates_current_subtask_output(self):
        """Evaluates the current sub-task output via critic."""
        nodes = _make_nodes(responses={"critic": CRITIC_WELL_FORMED})
        state = create_initial_state("Build a full-stack app")
        state["sub_tasks"] = [
            {
                "task_type": "code_generation",
                "specification": "Build the REST API endpoints",
                "output": "def get_users(): return []",
                "status": "executed",
            },
        ]
        state["current_sub_task_index"] = 0

        result = nodes.evaluate_sub_output(state)

        sub = result["sub_tasks"][0]
        assert sub["output_score"] > 0
        assert sub["output_feedback"] != ""
        assert sub["status"] == "evaluated"

    def test_index_out_of_range_returns_state(self):
        """When current_sub_task_index >= len(sub_tasks), returns state unchanged."""
        nodes = _make_nodes()
        state = create_initial_state("test")
        state["sub_tasks"] = []
        state["current_sub_task_index"] = 0

        result = nodes.evaluate_sub_output(state)
        assert result is state

    def test_with_skill_criteria(self):
        """When loaded_skills have quality_criteria, they are injected."""
        nodes = _make_nodes(responses={"critic": CRITIC_WELL_FORMED})
        state = create_initial_state("Build an API")
        state["loaded_skills"] = [
            {
                "name": "fastapi",
                "quality_criteria": ["Must use type hints", "Must have OpenAPI docs"],
            }
        ]
        state["sub_tasks"] = [
            {
                "task_type": "code_generation",
                "specification": "Build the REST API",
                "output": "def handler(): pass",
                "status": "executed",
            },
        ]
        state["current_sub_task_index"] = 0

        result = nodes.evaluate_sub_output(state)
        assert result["sub_tasks"][0]["output_score"] > 0

    def test_with_task_specific_criteria_fallback(self):
        """When no skill criteria, falls back to TASK_EVALUATION_CRITERIA."""
        nodes = _make_nodes(responses={"critic": CRITIC_WELL_FORMED})
        state = create_initial_state("Write tests")
        state["loaded_skills"] = []
        state["sub_tasks"] = [
            {
                "task_type": "test_generation",
                "specification": "Write unit tests",
                "output": "def test_add(): assert add(1,2)==3",
                "status": "executed",
            },
        ]
        state["current_sub_task_index"] = 0

        result = nodes.evaluate_sub_output(state)
        assert result["sub_tasks"][0]["output_score"] > 0


class TestEvaluateAggregatedOutput:
    """Tests for CriticNodesMixin.evaluate_aggregated_output."""

    def test_evaluates_aggregated_output(self):
        """Evaluates the combined output from multiple specialists."""
        nodes = _make_nodes(responses={"critic": CRITIC_WELL_FORMED})
        state = create_initial_state("Build a full-stack app")
        state["specification"] = "Build a full-stack application"
        state["aggregated_output"] = "# Full App\n## API\ndef get_users(): ...\n## Tests\ndef test_get_users(): ..."
        state["sub_tasks"] = [
            {
                "task_type": "code_generation",
                "specialist_adapter": "code_expert",
                "status": "completed",
                "output_score": 85,
            },
            {
                "task_type": "test_generation",
                "specialist_adapter": "test_generator",
                "status": "completed",
                "output_score": 80,
            },
        ]

        result = nodes.evaluate_aggregated_output(state)

        assert result["output_critic_score"] > 0
        assert result["output_critic_scores"]["overall"] > 0
        assert result["output_critic_feedback"] != ""
        # aggregated_output should be copied to specialist_output
        assert result["specialist_output"] == state["aggregated_output"]

    def test_no_subtasks_still_evaluates(self):
        """Even with empty sub_tasks, aggregated output is evaluated."""
        nodes = _make_nodes(responses={"critic": CRITIC_WELL_FORMED})
        state = create_initial_state("Simple task")
        state["specification"] = "Simple spec"
        state["aggregated_output"] = "Simple aggregated result"
        state["sub_tasks"] = []

        result = nodes.evaluate_aggregated_output(state)
        assert result["output_critic_score"] > 0

    def test_with_failed_subtask_in_summary(self):
        """Sub-tasks with non-completed status show as failed in summary."""
        nodes = _make_nodes(responses={"critic": CRITIC_WELL_FORMED})
        state = create_initial_state("Test task")
        state["specification"] = "Test spec"
        state["aggregated_output"] = "aggregated result"
        state["sub_tasks"] = [
            {
                "task_type": "code_generation",
                "specialist_adapter": "code_expert",
                "status": "failed",
                "output_score": 30,
            },
        ]

        result = nodes.evaluate_aggregated_output(state)
        assert result["output_critic_score"] > 0

    def test_with_skill_criteria_injection(self):
        """Skill-declared quality_criteria are injected into aggregated critic prompt."""
        nodes = _make_nodes(responses={"critic": CRITIC_WELL_FORMED})
        state = create_initial_state("Build an API")
        state["specification"] = "Build REST API"
        state["aggregated_output"] = "Full API code"
        state["routed_task_type"] = "code_generation"
        state["loaded_skills"] = [
            {
                "name": "fastapi",
                "quality_criteria": ["Must use type hints", "Must have error handling"],
            }
        ]
        state["sub_tasks"] = []

        result = nodes.evaluate_aggregated_output(state)
        assert result["output_critic_score"] > 0


# ===========================================================================
# 5. agents/specialist_nodes.py  -- _resolve_skill_generation_config, plan_refinement
# ===========================================================================


class TestResolveSkillGenerationConfig:
    """Tests for SpecialistNodesMixin._resolve_skill_generation_config."""

    def test_no_skills(self):
        result = AgentNodes._resolve_skill_generation_config([])
        assert result == {}

    def test_single_skill_with_config(self):
        skills = [
            {"name": "fastapi", "generation_config": {"temperature": 0.2, "max_tokens": 2000}}
        ]
        result = AgentNodes._resolve_skill_generation_config(skills)
        assert result == {"temperature": 0.2, "max_tokens": 2000}

    def test_multiple_skills_first_wins(self):
        """First skill's config takes priority on conflicts."""
        skills = [
            {"name": "primary", "generation_config": {"temperature": 0.1}},
            {"name": "secondary", "generation_config": {"temperature": 0.9, "max_tokens": 3000}},
        ]
        result = AgentNodes._resolve_skill_generation_config(skills)
        # First skill wins on temperature, second skill's max_tokens is included
        assert result["temperature"] == 0.1
        assert result["max_tokens"] == 3000

    def test_skills_without_generation_config(self):
        skills = [
            {"name": "no_config"},
            {"name": "also_no_config", "generation_config": None},
        ]
        result = AgentNodes._resolve_skill_generation_config(skills)
        assert result == {}

    def test_mixed_skills_some_with_config(self):
        skills = [
            {"name": "no_config"},
            {"name": "has_config", "generation_config": {"top_p": 0.95}},
        ]
        result = AgentNodes._resolve_skill_generation_config(skills)
        assert result == {"top_p": 0.95}


class TestPlanRefinement:
    """Tests for SpecialistNodesMixin.plan_refinement."""

    def test_creates_refinement_plan(self):
        """plan_refinement uses the refinement adapter and adds to history."""
        nodes = _make_nodes(responses={
            "refinement": "1. Add error handling\n2. Improve variable naming",
        })
        state = create_initial_state("Write a function")
        state["specialist_adapter"] = "code_expert"
        state["routed_task_type"] = "code_generation"
        state["output_critic_score"] = 65
        state["output_critic_scores"] = {"completeness": 60, "quality": 70}
        state["output_critic_feedback"] = "Missing error handling. Variable names unclear."
        state["specialist_output"] = "def f(x): return x"
        state["specialist_iteration_count"] = 1
        state["specialist_max_iterations"] = 3
        state["iteration_count"] = 1
        state["specification"] = "Write a sorting function"

        result = nodes.plan_refinement(state)

        assert "refinement" in result.get("adapters_used", [])

    def test_plan_refinement_adds_to_history(self):
        """plan_refinement calls add_to_history, which requires specialist_output and output_critic_score."""
        nodes = _make_nodes(responses={
            "refinement": "Improve error handling",
        })
        state = create_initial_state("Write a function")
        state["specialist_adapter"] = "code_expert"
        state["routed_task_type"] = "code_generation"
        state["output_critic_score"] = 65
        state["output_critic_scores"] = {"completeness": 60}
        state["output_critic_feedback"] = "Needs work."
        state["specialist_output"] = "def f(): pass"
        state["iteration_count"] = 0
        state["specification"] = "Write code"

        result = nodes.plan_refinement(state)

        # add_to_history should have added an entry
        history = result.get("conversation_history", [])
        assert len(history) == 1
        assert history[0]["iteration"] == 0
        assert history[0]["score"] == 65


class TestGetSpecialistConfig:
    """Tests for SpecialistNodesMixin._get_specialist_config."""

    def test_known_specialist_returns_defaults(self):
        """Known specialist names return their defaults."""
        nodes = _make_nodes()
        config = nodes._get_specialist_config("test_generator")
        assert config["temperature"] == 0.3
        assert config["max_tokens"] == 1500

    def test_unknown_specialist_returns_vibe_defaults(self):
        """Unknown specialist name falls back to vibe defaults."""
        nodes = _make_nodes()
        config = nodes._get_specialist_config("totally_unknown_specialist")
        assert config["temperature"] == 0.5  # vibe default
        assert config["max_tokens"] == 1500

    def test_with_system_config_generation(self):
        """When SystemConfig.generation is available, uses it."""
        from agents.config import SystemConfig

        sys_config = SystemConfig()
        # Create a mock generation config
        mock_gen = MagicMock()
        custom_config = {"temperature": 0.7, "max_tokens": 4000}
        # get_config returns different value for our specialist vs general
        mock_gen.get_config.side_effect = lambda name: (
            custom_config if name == "my_specialist" else {"temperature": 0.5, "max_tokens": 1000}
        )
        sys_config.generation = mock_gen

        nodes = _make_nodes(config=sys_config)
        result = nodes._get_specialist_config("my_specialist")
        assert result == custom_config


class TestFormatScores:
    """Tests for CriticNodesMixin._format_scores."""

    def test_formats_scores_dict(self):
        nodes = _make_nodes()
        result = nodes._format_scores({"completeness": 80, "quality": 90})
        assert "Completeness: 80/100" in result
        assert "Quality: 90/100" in result

    def test_empty_scores(self):
        nodes = _make_nodes()
        result = nodes._format_scores({})
        assert result == ""


# ===========================================================================
# Additional edge-case tests for _get_skill_criteria (critic_nodes.py lines 17-38)
# ===========================================================================


class TestGetSkillCriteria:
    """Tests for _get_skill_criteria helper function."""

    def test_no_loaded_skills(self):
        from agents.critic_nodes import _get_skill_criteria
        state = create_initial_state("test")
        state["loaded_skills"] = []
        result = _get_skill_criteria(state, "code_generation")
        assert result == ""

    def test_skills_without_quality_criteria(self):
        from agents.critic_nodes import _get_skill_criteria
        state = create_initial_state("test")
        state["loaded_skills"] = [{"name": "fastapi"}]
        result = _get_skill_criteria(state, "code_generation")
        assert result == ""

    def test_skills_with_quality_criteria(self):
        from agents.critic_nodes import _get_skill_criteria
        state = create_initial_state("test")
        state["loaded_skills"] = [
            {
                "name": "fastapi",
                "quality_criteria": ["Use type hints", "Include OpenAPI schema"],
            }
        ]
        result = _get_skill_criteria(state, "code_generation")
        assert "fastapi" in result
        assert "Use type hints" in result
        assert "Include OpenAPI schema" in result

    def test_first_skill_with_criteria_wins(self):
        from agents.critic_nodes import _get_skill_criteria
        state = create_initial_state("test")
        state["loaded_skills"] = [
            {"name": "no_criteria"},
            {"name": "has_criteria", "quality_criteria": ["Criterion A"]},
            {"name": "also_has", "quality_criteria": ["Criterion B"]},
        ]
        result = _get_skill_criteria(state, "code_generation")
        assert "has_criteria" in result
        assert "Criterion A" in result
        # Second skill with criteria is not included
        assert "Criterion B" not in result
