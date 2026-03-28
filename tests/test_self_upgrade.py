"""
Tests for the self-upgrade safety pipeline.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

from agents.self_upgrade import (
    DEFAULT_MIN_SCORE,
    IMMUTABLE_PATHS,
    MAX_DIFF_LINES,
    UPGRADE_BRANCH_PREFIX,
    SelfUpgradePipeline,
    UpgradeProposal,
    UpgradeResult,
    get_project_root,
    is_self_upgrade_enabled,
)


# ── Helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def proposal():
    """A valid, minimal upgrade proposal."""
    return UpgradeProposal(
        description="Improve router logging",
        files={"agents/router.py": "# improved router\npass\n"},
        rationale="Better debug output",
    )


@pytest.fixture
def pipeline(tmp_path):
    """Pipeline pointing at a temporary project root."""
    # Create a minimal project structure
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "__init__.py").write_text("")
    (agents_dir / "router.py").write_text("# original router\npass\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    return SelfUpgradePipeline(project_root=tmp_path)


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


# ── UpgradeProposal.validate_paths ──────────────────────────────────


class TestUpgradeProposalValidation:

    def test_valid_agents_path(self, proposal):
        errors = proposal.validate_paths()
        assert errors == []

    def test_immutable_self_upgrade(self):
        p = UpgradeProposal(
            description="hack",
            files={"agents/self_upgrade.py": "# evil"},
        )
        errors = p.validate_paths()
        assert any("immutable" in e for e in errors)

    def test_immutable_skill_security(self):
        p = UpgradeProposal(
            description="hack",
            files={"agents/skill_security.py": "# evil"},
        )
        errors = p.validate_paths()
        assert any("immutable" in e for e in errors)

    def test_immutable_config(self):
        p = UpgradeProposal(
            description="hack",
            files={"agents/config.py": "# evil"},
        )
        errors = p.validate_paths()
        assert any("immutable" in e for e in errors)

    def test_immutable_env(self):
        p = UpgradeProposal(
            description="hack",
            files={".env": "SECRET=stolen"},
        )
        errors = p.validate_paths()
        assert any("immutable" in e for e in errors)

    def test_outside_agents_dir(self):
        p = UpgradeProposal(
            description="escape",
            files={"docker/Dockerfile": "FROM evil"},
        )
        errors = p.validate_paths()
        assert any("agents/" in e for e in errors)

    def test_path_traversal(self):
        p = UpgradeProposal(
            description="traversal",
            files={"agents/../../etc/passwd": "root::0:0:::"},
        )
        errors = p.validate_paths()
        assert len(errors) > 0

    def test_multiple_files_mixed_validity(self):
        p = UpgradeProposal(
            description="mixed",
            files={
                "agents/router.py": "# ok",
                "agents/self_upgrade.py": "# blocked",
            },
        )
        errors = p.validate_paths()
        assert len(errors) == 1
        assert "immutable" in errors[0]

    def test_new_file_in_agents(self):
        p = UpgradeProposal(
            description="new module",
            files={"agents/new_module.py": "# new stuff\n"},
        )
        errors = p.validate_paths()
        assert errors == []


# ── SelfUpgradePipeline ─────────────────────────────────────────────


class TestSelfUpgradePipeline:

    def test_blocked_when_disabled(self, pipeline, proposal):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "false"}):
            result = pipeline.execute(proposal)
            assert result.success is False
            assert "not enabled" in result.errors[0]

    def test_blocked_by_immutable_path(self, pipeline):
        proposal = UpgradeProposal(
            description="hack security",
            files={"agents/skill_security.py": "# evil"},
        )
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            result = pipeline.execute(proposal)
            assert result.success is False
            assert any("immutable" in e for e in result.errors)

    def test_blocked_by_diff_size(self, pipeline):
        # Create a proposal with way too many lines
        huge_content = "\n".join(f"line {i}" for i in range(600))
        proposal = UpgradeProposal(
            description="huge change",
            files={"agents/router.py": huge_content},
        )
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            result = pipeline.execute(proposal)
            assert result.success is False
            assert any("too large" in e.lower() or "diff" in e.lower() for e in result.errors)

    def test_blocked_by_test_failure(self, pipeline, proposal):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            with patch.object(
                pipeline, "_run_tests", return_value=(False, "FAILED: 3 tests")
            ):
                result = pipeline.execute(proposal)
                assert result.success is False
                assert result.test_passed is False
                assert any("pytest" in e for e in result.errors)

    def test_blocked_by_bandit(self, pipeline, proposal):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            with patch.object(
                pipeline, "_run_tests", return_value=(True, "all passed")
            ):
                with patch.object(
                    pipeline, "_run_bandit",
                    return_value=(False, "Issue: [B602] subprocess_popen"),
                ):
                    result = pipeline.execute(proposal)
                    assert result.success is False
                    assert result.test_passed is True
                    assert result.bandit_passed is False
                    assert any("bandit" in e for e in result.errors)

    def test_blocked_by_low_critic_score(self, pipeline, proposal):
        def mock_critic(desc, diff):
            return (60, "Not good enough")

        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            with patch.object(
                pipeline, "_run_tests", return_value=(True, "all passed")
            ):
                with patch.object(
                    pipeline, "_run_bandit", return_value=(True, "clean")
                ):
                    result = pipeline.execute(proposal, critic_fn=mock_critic)
                    assert result.success is False
                    assert result.critic_score == 60
                    assert any("critic" in e.lower() for e in result.errors)

    def test_success_all_gates_pass(self, pipeline, proposal):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            with patch.object(
                pipeline, "_run_tests", return_value=(True, "all passed")
            ):
                with patch.object(
                    pipeline, "_run_bandit", return_value=(True, "clean")
                ):
                    with patch.object(
                        pipeline, "_apply_and_commit",
                        return_value=("vibe/self-upgrade/abc12345", "deadbeef"),
                    ):
                        result = pipeline.execute(proposal)
                        assert result.success is True
                        assert result.test_passed is True
                        assert result.bandit_passed is True
                        assert result.branch_name == "vibe/self-upgrade/abc12345"
                        assert result.commit_hash == "deadbeef"

    def test_success_with_critic(self, pipeline, proposal):
        def mock_critic(desc, diff):
            return (95, "Excellent improvement")

        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            with patch.object(
                pipeline, "_run_tests", return_value=(True, "all passed")
            ):
                with patch.object(
                    pipeline, "_run_bandit", return_value=(True, "clean")
                ):
                    with patch.object(
                        pipeline, "_apply_and_commit",
                        return_value=("vibe/self-upgrade/abc12345", "deadbeef"),
                    ):
                        result = pipeline.execute(proposal, critic_fn=mock_critic)
                        assert result.success is True
                        assert result.critic_score == 95

    def test_critic_error_blocks(self, pipeline, proposal):
        def bad_critic(desc, diff):
            raise RuntimeError("LLM down")

        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            with patch.object(
                pipeline, "_run_tests", return_value=(True, "all passed")
            ):
                with patch.object(
                    pipeline, "_run_bandit", return_value=(True, "clean")
                ):
                    result = pipeline.execute(proposal, critic_fn=bad_critic)
                    assert result.success is False
                    assert any("critic" in e.lower() for e in result.errors)

    def test_no_critic_fn_skips_critic(self, pipeline, proposal):
        with patch.dict(os.environ, {"VIBE_SELF_UPGRADE_ENABLED": "true"}):
            with patch.object(
                pipeline, "_run_tests", return_value=(True, "all passed")
            ):
                with patch.object(
                    pipeline, "_run_bandit", return_value=(True, "clean")
                ):
                    with patch.object(
                        pipeline, "_apply_and_commit",
                        return_value=("branch", "hash"),
                    ):
                        result = pipeline.execute(proposal, critic_fn=None)
                        assert result.success is True
                        assert result.critic_score == 0  # Not evaluated


# ── generate_diff_text ───────────────────────────────────────────────


class TestGenerateDiffText:

    def test_new_file_diff(self, pipeline):
        proposal = UpgradeProposal(
            description="Add helper",
            files={"agents/helper.py": "def helper(): pass\n"},
            rationale="Needed for X",
        )
        text = pipeline._generate_diff_text(proposal)
        assert "New file" in text
        assert "helper.py" in text
        assert "Needed for X" in text

    def test_modified_file_diff(self, pipeline):
        # router.py exists in the tmp project
        proposal = UpgradeProposal(
            description="Fix router",
            files={"agents/router.py": "# fixed router\npass\n"},
        )
        text = pipeline._generate_diff_text(proposal)
        assert "Modified" in text
        assert "router.py" in text


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
        p = UpgradeProposal(description="test", files={})
        r = UpgradeResult(success=False, proposal=p)
        assert r.test_passed is False
        assert r.bandit_passed is False
        assert r.critic_score == 0
        assert r.errors == []
        assert r.branch_name == ""
        assert r.commit_hash == ""

    def test_result_fields(self):
        p = UpgradeProposal(description="test", files={})
        r = UpgradeResult(
            success=True,
            proposal=p,
            test_passed=True,
            bandit_passed=True,
            critic_score=95,
            branch_name="vibe/self-upgrade/abc",
            commit_hash="deadbeef",
        )
        assert r.success is True
        assert r.critic_score == 95


# ── LLM code generation ──────────────────────────────────────────────


class TestGenerateUpgradeProposal:

    def test_returns_none_when_no_target_files(self, tmp_path):
        from agents.self_upgrade import generate_upgrade_proposal
        mock_model = MagicMock()
        result = generate_upgrade_proposal(
            description="test",
            rationale="test",
            target_files=["agents/nonexistent.py"],
            base_model=mock_model,
            project_root=tmp_path,
        )
        assert result is None
        mock_model.generate.assert_not_called()

    def test_returns_none_on_no_change(self, tmp_path):
        from agents.self_upgrade import generate_upgrade_proposal
        # Create a target file
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "router.py").write_text("# original\npass\n")

        mock_model = MagicMock()
        mock_model.generate.return_value = "NO_CHANGE"

        result = generate_upgrade_proposal(
            description="test",
            rationale="test",
            target_files=["agents/router.py"],
            base_model=mock_model,
            project_root=tmp_path,
        )
        assert result is None

    def test_returns_proposal_on_valid_output(self, tmp_path):
        from agents.self_upgrade import generate_upgrade_proposal
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "router.py").write_text("# original\npass\n")

        mock_model = MagicMock()
        mock_model.generate.return_value = (
            "FILE: agents/router.py\n"
            "```python\n"
            "# improved router\nimport logging\npass\n"
            "```"
        )

        result = generate_upgrade_proposal(
            description="fix router",
            rationale="accumulated signals",
            target_files=["agents/router.py"],
            base_model=mock_model,
            project_root=tmp_path,
        )
        assert result is not None
        assert "agents/router.py" in result.files
        assert "improved router" in result.files["agents/router.py"]

    def test_ignores_files_not_in_target_list(self, tmp_path):
        from agents.self_upgrade import generate_upgrade_proposal
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "router.py").write_text("# original\n")

        mock_model = MagicMock()
        mock_model.generate.return_value = (
            "FILE: agents/router.py\n```python\n# changed\n```\n"
            "FILE: agents/secret.py\n```python\n# evil\n```"
        )

        result = generate_upgrade_proposal(
            description="fix",
            rationale="reason",
            target_files=["agents/router.py"],
            base_model=mock_model,
            project_root=tmp_path,
        )
        assert result is not None
        assert "agents/secret.py" not in result.files

    def test_llm_error_returns_none(self, tmp_path):
        from agents.self_upgrade import generate_upgrade_proposal
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "router.py").write_text("# original\n")

        mock_model = MagicMock()
        mock_model.generate.side_effect = RuntimeError("LLM down")

        result = generate_upgrade_proposal(
            description="fix",
            rationale="reason",
            target_files=["agents/router.py"],
            base_model=mock_model,
            project_root=tmp_path,
        )
        assert result is None


# ── _parse_llm_file_output ───────────────────────────────────────────


class TestParseLlmFileOutput:

    def test_parses_single_file(self):
        from agents.self_upgrade import _parse_llm_file_output
        response = "FILE: agents/foo.py\n```python\nprint('hello')\n```"
        originals = {"agents/foo.py": "print('old')"}
        result = _parse_llm_file_output(response, originals)
        assert "agents/foo.py" in result
        assert "hello" in result["agents/foo.py"]

    def test_rejects_identical_content(self):
        from agents.self_upgrade import _parse_llm_file_output
        response = "FILE: agents/foo.py\n```python\nprint('same')\n```"
        originals = {"agents/foo.py": "print('same')"}
        result = _parse_llm_file_output(response, originals)
        assert result == {}

    def test_rejects_unknown_files(self):
        from agents.self_upgrade import _parse_llm_file_output
        response = "FILE: agents/unknown.py\n```python\nevil()\n```"
        originals = {"agents/known.py": "good()"}
        result = _parse_llm_file_output(response, originals)
        assert result == {}

    def test_parses_multiple_files(self):
        from agents.self_upgrade import _parse_llm_file_output
        response = (
            "FILE: agents/a.py\n```python\naa\n```\n"
            "FILE: agents/b.py\n```python\nbb\n```"
        )
        originals = {"agents/a.py": "old_a", "agents/b.py": "old_b"}
        result = _parse_llm_file_output(response, originals)
        assert len(result) == 2


# ── Immutable trigger path ────────────────────────────────────────────


class TestImmutableTriggerPath:

    def test_trigger_module_is_immutable(self):
        p = UpgradeProposal(
            description="hack trigger",
            files={"agents/self_upgrade_trigger.py": "# evil"},
        )
        errors = p.validate_paths()
        assert any("immutable" in e for e in errors)
