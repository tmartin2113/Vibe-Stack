"""
Memory persistence — write-back layer for the long-term memory store.

Captures the artefacts of a completed workflow run into MemoryStore so
subsequent runs can recall them via inject_memory. This is the symmetric
counterpart to graph_nodes.inject_memory (the read side).

Two entry points:

- ``persist_memory_node(state)`` — graph node, called after the critic /
  aggregator approves output. Writes spec, output summary, decisions, and
  any clarification questions tagged with the routed task type.

- ``persist_partial_state(partial_state, agent_id, task_id, status)`` —
  used by the heartbeat SIGTERM/clarification/blocked branches when the
  graph never reaches the persist node. Best-effort, never raises.

All writes are scoped by ``agent_id`` (heartbeat agent role/name) and
``task_id`` (Paperclip issue id) so subsequent recall stays scoped.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Maximum length of any single memory entry written by persist.
# Long outputs get truncated with an ellipsis to keep the store useful
# for recall (FTS handles long content but we don't want to dilute hits).
_MAX_CONTENT_CHARS = 4000

# Importance buckets used by the persist layer. The graph node sets
# importance based on the critic score; clarifications and blockers get
# fixed buckets so they're easy to find later.
_IMPORTANCE_CLARIFICATION = 0.7
_IMPORTANCE_BLOCKER = 0.6
_IMPORTANCE_PARTIAL = 0.4


def _truncate(text: str, limit: int = _MAX_CONTENT_CHARS) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _shared_store():
    """Return the shared MemoryStore singleton (lazy import)."""
    from .tools.registry import _get_shared_memory_store
    return _get_shared_memory_store()


def _safe_store(
    store,
    *,
    content: str,
    source: str,
    tags: str,
    agent_id: str,
    task_id: str,
    importance: float,
) -> Optional[int]:
    """Write a single memory entry, swallowing all errors."""
    if not content or not content.strip():
        return None
    try:
        return store.store(
            content=_truncate(content),
            source=source,
            tags=tags,
            agent_id=agent_id,
            task_id=task_id,
            importance=importance,
        )
    except Exception as e:
        logger.debug("persist_memory: store skipped (%s)", e)
        return None


def persist_memory_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Graph node — persist completed-run artifacts into memory.

    Idempotent within a run via the dedup hash. Safe to call from any
    workflow path; missing fields are silently skipped.
    """
    agent_id = (state.get("agent_id") or "").strip()
    task_id = (state.get("task_id") or state.get("session_id") or "").strip()
    routed = state.get("routed_task_type") or state.get("task_type") or "general"

    output = state.get("final_output") or state.get("specialist_output") or ""
    spec = state.get("specification") or state.get("user_request") or ""
    score = state.get("final_score") or state.get("output_critic_score") or 0
    feedback = state.get("output_critic_feedback", "")

    importance = max(0.0, min(1.0, float(score) / 100.0)) if score else 0.5
    base_tags = f"{routed} persist"

    persisted: List[int] = []
    try:
        store = _shared_store()
    except Exception as e:
        logger.debug("persist_memory_node: store unavailable (%s)", e)
        state["memory_persisted_ids"] = []
        return state

    # Specification — what we set out to do.
    if spec:
        mid = _safe_store(
            store,
            content=f"Specification: {spec}",
            source=f"agent:{agent_id}" if agent_id else "agent",
            tags=f"{base_tags} spec",
            agent_id=agent_id,
            task_id=task_id,
            importance=importance,
        )
        if mid:
            persisted.append(mid)

    # Approved output — the result the critic accepted.
    if output:
        mid = _safe_store(
            store,
            content=f"Result (score {int(score)}): {output}",
            source=f"agent:{agent_id}" if agent_id else "agent",
            tags=f"{base_tags} result",
            agent_id=agent_id,
            task_id=task_id,
            importance=importance,
        )
        if mid:
            persisted.append(mid)

    # Critic feedback — useful for debugging future regressions.
    if feedback:
        mid = _safe_store(
            store,
            content=f"Critic feedback: {feedback}",
            source=f"tool:critic",
            tags=f"{base_tags} feedback",
            agent_id=agent_id,
            task_id=task_id,
            importance=max(0.3, importance - 0.1),
        )
        if mid:
            persisted.append(mid)

    # Tool calls — record the names so the next run knows which were used.
    tool_calls = state.get("tool_calls_made") or []
    if tool_calls:
        names = [
            (tc.get("tool") or tc.get("name") or "").strip()
            for tc in tool_calls
            if isinstance(tc, dict)
        ]
        names = [n for n in names if n]
        if names:
            mid = _safe_store(
                store,
                content="Tools used: " + ", ".join(sorted(set(names))),
                source="tool:registry",
                tags=f"{base_tags} tools",
                agent_id=agent_id,
                task_id=task_id,
                importance=0.3,
            )
            if mid:
                persisted.append(mid)

    state["memory_persisted_ids"] = persisted
    if persisted:
        logger.info(
            "persist_memory: wrote %d entries (agent=%s task=%s)",
            len(persisted), agent_id or "-", task_id or "-",
        )
    return state


