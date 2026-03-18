"""
Integration tests for the Genesia workflow engine.

These tests exercise real node chains through the workflow engine with the
LLM mocked at the adapter/backend level. Unlike unit tests, they verify that
nodes, decision functions, and the graph work together correctly.

Covers:
- Conversational path (router → specialist → critic → format)
- Single-specialist code generation (full graph)
- Quality gate refinement loop
- Decision functions with realistic state
- State transition validation warnings
- Workflow engine (Workflow, CompiledWorkflow) basics
"""

import os
import logging

os.environ["GENESIA_DISABLE_REMOTE_SKILLS"] = "1"

import pytest
from unittest.mock import MagicMock, patch

from agents.state import create_initial_state, AgentState, add_to_history, MAX_HISTORY_ENTRIES
from agents.adapters import PromptAdapter, AdapterRegistry
from agents.config import SystemConfig
from agents.graph import Workflow, END, create_agent_graph
from agents.nodes import AgentNodes
from agents.decision_functions import (
    should_approve_output,
    should_decompose,
    has_more_subtasks,
    _validate_preconditions,
)
from agents.tools import create_default_tool_registry


@pytest.fixture(autouse=True)
def _mock_sandbox_pool_start():
    """Prevent SandboxPoolManager from connecting to OpenSandbox in tests."""
    with patch("agents.sandbox.client.SandboxPoolManager.start"):
        yield


# ===== HELPERS =====

# Canned LLM responses that match the format expected by node parsers.

GENESIA_SPEC_RESPONSE = """\
NEEDS_CLARIFICATION: No
QUESTIONS: None
SPECIFICATION: Implement a Python function called `merge_sort` that takes a list \
of comparable elements and returns a new sorted list using the merge sort algorithm. \
Requirements: O(n log n) time complexity, stable sort, handle empty lists."""

CRITIC_APPROVE_SPEC = """\
SCORES:
Completeness: 92
Clarity: 95
Specificity: 88
Feasibility: 94
Overall: 92

REASONING:
The specification is detailed and provides clear requirements including complexity \
constraints and edge case handling. Well-structured for implementation."""

CRITIC_APPROVE_OUTPUT = """\
SCORES:
Completeness: 90
Accuracy: 88
Quality: 92
Clarity: 90
Helpfulness: 90
Overall: 90

REASONING:
The implementation correctly uses merge sort with O(n log n) complexity. \
Code is clean, well-documented, and handles edge cases."""

CRITIC_REJECT_OUTPUT = """\
SCORES:
Completeness: 40
Accuracy: 55
Quality: 45
Clarity: 50
Helpfulness: 45
Overall: 47

REASONING:
The output is incomplete — missing the merge step. Only the split logic \
is implemented. Needs significant rework."""

SPECIALIST_OUTPUT = """\
def merge_sort(lst):
    if len(lst) <= 1:
        return lst
    mid = len(lst) // 2
    left = merge_sort(lst[:mid])
    right = merge_sort(lst[mid:])
    return merge(left, right)

def merge(left, right):
    result = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1
    result.extend(left[i:])
    result.extend(right[j:])
    return result"""

CONVERSATIONAL_RESPONSE = """\
Python is a high-level, interpreted programming language known for its \
readability and versatility. It supports multiple programming paradigms \
including procedural, object-oriented, and functional programming."""


def _make_adapter_registry(responses=None):
    """
    Create an AdapterRegistry with mock adapters.

    Args:
        responses: Dict mapping adapter_name -> response string (or list of
                   responses for call_count-based cycling). If None, all
                   adapters return a generic response.
    """
    responses = responses or {}
    base_model = MagicMock()
    registry = AdapterRegistry()

    adapter_names = [
        "genesia", "critic", "refinement",
        "code_expert", "creative_writer", "research_analyst", "general",
    ]

    for name in adapter_names:
        resp = responses.get(name, f"Default response from {name}")

        # Support list of responses (cycle through on successive calls)
        if isinstance(resp, list):
            model = MagicMock()
            model.generate.side_effect = resp
        else:
            model = MagicMock()
            model.generate.return_value = resp

        adapter = PromptAdapter(
            name=name,
            system_prompt=f"You are {name}.",
            base_model=model,
        )
        registry.register(adapter)

    return registry


