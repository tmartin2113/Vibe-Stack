"""End-to-end test: simulated signal accumulation → dispatcher → real LessonStore + fake Paperclip client.

Exercises the full M1 integration path with real builders and a real
LessonStore, mocking only the LLM (Tier0Builder and Tier3Builder) and
the PaperclipClient (which would otherwise require network + auth).
"""

import json
from unittest.mock import MagicMock

import pytest

from agents.lesson_store import LessonStore
from agents.self_upgrade.tier0_builder import Tier0Builder
from agents.self_upgrade.tier3_builder import Tier3Builder
from agents.self_upgrade_dispatcher import (
    DispatchResult,
    SelfUpgradeDispatcher,
)
from agents.self_upgrade_trigger import UpgradeSignal


def _signal(**overrides):
    defaults = dict(
        category="low_score",
        task_type="code_generation",
        detail="Missing Pydantic validation on request body",
        score=60,
        source_node="critic",
    )
    defaults.update(overrides)
    return UpgradeSignal(**defaults)


def test_single_actionable_signal_writes_lesson(tmp_path):
    """A single actionable signal → Tier 0 → lesson persisted to real LessonStore."""
    lesson_store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    fake_llm = MagicMock()
    fake_llm.generate.return_value = "Include Pydantic validation for FastAPI endpoints."

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=lesson_store,
        tier0_builder=Tier0Builder(llm=fake_llm),
        tier3_builder=MagicMock(),
        paperclip_client=MagicMock(),
        human_triage_user_id="user_prime",
    )

    result = dispatcher.dispatch(
        [_signal()],
        author_agent_id="agent_1",
        author_run_id="run_1",
        role="backend_engineer",
    )

    assert isinstance(result, DispatchResult.Tier0Written)

    # Lesson is retrievable from the real store
    lessons = lesson_store.list_by_scope(
        role="backend_engineer",
        task_type="code_generation",
    )
    assert len(lessons) == 1
    assert "Pydantic" in lessons[0].lesson
    assert lessons[0].author_agent_id == "agent_1"
    assert lessons[0].author_run_id == "run_1"


def test_empty_feedback_cluster_files_tier3_report(tmp_path):
    """3 empty-feedback signals → Tier 3 → real builder drafts + self-critiques → Paperclip issue filed."""
    lesson_store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    # Tier3Builder makes two LLM calls: one for the draft, one for self-critique.
    # side_effect queues responses in order.
    fake_llm = MagicMock()
    fake_llm.generate.side_effect = [
        json.dumps({
            "title": "Critic returns empty feedback",
            "hypothesis": "heuristic_critic falls through without feedback",
            "suggested_change": "Log a warning and skip signal persistence when feedback is empty",
            "suggested_change_kind": "code",
            "confidence": 0.8,
        }),
        json.dumps({"score": 85, "feedback": "clear and actionable"}),
    ]

    fake_client = MagicMock()
    fake_client.create_issue.return_value = MagicMock(id="iss_42")

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=lesson_store,
        tier0_builder=MagicMock(),
        tier3_builder=Tier3Builder(llm=fake_llm),
        paperclip_client=fake_client,
        human_triage_user_id="user_prime",
    )

    signals = [_signal(detail="") for _ in range(3)]
    result = dispatcher.dispatch(
        signals, author_agent_id="a", author_run_id="r", role="*",
    )

    assert isinstance(result, DispatchResult.Tier3Filed)
    assert result.issue_id == "iss_42"

    # Verify the Paperclip client was called with the right shape
    fake_client.create_issue.assert_called_once()
    kwargs = fake_client.create_issue.call_args.kwargs
    assert "self-upgrade" in kwargs["labels"]
    assert "tier-3" in kwargs["labels"]
    assert "auto-generated" in kwargs["labels"]
    assert kwargs["assignee_user_id"] == "user_prime"
    assert "[self-report]" in kwargs["title"]

    # The rendered description should contain the markdown report structure
    description = kwargs["description"]
    assert "## Hypothesis" in description
    assert "## Suggested change" in description
    assert "tier: 3" in description


def test_tier3_dropped_when_self_critique_fails(tmp_path):
    """When the Tier3 self-critique fails, no Paperclip issue is filed."""
    lesson_store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    fake_llm = MagicMock()
    fake_llm.generate.side_effect = [
        json.dumps({
            "title": "vague",
            "hypothesis": "unclear",
            "suggested_change": "maybe fix it",
            "suggested_change_kind": "code",
            "confidence": 0.3,
        }),
        json.dumps({"score": 40, "feedback": "too vague"}),  # below 70 threshold
    ]

    fake_client = MagicMock()

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=lesson_store,
        tier0_builder=MagicMock(),
        tier3_builder=Tier3Builder(llm=fake_llm),
        paperclip_client=fake_client,
        human_triage_user_id="user_prime",
    )

    signals = [_signal(detail="") for _ in range(3)]
    result = dispatcher.dispatch(signals)

    assert isinstance(result, DispatchResult.Rejected)
    assert "self-critique" in result.reason
    fake_client.create_issue.assert_not_called()


def test_lesson_outcome_scoring_closes_the_loop(tmp_path):
    """Write a lesson via Tier 0 → record uses → compute outcome_delta → verify scoring works."""
    lesson_store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    fake_llm = MagicMock()
    fake_llm.generate.return_value = "Always validate input at the boundary."

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=lesson_store,
        tier0_builder=Tier0Builder(llm=fake_llm),
        tier3_builder=MagicMock(),
        paperclip_client=MagicMock(),
    )

    # Dispatch produces a lesson
    result = dispatcher.dispatch(
        [_signal()],
        author_agent_id="agent_1",
        author_run_id="run_author",
        role="backend_engineer",
    )
    assert isinstance(result, DispatchResult.Tier0Written)
    lesson_id = result.lesson_id

    # Simulate 3 subsequent runs using this lesson, each scoring 85
    for i in range(3):
        lesson_store.record_use(lesson_id, run_id=f"run_{i}", run_score=85)

    # Recompute outcome_delta with a baseline of 70
    delta = lesson_store.recompute_outcome_delta(lesson_id, baseline_score=70.0)
    assert delta == 15.0

    # Verify the lesson now reports uses + delta
    lessons = lesson_store.list_by_scope(
        role="backend_engineer", task_type="code_generation",
    )
    assert lessons[0].uses == 3
    assert lessons[0].outcome_delta == 15.0
