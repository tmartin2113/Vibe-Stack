"""Lock-in tests for Tier 1a: prevent regressions on deletions and
immutable-set membership for self-upgrade mechanics."""

from agents.self_upgrade import _ADDITIONAL_IMMUTABLES, is_path_immutable


def test_skill_ab_is_immutable():
    assert "agents/skill_ab.py" in _ADDITIONAL_IMMUTABLES
    assert is_path_immutable("agents/skill_ab.py")
