"""
Workflow Runner Utilities

Standalone functions for executing and inspecting compiled workflows.
Extracted from graph.py to keep the graph builder focused on wiring.
"""

import logging
from typing import Optional

from .state import AgentState, create_initial_state, finalize_state
from .graph_engine import (
    CompiledWorkflow,
    WorkflowRecursionError,
    NodeTimeoutError,
    WorkflowTimeoutError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "print_graph_structure",
    "run_workflow",
    "stream_workflow",
]


def print_graph_structure(app=None):
    """
    Print text representation of graph structure.
    """
    print("\n" + "="*80)
    print("MULTI-AGENT WORKFLOW STRUCTURE (Skill-Driven Architecture)")
    print("="*80 + "\n")

    print("NODES:")
    print("  Pipeline:")
    print("    1. router              - Classify task type & set specification")
    print("    2. skill_generator     - Create ephemeral skills for unmatched capabilities")
    print("    3. skill_loader        - Read SKILL.md content for all discovered skills")
    print("    4. inject_memory       - Auto-recall cross-session context")
    print("    5. cache_lookup        - Artifact store (skip specialist on HIT)")
    print("    6. specialist          - Execute with single specialist")
    print("    7. heuristic_critic    - Fast output check (zero LLM calls)")
    print("    8. critic_output       - Validate output (LLM critic)")
    print()
    print("  Multi-Specialist Path:")
    print("    9. sub_specialist      - Execute each sub-task with specialist")
    print("   10. sub_critic_output   - Validate each sub-output")
    print("   11. aggregator          - Combine all specialist outputs")
    print("   12. final_critic        - Validate aggregated output")
    print()
    print("  Final:")
    print("   13. format              - Format output for Mattermost")
    print("   14. post                - Post to Mattermost channel")
    print("   15. skill_cleanup       - Track usage stats and clean up temp skills")
    print()

    print("FLOW (Single-Specialist):")
    print("  START -> router -> skill_generator -> skill_loader -> inject_memory -> cache_lookup -> specialist -> heuristic_critic -> [LLM critic if needed] -> format -> post -> skill_cleanup -> END")
    print()

    print("FLOW (Multi-Specialist):")
    print("  START -> router -> skill_generator -> skill_loader -> inject_memory -> cache_lookup")
    print("                                     |")
    print("                              [Decompose]")
    print("                                     |")
    print("                            +- sub_specialist <-+")
    print("                            |        |          |")
    print("                            +- sub_critic_output")
    print("                                     |")
    print("                                aggregator")
    print("                                     |")
    print("                               final_critic")
    print("                                     |")
    print("                          format -> post -> END")
    print()

    print("SPECIALIST ADAPTERS:")
    print("  * test_generator        - Generate unit tests")
    print("  * security_auditor      - Find security vulnerabilities")
    print("  * doc_generator         - Write documentation")
    print("  * performance_optimizer - Optimize code performance")
    print("  * debugging_assistant   - Debug and fix issues")
    print("  * vibe              - General purpose (fallback)")
    print()

    print("DECOMPOSITION TRIGGERS:")
    print("  * 2+ specialist patterns detected in specification")
    print("  * Keywords: 'comprehensive', 'production-ready', 'full implementation'")
    print("  * Explicit combinations: 'with tests', 'and security audit', etc.")
    print()

    print("="*80 + "\n")


# ===== HELPER FUNCTIONS =====

def run_workflow(
    app,
    user_request: str,
    max_iterations: int = 3,
    quality_threshold: int = 85,
    verbose: bool = True
):
    """
    Run the workflow with a user request.

    Args:
        app: Compiled workflow
        user_request: User's input request
        max_iterations: Maximum refinement iterations (per stage)
        quality_threshold: Minimum score to pass
        verbose: Print progress updates

    Returns:
        Final state after workflow completion
    """
    # Create initial state
    initial_state = create_initial_state(
        user_request=user_request,
        max_iterations=max_iterations,
        quality_threshold=quality_threshold
    )

    if verbose:
        print(f"\nStarting workflow for: {user_request[:80]}...")
        print(f"   Max iterations: {max_iterations} (output)")
        print(f"   Threshold: {quality_threshold}\n")

    # Run the workflow
    final_state = app.invoke(initial_state)

    # Finalize (add timing, etc.)
    final_state = finalize_state(final_state)

    if verbose:
        _print_workflow_summary(final_state)

    return final_state


