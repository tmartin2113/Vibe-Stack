"""Integration test: create_node_wrappers exposes a memory_note wrapper that
writes a lesson when state["lesson_eligible"] is True.
"""
from unittest.mock import MagicMock

from agents.graph_nodes import create_node_wrappers


def test_create_node_wrappers_exposes_memory_note():
    """The wrapper dict should contain memory_note after Task 14."""
    fake_nodes = MagicMock()
    fake_skill_registry = MagicMock()
    fake_outcome_store = MagicMock()
    fake_upgrade_trigger = MagicMock()
    fake_lesson_store = MagicMock()

    wrappers = create_node_wrappers(
        nodes=fake_nodes,
        shared_skill_registry=fake_skill_registry,
        shared_outcome_store=fake_outcome_store,
        shared_artifact_store=None,
        shared_upgrade_trigger=fake_upgrade_trigger,
        shared_lesson_store=fake_lesson_store,
        base_model=MagicMock(),
        config=MagicMock(),
        adapter_registry=MagicMock(),
        tool_registry=MagicMock(),
        cancellation_token=None,
    )

    assert "memory_note" in wrappers
    assert callable(wrappers["memory_note"])


def test_memory_note_wrapper_skips_when_not_eligible():
    """Wrapper is a no-op when state["lesson_eligible"] is False."""
    fake_lesson_store = MagicMock()
    wrappers = create_node_wrappers(
        nodes=MagicMock(),
        shared_skill_registry=MagicMock(),
        shared_outcome_store=MagicMock(),
        shared_artifact_store=None,
        shared_upgrade_trigger=MagicMock(),
        shared_lesson_store=fake_lesson_store,
        base_model=MagicMock(),
        config=MagicMock(),
        adapter_registry=MagicMock(),
        tool_registry=MagicMock(),
        cancellation_token=None,
    )

    state = {"lesson_eligible": False}
    result = wrappers["memory_note"](state)

    fake_lesson_store.add.assert_not_called()
    assert result is state or result == state


def test_memory_note_wrapper_writes_lesson_when_eligible():
    """Wrapper writes a lesson via the store when lesson_eligible=True."""
    fake_lesson_store = MagicMock()
    fake_lesson_store.add.return_value = "lesson_abc"

    fake_llm = MagicMock()
    fake_llm.generate.return_value = "Always validate input."

    wrappers = create_node_wrappers(
        nodes=MagicMock(),
        shared_skill_registry=MagicMock(),
        shared_outcome_store=MagicMock(),
        shared_artifact_store=None,
        shared_upgrade_trigger=MagicMock(),
        shared_lesson_store=fake_lesson_store,
        base_model=fake_llm,
        config=MagicMock(),
        adapter_registry=MagicMock(),
        tool_registry=MagicMock(),
        cancellation_token=None,
    )

    from agents.self_upgrade_trigger import UpgradeSignal
    state = {
        "lesson_eligible": True,
        "output_critic_score": 60,
        "output_critic_feedback": "missing validation",
        "routed_task_type": "code_generation",
        "agent_role": "backend",
        "agent_id": "agent_1",
        "run_id": "run_1",
        "accumulated_signals": [
            UpgradeSignal(
                category="low_score",
                task_type="code_generation",
                detail="missing input validation",
                score=60,
            ),
        ],
    }

    result = wrappers["memory_note"](state)

    fake_lesson_store.add.assert_called_once()
    assert result.get("lesson_written_id") == "lesson_abc"
