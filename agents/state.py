"""
State Definition for Multi-Agent System

This module defines the state object that flows through the workflow graph,
tracking all context, outputs, and metadata across iterations.
"""

from typing import TypedDict, Literal, Optional, Dict, List, Any
from datetime import datetime

# Maximum number of conversation history entries to retain.
# Covers aggressive quality configs (5 iterations × 2 cycles) with headroom.
MAX_HISTORY_ENTRIES = 10


class AgentState(TypedDict, total=False):
    """
    State object that flows through the workflow graph.

    Using total=False allows optional fields, but we mark required ones
    in the docstrings.
    """

    # ===== INPUT (Required) =====
    user_request: str  # Original user request
    session_id: str  # Unique session identifier

    # ===== INTENT CLASSIFICATION (Before Routing) =====
    intent: Literal["conversational", "research", "planning", "code_generation"]
    intent_confidence: float  # 0.0-1.0 confidence in intent classification

    # ===== ROUTING & CLASSIFICATION =====
    task_type: Literal["code", "creative", "research", "general"]

    # ===== ITERATION TRACKING =====
    iteration_count: int  # Current iteration number (starts at 0)
    max_iterations: int  # Maximum allowed iterations (default: 3)

    # ===== VIBE NODE OUTPUT (Specification Builder) =====
    specification: str  # Detailed specification/prompt for specialist
    clarification_needed: bool  # Does Vibe need more info from user?
    clarification_questions: List[str]  # Questions to ask user

    # ===== CRITIC STAGE 1 (Specification Validation) =====
    spec_critic_scores: Dict[str, int]  # Scores for specification quality
    spec_critic_score: int  # Overall specification score (0-100)
    spec_critic_feedback: str  # Feedback on specification completeness

    # ===== ROUTER NODE OUTPUT =====
    routed_task_type: str  # Classified task (e.g., "test_generation", "security_audit")
    specialist_adapter: str  # Which specialist adapter to use
    routing_confidence: float  # Router's confidence in classification

    # ===== SKILL MANAGEMENT (Three-Tier System) =====
    discovered_skills: List[Dict[str, Any]]  # Skills found during routing
    skills_in_use: List[str]  # Active skill names being used
    skill_quality_scores: Dict[str, int]  # Quality scores for each skill (0-100)
    loaded_skills: List[Dict[str, Any]]  # Loaded skill content (SKILL.md)
    skills_cleaned_up: bool  # Cleanup complete marker
    # Workspace tier: project-specific skills, cleared after each task
    workspace_dir: Optional[str]       # Project repo being worked on (scanned for skills/)
    skill_repo_dirs: List[str]         # Additional repos to scan for skills

    # ===== MULTI-SPECIALIST TASK DECOMPOSITION =====
    requires_decomposition: bool  # Does task require multiple specialists?
    sub_tasks: List[Dict[str, Any]]  # List of sub-tasks for multi-specialist workflows
    current_sub_task_index: int  # Index of currently executing sub-task
    completed_sub_tasks: int  # Number of completed sub-tasks
    parallel_execution: bool  # Can sub-tasks execute in parallel?
    aggregation_strategy: str  # How to combine outputs: "merge", "sequential", "report"

    # ===== SPECIALIST NODE OUTPUT =====
    specialist_output: str  # Output from specialist adapter
    specialist_iteration_count: int  # Iterations at specialist level
    specialist_max_iterations: int  # Max iterations for specialist (default: 3)

    # ===== CRITIC STAGE 2 (Output Validation) =====
    output_critic_scores: Dict[str, int]  # Scores for specialist output
    output_critic_score: int  # Overall output score (0-100)
    output_critic_feedback: str  # Feedback on output quality

    # ===== AGGREGATION (Multi-Specialist) =====
    aggregated_output: str  # Combined output from all specialists
    final_aggregation_score: int  # Final critic score for aggregated output

    # Legacy fields (for backwards compatibility)
    current_output: str  # Alias for specialist_output
    critic_scores: Dict[str, int]  # Alias for output_critic_scores
    critic_score: int  # Alias for output_critic_score
    critic_feedback: str  # Alias for output_critic_feedback

    # ===== QUALITY GATE DECISION =====
    quality_gate_decision: Literal["pass", "refine", "fail", "max_iterations"]
    quality_threshold: int  # Score needed to pass (default: 85)

    # ===== COMPLEXITY TIERING =====
    complexity_tier: str  # "fast" | "standard" | "full"
    effective_quality_threshold: int  # Tier-adjusted quality threshold
    heuristic_critic_score: int  # Heuristic critic score (0-100)
    heuristic_critic_passed: bool  # Whether heuristic approved output

    # ===== MEMORY CONTEXT (auto-injected) =====
    memory_context: str  # Relevant memories auto-injected for the specialist
    pending_messages: List[Dict[str, Any]]  # Raw message dicts from MessageStore

    # ===== TOOL CALLING =====
    tool_calls_made: List[Dict[str, Any]]  # History of tool executions

    # ===== CONVERSATION HISTORY =====
    # Stores summarized context from previous iterations
    conversation_history: List[Dict[str, Any]]

    # ===== OUTPUT & METADATA =====
    final_output: str  # The output that passed quality gate
    final_score: int  # Final critic score
    mattermost_message: str  # Formatted message for Mattermost posting
    mattermost_message_id: Optional[str]  # Posted message ID

    # ===== RESULT CACHE =====
    cache_hit: bool  # True if specialist output was served from cache
    cache_key: str  # SHA-256 hash used for cache lookup
    cache_entry_stored: bool  # True if this run's result was written to cache

    # ===== PARALLEL EXECUTION =====
    parallel_execution_errors: List[Dict[str, Any]]  # Errors from parallel sub-tasks

    # Timing information
    start_time: str
    end_time: Optional[str]
    total_time_seconds: Optional[float]

    # Adapter tracking
    adapters_used: List[str]  # Track which adapters were loaded
    current_adapter: Optional[str]  # Currently active adapter

    # Debug/logging
    debug_info: Dict[str, Any]


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
        # Aggregator needs: sub-tasks, strategy, and the original request
        context["sub_tasks"] = state.get("sub_tasks", [])
        context["aggregation_strategy"] = state.get("aggregation_strategy", "merge")
        context["specification"] = state.get("specification", "")

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
