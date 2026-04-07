"""
Node Wrapper Factory

Creates all node wrapper functions that close over shared dependencies
(skill registry, outcome store, artifact store, base model, config, etc.).

Extracted from graph.py to keep the graph builder focused on wiring.
"""

import logging
from typing import Optional, Any, Dict, Callable

from .state import AgentState
from .router import route_to_specialist
from .aggregator import aggregate_outputs
from .artifact_store import ArtifactStore
from .parallel_subtasks import execute_parallel_subtasks
from .self_upgrade_dispatcher import DispatchResult, SelfUpgradeDispatcher

logger = logging.getLogger(__name__)


def _run_self_upgrade_dispatch(
    trigger: "SelfUpgradeTrigger",
    task_type: str,
) -> "DispatchResult.AnyResult":
    """End-of-run hook: invoke the dispatcher with any undispatched signals.

    Logs the result. On success tiers, marks the contributing signals with
    the artifact_ref so they aren't re-dispatched.
    """
    signals = trigger.get_accumulated_signals(task_type)
    if not signals:
        logger.debug("Self-upgrade dispatch: no accumulated signals")
        return DispatchResult.Rejected(reason="no signals", signal_refs=[])

    dispatcher = SelfUpgradeDispatcher()
    result = dispatcher.dispatch(signals)

    logger.info(
        "Self-upgrade dispatch result: %s",
        type(result).__name__,
    )

    # Mark signals as dispatched when the result carries an artifact_ref.
    # M0: only Tier0Written and Tier3Filed actually emit refs; Tier1a/1b/2
    # branches will be wired in later milestones.
    artifact_ref: Optional[str] = None
    if isinstance(result, DispatchResult.Tier0Written):
        artifact_ref = result.lesson_id
    elif isinstance(result, DispatchResult.Tier3Filed):
        artifact_ref = result.issue_id

    if artifact_ref:
        # Use result.signal_refs (which the dispatcher computed for this
        # specific tier) rather than the full accumulated signals list. In
        # M0 these sets are identical, but in M1+ a classifier may select
        # only a subset, and marking the wrong superset would prevent the
        # remaining signals from being re-dispatched on the next run.
        trigger.mark_artifact_ref(result.signal_refs, artifact_ref)

    return result