def persist_partial_state(
    partial_state: Dict[str, Any],
    *,
    agent_id: str,
    task_id: str,
    status: str,
) -> List[int]:
    """Best-effort write for SIGTERM / clarification / blocked branches.

    The graph never reaches ``persist_memory_node`` in these branches, so
    this function is called directly from ``heartbeat.py`` to ensure the
    next run still benefits from whatever the agent learned.

    Returns the list of memory IDs that were successfully written.
    """
    if not partial_state:
        return []

    persisted: List[int] = []
    try:
        store = _shared_store()
    except Exception as e:
        logger.debug("persist_partial_state: store unavailable (%s)", e)
        return persisted

    routed = (
        partial_state.get("routed_task_type")
        or partial_state.get("task_type")
        or "general"
    )
    base_tags = f"{routed} persist {status}"
    last_node = partial_state.get("last_node", "unknown")

    output = partial_state.get("specialist_output") or partial_state.get(
        "final_output", ""
    )
    spec = partial_state.get("specification") or partial_state.get(
        "user_request", ""
    )

    if status == "clarification_needed":
        questions = partial_state.get("clarification_questions") or []
        if questions:
            content = "Clarification requested at {node}: {qs}".format(
                node=last_node,
                qs="; ".join(str(q) for q in questions),
            )
            mid = _safe_store(
                store,
                content=content,
                source=f"agent:{agent_id}" if agent_id else "agent",
                tags=f"{base_tags} clarification",
                agent_id=agent_id,
                task_id=task_id,
                importance=_IMPORTANCE_CLARIFICATION,
            )
            if mid:
                persisted.append(mid)
        if spec:
            mid = _safe_store(
                store,
                content=f"Specification (clarification pending): {spec}",
                source=f"agent:{agent_id}" if agent_id else "agent",
                tags=f"{base_tags} spec",
                agent_id=agent_id,
                task_id=task_id,
                importance=_IMPORTANCE_CLARIFICATION,
            )
            if mid:
                persisted.append(mid)
        return persisted

    if status == "blocked":
        score = partial_state.get("output_critic_score", 0) or partial_state.get(
            "heuristic_critic_score", 0
        )
        feedback = partial_state.get("output_critic_feedback", "")
        if output:
            mid = _safe_store(
                store,
                content=f"Blocked at {last_node} (score {score}): {output}",
                source=f"agent:{agent_id}" if agent_id else "agent",
                tags=f"{base_tags} blocked",
                agent_id=agent_id,
                task_id=task_id,
                importance=_IMPORTANCE_BLOCKER,
            )
            if mid:
                persisted.append(mid)
        if feedback:
            mid = _safe_store(
                store,
                content=f"Blocker feedback: {feedback}",
                source="tool:critic",
                tags=f"{base_tags} feedback",
                agent_id=agent_id,
                task_id=task_id,
                importance=_IMPORTANCE_BLOCKER,
            )
            if mid:
                persisted.append(mid)
        return persisted

    # Default: SIGTERM / interrupted / cancelled — record what we have.
    if output:
        mid = _safe_store(
            store,
            content=f"Partial output at {last_node}: {output}",
            source=f"agent:{agent_id}" if agent_id else "agent",
            tags=f"{base_tags} partial",
            agent_id=agent_id,
            task_id=task_id,
            importance=_IMPORTANCE_PARTIAL,
        )
        if mid:
            persisted.append(mid)
    return persisted


__all__ = [
    "persist_memory_node",
    "persist_partial_state",
]
