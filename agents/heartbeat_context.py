"""
Heartbeat context helpers.

Task ranking, task type resolution, user request construction,
clarification resume detection, and complexity hint extraction.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

from .lesson_store import LessonStore
from .paperclip_client import Issue

logger = logging.getLogger(__name__)


def _rank_tasks(assignments: List[Issue]) -> List[Issue]:
    """
    Rank tasks by priority, returning a sorted list for fallthrough checkout.

    Priority order:
    1. PAPERCLIP_TASK_ID if set and in assignments (always first)
    2. in_progress tasks (resume existing work)
    3. todo tasks
    4. blocked only if explicitly woken for it
    """
    forced_task_id = os.environ.get("PAPERCLIP_TASK_ID", "").strip()
    ranked: List[Issue] = []
    seen_ids: set = set()

    # Forced task always first
    if forced_task_id:
        for issue in assignments:
            if issue.id == forced_task_id:
                ranked.append(issue)
                seen_ids.add(issue.id)
                break

    # in_progress first
    for issue in assignments:
        if issue.id not in seen_ids and issue.status == "in_progress":
            ranked.append(issue)
            seen_ids.add(issue.id)

    # then todo
    for issue in assignments:
        if issue.id not in seen_ids and issue.status == "todo":
            ranked.append(issue)
            seen_ids.add(issue.id)

    # blocked only if explicitly woken for it
    wake_reason = os.environ.get("PAPERCLIP_WAKE_REASON", "")
    if wake_reason in ("issue_comment_mentioned", "issue_assigned"):
        for issue in assignments:
            if issue.id not in seen_ids and issue.status == "blocked" and issue.id == forced_task_id:
                ranked.append(issue)
                seen_ids.add(issue.id)

    return ranked


def _resolve_task_type(config: Any) -> str:
    """Resolve the task type from config or env var."""
    if config.paperclip.task_type:
        return config.paperclip.task_type
    return os.environ.get("VIBE_TASK_TYPE", "")


def _load_lessons_for_run(
    *,
    lesson_store: "LessonStore",
    role: str,
    task_type: str,
) -> List[str]:
    """Return a list of formatted lesson strings for injection into user_request.

    Each string is formatted as ``- (lesson_id) lesson_text`` so the lesson_id
    can be parsed out later by injected_lesson_ids tracking.
    """
    matches = lesson_store.list_by_scope(
        role=role,
        task_type=task_type,
        status="active",
        limit=5,
    )
    return [
        f"- ({m.lesson_id}) {m.lesson}"
        for m in matches
    ]


def _build_user_request(
    issue: Issue,
    comments: list,
    clarification_reply: Optional[str] = None,
    *,
    lesson_store: "Optional[LessonStore]" = None,
    role: Optional[str] = None,
    task_type: Optional[str] = None,
    state: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build a user_request string from issue context.

    Includes title, description, ancestor chain (the "why"), and
    recent comments for additional context. When resuming from a
    clarification, the human's reply is injected prominently.

    If ``lesson_store``, ``role``, and ``task_type`` are all provided, appends a
    "## Lessons from past runs" block with up to 5 matching lessons. If
    ``state`` is also provided, the injected lesson_ids are recorded in
    ``state["injected_lesson_ids"]`` for later scoring attribution.
    """
    parts = []

    # Ancestor context (the "why" chain)
    if issue.ancestors:
        ancestor_chain = " → ".join(
            a.get("title", "Unknown") for a in reversed(issue.ancestors)
        )
        parts.append(f"Goal chain: {ancestor_chain}")

    # Primary task
    parts.append(f"Task: {issue.title}")
    if issue.description:
        parts.append(f"\n{issue.description}")

    # Human clarification reply — injected prominently before discussion
    if clarification_reply:
        parts.append(
            f"\n[Clarification from human]: {clarification_reply}"
        )

    # Recent comments for additional context (last 5)
    if comments:
        recent = comments[-5:]
        comment_text = "\n".join(f"- {c.body[:200]}" for c in recent)
        parts.append(f"\nRecent discussion:\n{comment_text}")

    # Tier 0 lesson injection — only if all lesson args were provided
    if lesson_store is not None and role and task_type:
        try:
            lessons = _load_lessons_for_run(
                lesson_store=lesson_store,
                role=role,
                task_type=task_type,
            )
            if lessons:
                parts.append("\n## Lessons from past runs\n" + "\n".join(lessons))
                if state is not None:
                    # Parse lesson_ids out of the "- (lesson_id) text" format
                    state["injected_lesson_ids"] = [
                        line.split(")", 1)[0].lstrip("- (")
                        for line in lessons
                    ]
        except Exception as e:
            logger.debug("lesson injection skipped: %s", e)

    return "\n".join(parts)


def _detect_clarification_resume(
    issue: Issue,
    comments: List[Any],
    agent_id: str,
) -> Optional[str]:
    """
    Detect if this heartbeat is a resume from a human clarification reply.

    Returns the human's reply text if all conditions are met:
    1. PAPERCLIP_WAKE_REASON is 'issue_comment_mentioned'
    2. PAPERCLIP_WAKE_COMMENT_ID is set
    3. The issue was previously blocked
    4. The wake comment is from a human (not this agent)

    Returns None if this is a normal (non-resume) invocation.
    """
    wake_reason = os.environ.get("PAPERCLIP_WAKE_REASON", "").strip()
    wake_comment_id = os.environ.get("PAPERCLIP_WAKE_COMMENT_ID", "").strip()

    if wake_reason != "issue_comment_mentioned" or not wake_comment_id:
        return None

    if issue.status != "blocked":
        return None

    # Find the specific wake comment
    for comment in comments:
        if comment.id == wake_comment_id:
            # Ensure it's from a human, not this agent echoing itself
            if comment.author_agent_id and comment.author_agent_id == agent_id:
                logger.debug("Wake comment %s is from self — not a clarification", wake_comment_id)
                return None
            # Strip whitespace; treat empty/whitespace-only replies as no reply
            body = (comment.body or "").strip()
            if not body:
                logger.debug("Wake comment %s has empty body — skipping", wake_comment_id)
                return None
            return body

    logger.warning("Wake comment %s not found in issue comments", wake_comment_id)
    return None


def _extract_complexity_hint(description: str) -> str:
    """Extract complexity tier from orchestrator-embedded HTML comment."""
    match = re.search(r"<!-- complexity:(\w+) -->", description or "")
    return match.group(1) if match else ""
