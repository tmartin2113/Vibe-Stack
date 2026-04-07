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