def _print_workflow_summary(state: AgentState):
    """Print a summary of the workflow execution"""
    print("\n" + "="*80)
    print("WORKFLOW COMPLETE")
    print("="*80)

    # Check if multi-specialist workflow was used
    is_multi = state.get('requires_decomposition', False)

    print(f"\nResults:")
    print(f"   Workflow Type:        {'Multi-Specialist' if is_multi else 'Single-Specialist'}")
    print(f"   Spec Iterations:      {state.get('iteration_count', 0)}/{state.get('max_iterations', 3)}")

    if is_multi:
        # Multi-specialist stats
        sub_tasks = state.get('sub_tasks', [])
        completed = state.get('completed_sub_tasks', 0)
        print(f"   Sub-tasks:            {completed}/{len(sub_tasks)} completed")
        print(f"   Aggregation Score:    {state.get('final_aggregation_score', 0)}/100")
    else:
        # Single-specialist stats
        print(f"   Specialist Iterations: {state.get('specialist_iteration_count', 0)}/{state.get('specialist_max_iterations', 3)}")

    print(f"   Spec Score:           {state.get('spec_critic_score', 0)}/100")
    print(f"   Output Score:         {state.get('output_critic_score', 0)}/100")
    print(f"   Decision:             {state.get('quality_gate_decision', 'unknown').upper()}")
    print(f"   Time:                 {state.get('total_time_seconds', 0):.1f}s")

    # Show if clarification was needed
    if state.get('clarification_needed'):
        print(f"\nClarification Questions:")
        for i, q in enumerate(state.get('clarification_questions', []), 1):
            print(f"   {i}. {q}")

    # Show routing decision
    if is_multi:
        print(f"\nMulti-Specialist Routing:")
        print(f"   Parallel Execution:  {state.get('parallel_execution', False)}")
        print(f"   Aggregation Strategy: {state.get('aggregation_strategy', 'N/A')}")
        print(f"\n   Sub-tasks:")
        for i, subtask in enumerate(state.get('sub_tasks', []), 1):
            status_icon = "[OK]" if subtask.get("status") == "completed" else "[FAIL]"
            print(f"   {status_icon} {i}. {subtask.get('task_type')} -> {subtask.get('specialist_adapter')} "
                  f"(score: {subtask.get('output_score', 0)}/100)")
    elif state.get('routed_task_type'):
        print(f"\nRouting:")
        print(f"   Task Type:       {state.get('routed_task_type')}")
        print(f"   Specialist:      {state.get('specialist_adapter')}")
        print(f"   Confidence:      {state.get('routing_confidence', 0):.0%}")

    print(f"\nAdapters Used:")
    for adapter in state.get('adapters_used', []):
        print(f"   * {adapter}")

    if state.get('output_critic_scores'):
        print(f"\nOutput Quality Breakdown:")
        for dimension, score in state['output_critic_scores'].items():
            print(f"   {dimension.title():15s}: {score}/100")

    print(f"\nFinal Output Preview:")
    output = state.get('aggregated_output') or state.get('specialist_output') or state.get('final_output', 'N/A')
    preview = output[:300] + "..." if len(output) > 300 else output
    print(f"   {preview}")

    print("\n" + "="*80 + "\n")