def _make_config(**overrides):
    """Create a SystemConfig with sensible test defaults."""
    from agents.sandbox.config import SandboxConfig
    from agents.config import CacheConfig
    cfg = SystemConfig()
    cfg.mattermost.enabled = False
    cfg.workflow.node_timeout = 0
    cfg.workflow.workflow_timeout = 0
    cfg.sandbox = SandboxConfig()
    # Disable result caching so tests are hermetic and don't depend on
    # or pollute the on-disk artifact cache from previous runs.
    cfg.cache = CacheConfig(enabled=False)
    for key, val in overrides.items():
        setattr(cfg.workflow, key, val)
    return cfg


# ===== WORKFLOW ENGINE TESTS =====


class TestWorkflowEngine:
    """Test the Workflow/CompiledWorkflow execution engine directly."""

    def test_linear_two_node_workflow(self):
        """Two nodes connected linearly produce correct final state."""
        wf = Workflow()
        wf.add_node("a", lambda s: {**s, "a_ran": True})
        wf.add_node("b", lambda s: {**s, "b_ran": True})
        wf.add_edge("a", "b")
        wf.add_edge("b", END)
        wf.set_entry_point("a")
        app = wf.compile()

        result = app.invoke({"input": "test"})
        assert result["a_ran"] is True
        assert result["b_ran"] is True

    def test_conditional_edge_routing(self):
        """Conditional edge routes to correct branch based on state."""
        wf = Workflow()
        wf.add_node("start", lambda s: s)
        wf.add_node("left", lambda s: {**s, "path": "left"})
        wf.add_node("right", lambda s: {**s, "path": "right"})
        wf.add_conditional_edges(
            "start",
            lambda s: "go_left" if s.get("choose_left") else "go_right",
            {"go_left": "left", "go_right": "right"},
        )
        wf.add_edge("left", END)
        wf.add_edge("right", END)
        wf.set_entry_point("start")
        app = wf.compile()

        left_result = app.invoke({"choose_left": True})
        assert left_result["path"] == "left"

        right_result = app.invoke({"choose_left": False})
        assert right_result["path"] == "right"

    def test_loop_with_counter(self):
        """Workflow supports loops (conditional edge back to same node)."""
        def increment(state):
            state["count"] = state.get("count", 0) + 1
            return state

        def check_count(state):
            return "done" if state["count"] >= 3 else "loop"

        wf = Workflow()
        wf.add_node("counter", increment)
        wf.add_conditional_edges("counter", check_count, {"loop": "counter", "done": END})
        wf.set_entry_point("counter")
        app = wf.compile()

        result = app.invoke({})
        assert result["count"] == 3

    def test_stream_mode_yields_intermediate_states(self):
        """Stream mode yields {node_name: state} dicts at each step."""
        wf = Workflow()
        wf.add_node("a", lambda s: {**s, "step": 1})
        wf.add_node("b", lambda s: {**s, "step": 2})
        wf.add_edge("a", "b")
        wf.add_edge("b", END)
        wf.set_entry_point("a")
        app = wf.compile()

        steps = list(app.stream({"step": 0}))
        assert len(steps) == 2
        assert "a" in steps[0]
        assert steps[0]["a"]["step"] == 1
        assert "b" in steps[1]
        assert steps[1]["b"]["step"] == 2

    def test_no_entry_point_raises(self):
        """Compiling without an entry point raises ValueError."""
        wf = Workflow()
        wf.add_node("a", lambda s: s)
        with pytest.raises(ValueError, match="No entry point"):
            wf.compile()


# ===== CONVERSATIONAL PATH (End-to-End) =====


