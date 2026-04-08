"""Tests for record_lesson_uses_node in memory_note_node module."""
from unittest.mock import MagicMock

from agents.memory_note_node import record_lesson_uses_node


def test_record_uses_writes_one_use_per_injected_lesson():
    fake_store = MagicMock()

    state = {
        "injected_lesson_ids": ["lesson_1", "lesson_2"],
        "run_id": "run_abc",
        "output_critic_score": 88,
    }

    record_lesson_uses_node(state, lesson_store=fake_store)

    assert fake_store.record_use.call_count == 2
    fake_store.record_use.assert_any_call(
        "lesson_1", run_id="run_abc", run_score=88,
    )
    fake_store.record_use.assert_any_call(
        "lesson_2", run_id="run_abc", run_score=88,
    )


def test_record_uses_noop_when_no_injected_lessons():
    fake_store = MagicMock()
    state = {"injected_lesson_ids": [], "run_id": "run_abc", "output_critic_score": 80}
    record_lesson_uses_node(state, lesson_store=fake_store)
    fake_store.record_use.assert_not_called()


def test_record_uses_noop_when_missing_key():
    fake_store = MagicMock()
    state = {"run_id": "run_abc", "output_critic_score": 80}
    record_lesson_uses_node(state, lesson_store=fake_store)
    fake_store.record_use.assert_not_called()