def create_node_wrappers(
    nodes,
    shared_skill_registry,
    shared_outcome_store,
    shared_artifact_store: Optional[ArtifactStore],
    shared_upgrade_trigger,
    shared_lesson_store,
    base_model,
    config,
    adapter_registry,
    tool_registry,
    cancellation_token,
) -> Dict[str, Callable]:
    """Create all node wrapper functions with shared dependencies.

    Returns a dict mapping node names to their wrapper callables.
    Each wrapper closes over the shared state created in create_agent_graph().
    """

    # --- Skill generation imports (lazy, matching original) ---
    from .skill_generator import generate_skills
    from .skill_loader import load_skills
    from .memory_store import MemoryStore
    from .memory_persist import persist_memory_node
    from .tools.registry import _get_shared_memory_store
    from .skill_cleanup import cleanup_skills

    # ===== ROUTER =====
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

    # ===== SKILL GENERATOR =====
    def skill_generator_wrapper(state: AgentState) -> AgentState:
        """Generate ephemeral skills for any capabilities the router couldn't match."""
        return generate_skills(
            state,
            skill_registry=shared_skill_registry,
            outcome_store=shared_outcome_store,
            base_model=base_model,
            adapter_registry=adapter_registry,
        )

    # ===== SKILL LOADER =====
    def skill_loader_wrapper(state: AgentState) -> AgentState:
        """Load SKILL.md content for all skills discovered by the router."""
        return load_skills(state, skill_registry=shared_skill_registry)

    # ===== MEMORY INJECTION =====
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
            agent_id = (state.get("agent_id") or "").strip()
            task_id = (state.get("task_id") or state.get("session_id") or "").strip()
            results = store.hybrid_recall(
                query=user_request,
                max_results=5,
                agent_id=agent_id,
                task_id=task_id,
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

    # ===== CACHE LOOKUP =====
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
        state["output_critic_score"] = entry.output_critic_score
        state["final_output"] = entry.specialist_output
        state["final_score"] = entry.final_score
        state["tool_calls_made"] = entry.tool_calls

        logger.info(
            f"Cache HIT: {cache_key[:12]}... "
            f"(score={entry.final_score}, hits={entry.access_count}, "
            f"task={entry.task_type})"
        )
        return state

    # ===== CRITIC OUTPUT =====
    def critic_output_wrapper(state: AgentState) -> AgentState:
        """Evaluate specialist output."""
        return nodes.evaluate_output(state)

    # ===== SUB CRITIC OUTPUT =====
    def sub_critic_output_wrapper(state: AgentState) -> AgentState:
        """Evaluate sub-task output."""
        return nodes.evaluate_sub_output(state)

    # ===== PARALLEL SUBTASKS =====
    def parallel_subtasks_wrapper(state: AgentState) -> AgentState:
        """Execute all sub-tasks concurrently using thread pool.

        Passes adapter_registry so the simulation sidecar can create
        persona LLM calls on the same base_model.
        """
        return execute_parallel_subtasks(
            state, nodes, config,
            adapter_registry=adapter_registry,
        )

    # ===== AGGREGATOR =====
    def aggregator_wrapper(state: AgentState) -> AgentState:
        """Wrapper to pass adapter registry to LLM-driven aggregator."""
        return aggregate_outputs(state, adapter_registry=adapter_registry)

    # ===== FINAL CRITIC =====
    def final_critic_wrapper(state: AgentState) -> AgentState:
        """Evaluate aggregated output."""
        return nodes.evaluate_aggregated_output(state)

    # ===== SKILL CLEANUP =====
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
            specialist_output = result.get("specialist_output", "")
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

        # Self-upgrade end-of-run hook: accumulate signals via the trigger,
        # then invoke the dispatcher to route to the appropriate tier builder.
        # In M0 every dispatch returns Rejected("stub"); real tier routing
        # comes online in M1+.
        try:
            from .self_upgrade_trigger import analyse_for_upgrade
            from .self_upgrade import is_self_upgrade_enabled

            if is_self_upgrade_enabled():
                trigger_result = analyse_for_upgrade(
                    result, trigger=shared_upgrade_trigger,
                )
                if trigger_result.signals:
                    result["upgrade_signals"] = [
                        {"category": s.category, "detail": s.detail}
                        for s in trigger_result.signals
                    ]

                task_type = result.get("routed_task_type", "general")
                dispatch_result = _run_self_upgrade_dispatch(
                    shared_upgrade_trigger, task_type,
                )
                result["upgrade_dispatch_result"] = type(dispatch_result).__name__
        except Exception as e:
            logger.warning("Self-upgrade dispatch skipped: %s", e)

        return result

    # ===== PERSIST MEMORY =====
    def persist_memory_wrapper(state):
        """Write run artifacts (spec, output, feedback, tools) into MemoryStore.

        Runs on every terminal path so subsequent heartbeats can recall the
        agent's prior decisions via inject_memory. Best-effort; never raises.
        """
        try:
            return persist_memory_node(state)
        except Exception as e:
            logger.debug("persist_memory_wrapper: skipped (%s)", e)
            return state

    # ===== MEMORY NOTE (Tier 0 lesson writer) =====
    def memory_note_wrapper(state: AgentState) -> AgentState:
        """Write a Tier 0 lesson when the critic flagged lesson_eligible.

        Lazy-imports Tier0Builder to avoid circular imports, and constructs
        it per-call with the shared base_model so the builder reuses the same
        LLM backend as the rest of the workflow. The builder is stateless so
        re-creation per call is cheap.
        """
        from .memory_note_node import memory_note_node
        from .self_upgrade.tier0_builder import Tier0Builder

        try:
            builder = Tier0Builder(llm=base_model)
            return memory_note_node(
                state,
                lesson_store=shared_lesson_store,
                tier0_builder=builder,
            )
        except Exception as e:
            logger.debug("memory_note_wrapper: skipped (%s)", e)
            return state

    # ===== RECORD LESSON USES (Tier 0 outcome scoring) =====
    def record_lesson_uses_wrapper(state: AgentState) -> AgentState:
        """Record each injected lesson's use with the run's final score.

        Runs after memory_note so that any newly-written lesson from this run
        is NOT counted as a use (a lesson can't apply to the run that authored
        it). Safe pass-through — state is unchanged except for the side effect
        on lesson_store.
        """
        from .memory_note_node import record_lesson_uses_node
        try:
            return record_lesson_uses_node(state, lesson_store=shared_lesson_store)
        except Exception as e:
            logger.debug("record_lesson_uses_wrapper: skipped (%s)", e)
            return state

    # ===== RETURN ALL WRAPPERS =====
    return {
        "router": router_wrapper,
        "skill_generator": skill_generator_wrapper,
        "skill_loader": skill_loader_wrapper,
        "inject_memory": inject_memory,
        "cache_lookup": cache_lookup,
        "specialist": nodes.execute_with_specialist,
        "sub_specialist": nodes.execute_sub_task,
        "critic_output": critic_output_wrapper,
        "sub_critic_output": sub_critic_output_wrapper,
        "parallel_subtasks": parallel_subtasks_wrapper,
        "aggregator": aggregator_wrapper,
        "final_critic": final_critic_wrapper,
        "skill_cleanup": skill_cleanup_wrapper,
        "persist_memory": persist_memory_wrapper,
        "memory_note": memory_note_wrapper,
        "record_lesson_uses": record_lesson_uses_wrapper,
    }
