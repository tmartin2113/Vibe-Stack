from datetime import datetime

from agents.lesson_store import Lesson, LessonStore


def test_store_and_retrieve_by_scope(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    lesson_id = store.add(
        role="backend_engineer",
        task_type="code_generation",
        tag="fastapi",
        lesson="When generating FastAPI endpoints, always include Pydantic request validation.",
        author_agent_id="agent_123",
        author_run_id="run_456",
    )

    assert lesson_id.startswith("lesson_")

    matches = store.list_by_scope(
        role="backend_engineer",
        task_type="code_generation",
    )
    assert len(matches) == 1
    assert matches[0].lesson.startswith("When generating")
    assert matches[0].uses == 0
    assert matches[0].outcome_delta is None
    assert matches[0].status == "active"


def test_list_by_scope_matches_wildcards(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    # Role-specific
    store.add(role="backend_engineer", task_type="code_generation",
              tag="", lesson="A", author_agent_id="", author_run_id="")
    # Role-wildcard (applies to any role)
    store.add(role="*", task_type="code_generation",
              tag="", lesson="B", author_agent_id="", author_run_id="")
    # Different task type
    store.add(role="backend_engineer", task_type="research",
              tag="", lesson="C", author_agent_id="", author_run_id="")

    matches = store.list_by_scope(
        role="backend_engineer",
        task_type="code_generation",
    )
    assert len(matches) == 2
    lessons = {m.lesson for m in matches}
    assert lessons == {"A", "B"}


def test_list_by_scope_respects_status_filter(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    lesson_id = store.add(role="*", task_type="*", tag="",
                          lesson="X", author_agent_id="", author_run_id="")
    store.set_status(lesson_id, "decayed")

    assert store.list_by_scope(role="r", task_type="t", status="active") == []
    assert len(store.list_by_scope(role="r", task_type="t", status="decayed")) == 1