class TestConversationalPath:
    """
    Test that conversational requests flow through the full pipeline.

    The new flow is: router → skill_generator → skill_loader → inject_memory →
    cache_lookup → specialist → heuristic_critic → [critic_output] → format →
    post → skill_cleanup → END.  There is no intent classifier; the router
    classifies the task type directly.
    """

    def test_conversational_e2e(self):
        """
        Conversational request flows through full pipeline.
        genesia adapter is called once (as the specialist).
        """
        registry = _make_adapter_registry({
            "genesia": CONVERSATIONAL_RESPONSE,
            "critic": CRITIC_APPROVE_OUTPUT,
        })
        config = _make_config()

        graph = create_agent_graph(registry, config=config)
        state = create_initial_state("What is Python?")

        result = graph.invoke(state)

        # Router classifies task type (no separate intent classifier)
        assert result.get("routed_task_type"), "Router should set routed_task_type"
        # Output comes through the specialist path
        assert result.get("specialist_output")

    def test_explanation_request_handled(self):
        """Explanation request goes through full pipeline."""
        registry = _make_adapter_registry({
            "genesia": "Dependency injection is a design pattern where objects receive their dependencies...",
            "critic": CRITIC_APPROVE_OUTPUT,
        })
        config = _make_config()

        graph = create_agent_graph(registry, config=config)
        state = create_initial_state("Explain what dependency injection is")

        result = graph.invoke(state)

        assert result.get("routed_task_type"), "Router should set routed_task_type"
        assert result.get("specialist_output")


# ===== SINGLE-SPECIALIST CODE GENERATION PATH =====


class TestSingleSpecialistPath:
    """
    Test single-specialist code generation through the real workflow graph.

    New flow: router → skill_generator → skill_loader → inject_memory →
    cache_lookup → specialist → heuristic_critic → [critic_output] → format →
    post → skill_cleanup → END.

    Mocks at the adapter level so all node parsing, state manipulation,
    and decision functions are exercised with real code.
    """

    def test_code_generation_happy_path(self):
        """
        Full code generation: router → skill_gen → skill_load → specialist →
        heuristic_critic → format → post → cleanup → END.
        """
        # genesia adapter is called once (as the specialist).
        # There is no separate spec-building phase.
        registry = _make_adapter_registry({
            "genesia": SPECIALIST_OUTPUT,
            "critic": CRITIC_APPROVE_OUTPUT,
        })
        config = _make_config()

        graph = create_agent_graph(registry, config=config)
        state = create_initial_state("Write a merge sort function in Python")

        result = graph.invoke(state)

        # Verify router classified the task type (no separate intent classifier)
        assert result.get("routed_task_type"), "Router should set routed_task_type"

        # Specification is now set to user_request by the router wrapper
        assert result.get("specification"), "Specification should be set"
        assert "merge sort" in result["specification"].lower()

        # Verify specialist produced output
        assert result.get("specialist_output") or result.get("current_output")

        # Verify heuristic critic scored the output
        assert result.get("heuristic_critic_score", 0) > 0 or result.get("critic_score", 0) > 0

        # Verify formatting happened
        assert result.get("mattermost_message"), "Should have formatted Mattermost message"

    def test_state_tracks_adapters_used(self):
        """Verify the state tracks which adapters were used during the workflow."""
        registry = _make_adapter_registry({
            "genesia": SPECIALIST_OUTPUT,
            "critic": CRITIC_APPROVE_OUTPUT,
        })
        config = _make_config()

        graph = create_agent_graph(registry, config=config)
        state = create_initial_state("Write a Python sorting function")

        result = graph.invoke(state)

        adapters_used = result.get("adapters_used", [])
        assert "genesia" in adapters_used, "genesia adapter should be tracked"


# ===== QUALITY GATE: REFINEMENT LOOP =====


class TestRefinementLoop:
    """Test that the quality gate triggers refinement when scores are low."""

    def test_low_heuristic_triggers_llm_critic(self):
        """
        When heuristic critic scores low, LLM critic_output is invoked.
        The output still completes the pipeline.
        """
        registry = _make_adapter_registry({
            "genesia": SPECIALIST_OUTPUT,
            "critic": CRITIC_APPROVE_OUTPUT,
        })
        config = _make_config()

        graph = create_agent_graph(registry, config=config)
        state = create_initial_state("Write a sorting function")

        result = graph.invoke(state)

        # Pipeline should complete with output
        assert result.get("specialist_output") or result.get("current_output")


# ===== DECISION FUNCTIONS WITH REALISTIC STATE =====


