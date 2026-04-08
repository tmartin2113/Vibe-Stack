"""Tests for the narrowed Tier2Pipeline (dormant until M4)."""
import pytest

from agents.self_upgrade import Tier2Pipeline, is_self_upgrade_enabled


def test_tier2_pipeline_rejects_when_disabled(monkeypatch):
    monkeypatch.setenv("VIBE_SELF_UPGRADE_ENABLED", "false")
    pipeline = Tier2Pipeline()
    # TypedEdit isn't implemented until M4. Passing None should be a clean rejection.
    result = pipeline.execute(None)
    assert result.success is False
    assert any("not enabled" in e.lower() for e in result.errors)


def test_tier2_pipeline_rejects_none_proposal_when_enabled(monkeypatch):
    monkeypatch.setenv("VIBE_SELF_UPGRADE_ENABLED", "true")
    pipeline = Tier2Pipeline()
    result = pipeline.execute(None)
    assert result.success is False


def test_immutable_directory_prefixes_blocked():
    """Files under immutable directories (storage/, sandbox/, skill_registry*) are blocked."""
    from agents.self_upgrade import is_path_immutable

    # File-level (already covered)
    assert is_path_immutable("agents/self_upgrade.py") is True

    # Directory prefixes
    assert is_path_immutable("agents/storage/sqlite.py") is True
    assert is_path_immutable("agents/sandbox/docker.py") is True
    assert is_path_immutable("vibe/backends/vllm.py") is True

    # Skill registry pattern
    assert is_path_immutable("agents/skill_registry.py") is True
    assert is_path_immutable("agents/skill_registry_index.py") is True

    # Lesson store (M1 will add it — must be immutable at rest)
    assert is_path_immutable("agents/lesson_store.py") is True

    # Allowed
    assert is_path_immutable("agents/tools/web_search.py") is False
    assert is_path_immutable("agents/heuristic_critic.py") is False
