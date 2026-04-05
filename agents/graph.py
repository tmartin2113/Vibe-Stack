"""
Workflow Orchestration - Skill-Driven Multi-Specialist Architecture

Lightweight state machine that replaces LangGraph with zero external dependencies.
All nodes are pure AgentState -> AgentState functions; this module handles only
the execution order (edges, conditional branches, loops).

Graph Structure:
    START
      |
    Router (classify task type, set specification = user_request)
      |
    Skill Generator (create ephemeral skills for unmatched capabilities)
      |
    Skill Loader (read SKILL.md content for all discovered/generated skills)
      |
    Memory Injection (auto-recall cross-session context)
      |
    Cache Lookup (artifact store — skip specialist on HIT)
      |
    [Cache + Decomposition Check]
      +-- cache_hit -> Format (serve cached result)
      +-- single -> Specialist -> [clarification? -> Skill Cleanup -> END] | Heuristic Critic -> (approve | LLM Critic) -> Format -> Post -> Skill Cleanup -> END
      +-- parallel_decompose -> Parallel Sub-tasks (ThreadPoolExecutor) -> Aggregator -> ...
      +-- decompose -> Sub-task Loop (sequential):
                        +---------------------+
                        | FOR EACH SUB-TASK:  |
                        |  Specialist         |
                        |  Heuristic Critic   |
                        +---------------------+
                              |
                        Aggregator (combine outputs)
                              |
                        Final Critic (validate aggregated)
                              |
                        Format -> Post -> Skill Cleanup -> END
"""

from typing import Optional, Any
import logging

from .cancellation import CancellationToken
from .config import SystemConfig
from .graph_engine import (
    Workflow,
    CompiledWorkflow,
    WorkflowRecursionError,
    NodeTimeoutError,
    WorkflowTimeoutError,
    END,
)
from .state import AgentState, create_initial_state, finalize_state
from .nodes import (
    AgentNodes,
    should_approve_output,
    should_approve_sub_output,
    has_more_subtasks,
    should_decompose,
    should_use_llm_critic,
)
from .adapters import AdapterRegistry
from .tools import ToolRegistry, create_default_tool_registry
from .heuristic_critic import heuristic_evaluate_output
from .artifact_store import ArtifactStore
from .graph_nodes import create_node_wrappers
from .graph_runners import (
    print_graph_structure,
    run_workflow,
    stream_workflow,
    _print_workflow_summary,
    _print_node_status,
)

logger = logging.getLogger(__name__)

# Re-export engine classes for backward compatibility
__all__ = [
    # Engine (from graph_engine)
    "Workflow",
    "CompiledWorkflow",
    "WorkflowRecursionError",
    "NodeTimeoutError",
    "WorkflowTimeoutError",
    "END",
    # Graph builder & helpers
    "create_agent_graph",
    "run_workflow",
    "stream_workflow",
    "print_graph_structure",
    "sub_output_and_more_check",
]


# ===== ROUTING FUNCTIONS =====

def sub_output_and_more_check(state: AgentState) -> str:
    """
    Combined routing: approve sub-output AND check if more sub-tasks exist.

    This function handles the sub-task loop logic by:
    1. Checking if current sub-task output needs refinement
    2. If approved/failed, checking if there are more sub-tasks
    3. Routing to next sub-task or aggregation accordingly

    Returns:
        - "refine_sub_output": Current sub-task needs improvement
        - "next_subtask": Move to next sub-task
        - "aggregate": All sub-tasks complete, aggregate results
    """
    # First, evaluate the sub-output
    result = should_approve_sub_output(state)

    if result == "refine_sub_output":
        # Need to refine this sub-task
        return "refine_sub_output"

    # Sub-task is done (approved or failed)
    # Now check if there are more sub-tasks
    more_result = has_more_subtasks(state)

    if more_result == "more":
        # More sub-tasks to process
        return "next_subtask"
    else:
        # All sub-tasks processed, go to aggregation
        return "aggregate"


