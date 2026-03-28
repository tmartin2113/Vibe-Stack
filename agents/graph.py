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

from typing import Callable, Dict, Optional, Any, Iterator, Tuple
import logging
import concurrent.futures
import time

from .cancellation import CancellationToken, WorkflowCancelledError
from .metrics import metrics as app_metrics
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
from .router import route_to_specialist
from .aggregator import aggregate_outputs
from .tools import ToolRegistry, create_default_tool_registry
from .heuristic_critic import heuristic_evaluate_output
from .artifact_store import ArtifactStore
from .parallel_subtasks import execute_parallel_subtasks

logger = logging.getLogger(__name__)


# ===== LIGHTWEIGHT STATE MACHINE =====

# Sentinel that means "stop execution"
END = "__end__"


class Workflow:
    """
    Minimal DAG-based state machine.

    Nodes are callables: (AgentState) -> AgentState
    Edges are either:
      - Linear:      source -> target
      - Conditional:  source -> decision_fn(state) -> {result: target}

    Usage:
        wf = Workflow()
        wf.add_node("a", fn_a)
        wf.add_node("b", fn_b)
        wf.add_edge("a", "b")
        wf.add_edge("b", END)
        wf.set_entry_point("a")
        app = wf.compile()
        final_state = app.invoke(initial_state)
    """

    def __init__(self):
        self._nodes: Dict[str, Callable[[AgentState], AgentState]] = {}
        self._edges: Dict[str, str] = {}
        self._conditional_edges: Dict[str, Tuple[Callable, Dict[str, str]]] = {}
        self._entry_point: Optional[str] = None

    def add_node(self, name: str, fn: Callable[[AgentState], AgentState]):
        self._nodes[name] = fn

    def add_edge(self, source: str, target: str):
        self._edges[source] = target

    def add_conditional_edges(
        self,
        source: str,
        decision_fn: Callable[[AgentState], str],
        route_map: Dict[str, str],
    ):
        self._conditional_edges[source] = (decision_fn, route_map)

    def set_entry_point(self, name: str):
        self._entry_point = name

    def compile(
        self,
        node_timeout: int = 0,
        workflow_timeout: int = 0,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> "CompiledWorkflow":
        """
        Compile the workflow into an executable form.

        Args:
            node_timeout: Per-node timeout in seconds (0 = no timeout)
            workflow_timeout: Total workflow timeout in seconds (0 = no timeout)
            cancellation_token: Optional token for cooperative cancellation
        """
        if self._entry_point is None:
            raise ValueError("No entry point set. Call set_entry_point() first.")
        if self._entry_point not in self._nodes:
            raise ValueError(f"Entry point '{self._entry_point}' is not a registered node.")
        return CompiledWorkflow(
            nodes=self._nodes,
            edges=self._edges,
            conditional_edges=self._conditional_edges,
            entry_point=self._entry_point,
            node_timeout=node_timeout,
            workflow_timeout=workflow_timeout,
            cancellation_token=cancellation_token,
        )


class WorkflowRecursionError(RuntimeError):
    """Raised when the workflow exceeds the maximum step limit."""


class NodeTimeoutError(RuntimeError):
    """Raised when a single node exceeds its time budget."""

    def __init__(self, node_name: str, timeout: int):
        self.node_name = node_name
        self.timeout = timeout
        super().__init__(
            f"Node '{node_name}' exceeded {timeout}s timeout"
        )


class WorkflowTimeoutError(RuntimeError):
    """Raised when total workflow execution exceeds its time budget."""

    def __init__(self, elapsed: float, timeout: int):
        self.elapsed = elapsed
        self.timeout = timeout
        super().__init__(
            f"Workflow exceeded {timeout}s timeout (elapsed: {elapsed:.1f}s)"
        )


# Default matches LangGraph's recursion_limit for parity.
DEFAULT_MAX_STEPS = 50


class CompiledWorkflow:
    """Executable workflow returned by Workflow.compile()."""

    def __init__(
        self,
        nodes: Dict[str, Callable],
        edges: Dict[str, str],
        conditional_edges: Dict[str, Tuple[Callable, Dict[str, str]]],
        entry_point: str,
        max_steps: int = DEFAULT_MAX_STEPS,
        node_timeout: int = 0,
        workflow_timeout: int = 0,
        cancellation_token: Optional[CancellationToken] = None,
    ):
        self._nodes = nodes
        self._edges = edges
        self._conditional_edges = conditional_edges
        self._entry_point = entry_point
        self._max_steps = max_steps
        self._node_timeout = node_timeout
        self._workflow_timeout = workflow_timeout
        self._cancellation_token = cancellation_token

    def _resolve_next(self, current: str, state: AgentState) -> str:
        """Determine the next node after *current* has executed."""
        # Conditional edge takes priority
        if current in self._conditional_edges:
            decision_fn, route_map = self._conditional_edges[current]
            result = decision_fn(state)
            target = route_map.get(result)
            if target is None:
                raise ValueError(
                    f"Decision function for '{current}' returned '{result}', "
                    f"which is not in route map {list(route_map.keys())}"
                )
            return target

        # Linear edge
        if current in self._edges:
            return self._edges[current]

        # No edge defined — implicit END
        return END

    # ---- internal helpers ----

    def _execute_node(self, node_name: str, node_fn: Callable, state: AgentState) -> AgentState:
        """
        Execute a single node, enforcing per-node timeout if configured.

        Uses a thread pool to run the node function so that a hung LLM call
        can be interrupted after the timeout period.  Note: the background
        thread cannot be forcibly killed in CPython, but we return control
        to the caller immediately by using shutdown(wait=False).
        """
        node_start = time.monotonic()
        try:
            if self._node_timeout <= 0:
                return node_fn(state)  # type: ignore[no-any-return]

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(node_fn, state)
            try:
                result: AgentState = future.result(timeout=self._node_timeout)
                executor.shutdown(wait=False)
                return result
            except concurrent.futures.TimeoutError:
                # Don't wait for the thread — return control immediately.
                executor.shutdown(wait=False)
                raise NodeTimeoutError(node_name, self._node_timeout)
        finally:
            duration = time.monotonic() - node_start
            app_metrics.observe(
                "vibe_node_duration_seconds", duration,
                labels={"node": node_name},
            )

    def _check_workflow_timeout(self, start_time: float) -> None:
        """Raise WorkflowTimeoutError if total elapsed time exceeds budget."""
        if self._workflow_timeout <= 0:
            return
        elapsed = time.monotonic() - start_time
        if elapsed > self._workflow_timeout:
            raise WorkflowTimeoutError(elapsed, self._workflow_timeout)

    # ---- public API (matches the subset of LangGraph we actually use) ----

    def _check_cancellation(self) -> None:
        """Raise WorkflowCancelledError if the cancellation token has fired."""
        if self._cancellation_token is not None:
            self._cancellation_token.check()

    def invoke(self, state: AgentState) -> AgentState:
        """Run the workflow to completion, returning the final state."""
        current = self._entry_point
        steps = 0
        start_time = time.monotonic()

        while current != END:
            if steps >= self._max_steps:
                raise WorkflowRecursionError(
                    f"Workflow exceeded {self._max_steps} steps "
                    f"(last node: '{current}'). This usually means a "
                    f"decision function is stuck in a loop."
                )

            self._check_workflow_timeout(start_time)
            self._check_cancellation()

            node_fn = self._nodes.get(current)
            if node_fn is None:
                raise ValueError(f"No node registered for '{current}'")

            logger.debug(f"Executing node: {current} (step {steps + 1})",
                         extra={"node": current, "step": steps + 1})
            state = self._execute_node(current, node_fn, state)
            steps += 1

            current = self._resolve_next(current, state)

        return state

    def stream(self, state: AgentState) -> Iterator[Dict[str, AgentState]]:
        """
        Yield {node_name: state} after each node completes.

        Compatible with the streaming interface used by stream_workflow
        and the daemon service.
        """
        current = self._entry_point
        steps = 0
        start_time = time.monotonic()

        while current != END:
            if steps >= self._max_steps:
                raise WorkflowRecursionError(
                    f"Workflow exceeded {self._max_steps} steps "
                    f"(last node: '{current}'). This usually means a "
                    f"decision function is stuck in a loop."
                )

            self._check_workflow_timeout(start_time)
            self._check_cancellation()

            node_fn = self._nodes.get(current)
            if node_fn is None:
                raise ValueError(f"No node registered for '{current}'")

            logger.debug(f"Executing node: {current} (step {steps + 1})")
            state = self._execute_node(current, node_fn, state)
            steps += 1

            yield {current: state}

            current = self._resolve_next(current, state)


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


def create_agent_graph(adapter_registry: AdapterRegistry, tool_registry: Optional[ToolRegistry] = None, config: Any = None, base_model: Any = None, cancellation_token: Optional[CancellationToken] = None, sandbox_pool: Any = None):
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

    # Create node instance with access to adapters and tools
    nodes = AgentNodes(
        adapter_registry,
        tool_registry,
        config=config
    )

    # Initialize workflow
    workflow = Workflow()

    # ===== ADD NODES =====

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

    def router_wrapper(state: AgentState) -> AgentState:
        """Wrapper to pass shared skill registry and base model to router.

        Also sets specification = user_request so downstream nodes that
        read the specification field continue to work in the skill-only
        pipeline (no Vibe spec-building step).
        """
        state["specification"] = state.get("user_request", "")
        return route_to_specialist(
            state,
            skill_registry=shared_skill_registry,
            base_model=base_model,
        )

    workflow.add_node("router", router_wrapper)

    # Skill generation: creates ephemeral SKILL.md for unmatched capabilities
    from .skill_generator import generate_skills

    def skill_generator_wrapper(state: AgentState) -> AgentState:
        """Generate ephemeral skills for any capabilities the router couldn't match."""
        return generate_skills(
            state,
            skill_registry=shared_skill_registry,
            outcome_store=shared_outcome_store,
            base_model=base_model,
            adapter_registry=adapter_registry,
        )

    workflow.add_node("skill_generator", skill_generator_wrapper)

    # Skill loading: reads SKILL.md content for all discovered skills
    from .skill_loader import load_skills

    def skill_loader_wrapper(state: AgentState) -> AgentState:
        """Load SKILL.md content for all skills discovered by the router."""
        return load_skills(state, skill_registry=shared_skill_registry)

    workflow.add_node("skill_loader", skill_loader_wrapper)

    # Memory injection: auto-inject relevant memories into specialist context
    from .memory_store import MemoryStore
    from .tools.registry import _get_shared_memory_store

    def inject_memory(state: AgentState) -> AgentState:
        """Auto-inject relevant memories into specialist context.

        Uses hybrid recall (BM25 + semantic) on the user request to find
        related facts, decisions, and context from previous sessions.
        Injected as a formatted section that specialists receive alongside
        skill context.
        """
        user_request = state.get("user_request", "")
        if not user_request:
            return state

        try:
            store = _get_shared_memory_store()
            results = store.hybrid_recall(
                query=user_request,
                max_results=5,
            )

            if not results:
                state["memory_context"] = ""
                return state

            # Format memories with citations for specialist injection
            sections = []
            for entry in results:
                section = f"- {entry.content}"
                if entry.citation:
                    section += f" (source: {entry.citation})"
                sections.append(section)

            state["memory_context"] = (
                "\n\n## Relevant Memories\n\n"
                "The following facts and context were recalled from previous sessions:\n\n"
                + "\n".join(sections)
            )
            logger.info(f"Injected {len(results)} memories into specialist context")

        except Exception as e:
            logger.debug(f"Memory injection skipped: {e}")
            state["memory_context"] = ""

        # Append inter-agent messages (MessageStore or v1 fallback)
        try:
            from .message_store import get_shared_message_store, _get_agent_name as _msg_agent_name
            msg_store = get_shared_message_store()
            agent_name = _msg_agent_name()
            messages = msg_store.relevant_messages(
                query=user_request,
                agent_name=agent_name,
                max_results=10,
            )
            if messages:
                sections = [m.format_for_context() for m in messages]
                bulletin_text = (
                    "\n\n## Inter-Agent Messages\n\n"
                    + "\n".join(sections)
                )
                state["memory_context"] = state.get("memory_context", "") + bulletin_text
                state["pending_messages"] = [m.to_dict() for m in messages]
                logger.info(f"Injected {len(messages)} inter-agent messages into specialist context")
        except Exception as e:
            logger.debug(f"Message injection skipped: {e}")
            # Fallback to v1 bulletin board
            try:
                from .tools.bulletin_board import read_recent_entries
                bulletin_text = read_recent_entries(limit=10)
                if bulletin_text:
                    state["memory_context"] = state.get("memory_context", "") + bulletin_text
                    logger.info("Injected v1 bulletin board entries into specialist context")
            except Exception as e2:
                logger.debug(f"V1 bulletin fallback also skipped: {e2}")

        return state

    workflow.add_node("inject_memory", inject_memory)

    # ===== SELF-UPGRADE TRIGGER =====
    from .self_upgrade_trigger import SelfUpgradeTrigger
    shared_upgrade_trigger = SelfUpgradeTrigger()

    # ===== RESULT CACHE (Artifact Store) =====
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

    def cache_lookup(state: AgentState) -> AgentState:
        """Check if an identical specification has a cached result."""
        if shared_artifact_store is None:
            state["cache_hit"] = False
            return state

        specification = state.get("specification", "")
        loaded_skills = state.get("loaded_skills", [])
        task_type = state.get("routed_task_type", "general")
        adapter = state.get("specialist_adapter", "")

        if not specification:
            state["cache_hit"] = False
            return state

        cache_key = ArtifactStore.compute_cache_key(
            specification, loaded_skills, task_type, adapter,
        )
        state["cache_key"] = cache_key

        entry = shared_artifact_store.lookup(cache_key)
        if entry is None:
            state["cache_hit"] = False
            logger.debug(f"Cache MISS: {cache_key[:12]}...")
            return state

        # Cache HIT — populate state with cached results
        state["cache_hit"] = True
        state["specialist_output"] = entry.specialist_output
        state["current_output"] = entry.specialist_output
        state["output_critic_score"] = entry.output_critic_score
        state["critic_score"] = entry.output_critic_score
        state["final_output"] = entry.specialist_output
        state["final_score"] = entry.final_score
        state["tool_calls_made"] = entry.tool_calls

        logger.info(
            f"Cache HIT: {cache_key[:12]}... "
            f"(score={entry.final_score}, hits={entry.access_count}, "
            f"task={entry.task_type})"
        )
        return state

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

    workflow.add_node("cache_lookup", cache_lookup)

    # Single-specialist path (original workflow)
    workflow.add_node("specialist", nodes.execute_with_specialist)

    def critic_output_wrapper(state: AgentState) -> AgentState:
        """Evaluate specialist output."""
        return nodes.evaluate_output(state)

    workflow.add_node("critic_output", critic_output_wrapper)

    # Multi-specialist path
    workflow.add_node("sub_specialist", nodes.execute_sub_task)

    def sub_critic_output_wrapper(state: AgentState) -> AgentState:
        """Evaluate sub-task output."""
        return nodes.evaluate_sub_output(state)

    workflow.add_node("sub_critic_output", sub_critic_output_wrapper)

    # Parallel sub-task execution (when parallel_execution=True)
    def parallel_subtasks_wrapper(state: AgentState) -> AgentState:
        """Execute all sub-tasks concurrently using thread pool.

        Passes adapter_registry so the simulation sidecar can create
        persona LLM calls on the same base_model.
        """
        return execute_parallel_subtasks(
            state, nodes, config,
            adapter_registry=adapter_registry,
        )

    workflow.add_node("parallel_subtasks", parallel_subtasks_wrapper)

    def aggregator_wrapper(state: AgentState) -> AgentState:
        """Wrapper to pass adapter registry to LLM-driven aggregator."""
        return aggregate_outputs(state, adapter_registry=adapter_registry)

    workflow.add_node("aggregator", aggregator_wrapper)

    def final_critic_wrapper(state: AgentState) -> AgentState:
        """Evaluate aggregated output."""
        return nodes.evaluate_aggregated_output(state)

    workflow.add_node("final_critic", final_critic_wrapper)

    # Skill cleanup: track usage stats and remove temp skills
    from .skill_cleanup import cleanup_skills

    def skill_cleanup_wrapper(state: AgentState) -> AgentState:
        """Track skill usage statistics, record outcomes, cache results, analyse for self-upgrade, and clean up temp skills."""
        result = cleanup_skills(
            state,
            skill_registry=shared_skill_registry,
            outcome_store=shared_outcome_store,
            base_model=base_model,
        )

        # Store approved results in the artifact cache
        if shared_artifact_store is not None and not result.get("cache_hit", False):
            cache_key = result.get("cache_key", "")
            specialist_output = result.get("specialist_output", "") or result.get("current_output", "")
            output_score = result.get("output_critic_score", 0)
            final_score = result.get("final_score", 0)

            if cache_key and specialist_output:
                stored = shared_artifact_store.store(
                    cache_key=cache_key,
                    specification=result.get("specification", ""),
                    specialist_output=specialist_output,
                    output_critic_score=output_score,
                    final_score=final_score,
                    tool_calls=result.get("tool_calls_made", []),
                    task_type=result.get("routed_task_type", "general"),
                    specialist_adapter=result.get("specialist_adapter", ""),
                    skills_hash=ArtifactStore.compute_skills_hash(
                        result.get("loaded_skills", [])
                    ),
                    num_iterations=result.get("iteration_count", 1),
                )
                result["cache_entry_stored"] = stored

        # Analyse workflow outcome for self-upgrade signals and execute
        # the full pipeline if enough evidence has accumulated.
        try:
            from .self_upgrade_trigger import analyse_for_upgrade
            from .self_upgrade import (
                SelfUpgradePipeline,
                generate_upgrade_proposal,
                is_self_upgrade_enabled,
            )

            if is_self_upgrade_enabled():
                trigger_result = analyse_for_upgrade(
                    result, trigger=shared_upgrade_trigger,
                )
                if trigger_result.signals:
                    result["upgrade_signals"] = [
                        {"category": s.category, "detail": s.detail}
                        for s in trigger_result.signals
                    ]
                if trigger_result.should_propose and base_model is not None:
                    result["upgrade_proposal_ready"] = True
                    result["upgrade_proposal_description"] = (
                        trigger_result.proposal_description
                    )
                    logger.info(
                        "Self-upgrade proposal ready: %s — generating code",
                        trigger_result.proposal_description,
                    )

                    # LLM generates the actual code changes
                    proposal = generate_upgrade_proposal(
                        description=trigger_result.proposal_description,
                        rationale=trigger_result.proposal_rationale,
                        target_files=trigger_result.target_files,
                        base_model=base_model,
                        state=result,
                    )

                    if proposal is not None:
                        # Run through the safety pipeline
                        pipeline = SelfUpgradePipeline()
                        upgrade_result = pipeline.execute(proposal)

                        result["upgrade_applied"] = upgrade_result.success
                        result["upgrade_branch"] = upgrade_result.branch_name
                        result["upgrade_commit"] = upgrade_result.commit_hash
                        result["upgrade_errors"] = upgrade_result.errors

                        if upgrade_result.success:
                            # Clear accumulated signals for this task type
                            task_type = result.get("routed_task_type", "general")
                            shared_upgrade_trigger.clear_signals(task_type)
                            logger.info(
                                "Self-upgrade applied: branch=%s commit=%s",
                                upgrade_result.branch_name,
                                upgrade_result.commit_hash,
                            )
                        else:
                            logger.info(
                                "Self-upgrade proposal rejected: %s",
                                upgrade_result.errors,
                            )
                    else:
                        result["upgrade_applied"] = False
                        result["upgrade_errors"] = [
                            "LLM declined to propose changes"
                        ]
        except Exception as e:
            logger.debug("Self-upgrade analysis skipped: %s", e)

        return result

    workflow.add_node("skill_cleanup", skill_cleanup_wrapper)

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
