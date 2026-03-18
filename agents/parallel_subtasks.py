"""
Parallel Sub-Task Execution.

When the router marks a decomposed workflow as `parallel_execution=True`,
this module runs all sub-tasks concurrently using a thread pool instead of
the sequential graph loop.

Each thread executes the specialist → output critic pipeline with inline
refinement loops on an **isolated copy of state**.  The existing node functions work unmodified because each
thread's private state has `sub_tasks = [single_subtask]` with
`current_sub_task_index = 0`.

After all threads complete, results are merged back into the main state
and execution continues to the aggregator.
"""

import copy
import concurrent.futures
import logging
import time
from typing import Any, Dict, List, Optional

from .state import AgentState

logger = logging.getLogger(__name__)


def _build_local_state(
    sub_task_dict: Dict[str, Any],
    shared_context: Dict[str, Any],
) -> AgentState:
    """
    Build an isolated AgentState for a single sub-task thread.

    The key trick: existing node functions read `sub_tasks[current_sub_task_index]`.
    By setting `sub_tasks = [sub_task_copy]` and `current_sub_task_index = 0`,
    they operate on this single sub-task without modification.
    """
    sub_task_copy = copy.deepcopy(sub_task_dict)

    local_state: AgentState = {  # type: ignore[typeddict-item]
        # Single sub-task in a list
        "sub_tasks": [sub_task_copy],
        "current_sub_task_index": 0,
        "completed_sub_tasks": 0,

        # Read-only context from the parent state
        "user_request": shared_context["user_request"],
        "specification": shared_context["specification"],
        "loaded_skills": shared_context["loaded_skills"],
        "memory_context": shared_context["memory_context"],
        "quality_threshold": shared_context["quality_threshold"],
        "parallel_execution": True,

        # Fields nodes may read/write
        "specialist_iteration_count": 0,
        "specialist_max_iterations": shared_context.get("specialist_max_iterations", 3),
        "iteration_count": 0,
        "max_iterations": shared_context.get("max_iterations", 3),
        "adapters_used": [],
        "tool_calls_made": [],
        "session_id": shared_context.get("session_id", ""),
    }
    return local_state


def run_single_subtask(
    sub_task_index: int,
    nodes: Any,
    shared_context: Dict[str, Any],
    sub_task_dict: Dict[str, Any],
    quality_threshold: int = 85,
    max_iterations: int = 3,
) -> Dict[str, Any]:
    """
    Execute a single sub-task's full pipeline in the current thread.

    Phases:
        1. Specialist execution + output critic (with refinement loop)

    Args:
        sub_task_index: Original index in the parent sub_tasks list (for logging).
        nodes: AgentNodes instance.
        shared_context: Read-only context from parent state.
        sub_task_dict: The sub-task dict to process.
        quality_threshold: Score needed to pass quality gates.
        max_iterations: Max refinement iterations per phase.

    Returns:
        The completed sub-task dict with status, output, scores, etc.
    """
    from .decision_functions import (
        should_approve_sub_output,
    )

    local_state = _build_local_state(sub_task_dict, shared_context)
    task_type = sub_task_dict.get("task_type", "unknown")
    sub_max_iter = sub_task_dict.get("max_iterations", max_iterations)

    logger.info(f"[Parallel] Starting sub-task {sub_task_index}: {task_type}")

    # ── Specialist execution + output critic loop ──

    for output_attempt in range(sub_max_iter):
        local_state = nodes.execute_sub_task(local_state)

        # If the specialist requested clarification, stop immediately —
        # don't waste an LLM call on the critic.
        if local_state.get("clarification_needed"):
            logger.info(
                f"[Parallel] Sub-task {sub_task_index} requested clarification "
                f"(attempt {output_attempt + 1})"
            )
            break

        local_state = nodes.evaluate_sub_output(local_state)

        decision = should_approve_sub_output(local_state)

        if decision in ("approved", "fail"):
            logger.info(
                f"[Parallel] Sub-task {sub_task_index} output {decision} "
                f"(attempt {output_attempt + 1})"
            )
            break
        # decision == "refine_sub_output" → loop

    result = local_state["sub_tasks"][0]
    logger.info(
        f"[Parallel] Sub-task {sub_task_index} finished: "
        f"status={result.get('status')}, score={result.get('output_score', 0)}"
    )
    return result


