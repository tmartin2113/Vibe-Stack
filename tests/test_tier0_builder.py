from unittest.mock import MagicMock

from agents.self_upgrade.tier0_builder import Tier0Builder, Tier0Result
from agents.self_upgrade_trigger import UpgradeSignal


def test_builder_drafts_lesson_from_signal_cluster():
    fake_llm = MagicMock()
    fake_llm.generate.return_value = (
        "When generating FastAPI endpoints, always include Pydantic request validation."
    )

    builder = Tier0Builder(llm=fake_llm)
    signals = [
        UpgradeSignal(
            category="low_score",
            task_type="code_generation",
            detail="Missing request validation in the endpoint",
            score=60,
        ),
    ]

    result = builder.build(
        signals,
        author_agent_id="agent_1",
        author_run_id="run_1",
        role="backend_engineer",
    )

    assert isinstance(result, Tier0Result.LessonDrafted)
    assert result.role == "backend_engineer"
    assert result.task_type == "code_generation"
    assert "FastAPI" in result.lesson
    assert fake_llm.generate.called


def test_builder_returns_empty_when_llm_returns_nothing():
    fake_llm = MagicMock()
    fake_llm.generate.return_value = ""

    builder = Tier0Builder(llm=fake_llm)
    result = builder.build(
        [UpgradeSignal(category="low_score", task_type="t", detail="d", score=60)],
        author_agent_id="", author_run_id="", role="r",
    )

    assert isinstance(result, Tier0Result.Empty)


def test_builder_returns_empty_when_no_signals():
    fake_llm = MagicMock()
    builder = Tier0Builder(llm=fake_llm)
    result = builder.build(
        [], author_agent_id="", author_run_id="", role="r",
    )
    assert isinstance(result, Tier0Result.Empty)
    assert not fake_llm.generate.called
