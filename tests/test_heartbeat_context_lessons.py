"""Tests for _load_lessons_for_run helper in heartbeat_context."""
from unittest.mock import MagicMock

from agents.heartbeat_context import _load_lessons_for_run


def test_load_lessons_returns_empty_when_no_matches():
    store = MagicMock()
    store.list_by_scope.return_value = []

    lessons = _load_lessons_for_run(
        lesson_store=store,
        role="backend_engineer",
        task_type="code_generation",
    )
    assert lessons == []


def test_load_lessons_formats_matching_lessons_for_injection():
    from agents.lesson_store import Lesson

    store = MagicMock()
    store.list_by_scope.return_value = [
        Lesson(
            lesson_id="lesson_1", role="backend_engineer",
            task_type="code_generation", tag="",
            lesson="Always include Pydantic validation.",
            author_agent_id="", author_run_id="",
            created_at="2026-04-06T00:00:00Z",
        ),
    ]

    lessons = _load_lessons_for_run(
        lesson_store=store,
        role="backend_engineer",
        task_type="code_generation",
    )

    assert len(lessons) == 1
    assert "Pydantic" in lessons[0]
    store.list_by_scope.assert_called_once_with(
        role="backend_engineer",
        task_type="code_generation",
        status="active",
        limit=5,
    )


def test_build_user_request_injects_lessons_when_store_provided():
    """_build_user_request appends a Lessons block when lesson_store + role + task_type are passed."""
    from agents.heartbeat_context import _build_user_request
    from agents.lesson_store import Lesson
    from agents.paperclip_client import Issue

    fake_store = MagicMock()
    fake_store.list_by_scope.return_value = [
        Lesson(
            lesson_id="lesson_abc", role="backend_engineer",
            task_type="code_generation", tag="",
            lesson="Always validate input.",
            author_agent_id="", author_run_id="",
            created_at="",
        ),
    ]

    fake_issue = Issue(
        id="1", title="Test task", description="do a thing",
    )

    state = {}
    result = _build_user_request(
        fake_issue,
        comments=[],
        lesson_store=fake_store,
        role="backend_engineer",
        task_type="code_generation",
        state=state,
    )

    assert "## Lessons from past runs" in result
    assert "Always validate input" in result
    assert state.get("injected_lesson_ids") == ["lesson_abc"]


def test_build_user_request_no_lessons_block_when_store_missing():
    """_build_user_request does NOT add a Lessons block when lesson_store is None."""
    from agents.heartbeat_context import _build_user_request
    from agents.paperclip_client import Issue

    fake_issue = Issue(
        id="1", title="Test task", description="do a thing",
    )

    result = _build_user_request(fake_issue, comments=[])
    assert "Lessons from past runs" not in result
