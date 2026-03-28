"""
Decision Functions for Workflow Graph

Standalone routing/decision functions used as conditional edges in the
workflow graph. Each function inspects state and returns a string key
that determines the next node to execute.

Also contains the conversational handler for non-code intents.
"""

from typing import List
import logging

from .state import AgentState

logger = logging.getLogger(__name__)


# ===== STATE VALIDATION =====

def _validate_preconditions(
    state: AgentState,
    required_fields: List[str],
    context: str,
) -> None:
    """
    Check that required state fields are set before a decision.

    Logs warnings for missing fields but does not raise exceptions
    to avoid breaking existing workflows. This is observability, not
    enforcement.

    Args:
        state: Current agent state
        required_fields: Field names expected to be present and non-None
        context: Human-readable label for the decision function (for log messages)
    """
    for field in required_fields:
        if field not in state or state.get(field) is None:
            logger.warning(
                f"State validation ({context}): expected '{field}' to be set, "
                f"but it is missing or None. Decision will use default value."
            )


# ===== ROUTING FUNCTIONS =====

def should_approve_output(state: AgentState) -> str:
    """
    Quality gate for specialist output approval (Critic Stage 2).

    Returns:
    - "approved": Output is excellent, deliver to user
    - "refine_output": Output needs improvement, send back to specialist
    - "fail": Max specialist iterations reached or score too low
    """
    _validate_preconditions(
        state,
        ["output_critic_score"],
        "should_approve_output",
    )

    output_score = state.get("output_critic_score", 0)
    specialist_iteration = state.get("specialist_iteration_count", 0)
    max_specialist_iterations = state.get("specialist_max_iterations", 3)
    threshold = state.get("effective_quality_threshold", state.get("quality_threshold", 85))

    # Increment specialist iteration count
    state["specialist_iteration_count"] = specialist_iteration + 1

    _extra = {"gate": "output", "score": output_score, "threshold": threshold,
              "iteration": state["specialist_iteration_count"], "max_iterations": max_specialist_iterations}


    # Success: output meets threshold
    if output_score >= threshold:
        logger.info(f"Output APPROVED (score={output_score}, threshold={threshold})",
                     extra={**_extra, "decision": "approved"})
        state["quality_gate_decision"] = "pass"
        return "approved"

    # Max specialist iterations reached
    if state["specialist_iteration_count"] >= max_specialist_iterations:
        logger.warning(f"Max specialist iterations reached ({max_specialist_iterations})",
                        extra={**_extra, "decision": "fail", "reason": "max_iterations"})
        state["quality_gate_decision"] = "max_iterations"
        return "fail"

    # Score too low to bother refining
    if output_score < 60:
        logger.warning(f"Output score too low (score={output_score})",
                        extra={**_extra, "decision": "fail", "reason": "score_too_low"})
        state["quality_gate_decision"] = "fail"
        return "fail"

    # Refinable: score between 60-85
    logger.info(f"Refining output (score={output_score}, iteration={state['specialist_iteration_count']}/{max_specialist_iterations})",
                 extra={**_extra, "decision": "refine_output"})
    state["quality_gate_decision"] = "refine"
    return "refine_output"


# ===== SUB-TASK ROUTING FUNCTIONS =====

def should_approve_sub_specification(state: AgentState) -> str:
    """
    Quality gate for sub-task specification approval.

    Returns:
    - "approved": Sub-spec is complete, execute sub-task
    - "refine_sub_spec": Sub-spec incomplete, refine it
    - "fail": Max iterations or score too low, skip this sub-task
    """
    sub_tasks = state.get("sub_tasks", [])
    current_index = state.get("current_sub_task_index", 0)

    if current_index >= len(sub_tasks):
        logger.warning(
            f"State validation (should_approve_sub_specification): "
            f"current_sub_task_index ({current_index}) >= len(sub_tasks) ({len(sub_tasks)})"
        )
        return "fail"

    current_subtask = sub_tasks[current_index]
    spec_score = current_subtask.get("spec_score", 0)
    iteration = current_subtask.get("iteration_count", 0)
    max_iterations = current_subtask.get("max_iterations", 3)
    threshold = state.get("effective_quality_threshold", state.get("quality_threshold", 85))

    # Increment iteration
    current_subtask["iteration_count"] = iteration + 1
    sub_tasks[current_index] = current_subtask
    state["sub_tasks"] = sub_tasks

    # Approved
    if spec_score >= threshold:
        logger.info(f"✅ Sub-spec {current_index} APPROVED (score={spec_score})")
        return "approved"

    # Max iterations
    if current_subtask["iteration_count"] >= max_iterations:
        logger.warning(f"⚠️ Sub-spec {current_index} max iterations reached")
        return "fail"

    # Too low
    if spec_score < 60:
        logger.warning(f"❌ Sub-spec {current_index} score too low ({spec_score})")
        return "fail"

    # Refinable
    logger.info(f"🔄 Refining sub-spec {current_index} (score={spec_score})")
    return "refine_sub_spec"