def create_agent_graph(adapter_registry: AdapterRegistry, tool_registry: Optional[ToolRegistry] = None, config: Optional[SystemConfig] = None, base_model: Any = None, cancellation_token: Optional[CancellationToken] = None, sandbox_pool: Any = None, agent_role: Optional[str] = None, agent_title: Optional[str] = None):
    """
    Create the workflow with multi-specialist architecture.

    Supports both single-specialist and multi-specialist workflows.

    Args:
        adapter_registry: Registry with all adapters configured
        tool_registry: Optional tool registry (creates default if not provided)
        config: Optional SystemConfig for API key management and messenger integration
        base_model: Base LLM backend (for skill generation and hybrid routing)
        sandbox_pool: Optional pre-built SandboxPoolManager (avoids re-creating
            expensive Docker containers on every invocation).

    Returns:
        Compiled workflow (supports .invoke() and .stream())
    """
    # Initialize sandbox pool (OpenSandbox preferred, subprocess fallback)
    sandbox_config = getattr(config, 'sandbox', None) if config else None
    if not sandbox_config:
        raise RuntimeError(
            "Sandbox configuration is required. "
            "Ensure config.sandbox is set (SandboxConfig is auto-populated at startup)."
        )

    network_egress = sandbox_config.network_egress if sandbox_config else False
    allowed_file_dirs = sandbox_config.allowed_file_dir_list if sandbox_config else None

    if tool_registry is None:
        use_subprocess = sandbox_config.backend == "subprocess"
        if use_subprocess:
            logger.info("Sandbox backend set to subprocess — skipping OpenSandbox")
            from .tools.registry import create_subprocess_tool_registry
            tool_registry = create_subprocess_tool_registry(
                network_egress=network_egress,
                allowed_file_dirs=allowed_file_dirs or None,
            )
        else:
            try:
                if sandbox_pool is None:
                    from .sandbox.client import SandboxPoolManager
                    sandbox_pool = SandboxPoolManager(sandbox_config)
                    sandbox_pool.start()
                    logger.info("OpenSandbox pool started")
                tool_registry = create_default_tool_registry(
                    sandbox_pool=sandbox_pool,
                    network_egress=network_egress,
                    allowed_file_dirs=allowed_file_dirs or None,
                )
            except Exception as e:
                logger.warning(f"OpenSandbox unavailable ({e}), falling back to subprocess execution")
                from .tools.registry import create_subprocess_tool_registry
                tool_registry = create_subprocess_tool_registry(
                    network_egress=network_egress,
                    allowed_file_dirs=allowed_file_dirs or None,
                )

    # Apply role-based tool filtering (restricts research tools for engineers,
    # ensures DeerFlow assistants get full research access, etc.)
    if agent_role:
        tool_registry = tool_registry.filter_for_role(agent_role, title=agent_title or "")
        logger.info("Tool registry filtered for role '%s': %d tools",
                     agent_role, len(tool_registry.list_tools()))

    # Create node instance with access to adapters and tools
    nodes = AgentNodes(
        adapter_registry,
        tool_registry,
        config=config
    )

    # Initialize workflow
    workflow = Workflow()

    # ===== SHARED STATE =====

    # Heuristic critic (zero LLM calls — format/length checks)
    workflow.add_node("heuristic_critic", heuristic_evaluate_output)

    # Stage 1: Routing & Decomposition
    # Create a shared SkillRegistry so skill state persists across invocations.
    # Passing None would create a fresh registry per call (Bug #1 in router.py).
    # Pass skills_config for multi-source ingestion (anthropics, obra, vercel).
    from .skill_registry import SkillRegistry
    from .config import get_skills_dir
    skills_config = config.skills if hasattr(config, "skills") else None
    shared_skill_registry = SkillRegistry(
        get_skills_dir(), skills_config=skills_config
    )

    # Shared SkillOutcomeStore — closes the reinforcement loop:
    #   skill_cleanup records outcomes -> skill_generator retrieves top-K for RAG
    from .skill_outcome_store import SkillOutcomeStore
    import os as _os
    shared_outcome_store = SkillOutcomeStore(
        store_path=_os.path.join(get_skills_dir(), "outcome_store.jsonl")
    )

    # Self-upgrade trigger
    from .self_upgrade_trigger import SelfUpgradeTrigger
    shared_upgrade_trigger = SelfUpgradeTrigger()

    # Result cache (Artifact Store)
    cache_config = getattr(config, 'cache', None) if config else None
    shared_artifact_store: Optional[ArtifactStore] = None

    if cache_config and cache_config.enabled:
        shared_artifact_store = ArtifactStore(
            db_path=cache_config.db_path,
            max_entries=cache_config.max_entries,
            default_ttl_seconds=cache_config.default_ttl_seconds,
            min_score_to_cache=cache_config.min_score_to_cache,
        )
        logger.info("Artifact store initialized (result caching enabled)")

    # ===== CREATE NODE WRAPPERS =====

    wrappers = create_node_wrappers(
        nodes=nodes,
        shared_skill_registry=shared_skill_registry,
        shared_outcome_store=shared_outcome_store,
        shared_artifact_store=shared_artifact_store,
        shared_upgrade_trigger=shared_upgrade_trigger,
        base_model=base_model,
        config=config,
        adapter_registry=adapter_registry,
        tool_registry=tool_registry,
        cancellation_token=cancellation_token,
    )

    # ===== ADD NODES =====

    workflow.add_node("router", wrappers["router"])
    workflow.add_node("skill_generator", wrappers["skill_generator"])
    workflow.add_node("skill_loader", wrappers["skill_loader"])
    workflow.add_node("inject_memory", wrappers["inject_memory"])
    workflow.add_node("cache_lookup", wrappers["cache_lookup"])

    # Single-specialist path (original workflow)
    workflow.add_node("specialist", wrappers["specialist"])
    workflow.add_node("critic_output", wrappers["critic_output"])

    # Multi-specialist path
    workflow.add_node("sub_specialist", wrappers["sub_specialist"])
    workflow.add_node("sub_critic_output", wrappers["sub_critic_output"])

    # Parallel sub-task execution (when parallel_execution=True)
    workflow.add_node("parallel_subtasks", wrappers["parallel_subtasks"])

    workflow.add_node("aggregator", wrappers["aggregator"])
    workflow.add_node("final_critic", wrappers["final_critic"])
    workflow.add_node("skill_cleanup", wrappers["skill_cleanup"])

    # Stage 3: Output Formatting
    workflow.add_node("format", nodes.format_for_mattermost)
    workflow.add_node("post", nodes.post_to_mattermost)

    # ===== SET ENTRY POINT =====

    workflow.set_entry_point("router")

    # ===== ADD EDGES =====

    # Router -> Skill Generator -> Skill Loader -> Memory Injection -> Cache Lookup
    # The generator creates SKILL.md for any ephemeral skills the router couldn't
    # match locally or on GitHub. The loader then reads all SKILL.md content.
    # Memory injection auto-recalls relevant cross-session context for the specialist.
    workflow.add_edge("router", "skill_generator")
    workflow.add_edge("skill_generator", "skill_loader")
    workflow.add_edge("skill_loader", "inject_memory")
    workflow.add_edge("inject_memory", "cache_lookup")

    # Cache check + Decomposition Check: cache_hit, single, sequential decompose, or parallel decompose
    def cache_hit_or_miss(state: AgentState) -> str:
        """Route based on cache hit: skip specialist on hit."""
        if state.get("cache_hit", False):
            return "cache_hit"
        # Fall through to decomposition check
        if state.get("requires_decomposition", False):
            if state.get("parallel_execution", False):
                return "parallel_decompose"
            return "decompose"
        return "single"

    workflow.add_conditional_edges(
        "cache_lookup",
        cache_hit_or_miss,
        {
            "cache_hit": "format",                    # Serve cached result directly
            "single": "specialist",                   # Single-specialist workflow
            "decompose": "sub_specialist",             # Sequential multi-specialist
            "parallel_decompose": "parallel_subtasks", # Parallel multi-specialist
        }
    )

    # Parallel path: check for clarification before aggregation
    def parallel_next(state: AgentState) -> str:
        """Route after parallel subtasks: if clarification needed, skip aggregation."""
        if state.get("clarification_needed"):
            return "clarification"
        return "continue"

    workflow.add_conditional_edges(
        "parallel_subtasks",
        parallel_next,
        {
            "clarification": "skill_cleanup",  # Skip aggregation + critic
            "continue": "aggregator",           # Normal path
        }
    )

    # ===== SINGLE-SPECIALIST PATH =====

    # Specialist -> Clarification check -> Heuristic Critic (or skip to cleanup)
    def specialist_next(state: AgentState) -> str:
        """Route after specialist: if clarification needed, skip critic."""
        if state.get("clarification_needed"):
            return "clarification"
        return "continue"

    workflow.add_conditional_edges(
        "specialist",
        specialist_next,
        {
            "clarification": "skill_cleanup",   # Skip critic, let heartbeat post questions
            "continue": "heuristic_critic",      # Normal path
        }
    )

    # Heuristic gate: if heuristic passes, skip LLM critic; otherwise fall through
    workflow.add_conditional_edges(
        "heuristic_critic",
        should_use_llm_critic,
        {
            "approve": "format",          # Heuristic passed — skip LLM critic
            "critic_output": "critic_output",  # Heuristic failed — use LLM critic
        }
    )

    # Quality Gate 2: Output Approval (LLM critic, reached only when heuristic fails)
    workflow.add_conditional_edges(
        "critic_output",
        should_approve_output,
        {
            "approved": "format",
            "refine_output": "specialist",
            "fail": "skill_cleanup"  # Track usage + cleanup even on failure
        }
    )

    # ===== MULTI-SPECIALIST PATH (Sub-task Loop) =====

    # Sub-task execution: Specialist -> Clarification check -> Critic (or skip)
    def sub_specialist_next(state: AgentState) -> str:
        """Route after sub-specialist: if clarification needed, skip critic."""
        if state.get("clarification_needed"):
            return "clarification"
        return "continue"

    workflow.add_conditional_edges(
        "sub_specialist",
        sub_specialist_next,
        {
            "clarification": "skill_cleanup",      # Skip critic + aggregation
            "continue": "sub_critic_output",         # Normal path
        }
    )

    # Quality Gate: Sub-output Approval + More Sub-tasks Check
    workflow.add_conditional_edges(
        "sub_critic_output",
        sub_output_and_more_check,
        {
            "refine_sub_output": "sub_specialist",  # Refine current sub-task
            "next_subtask": "sub_specialist",        # Process next sub-task
            "aggregate": "aggregator"                # All done, aggregate results
        }
    )

    # Aggregation: Combine all sub-task outputs
    workflow.add_edge("aggregator", "final_critic")

    # Final validation of aggregated output
    workflow.add_conditional_edges(
        "final_critic",
        should_approve_output,
        {
            "approved": "format",
            "refine_output": "skill_cleanup",  # Can't refine aggregated output; cleanup and exit
            "fail": "skill_cleanup"            # Track usage + cleanup even on failure
        }
    )

    # ===== FINAL STEPS =====

    # Format -> Post -> Skill Cleanup -> END
    workflow.add_edge("format", "post")
    workflow.add_edge("post", "skill_cleanup")
    workflow.add_edge("skill_cleanup", END)

    # Read timeout settings from config
    node_timeout = 0
    workflow_timeout = 0
    if config and hasattr(config, 'workflow'):
        node_timeout = getattr(config.workflow, 'node_timeout', 0)
        workflow_timeout = getattr(config.workflow, 'workflow_timeout', 0)

    # Compile the graph with timeout enforcement and optional cancellation
    app = workflow.compile(
        node_timeout=node_timeout,
        workflow_timeout=workflow_timeout,
        cancellation_token=cancellation_token,
    )

    logger.info("Workflow (multi-specialist architecture) compiled successfully")

    return app