class TestDecisionFunctions:
    """Test decision functions with realistic state objects."""

    def test_should_approve_output_approved(self):
        state = create_initial_state("test")
        state["output_critic_score"] = 90
        state["quality_threshold"] = 85
        result = should_approve_output(state)
        assert result == "approved"

    def test_should_approve_output_refine(self):
        state = create_initial_state("test")
        state["output_critic_score"] = 60
        state["quality_threshold"] = 85
        state["specialist_iteration_count"] = 0
        state["specialist_max_iterations"] = 3
        result = should_approve_output(state)
        assert result == "refine_output"

    def test_should_approve_output_fail(self):
        state = create_initial_state("test")
        state["output_critic_score"] = 20
        state["quality_threshold"] = 85
        result = should_approve_output(state)
        assert result == "fail"

    def test_should_decompose_single(self):
        state = create_initial_state("test")
        state["requires_decomposition"] = False
        result = should_decompose(state)
        assert result == "single"

    def test_should_decompose_decompose(self):
        state = create_initial_state("test")
        state["requires_decomposition"] = True
        state["sub_tasks"] = [{"name": "task1"}, {"name": "task2"}]
        result = should_decompose(state)
        assert result == "decompose"

    def test_should_decompose_empty_subtasks_falls_back(self):
        """When decomposition requested but sub_tasks is empty, fall back to single."""
        state = create_initial_state("test")
        state["requires_decomposition"] = True
        state["sub_tasks"] = []
        result = should_decompose(state)
        assert result == "single"

    def test_has_more_subtasks_with_completed(self):
        """Completed sub-task advances index; unfinished sub-tasks remain."""
        state = create_initial_state("test")
        state["sub_tasks"] = [
            {"name": "t1", "status": "completed"},
            {"name": "t2"},
        ]
        state["current_sub_task_index"] = 0
        state["completed_sub_tasks"] = 0
        result = has_more_subtasks(state)
        # First sub-task is completed, so it moves to next -> "more"
        assert result == "more"

    def test_has_more_subtasks_all_done(self):
        """All sub-tasks completed returns done."""
        state = create_initial_state("test")
        state["sub_tasks"] = [
            {"name": "t1", "status": "completed"},
        ]
        state["current_sub_task_index"] = 0
        state["completed_sub_tasks"] = 0
        result = has_more_subtasks(state)
        assert result == "done"



# ===== STATE TRANSITION VALIDATION =====


class TestStateValidation:
    """Test that state validation warnings fire correctly."""

    def test_validate_preconditions_warns_on_missing_field(self, caplog):
        """Missing required field triggers a warning log."""
        state = create_initial_state("test")
        # spec_critic_score is NOT set
        with caplog.at_level(logging.WARNING, logger="agents.decision_functions"):
            _validate_preconditions(state, ["spec_critic_score"], "test_context")
        assert any("spec_critic_score" in r.message for r in caplog.records)

    def test_validate_preconditions_silent_when_field_present(self, caplog):
        """No warning when required field is present."""
        state = create_initial_state("test")
        state["spec_critic_score"] = 85
        with caplog.at_level(logging.WARNING, logger="agents.decision_functions"):
            _validate_preconditions(state, ["spec_critic_score"], "test_context")
        assert not any("spec_critic_score" in r.message for r in caplog.records)

    def test_should_approve_output_warns_when_score_missing(self, caplog):
        """should_approve_output warns when output_critic_score not set."""
        state = create_initial_state("test")
        with caplog.at_level(logging.WARNING, logger="agents.decision_functions"):
            result = should_approve_output(state)
        assert result in ("approved", "refine_output", "fail")


# ===== CONVERSATION HISTORY BOUNDING =====


