"""
State Definition for Multi-Agent System

This module defines the state object that flows through the workflow graph,
tracking all context, outputs, and metadata across iterations.

Fields are organized into semantic groups via TypedDict inheritance.
AgentState composes all groups, so existing state["field"] access is unchanged.
"""

from typing import TypedDict, Literal, Optional, Dict, List, Any
from datetime import datetime

# Maximum number of conversation history entries to retain.
# Covers aggressive quality configs (5 iterations × 2 cycles) with headroom.
MAX_HISTORY_ENTRIES = 10


# ===== Semantic State Groups =====
# Each group is a TypedDict that AgentState inherits from.
# This provides IDE navigation and logical grouping without
# breaking any existing flat-key access patterns.


class InputState(TypedDict, total=False):
    """Required input fields."""

    user_request: str  # Original user request
    session_id: str  # Unique session identifier


class IntentState(TypedDict, total=False):
    """Intent classification (before routing)."""

    intent: Literal["conversational", "research", "planning", "code_generation"]
    intent_confidence: float  # 0.0-1.0 confidence in intent classification


class RoutingState(TypedDict, total=False):
    """Routing and task classification."""

    task_type: Literal["code", "creative", "research", "general"]
    routed_task_type: str  # Classified task (e.g., "test_generation", "security_audit")
    specialist_adapter: str  # Which specialist adapter to use
    routing_confidence: float  # Router's confidence in classification


class IterationState(TypedDict, total=False):
    """Iteration tracking."""

    iteration_count: int  # Current iteration number (starts at 0)
    max_iterations: int  # Maximum allowed iterations (default: 3)


class SpecState(TypedDict, total=False):
    """Specification builder output."""

    specification: str  # Detailed specification/prompt for specialist
    clarification_needed: bool  # Does Vibe need more info from user?
    clarification_questions: List[str]  # Questions to ask user


class SpecCriticState(TypedDict, total=False):
    """Critic stage 1 — specification validation."""

    spec_critic_scores: Dict[str, int]  # Scores for specification quality
    spec_critic_score: int  # Overall specification score (0-100)
    spec_critic_feedback: str  # Feedback on specification completeness


class SkillState(TypedDict, total=False):
    """Skill management (three-tier system)."""

    discovered_skills: List[Dict[str, Any]]  # Skills found during routing
    skills_in_use: List[str]  # Active skill names being used
    skill_quality_scores: Dict[str, int]  # Quality scores for each skill (0-100)
    loaded_skills: List[Dict[str, Any]]  # Loaded skill content (SKILL.md)
    skills_cleaned_up: bool  # Cleanup complete marker
    workspace_dir: Optional[str]  # Project repo being worked on (scanned for skills/)
    skill_repo_dirs: List[str]  # Additional repos to scan for skills


class DecompositionState(TypedDict, total=False):
    """Multi-specialist task decomposition."""

    requires_decomposition: bool  # Does task require multiple specialists?
    sub_tasks: List[Dict[str, Any]]  # List of sub-tasks for multi-specialist workflows
    current_sub_task_index: int  # Index of currently executing sub-task
    completed_sub_tasks: int  # Number of completed sub-tasks
    parallel_execution: bool  # Can sub-tasks execute in parallel?
    aggregation_strategy: str  # How to combine outputs: "merge", "sequential", "report"


class SpecialistOutputState(TypedDict, total=False):
    """Specialist node output."""

    specialist_output: str  # Output from specialist adapter
    specialist_iteration_count: int  # Iterations at specialist level
    specialist_max_iterations: int  # Max iterations for specialist (default: 3)


class OutputCriticState(TypedDict, total=False):
    """Critic stage 2 — output validation."""

    output_critic_scores: Dict[str, int]  # Scores for specialist output
    output_critic_score: int  # Overall output score (0-100)
    output_critic_feedback: str  # Feedback on output quality


class AggregationState(TypedDict, total=False):
    """Aggregation (multi-specialist)."""

    aggregated_output: str  # Combined output from all specialists
    final_aggregation_score: int  # Final critic score for aggregated output


class LegacyAliasState(TypedDict, total=False):
    """Legacy fields (for backwards compatibility)."""

    current_output: str  # Alias for specialist_output
    critic_scores: Dict[str, int]  # Alias for output_critic_scores
    critic_score: int  # Alias for output_critic_score
    critic_feedback: str  # Alias for output_critic_feedback


class QualityGateState(TypedDict, total=False):
    """Quality gate decision."""

    quality_gate_decision: Literal["pass", "refine", "fail", "max_iterations"]
    quality_threshold: int  # Score needed to pass (default: 85)


class ComplexityState(TypedDict, total=False):
    """Complexity tiering."""

    complexity_tier: str  # "fast" | "standard" | "full"
    effective_quality_threshold: int  # Tier-adjusted quality threshold
    heuristic_critic_score: int  # Heuristic critic score (0-100)
    heuristic_critic_passed: bool  # Whether heuristic approved output


class MemoryState(TypedDict, total=False):
    """Memory context (auto-injected)."""

    memory_context: str  # Relevant memories auto-injected for the specialist
    pending_messages: List[Dict[str, Any]]  # Raw message dicts from MessageStore


