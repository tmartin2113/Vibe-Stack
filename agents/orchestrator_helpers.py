"""
Orchestrator Helper Functions

Pure-function utilities for agent lookup, subtask filtering,
rebalancing, strategy/result extraction, and adapter registry creation.

Extracted from orchestrator.py for maintainability.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from .config import SystemConfig
from .paperclip_client import (
    AgentInfo,
    Comment,
    Issue,
    PaperclipAPIError,
    PaperclipClient,
)

if TYPE_CHECKING:
    from .adapters import AdapterRegistry

logger = logging.getLogger(__name__)

# Patterns shared with orchestrator.py
STRATEGY_PATTERN = re.compile(r"<!-- strategy:(\w+) -->")
RETRY_MARKER_PATTERN = re.compile(r"<!-- retry:(\d+) -->")
RESULT_SCORE_PATTERN = re.compile(
    r"## Completed \(score: (\d+)/100\)\s*\n\n(.*)",
    re.DOTALL,
)

# Maps keywords found in agent roles/titles to task types
_TASK_TYPE_KEYWORDS: Dict[str, List[str]] = {
    "code": ["code_generation", "refactoring", "debugging"],
    "test": ["test_generation"],
    "security": ["security_audit"],
    "doc": ["documentation"],
    "research": ["research", "general"],
    "performance": ["performance_optimization"],
    "data": ["data_processing"],
    "api": ["api_development"],
    "database": ["database_operations"],
    "review": ["code_review"],
}

_MAX_REBALANCE_PER_CYCLE = 2
_BACKLOG_THRESHOLD = 3  # An agent is backlogged if it has >= this many pending tasks


def _build_agent_lookup(
    agents: List[AgentInfo],
    self_agent_id: str,
) -> Dict[str, AgentInfo]:
    """
    Build a lookup mapping task-type keywords to agent info.

    Excludes the orchestrator itself. Matches agent roles to task types
    using keyword overlap (e.g., role "test_generator" matches "test_generation").
    """
    lookup: Dict[str, AgentInfo] = {}
    for agent in agents:
        if agent.id == self_agent_id:
            continue
        if agent.status != "active":
            continue

        # Use role as primary matching key
        role_lower = agent.role.lower()
        # Also check title for additional context
        title_lower = agent.title.lower() if agent.title else ""

        # Sort keywords longest-first so "review" matches before "code"
        # in roles like "code_reviewer", and "security" before "sec", etc.
        for keyword in sorted(_TASK_TYPE_KEYWORDS, key=len, reverse=True):
            if keyword in role_lower or keyword in title_lower:
                for task_type in _TASK_TYPE_KEYWORDS[keyword]:
                    if task_type not in lookup:
                        lookup[task_type] = agent

    return lookup


def _normalize_subtask_title(title: str) -> str:
    """Normalize a subtask title for dedup comparison.

    Lowercases and strips whitespace. Keeps the task-type prefix
    (e.g. '[code_generation]') so that different task types for the
    same parent are not treated as duplicates.
    """
    return title.lower().strip()


def _filter_duplicate_subtasks(
    proposed: List[Dict[str, Any]],
    existing_children: List[Issue],
    parent_title: str,
) -> List[Dict[str, Any]]:
    """Filter out proposed subtasks whose generated title would match an existing child."""
    existing_titles = set()
    for child in existing_children:
        existing_titles.add(_normalize_subtask_title(child.title))

    filtered = []
    for sub_task in proposed:
        task_type = sub_task.get("task_type", "general")
        would_be_title = f"[{task_type}] {parent_title}"
        normalized = _normalize_subtask_title(would_be_title)
        if normalized in existing_titles:
            logger.warning("Skipping duplicate subtask: %s (matches existing child)", would_be_title)
            continue
        filtered.append(sub_task)

    return filtered


def _rebalance_children(
    client,
    children: List[Issue],
    agents: List,  # AgentInfo or similar
) -> int:
    """
    Reassign pending subtasks from backlogged agents to idle ones.

    A backlogged agent has >= _BACKLOG_THRESHOLD pending (todo) tasks.
    An idle agent has all assigned tasks completed (status='done').

    Returns the number of tasks reassigned.
    """
    # Build per-agent task counts
    agent_pending: Dict[str, List[Issue]] = {}
    agent_done: Dict[str, int] = {}

    for child in children:
        aid = getattr(child, "assignee_agent_id", None) or ""
        if not aid:
            continue
        if child.status in ("todo",):  # Only reassign todo, not in_progress
            agent_pending.setdefault(aid, []).append(child)
        elif child.status == "done":
            agent_done[aid] = agent_done.get(aid, 0) + 1

    # Find backlogged and idle agents
    backlogged = {aid: tasks for aid, tasks in agent_pending.items()
                  if len(tasks) >= _BACKLOG_THRESHOLD}

    all_agent_ids = {getattr(c, "assignee_agent_id", "") for c in children} - {""}
    idle_agents = [aid for aid in all_agent_ids
                   if aid not in agent_pending and agent_done.get(aid, 0) > 0]

    if not backlogged or not idle_agents:
        return 0

    reassigned = 0
    idle_idx = 0

    for overloaded_id, pending_tasks in backlogged.items():
        for task in pending_tasks:
            if reassigned >= _MAX_REBALANCE_PER_CYCLE:
                break
            if idle_idx >= len(idle_agents):
                break

            target_id = idle_agents[idle_idx]
            try:
                client.update_issue(task.id, assignee_agent_id=target_id)
                client.add_comment(
                    task.id,
                    f"<!-- rebalanced-from:{overloaded_id} --> "
                    f"Rebalanced from overloaded agent to idle agent.",
                )
                reassigned += 1
                logger.info(
                    "Rebalanced %s from %s to %s",
                    task.id, overloaded_id, target_id,
                )
            except Exception as e:
                logger.warning("Failed to rebalance %s: %s", task.id, e)

            idle_idx += 1

    if reassigned:
        logger.info("Rebalanced %d tasks across agents", reassigned)
    return reassigned


def _match_agent(
    task_type: str,
    agent_lookup: Dict[str, AgentInfo],
) -> Optional[str]:
    """
    Find the best agent for a task type.

    Returns the agent_id if a match is found, None otherwise (Paperclip
    can assign the task later via its own routing).
    """
    agent = agent_lookup.get(task_type)
    if agent:
        return agent.id

    # Fallback: try "general" agent
    general_agent = agent_lookup.get("general")
    if general_agent:
        return general_agent.id

    return None


def _find_agent_name(agents: List[AgentInfo], agent_id: Optional[str]) -> str:
    """Find agent name by ID."""
    if not agent_id:
        return "unassigned"
    for agent in agents:
        if agent.id == agent_id:
            return agent.name or agent.role
    return agent_id[:8]


def _create_aggregation_registry(config: SystemConfig) -> Optional["AdapterRegistry"]:
    """
    Create a minimal adapter registry for LLM-driven aggregation.

    Returns an AdapterRegistry with a 'vibe' adapter if the LLM backend
    is available, or None to fall back to structured concatenation.
    """
    try:
        from .adapters import AdapterRegistry, PromptAdapter, VIBE_SYSTEM_PROMPT
        from .llm_backend import create_backend_from_config

        backend = create_backend_from_config(config)

        # Verify the backend is actually reachable before committing to LLM aggregation.
        # Without this, a non-functional backend would only fail at aggregation time,
        # silently falling back to concatenation after wasting time on the attempt.
        if not backend.health_check():
            logger.warning("LLM backend health check failed, using fallback concatenation")
            return None

        registry = AdapterRegistry()
        adapter = PromptAdapter(
            "vibe", VIBE_SYSTEM_PROMPT, backend,
            config=config.generation.get_config("vibe") if hasattr(config, "generation") else {},
        )
        registry.register(adapter)
        return registry
    except Exception as e:
        logger.warning(
            "Could not create LLM adapter for aggregation, using fallback concatenation: %s", e
        )
        return None


def _extract_strategy(client: PaperclipClient, issue: Issue) -> str:
    """Extract aggregation strategy from the orchestrator's plan comment."""
    try:
        comments = client.get_comments(issue.id)
    except PaperclipAPIError:
        return "merge"

    for comment in comments:
        match = STRATEGY_PATTERN.search(comment.body)
        if match:
            return match.group(1)

    return "merge"


def _extract_task_type(title: str) -> str:
    """Extract task_type from child issue title like '[test_generation] Build API'."""
    match = re.match(r"\[(\w+)\]", title)
    if match:
        return match.group(1)
    return "general"


def _extract_child_result(
    client: PaperclipClient,
    child: Issue,
) -> Tuple[str, int]:
    """
    Extract the result output and score from a child issue's comments.

    Looks for the standard format posted by heartbeat.py:
    '## Completed (score: 85/100)\n\n<output>'

    Returns:
        Tuple of (output_text, score). Empty string and 0 if not found.
    """
    try:
        comments = client.get_comments(child.id)
    except PaperclipAPIError:
        return "", 0

    # Search comments in reverse (most recent first)
    for comment in reversed(comments):
        match = RESULT_SCORE_PATTERN.search(comment.body)
        if match:
            score = int(match.group(1))
            output = match.group(2).strip()
            return output, score

    logger.warning(
        "Child %s (%s) is done but has no result comment matching expected format. "
        "Output will be missing from aggregation.",
        child.id, child.title,
    )
    return "", 0
