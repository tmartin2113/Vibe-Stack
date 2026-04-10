"""Tests for Tier1bBuilder publish path with fake git + fake paperclip."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from agents.self_upgrade.tier1b_builder import (
    GitRunResult,
    Tier1bBuilder,
    Tier1bResult,
)
from agents.self_upgrade_trigger import UpgradeSignal


def _make_signal(task_type="code_generation", detail="use explicit response_model"):
    return UpgradeSignal(
        category="critic_pattern",
        task_type=task_type,
        detail=detail,
        score=60,
        source_node="critic",
    )


@dataclass
class _FakeGitCall:
    args: List[str]
    cwd: Optional[Path] = None


class _FakeGitRunner:
    """Records calls and returns scripted results."""

    def __init__(self):
        self.calls: List[_FakeGitCall] = []
        self._responses: dict[str, GitRunResult] = {}
        self._default = GitRunResult(returncode=0, stdout="", stderr="")

    def set_response(self, args_key: str, result: GitRunResult):
        self._responses[args_key] = result

    def run(self, args, *, cwd=None, check=True):
        self.calls.append(_FakeGitCall(args=list(args), cwd=cwd))
        key = " ".join(args[:3])
        result = self._responses.get(key, self._default)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"fake git command failed: {args} returncode={result.returncode} stderr={result.stderr}"
            )
        return result


class _FakePaperclip:
    def __init__(self):
        self.issues_created: List[dict] = []

    def create_issue(
        self,
        title,
        description="",
        priority="medium",
        labels=None,
        assignee_user_id=None,
    ):
        issue = type("Issue", (), {
            "id": f"iss_{len(self.issues_created) + 1}",
        })()
        self.issues_created.append({
            "id": issue.id,
            "title": title,
            "description": description,
            "labels": list(labels or []),
            "assignee_user_id": assignee_user_id,
        })
        return issue


def _seed_fixtures_with_baseline(tmp_path, adapter="vibe"):
    d = tmp_path / "canonical" / adapter
    d.mkdir(parents=True)
    scores = {"can_01": 90, "can_02": 85, "can_03": 88}
    for fid in scores:
        (d / f"{fid}.json").write_text(json.dumps({
            "id": fid,
            "task_type": "code_generation",
            "prompt": "test",
            "expected_keywords": [],
            "baseline_score": scores[fid],
            "model_id": "vllm-local",
            "captured_at": "2026-04-09T12:00:00Z",
        }))
    (d / "baseline.json").write_text(
        json.dumps({k: float(v) for k, v in scores.items()})
    )
    return d


class _StubSmokeScorer:
    def __init__(self, default=95):
        self._default = default

    def score_fixture(self, fixture_id, augmented_prompt):
        return self._default


def _make_builder(tmp_path, git_runner=None, paperclip=None):
    _seed_fixtures_with_baseline(tmp_path)
    registry = MagicMock()
    registry.adapter_mapping.return_value = {"code_generation": "vibe"}
    return Tier1bBuilder(
        task_type_registry=registry,
        smoke_scorer=_StubSmokeScorer(default=95),  # all fixtures pass smoke
        git_runner=git_runner or _FakeGitRunner(),
        paperclip_client=paperclip or _FakePaperclip(),
        fixtures_root=tmp_path / "canonical",
        overrides_root=tmp_path / "overrides",
        human_triage_user_id="human_1",
        allow_publish=True,
    )


class TestPublishBranchCreate:
    def test_publish_creates_branch_via_git(self, tmp_path):
        git = _FakeGitRunner()
        builder = _make_builder(tmp_path, git_runner=git)
        signals = [_make_signal() for _ in range(3)]
        builder.build(signals, author_agent_id="x", author_run_id="y")
        # Expect at least one "git checkout -b" call
        branch_calls = [c for c in git.calls if c.args[:2] == ["checkout", "-b"]]
        assert len(branch_calls) == 1
        branch_name = branch_calls[0].args[2]
        assert branch_name.startswith("vibe/self-upgrade/tier1b-ovr_")


class TestPublishWriteFiles:
    def test_publish_writes_override_file(self, tmp_path):
        builder = _make_builder(tmp_path)
        signals = [_make_signal() for _ in range(3)]
        builder.build(signals, author_agent_id="x", author_run_id="y")
        # Look for the written YAML file
        overrides_dir = tmp_path / "overrides" / "code_generation"
        yaml_files = list(overrides_dir.glob("ovr_*.yaml"))
        assert len(yaml_files) == 1

    def test_publish_writes_baseline_sidecar(self, tmp_path):
        builder = _make_builder(tmp_path)
        signals = [_make_signal() for _ in range(3)]
        builder.build(signals, author_agent_id="x", author_run_id="y")
        overrides_dir = tmp_path / "overrides" / "code_generation"
        baseline_files = list(overrides_dir.glob("ovr_*.baseline"))
        assert len(baseline_files) == 1


class TestPublishDiffCheck:
    def test_publish_rejects_on_modified_paths(self, tmp_path):
        git = _FakeGitRunner()
        # Force the diff check to report a modified file under overrides/
        git.set_response(
            "diff --name-status HEAD",
            GitRunResult(
                returncode=0,
                stdout="M\tagents/prompt_library/overrides/code_generation/ovr_OLD.yaml\n",
                stderr="",
            ),
        )
        builder = _make_builder(tmp_path, git_runner=git)
        signals = [_make_signal() for _ in range(3)]
        result = builder.build(signals, author_agent_id="x", author_run_id="y")
        assert isinstance(result, Tier1bResult.GateFailed)
        assert result.gate == "diff_check"


class TestPublishPushAndPr:
    def test_publish_pushes_branch_and_opens_pr(self, tmp_path):
        git = _FakeGitRunner()
        git.set_response(
            "gh pr create",
            GitRunResult(
                returncode=0,
                stdout="https://github.com/tmartin2113/Vibe-Stack/pull/99\n",
                stderr="",
            ),
        )
        paperclip = _FakePaperclip()
        builder = _make_builder(tmp_path, git_runner=git, paperclip=paperclip)
        signals = [_make_signal() for _ in range(3)]
        result = builder.build(signals, author_agent_id="x", author_run_id="y")
        assert isinstance(result, Tier1bResult.OverrideCommitted)
        assert result.pr_url.startswith("https://github.com/")
        # Push must have happened
        push_calls = [c for c in git.calls if c.args[:1] == ["push"]]
        assert len(push_calls) >= 1
        # PR create must have happened
        pr_calls = [c for c in git.calls if c.args[:2] == ["gh", "pr"]]
        assert len(pr_calls) >= 1

    def test_publish_files_companion_issue(self, tmp_path):
        git = _FakeGitRunner()
        git.set_response(
            "gh pr create",
            GitRunResult(returncode=0, stdout="https://github.com/x/y/pull/1\n", stderr=""),
        )
        paperclip = _FakePaperclip()
        builder = _make_builder(tmp_path, git_runner=git, paperclip=paperclip)
        signals = [_make_signal() for _ in range(3)]
        result = builder.build(signals, author_agent_id="x", author_run_id="y")
        assert isinstance(result, Tier1bResult.OverrideCommitted)
        assert len(paperclip.issues_created) == 1
        issue = paperclip.issues_created[0]
        assert "tier-1b" in issue["labels"]
        assert "self-upgrade" in issue["labels"]
        assert issue["assignee_user_id"] == "human_1"
        assert result.issue_id == issue["id"]


class TestPublishPushFailure:
    def test_push_failure_is_gate_failed(self, tmp_path):
        git = _FakeGitRunner()
        # The builder calls ["push", "-u", "origin", branch]
        # " ".join(args[:3]) = "push -u origin"
        git.set_response(
            "push -u origin",
            GitRunResult(returncode=1, stdout="", stderr="remote rejected"),
        )
        builder = _make_builder(tmp_path, git_runner=git)
        signals = [_make_signal() for _ in range(3)]
        result = builder.build(signals, author_agent_id="x", author_run_id="y")
        assert isinstance(result, Tier1bResult.GateFailed)
        assert result.gate == "publish"
        assert "push" in result.detail.lower()


class TestPublishPrFailure:
    def test_pr_create_failure_is_gate_failed(self, tmp_path):
        git = _FakeGitRunner()
        git.set_response(
            "gh pr create",
            GitRunResult(returncode=1, stdout="", stderr="API rate limit"),
        )
        builder = _make_builder(tmp_path, git_runner=git)
        signals = [_make_signal() for _ in range(3)]
        result = builder.build(signals, author_agent_id="x", author_run_id="y")
        assert isinstance(result, Tier1bResult.GateFailed)
        assert result.gate == "publish"
        assert "pr" in result.detail.lower() or "rate limit" in result.detail.lower()


class TestPartialFailurePaperclip:
    def test_paperclip_failure_still_returns_override_committed(self, tmp_path):
        git = _FakeGitRunner()
        git.set_response(
            "gh pr create",
            GitRunResult(returncode=0, stdout="https://github.com/x/y/pull/1\n", stderr=""),
        )

        class _BoomPaperclip:
            def create_issue(self, **kwargs):
                raise RuntimeError("paperclip unreachable")

        builder = _make_builder(tmp_path, git_runner=git, paperclip=_BoomPaperclip())
        signals = [_make_signal() for _ in range(3)]
        result = builder.build(signals, author_agent_id="x", author_run_id="y")
        assert isinstance(result, Tier1bResult.OverrideCommitted)
        assert result.issue_id == ""  # orphaned PR