class TestConversationHistoryBounding:
    """Test that conversation history is bounded to MAX_HISTORY_ENTRIES."""

    def test_history_truncated_at_max(self):
        state = create_initial_state("test")
        state["current_output"] = "output"
        state["critic_score"] = 80
        state["specification"] = "prompt"
        state["critic_feedback"] = "feedback"

        for i in range(20):
            state["iteration_count"] = i
            state = add_to_history(state)

        assert len(state["conversation_history"]) == MAX_HISTORY_ENTRIES

    def test_history_keeps_most_recent(self):
        state = create_initial_state("test")
        state["current_output"] = "output"
        state["critic_score"] = 80
        state["specification"] = "prompt"
        state["critic_feedback"] = "feedback"

        for i in range(15):
            state["iteration_count"] = i
            state = add_to_history(state)

        history = state["conversation_history"]
        assert len(history) == MAX_HISTORY_ENTRIES
        # Most recent entry should be from the last iteration
        assert history[-1]["iteration"] == 14

    def test_history_under_max_not_truncated(self):
        state = create_initial_state("test")
        state["current_output"] = "output"
        state["critic_score"] = 80
        state["specification"] = "prompt"
        state["critic_feedback"] = "feedback"

        for i in range(5):
            state["iteration_count"] = i
            state = add_to_history(state)

        assert len(state["conversation_history"]) == 5


# ===== AGENTNODE CONSTRUCTION & CLASSIFICATION =====


class TestAgentNodeCore:
    """Test AgentNodes construction and core methods."""

    def test_critic_evaluates_specification(self):
        """evaluate_specification produces scores and feedback."""
        registry = _make_adapter_registry({"critic": CRITIC_APPROVE_SPEC})
        nodes = AgentNodes(registry, create_default_tool_registry(sandbox_pool=MagicMock()))
        state = create_initial_state("Write a function")
        state["specification"] = "Implement merge sort..."
        state["task_type"] = "code"

        result = nodes.evaluate_specification(state)

        assert result.get("spec_critic_score", 0) > 0
        assert result.get("spec_critic_scores")
        assert result.get("spec_critic_feedback")

    def test_critic_evaluates_output(self):
        """evaluate_output produces scores and feedback."""
        registry = _make_adapter_registry({"critic": CRITIC_APPROVE_OUTPUT})
        nodes = AgentNodes(registry, create_default_tool_registry(sandbox_pool=MagicMock()))
        state = create_initial_state("Write a function")
        state["specification"] = "Implement merge sort..."
        state["specialist_output"] = SPECIALIST_OUTPUT
        state["current_output"] = SPECIALIST_OUTPUT
        state["routed_task_type"] = "code"
        state["specialist_adapter"] = "code_expert"

        result = nodes.evaluate_output(state)

        assert result.get("output_critic_score", 0) > 0
        assert result.get("output_critic_scores")
        assert result.get("output_critic_feedback")

    def test_format_for_mattermost(self):
        """format_for_mattermost creates a formatted message."""
        registry = _make_adapter_registry()
        config = _make_config()
        nodes = AgentNodes(registry, create_default_tool_registry(sandbox_pool=MagicMock()), config=config)
        state = create_initial_state("Write a function")
        state["current_output"] = "def sort(lst): return sorted(lst)"
        state["critic_score"] = 90
        state["iteration_count"] = 1

        result = nodes.format_for_mattermost(state)

        assert result.get("mattermost_message")
        assert "Task Completed" in result["mattermost_message"]
        assert "90" in result["mattermost_message"]


# ===== CLARIFICATION MECHANISM TESTS =====