class ToolState(TypedDict, total=False):
    """Tool calling."""

    tool_calls_made: List[Dict[str, Any]]  # History of tool executions


class CacheState(TypedDict, total=False):
    """Result cache."""

    cache_hit: bool  # True if specialist output was served from cache
    cache_key: str  # SHA-256 hash used for cache lookup
    cache_entry_stored: bool  # True if this run's result was written to cache


class UpgradeState(TypedDict, total=False):
    """Self-upgrade."""

    upgrade_signals: List[Dict[str, Any]]  # Signals detected this run
    upgrade_proposal_ready: bool  # True if enough signals accumulated
    upgrade_proposal_description: str  # What should be upgraded
    upgrade_applied: bool  # True if an upgrade was successfully committed
    upgrade_branch: str  # Git branch name of the upgrade
    upgrade_commit: str  # Git commit hash of the upgrade
    upgrade_errors: List[str]  # Errors from pipeline execution


class SimulationState(TypedDict, total=False):
    """Simulation (MiroFish-inspired integration prediction)."""

    simulation_report: Optional[str]  # Integration risk report from sidecar sim
    simulation_conflicts: List[Dict[str, str]]  # Structured [{level, description}]
    simulation_risk_level: str  # "low", "medium", "high"
    simulation_skipped: bool  # True if hardware/config gated out


class OutputMetadataState(TypedDict, total=False):
    """Output, metadata, and timing."""

    final_output: str  # The output that passed quality gate
    final_score: int  # Final critic score
    mattermost_message: str  # Formatted message for Mattermost posting
    mattermost_message_id: Optional[str]  # Posted message ID
    conversation_history: List[Dict[str, Any]]  # Summarized context from prior iterations
    parallel_execution_errors: List[Dict[str, Any]]  # Errors from parallel sub-tasks
    start_time: str
    end_time: Optional[str]
    total_time_seconds: Optional[float]
    adapters_used: List[str]  # Track which adapters were loaded
    current_adapter: Optional[str]  # Currently active adapter
    debug_info: Dict[str, Any]


class AgentState(
    InputState,
    IntentState,
    RoutingState,
    IterationState,
    SpecState,
    SpecCriticState,
    SkillState,
    DecompositionState,
    SpecialistOutputState,
    OutputCriticState,
    AggregationState,
    LegacyAliasState,
    QualityGateState,
    ComplexityState,
    MemoryState,
    ToolState,
    CacheState,
    UpgradeState,
    SimulationState,
    OutputMetadataState,
    total=False,
):
    """
    State object that flows through the workflow graph.

    Composes all semantic state groups via TypedDict inheritance.
    All existing state["field"] access patterns continue to work unchanged.
    """

    pass


class ConversationHistoryEntry(TypedDict):
    """Structure for conversation history entries."""
    iteration: int
    specification: str
    output: str
    score: int
    feedback_summary: str  # Condensed version of critic feedback
    timestamp: str


def create_initial_state(
    user_request: str,
    session_id: Optional[str] = None,
    max_iterations: int = 3,
    quality_threshold: int = 85
) -> AgentState:
    """
    Create initial state object from user request.

    Args:
        user_request: The user's input request
        session_id: Optional session ID (generated if not provided)
        max_iterations: Maximum refinement iterations allowed
        quality_threshold: Minimum score to pass quality gate

    Returns:
        Initialized AgentState
    """
    if session_id is None:
        session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    return AgentState(
        # Input
        user_request=user_request,
        session_id=session_id,

        # Iteration tracking
        iteration_count=0,
        max_iterations=max_iterations,
        quality_threshold=quality_threshold,

        # Clarification (specialist can request more context from user)
        clarification_needed=False,
        clarification_questions=[],

        # Quality gate
        quality_gate_decision="refine",  # Start in refinement mode

        # Skill management
        discovered_skills=[],
        skills_in_use=[],
        skill_quality_scores={},
        loaded_skills=[],
        skills_cleaned_up=False,

        # Specialist iteration tracking
        specialist_iteration_count=0,
        specialist_max_iterations=3,

        # Multi-specialist decomposition
        requires_decomposition=False,
        sub_tasks=[],
        current_sub_task_index=0,
        completed_sub_tasks=0,
        parallel_execution=False,
        aggregation_strategy="merge",

        # Result cache
        cache_hit=False,
        cache_key="",
        cache_entry_stored=False,

        # Parallel execution
        parallel_execution_errors=[],

        # Simulation
        simulation_report=None,
        simulation_conflicts=[],
        simulation_risk_level="",
        simulation_skipped=False,

        # Complexity tiering
        complexity_tier="",
        effective_quality_threshold=quality_threshold,
        heuristic_critic_score=0,
        heuristic_critic_passed=False,

        # Memory context (auto-injected before specialist)
        memory_context="",
        pending_messages=[],

        # History
        conversation_history=[],
        adapters_used=[],
        tool_calls_made=[],  # Track tool executions

        # Timing
        start_time=datetime.now().isoformat(),

        # Debug
        debug_info={}
    )