def should_approve_sub_output(state: AgentState) -> str:
    """
    Quality gate for sub-task output approval.

    Returns:
    - "approved": Output is good, mark sub-task complete
    - "refine_sub_output": Output needs work, refine it
    - "fail": Max iterations or score too low, mark sub-task failed
    """
    sub_tasks = state.get("sub_tasks", [])
    current_index = state.get("current_sub_task_index", 0)

    if current_index >= len(sub_tasks):
        logger.warning(
            f"State validation (should_approve_sub_output): "
            f"current_sub_task_index ({current_index}) >= len(sub_tasks) ({len(sub_tasks)})"
        )
        return "fail"

    current_subtask = sub_tasks[current_index]
    output_score = current_subtask.get("output_score", 0)
    iteration = current_subtask.get("iteration_count", 0)
    max_iterations = current_subtask.get("max_iterations", 3)
    threshold = state.get("effective_quality_threshold", state.get("quality_threshold", 85))

    # Increment iteration count upfront (consistent with other decision functions)
    current_subtask["iteration_count"] = iteration + 1
    sub_tasks[current_index] = current_subtask
    state["sub_tasks"] = sub_tasks

    # Approved
    if output_score >= threshold:
        logger.info(f"✅ Sub-output {current_index} APPROVED (score={output_score})")
        current_subtask["status"] = "completed"
        sub_tasks[current_index] = current_subtask
        state["sub_tasks"] = sub_tasks
        return "approved"

    # Max iterations
    if current_subtask["iteration_count"] >= max_iterations:
        logger.warning(f"⚠️ Sub-output {current_index} max iterations reached")
        current_subtask["status"] = "failed"
        sub_tasks[current_index] = current_subtask
        state["sub_tasks"] = sub_tasks
        return "fail"

    # Too low
    if output_score < 60:
        logger.warning(f"❌ Sub-output {current_index} score too low ({output_score})")
        current_subtask["status"] = "failed"
        sub_tasks[current_index] = current_subtask
        state["sub_tasks"] = sub_tasks
        return "fail"

    # Refinable
    logger.info(f"🔄 Refining sub-output {current_index} (score={output_score}, iteration={current_subtask['iteration_count']}/{max_iterations})")
    return "refine_sub_output"


def should_use_llm_critic(state: AgentState) -> str:
    """
    Decide whether to run the LLM output critic or approve based on
    the heuristic critic result.

    Returns:
    - "approve": Heuristic passed — skip LLM critic, go to format
    - "critic_output": Heuristic failed — fall through to LLM critic
    """
    if state.get("heuristic_critic_passed", False):
        score = state.get("heuristic_critic_score", 0)
        logger.info(f"Heuristic critic passed (score={score}) — skipping LLM critic")
        # Set output scores so downstream nodes (format, cleanup) see values
        state["output_critic_score"] = score
        state["output_critic_feedback"] = "Approved by heuristic critic."
        state["quality_gate_decision"] = "pass"
        return "approve"

    logger.info("Heuristic critic did not pass — routing to LLM critic")
    return "critic_output"


def has_more_subtasks(state: AgentState) -> str:
    """
    Check if there are more sub-tasks to process.

    Returns:
    - "more": There are more sub-tasks, continue loop
    - "done": All sub-tasks processed, proceed to aggregation
    """
    sub_tasks = state.get("sub_tasks", [])
    current_index = state.get("current_sub_task_index", 0)
    completed = state.get("completed_sub_tasks", 0)

    # Handle empty sub-tasks list (edge case: router created 0 sub-tasks)
    if not sub_tasks:
        logger.warning("⚠️ No sub-tasks to process (empty list)")
        return "done"

    # Check if current sub-task is complete
    if current_index < len(sub_tasks):
        current_subtask = sub_tasks[current_index]
        if current_subtask.get("status") in ["completed", "failed"]:
            # Move to next sub-task
            state["current_sub_task_index"] = current_index + 1
            if current_subtask.get("status") == "completed":
                state["completed_sub_tasks"] = completed + 1
            logger.info(f"Moving to sub-task {current_index + 1}/{len(sub_tasks)}")

    # Check if we're done
    if state["current_sub_task_index"] >= len(sub_tasks):
        logger.info(f"✅ All sub-tasks processed ({state['completed_sub_tasks']}/{len(sub_tasks)} completed)")
        return "done"

    logger.info(f"📋 Processing sub-task {state['current_sub_task_index']}/{len(sub_tasks)}")
    return "more"


def should_decompose(state: AgentState) -> str:
    """
    Check if task requires decomposition into sub-tasks.

    Returns:
    - "decompose": Multi-specialist workflow required
    - "single": Single-specialist workflow
    """
    requires_decomposition = state.get("requires_decomposition", False)

    if requires_decomposition:
        sub_tasks = state.get("sub_tasks", [])
        if not sub_tasks:
            logger.warning(
                "State validation (should_decompose): requires_decomposition=True "
                "but sub_tasks is empty. Falling back to single-specialist workflow."
            )
            return "single"
        logger.info("🔀 Multi-specialist workflow activated")
        return "decompose"
    else:
        logger.info("➡️ Single-specialist workflow")
        return "single"