def execute_parallel_subtasks(
    state: AgentState,
    nodes: Any,
    config: Any = None,
) -> AgentState:
    """
    Graph node: execute all sub-tasks in parallel using ThreadPoolExecutor.

    Replaces the sequential sub-task loop when `parallel_execution=True`.
    Each sub-task runs in its own thread with an isolated state copy.

    Args:
        state: Current AgentState with sub_tasks populated by router.
        nodes: AgentNodes instance.
        config: SystemConfig for reading parallel settings.

    Returns:
        Updated AgentState with all sub-tasks completed, ready for aggregator.
    """
    sub_tasks = state.get("sub_tasks", [])

    if not sub_tasks:
        logger.warning("[Parallel] No sub-tasks to execute")
        return state

    # Read config
    max_workers = 4
    subtask_timeout = 300
    if config and hasattr(config, "workflow"):
        max_workers = getattr(config.workflow, "parallel_max_workers", 4)
        subtask_timeout = getattr(config.workflow, "parallel_subtask_timeout", 300)

    quality_threshold = state.get("quality_threshold", 85)
    max_iterations = state.get("max_iterations", 3)

    # Build shared read-only context
    shared_context = {
        "user_request": state.get("user_request", ""),
        "specification": state.get("specification", ""),
        "loaded_skills": state.get("loaded_skills", []),
        "memory_context": state.get("memory_context", ""),
        "quality_threshold": quality_threshold,
        "max_iterations": max_iterations,
        "specialist_max_iterations": state.get("specialist_max_iterations", 3),
        "session_id": state.get("session_id", ""),
    }

    logger.info(
        f"[Parallel] Launching {len(sub_tasks)} sub-tasks "
        f"(max_workers={max_workers}, timeout={subtask_timeout}s)"
    )
    start_time = time.monotonic()

    # Map future → original index
    errors: List[Dict[str, Any]] = []
    results: Dict[int, Dict[str, Any]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {}
        for idx, st in enumerate(sub_tasks):
            future = executor.submit(
                run_single_subtask,
                sub_task_index=idx,
                nodes=nodes,
                shared_context=shared_context,
                sub_task_dict=st,
                quality_threshold=quality_threshold,
                max_iterations=max_iterations,
            )
            future_to_index[future] = idx

        try:
            for future in concurrent.futures.as_completed(
                future_to_index, timeout=subtask_timeout + 30
            ):
                idx = future_to_index[future]
                try:
                    result = future.result()
                    results[idx] = result
                except Exception as e:
                    logger.error(
                        f"[Parallel] Sub-task {idx} raised {type(e).__name__}: {e}",
                        exc_info=True,
                    )
                    errors.append({
                        "sub_task_index": idx,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "task_type": sub_tasks[idx].get("task_type", "unknown"),
                    })
                    failed_copy = copy.deepcopy(sub_tasks[idx])
                    failed_copy["status"] = "failed"
                    results[idx] = failed_copy
        except concurrent.futures.TimeoutError:
            # as_completed timed out — collect whatever we have so far
            for future, idx in future_to_index.items():
                if idx not in results:
                    logger.error(f"[Parallel] Sub-task {idx} timed out after {subtask_timeout}s")
                    errors.append({
                        "sub_task_index": idx,
                        "error": "timeout",
                        "task_type": sub_tasks[idx].get("task_type", "unknown"),
                    })
                    failed_copy = copy.deepcopy(sub_tasks[idx])
                    failed_copy["status"] = "failed"
                    results[idx] = failed_copy

    # Merge results back into state
    for idx in range(len(sub_tasks)):
        if idx in results:
            sub_tasks[idx] = results[idx]

    completed_count = sum(
        1 for st in sub_tasks if st.get("status") == "completed"
    )

    # Check if any sub-task requested clarification — propagate to parent
    all_clarification_questions: List[str] = []
    for st in sub_tasks:
        if st.get("status") == "clarification_needed":
            # The sub-task specialist's output may contain questions
            # parsed by execute_sub_task; collect from the local state.
            # Since threads use isolated states, we stored questions in
            # the sub-task dict's output — re-parse them here.
            from .specialist_nodes import parse_clarification
            _, qs = parse_clarification(st.get("output", ""))
            all_clarification_questions.extend(qs)

    if all_clarification_questions:
        state["clarification_needed"] = True
        state["clarification_questions"] = all_clarification_questions

    elapsed = time.monotonic() - start_time
    logger.info(
        f"[Parallel] All sub-tasks finished in {elapsed:.1f}s: "
        f"{completed_count}/{len(sub_tasks)} completed"
    )

    state["sub_tasks"] = sub_tasks
    state["completed_sub_tasks"] = completed_count
    state["current_sub_task_index"] = len(sub_tasks)
    state["parallel_execution_errors"] = errors

    return state