def add_to_history(state: AgentState) -> AgentState:
    """
    Add current iteration to conversation history.

    This creates a summarized entry to prevent context explosion.
    """
    if state.get("current_output") and state.get("critic_score") is not None:
        entry = ConversationHistoryEntry(
            iteration=state["iteration_count"],
            specification=state.get("specification", "")[:200] + "...",  # Truncate
            output=state.get("current_output", "")[:300] + "...",  # Truncate
            score=state["critic_score"],
            feedback_summary=_summarize_feedback(state.get("critic_feedback", "")),
            timestamp=datetime.now().isoformat()
        )

        history = state.get("conversation_history", [])
        history.append(entry)  # type: ignore[arg-type]
        # Truncate to keep only the most recent entries
        if len(history) > MAX_HISTORY_ENTRIES:
            history = history[-MAX_HISTORY_ENTRIES:]
        state["conversation_history"] = history

    return state


def _summarize_feedback(feedback: str, max_length: int = 150) -> str:
    """Summarize critic feedback to save context."""
    if len(feedback) <= max_length:
        return feedback

    # Try to get the first sentence or key points
    sentences = feedback.split('. ')
    if sentences:
        return sentences[0] + "..."

    return feedback[:max_length] + "..."


def get_context_for_node(state: AgentState, node_name: str) -> Dict[str, Any]:
    """
    Get relevant context for a specific node.

    This implements selective context passing to avoid token bloat.
    Each node gets only the state slices it needs to do its job well.

    Args:
        state: Current state
        node_name: Name of the node requesting context

    Returns:
        Dictionary with relevant context for that node
    """
    # Base context everyone gets
    context = {
        "user_request": state["user_request"],
        "iteration": state.get("iteration_count", 0),
        "session_id": state.get("session_id", "")
    }

    if node_name == "executor":
        # Executor needs the specification and task classification
        context["specification"] = state.get("specification", "")
        context["task_type"] = state.get("task_type", "general")

    elif node_name == "specialist":
        # Specialist needs: specification, routing info, spec validation, skills
        context["specification"] = state.get("specification", "")
        context["routed_task_type"] = state.get("routed_task_type", "general")
        context["specialist_adapter"] = state.get("specialist_adapter", "")
        context["routing_confidence"] = state.get("routing_confidence", 0)
        context["spec_critic_score"] = state.get("spec_critic_score", 0)
        context["loaded_skills"] = state.get("loaded_skills", [])
        # Include previous output/feedback if refining
        if state.get("specialist_iteration_count", 0) > 0:
            context["specialist_output"] = state.get("specialist_output", "")
            context["output_critic_feedback"] = state.get("output_critic_feedback", "")
            context["output_critic_score"] = state.get("output_critic_score", 0)

    elif node_name == "critic":
        # Critic needs: specification, output, task type for domain-specific evaluation
        context["specification"] = state.get("specification", "")
        context["generated_output"] = state.get("current_output", "")
        context["routed_task_type"] = state.get("routed_task_type", "general")
        context["specialist_adapter"] = state.get("specialist_adapter", "")

    elif node_name == "refinement":
        # Refinement needs: critique + specialist type for targeted improvement plans
        context["critic_score"] = state.get("critic_score", 0)
        context["critic_scores"] = state.get("critic_scores", {})
        context["critic_feedback"] = state.get("critic_feedback", "")
        context["current_output"] = state.get("current_output", "")
        context["iterations_remaining"] = state.get("max_iterations", 3) - state.get("iteration_count", 0)
        context["specialist_adapter"] = state.get("specialist_adapter", "")
        context["routed_task_type"] = state.get("routed_task_type", "general")

    elif node_name == "router":
        # Router needs: specification + task classification
        context["specification"] = state.get("specification", "")
        context["task_type"] = state.get("task_type", "general")
        context["spec_critic_score"] = state.get("spec_critic_score", 0)

    elif node_name == "aggregator":
        # Aggregator needs: sub-tasks, strategy, original request, and simulation
        context["sub_tasks"] = state.get("sub_tasks", [])
        context["aggregation_strategy"] = state.get("aggregation_strategy", "merge")
        context["specification"] = state.get("specification", "")
        context["simulation_report"] = state.get("simulation_report")
        context["simulation_conflicts"] = state.get("simulation_conflicts", [])
        context["simulation_risk_level"] = state.get("simulation_risk_level", "")
        context["simulation_skipped"] = state.get("simulation_skipped", True)

    return context


def finalize_state(state: AgentState) -> AgentState:
    """
    Finalize state after workflow completion.

    Adds timing information and prepares for output.

    Args:
        state: Current agent state
                       (actual cleanup happens in separate node)

    Returns:
        Finalized agent state
    """
    state["end_time"] = datetime.now().isoformat()

    # Calculate total time
    start = datetime.fromisoformat(state["start_time"])
    end = datetime.fromisoformat(state["end_time"])  # type: ignore[arg-type]
    state["total_time_seconds"] = (end - start).total_seconds()

    # Set final output and score
    state["final_output"] = state.get("current_output", "")
    state["final_score"] = state.get("critic_score", 0)

    return state
