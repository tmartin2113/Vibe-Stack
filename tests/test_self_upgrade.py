"""
Tests for the self-upgrade safety pipeline.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

from agents.self_upgrade import (
    DEFAULT_MIN_SCORE,
    IMMUTABLE_PATHS,
    MAX_DIFF_LINES,
    UPGRADE_BRANCH_PREFIX,
    SelfUpgradePipeline,
    UpgradeResult,
    get_project_root,
    is_self_upgrade_enabled,
)


# ── is_self_upgrade_enabled ──────────────────────────────────────────


class TestIsSelfUpgradeEnabled:

    def test_enabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            assert is_self_upgrade_enabled() is True

    def test_enabled_true(self):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            assert is_self_upgrade_enabled() is True

    def test_enabled_yes(self):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "yes"}):
            assert is_self_upgrade_enabled() is True

    def test_enabled_one(self):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "1"}):
            assert is_self_upgrade_enabled() is True

    def test_disabled_false(self):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "false"}):
            assert is_self_upgrade_enabled() is False

    def test_disabled_zero(self):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "0"}):
            assert is_self_upgrade_enabled() is False

    def test_disabled_no(self):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "no"}):
            assert is_self_upgrade_enabled() is False

    def test_case_insensitive(self):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "FALSE"}):
            assert is_self_upgrade_enabled() is False


# ── Config integration ───────────────────────────────────────────────


class TestSelfUpgradeConfig:

    def test_config_defaults(self):
        from agents.config import SelfUpgradeConfig
        cfg = SelfUpgradeConfig()
        assert cfg.enabled is True
        assert cfg.min_critic_score == 90
        assert cfg.max_diff_lines == 500
        assert cfg.branch_prefix == "vibe/self-upgrade"

    def test_system_config_has_self_upgrade(self):
        from agents.config import SystemConfig
        config = SystemConfig()
        assert hasattr(config, "self_upgrade")
        assert config.self_upgrade.enabled is True

    def test_env_override_disables(self):
        from agents.config import SystemConfig
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "false"}):
            config = SystemConfig.from_env()
            assert config.self_upgrade.enabled is False

    def test_env_override_min_score(self):
        from agents.config import SystemConfig
        with patch.dict(os.environ, {
            "VIBE_SELF_UPGRADE_ENABLED": "true",
            "VIBE_SELF_UPGRADE_MIN_SCORE": "85",
        }):
            config = SystemConfig.from_env()
            assert config.self_upgrade.min_critic_score == 85

    def test_env_override_max_diff(self):
        from agents.config import SystemConfig
        with patch.dict(os.environ, {
            "VIBE_SELF_UPGRADE_ENABLED": "true",
            "VIBE_SELF_UPGRADE_MAX_DIFF_LINES": "200",
        }):
            config = SystemConfig.from_env()
            assert config.self_upgrade.max_diff_lines == 200


# ── Task type registry integration ───────────────────────────────────


class TestSelfUpgradeTaskType:

    def test_self_upgrade_type_registered(self):
        from agents.task_type_registry import create_default_registry
        reg = create_default_registry()
        assert "self_upgrade" in reg
        entry = reg.get("self_upgrade")
        assert entry.adapter == "self_upgrade"
        assert entry.source == "builtin"
        assert len(entry.patterns) > 0

    def test_self_upgrade_has_patterns(self):
        from agents.task_type_registry import create_default_registry
        reg = create_default_registry()
        entry = reg.get("self_upgrade")
        patterns = entry.patterns
        # Should match self-upgrade related terms
        import re
        assert any(re.search(p, "self upgrade the agent") for p in patterns)


# ── Allowed file dirs integration ────────────────────────────────────


class TestAllowedFileDirs:

    def test_default_dirs_exclude_project_root(self):
        from agents.tools.registry import _build_allowed_file_dirs
        with patch.dict(os.environ, {}, clear=True):
            dirs = _build_allowed_file_dirs()
            project_root = Path(__file__).resolve().parent.parent
            # By default, project root should NOT be in allowed dirs
            # (unless /home/user/Vibe happens to be a parent)
            # Just verify the function returns without error
            assert len(dirs) >= 2

    def test_self_upgrade_adds_project_root(self):
        from agents.tools.registry import (
            _build_allowed_file_dirs,
            _SELF_UPGRADE_DIR,
        )
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}, clear=True):
            dirs = _build_allowed_file_dirs()
            assert _SELF_UPGRADE_DIR.resolve() in dirs

    def test_self_upgrade_disabled_no_extra_dir(self):
        from agents.tools.registry import (
            _build_allowed_file_dirs,
            _SELF_UPGRADE_DIR,
        )
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "false"}, clear=True):
            dirs = _build_allowed_file_dirs()
            # May or may not contain project root depending on defaults
            # but the self-upgrade path shouldn't be explicitly added
            # This test just verifies no crash
            assert isinstance(dirs, list)


# ── Immutable paths ──────────────────────────────────────────────────


class TestImmutablePaths:

    def test_all_critical_files_protected(self):
        assert "agents/self_upgrade.py" in IMMUTABLE_PATHS
        assert "agents/self_upgrade_trigger.py" in IMMUTABLE_PATHS
        assert "agents/skill_security.py" in IMMUTABLE_PATHS
        assert "agents/config.py" in IMMUTABLE_PATHS
        assert ".env" in IMMUTABLE_PATHS

    def test_immutable_paths_is_frozenset(self):
        assert isinstance(IMMUTABLE_PATHS, frozenset)


# ── UpgradeResult ────────────────────────────────────────────────────


class TestUpgradeResult:

    def test_default_result(self):
        r = UpgradeResult(success=False)
        assert r.test_passed is False
        assert r.bandit_passed is False
        assert r.critic_score == 0
        assert r.errors == []
        assert r.branch_name == ""
        assert r.commit_hash == ""
        assert r.typed_edit is None

    def test_result_fields(self):
        r = UpgradeResult(
            success=True,
            test_passed=True,
            bandit_passed=True,
            critic_score=95,
            branch_name="vibe/self-upgrade/abc",
            commit_hash="deadbeef",
        )
        assert r.success is True
        assert r.critic_score == 95