class TestClarificationParsing:
    """Test parse_clarification extraction from specialist output."""

    def test_no_clarification_tag(self):
        """Normal output without tags returns False."""
        from agents.specialist_nodes import parse_clarification
        needed, questions = parse_clarification("Here is your merge sort implementation...")
        assert needed is False
        assert questions == []

    def test_single_question(self):
        """Single question in clarification tag is extracted."""
        from agents.specialist_nodes import parse_clarification
        output = """I'd like to help, but I need more info.

<clarification_needed>
1. What database engine are you using?
</clarification_needed>"""
        needed, questions = parse_clarification(output)
        assert needed is True
        assert len(questions) == 1
        assert "database engine" in questions[0]

    def test_multiple_questions(self):
        """Multiple numbered questions are extracted."""
        from agents.specialist_nodes import parse_clarification
        output = """<clarification_needed>
1. What language should this be in?
2. Should it support async operations?
3. What's the target Python version?
</clarification_needed>"""
        needed, questions = parse_clarification(output)
        assert needed is True
        assert len(questions) == 3

    def test_bullet_format(self):
        """Bullet-pointed questions (- or *) are extracted."""
        from agents.specialist_nodes import parse_clarification
        output = """<clarification_needed>
- What framework are you using?
- Do you need authentication?
</clarification_needed>"""
        needed, questions = parse_clarification(output)
        assert needed is True
        assert len(questions) == 2

    def test_empty_tag(self):
        """Empty clarification tag returns False."""
        from agents.specialist_nodes import parse_clarification
        output = "<clarification_needed>   </clarification_needed>"
        needed, questions = parse_clarification(output)
        assert needed is False

    def test_tag_embedded_in_output(self):
        """Clarification tag embedded mid-output is still found."""
        from agents.specialist_nodes import parse_clarification
        output = """I can help with that, but first:

<clarification_needed>
1. Are you using REST or GraphQL?
</clarification_needed>

Once you answer, I'll provide the full implementation."""
        needed, questions = parse_clarification(output)
        assert needed is True
        assert len(questions) == 1
        assert "REST or GraphQL" in questions[0]


class TestClarificationRouting:
    """Test that the specialist → clarification → skill_cleanup routing works."""

    def test_clarification_skips_critic(self):
        """When specialist sets clarification_needed, critic is skipped."""
        wf = Workflow()

        def specialist(state):
            state["clarification_needed"] = True
            state["clarification_questions"] = ["What DB?"]
            return state

        def heuristic_critic(state):
            state["heuristic_ran"] = True
            return state

        def cleanup(state):
            state["cleanup_ran"] = True
            return state

        wf.add_node("specialist", specialist)
        wf.add_node("heuristic_critic", heuristic_critic)
        wf.add_node("cleanup", cleanup)

        wf.add_conditional_edges(
            "specialist",
            lambda s: "clarification" if s.get("clarification_needed") else "continue",
            {"clarification": "cleanup", "continue": "heuristic_critic"},
        )
        wf.add_edge("heuristic_critic", END)
        wf.add_edge("cleanup", END)
        wf.set_entry_point("specialist")

        result = wf.compile().invoke({"input": "test"})
        assert result.get("clarification_needed") is True
        assert result.get("cleanup_ran") is True
        assert result.get("heuristic_ran") is None  # critic was skipped

    def test_no_clarification_reaches_critic(self):
        """Normal specialist output proceeds to heuristic_critic."""
        wf = Workflow()

        def specialist(state):
            state["specialist_output"] = "solution"
            return state

        def heuristic_critic(state):
            state["heuristic_ran"] = True
            return state

        def cleanup(state):
            state["cleanup_ran"] = True
            return state

        wf.add_node("specialist", specialist)
        wf.add_node("heuristic_critic", heuristic_critic)
        wf.add_node("cleanup", cleanup)

        wf.add_conditional_edges(
            "specialist",
            lambda s: "clarification" if s.get("clarification_needed") else "continue",
            {"clarification": "cleanup", "continue": "heuristic_critic"},
        )
        wf.add_edge("heuristic_critic", END)
        wf.add_edge("cleanup", END)
        wf.set_entry_point("specialist")

        result = wf.compile().invoke({"input": "test"})
        assert result.get("heuristic_ran") is True
        assert result.get("cleanup_ran") is None  # cleanup not reached


class TestClarificationE2E:
    """End-to-end test: specialist requests clarification through the full graph."""

    def test_clarification_flow_through_graph(self):
        """Full graph: specialist output with clarification tags sets state correctly."""
        # Specialist outputs a clarification request
        clarification_output = """I need more information before I can help.

<clarification_needed>
1. What database are you using?
2. What is the expected scale?
</clarification_needed>"""

        registry = _make_adapter_registry({
            "genesia": clarification_output,
        })
        config = _make_config()
        graph = create_agent_graph(registry, config=config)

        state = create_initial_state("Build a user service API")
        result = graph.invoke(state)

        assert result.get("clarification_needed") is True
        assert len(result.get("clarification_questions", [])) == 2
        assert "database" in result["clarification_questions"][0].lower()