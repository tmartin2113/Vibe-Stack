"""Workflow node — writes a Tier 0 lesson at the end of a run if eligible.

Runs after the critic. Only fires when `state["lesson_eligible"]` is True
(set by the critic when score < 85 AND feedback is non-empty).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .lesson_store import LessonStore
from .self_upgrade.tier0_builder import Tier0Builder, Tier0Result

logger = logging.getLogger(__name__)


def memory_note_node(
    state: Dict[str, Any],
    *,
    lesson_store: LessonStore,
    tier0_builder: Tier0Builder,
) -> Dict[str, Any]:
    """Optionally write a lesson to the lesson store.

    Passes through `state` unchanged except for adding `lesson_written_id` when
    a lesson is successfully written.
    """
    if not state.get("lesson_eligible"):
        return state

    # The critic populated these flags; reading them for the builder
    signals = state.get("accumulated_signals", [])
    role = state.get("agent_role", "*")
    task_type = state.get("routed_task_type", "*")

    result = tier0_builder.build(
        signals,
        author_agent_id=state.get("agent_id", ""),
        author_run_id=state.get("run_id", ""),
        role=role,
    )

    if isinstance(result, Tier0Result.Empty):
        logger.debug("memory_note_node: builder empty (%s)", result.reason)
        return state

    assert isinstance(result, Tier0Result.LessonDrafted)
    lesson_id = lesson_store.add(
        role=result.role,
        task_type=result.task_type,
        tag=result.tag,
        lesson=result.lesson,
        author_agent_id=state.get("agent_id", ""),
        author_run_id=state.get("run_id", ""),
    )

    logger.info("memory_note_node: wrote lesson %s", lesson_id)
    state["lesson_written_id"] = lesson_id
    return state
