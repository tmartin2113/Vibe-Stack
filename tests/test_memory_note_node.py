from unittest.mock import MagicMock, patch

from agents.memory_note_node import memory_note_node
from agents.self_upgrade.tier0_builder import Tier0Result


def test_memory_note_node_skips_when_not_eligible():
    state = {
        "lesson_eligible": False,
        "output_critic_score": 90,
        "output_critic_feedback": "all good",
    }

    result_state = memory_note_node(state, lesson_store=MagicMock(), tier0_builder=MagicMock())

    assert "lesson_written_id" not in result_state or result_state["lesson_written_id"] is None


def test_memory_note_node_writes_lesson_when_eligible():
    fake_store = MagicMock()
    fake_store.add.return_value = "lesson_xyz"

    fake_builder = MagicMock()
    fake_builder.build.return_value = Tier0Result.LessonDrafted(
        lesson="use validation",
        role="backend",
        task_type="code_generation",
        tag="",
        signal_refs=[],
    )

    state = {
        "lesson_eligible": True,
        "output_critic_score": 60,
        "output_critic_feedback": "missing validation",
        "routed_task_type": "code_generation",
        "agent_role": "backend",
        "agent_id": "agent_1",
        "run_id": "run_1",
        "accumulated_signals": [],
    }

    result_state = memory_note_node(
        state, lesson_store=fake_store, tier0_builder=fake_builder,
    )

    assert result_state["lesson_written_id"] == "lesson_xyz"
    fake_store.add.assert_called_once()


def test_memory_note_node_no_op_when_builder_returns_empty():
    fake_store = MagicMock()
    fake_builder = MagicMock()
    fake_builder.build.return_value = Tier0Result.Empty(reason="llm empty")

    state = {
        "lesson_eligible": True,
        "output_critic_score": 60,
        "output_critic_feedback": "x",
        "routed_task_type": "t",
        "agent_role": "r",
        "agent_id": "",
        "run_id": "",
        "accumulated_signals": [],
    }

    result_state = memory_note_node(
        state, lesson_store=fake_store, tier0_builder=fake_builder,
    )

    assert result_state.get("lesson_written_id") is None
    fake_store.add.assert_not_called()