def stream_workflow(
    app,
    user_request: str,
    max_iterations: int = 3,
    quality_threshold: int = 85
):
    """
    Stream workflow execution, printing updates as each node completes.

    This is useful for long-running workflows where you want real-time feedback.

    Args:
        app: Compiled workflow
        user_request: User's input request
        max_iterations: Maximum refinement iterations
        quality_threshold: Minimum score to pass

    Returns:
        Final state after workflow completion
    """
    try:
        from rich.console import Console
    except ImportError:
        logger.warning("rich library not available, falling back to basic output")
        return run_workflow(app, user_request, max_iterations, quality_threshold, verbose=True)

    console = Console()

    # Create initial state
    initial_state = create_initial_state(
        user_request=user_request,
        max_iterations=max_iterations,
        quality_threshold=quality_threshold
    )

    console.print(f"\n[bold blue]Starting workflow:[/bold blue] {user_request[:60]}...\n")

    # Stream through the workflow
    final_state = None
    for step, state in enumerate(app.stream(initial_state), 1):
        # state is a dict with node name as key
        node_name = list(state.keys())[0]
        node_state = state[node_name]

        # Print node completion
        _print_node_status(console, node_name, node_state, step)

        final_state = node_state

    # Finalize
    if final_state:
        final_state = finalize_state(final_state)
        console.print("\n[bold green]Workflow Complete![/bold green]\n")
        _print_workflow_summary(final_state)

    return final_state


def _print_node_status(console, node_name: str, state: AgentState, step: int):
    """Print status update for a completed node"""
    label_map = {
        "router": "ROUTER",
        "skill_generator": "SKILL_GENERATOR",
        "skill_loader": "SKILL_LOADER",
        "inject_memory": "MEMORY_INJECTION",
        "cache_lookup": "CACHE_LOOKUP",
        "specialist": "SPECIALIST",
        "heuristic_critic": "HEURISTIC_CRITIC",
        "critic_output": "CRITIC_OUTPUT",
        "sub_specialist": "SUB_SPECIALIST",
        "sub_critic_output": "SUB_CRITIC_OUTPUT",
        "parallel_subtasks": "PARALLEL_SUBTASKS",
        "aggregator": "AGGREGATOR",
        "final_critic": "FINAL_CRITIC",
        "format": "FORMAT",
        "post": "POST",
        "skill_cleanup": "SKILL_CLEANUP"
    }

    output_iteration = state.get("specialist_iteration_count", 0)
    sub_tasks = state.get("sub_tasks", [])
    current_sub_idx = state.get("current_sub_task_index", 0)

    label = label_map.get(node_name, node_name.upper())
    console.print(f"[bold]{label}[/bold] completed (step {step})")

    # Show relevant info based on node
    if node_name == "heuristic_critic":
        score = state.get('heuristic_critic_score', 0)
        passed = state.get('heuristic_critic_passed', False)
        label = "[green]PASSED[/green]" if passed else "[yellow]DEFERRED to LLM critic[/yellow]"
        console.print(f"   Heuristic Score: {score}/100 — {label}")

    elif node_name == "router":
        if state.get('requires_decomposition'):
            console.print(f"   [cyan]Multi-specialist workflow: {len(sub_tasks)} sub-tasks[/cyan]")
        else:
            task_type = state.get('routed_task_type', 'unknown')
            specialist = state.get('specialist_adapter', 'unknown')
            console.print(f"   Routed to: {specialist} (task: {task_type})")

    elif node_name == "specialist":
        console.print(f"   Output Iteration {output_iteration}: Generated with {state.get('specialist_adapter', 'unknown')}")

    elif node_name == "critic_output":
        score = state.get('output_critic_score', 0)
        console.print(f"   Output Score: {score}/100")

    elif node_name == "sub_specialist":
        if current_sub_idx < len(sub_tasks):
            subtask = sub_tasks[current_sub_idx]
            console.print(f"   Executed with: {subtask.get('specialist_adapter', 'unknown')}")

    elif node_name == "sub_critic_output":
        if current_sub_idx < len(sub_tasks):
            subtask = sub_tasks[current_sub_idx]
            console.print(f"   Sub-output Score: {subtask.get('output_score', 0)}/100")

    elif node_name == "aggregator":
        completed = state.get('completed_sub_tasks', 0)
        console.print(f"   Aggregating {completed} completed sub-tasks")
        console.print(f"   Strategy: {state.get('aggregation_strategy', 'merge')}")

    elif node_name == "final_critic":
        score = state.get('output_critic_score', 0)
        console.print(f"   Aggregated Output Score: {score}/100")

    console.print()
