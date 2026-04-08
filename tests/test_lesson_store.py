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


def test_record_use_and_compute_outcome_delta(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))

    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="lesson", author_agent_id="", author_run_id="")

    # Record 3 runs that used this lesson with scores 80, 85, 90 (avg 85)
    store.record_use(lesson_id, run_id="run_1", run_score=80)
    store.record_use(lesson_id, run_id="run_2", run_score=85)
    store.record_use(lesson_id, run_id="run_3", run_score=90)

    # Baseline is passed explicitly in M1 (M2+ may compute lazily)
    delta = store.recompute_outcome_delta(lesson_id, baseline_score=70.0)

    # avg(80, 85, 90) = 85, baseline 70 → delta 15
    assert delta == 15.0

    lessons = store.list_by_scope(role="r", task_type="t")
    assert lessons[0].uses == 3
    assert lessons[0].outcome_delta == 15.0


def test_record_use_is_idempotent_per_run(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))
    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="lesson", author_agent_id="", author_run_id="")

    store.record_use(lesson_id, run_id="run_1", run_score=80)
    store.record_use(lesson_id, run_id="run_1", run_score=80)  # duplicate

    lessons = store.list_by_scope(role="r", task_type="t")
    assert lessons[0].uses == 1


def test_decay_check_marks_underperforming_lessons(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))
    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="bad", author_agent_id="", author_run_id="")

    # 10 uses, all scoring 50, baseline 70 → delta -20
    for i in range(10):
        store.record_use(lesson_id, run_id=f"run_{i}", run_score=50)
    store.recompute_outcome_delta(lesson_id, baseline_score=70.0)

    decayed = store.decay_check(min_uses=10)
    assert lesson_id in decayed

    lessons = store.list_by_scope(role="r", task_type="t", status="active")
    assert len(lessons) == 0  # It's been decayed

    decayed_list = store.list_by_scope(role="r", task_type="t", status="decayed")
    assert len(decayed_list) == 1


def test_decay_check_preserves_good_lessons(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))
    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="good", author_agent_id="", author_run_id="")

    for i in range(10):
        store.record_use(lesson_id, run_id=f"run_{i}", run_score=90)
    store.recompute_outcome_delta(lesson_id, baseline_score=70.0)

    decayed = store.decay_check(min_uses=10)
    assert lesson_id not in decayed


def test_decay_check_ignores_low_use_lessons(tmp_path):
    store = LessonStore(db_path=str(tmp_path / "lessons.db"))
    lesson_id = store.add(role="r", task_type="t", tag="",
                          lesson="early", author_agent_id="", author_run_id="")

    # Only 5 uses — not enough to judge
    for i in range(5):
        store.record_use(lesson_id, run_id=f"run_{i}", run_score=50)
    store.recompute_outcome_delta(lesson_id, baseline_score=70.0)

    decayed = store.decay_check(min_uses=10)
    assert lesson_id not in decayed
