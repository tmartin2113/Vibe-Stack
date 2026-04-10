"""Tests for Tier 1b post-merge regression monitor in skill_cleanup."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _write_override(overrides_root: Path, task_type: str, override_id: str,
                    baseline_score: float = 85.0):
    task_dir = overrides_root / task_type
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / f"{override_id}.yaml").write_text(
        f"id: {override_id}\n"
        f"task_type: {task_type}\n"
        f"append: |\n  test append\n"
        f"signal_refs:\n  - sig_1\n"
        f"author_agent_id: x\n"
        f"author_run_id: y\n"
        f"created_at: 2026-04-09T12:00:00Z\n"
    )
    (task_dir / f"{override_id}.baseline").write_text(
        f"2026-04-09T12:00:00Z {baseline_score:.1f}\n"
    )


class TestRegressionMonitor:
    def test_no_regression_files_no_issue(self, tmp_path):
        from agents.skill_cleanup import SkillCleanup  # type: ignore

        overrides_root = tmp_path / "overrides"
        _write_override(overrides_root, "code_generation", "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X", baseline_score=85.0)

        paperclip = MagicMock()
        cleanup = MagicMock(spec=SkillCleanup)
        cleanup._paperclip = paperclip
        cleanup._overrides_root = overrides_root
        cleanup._alerts_log = tmp_path / ".regression_alerts.jsonl"
        cleanup._rolling_avg_for = lambda task_type, k: 84.0  # only -1 drop

        SkillCleanup._check_override_regressions(cleanup)
        assert paperclip.create_issue.call_count == 0

    def test_regression_over_threshold_files_issue(self, tmp_path):
        from agents.skill_cleanup import SkillCleanup  # type: ignore

        overrides_root = tmp_path / "overrides"
        _write_override(overrides_root, "code_generation", "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X", baseline_score=85.0)

        paperclip = MagicMock()
        paperclip.create_issue.return_value = MagicMock(id="iss_reg_1")
        cleanup = MagicMock(spec=SkillCleanup)
        cleanup._paperclip = paperclip
        cleanup._overrides_root = overrides_root
        cleanup._alerts_log = tmp_path / ".regression_alerts.jsonl"
        cleanup._human_triage_user_id = "human_1"
        cleanup._rolling_avg_for = lambda task_type, k: 75.0  # -10 drop

        SkillCleanup._check_override_regressions(cleanup)
        assert paperclip.create_issue.call_count == 1
        call = paperclip.create_issue.call_args
        assert "tier-1b-regression" in call.kwargs.get("labels", [])

    def test_decayed_override_skipped(self, tmp_path):
        from agents.skill_cleanup import SkillCleanup  # type: ignore

        overrides_root = tmp_path / "overrides"
        _write_override(overrides_root, "code_generation", "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X", baseline_score=85.0)
        (overrides_root / "code_generation" / "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.decayed").write_text("reverted\n")

        paperclip = MagicMock()
        cleanup = MagicMock(spec=SkillCleanup)
        cleanup._paperclip = paperclip
        cleanup._overrides_root = overrides_root
        cleanup._alerts_log = tmp_path / ".regression_alerts.jsonl"
        cleanup._rolling_avg_for = lambda task_type, k: 50.0  # huge drop

        SkillCleanup._check_override_regressions(cleanup)
        assert paperclip.create_issue.call_count == 0

    def test_dedup_prevents_duplicate_alert(self, tmp_path):
        from agents.skill_cleanup import SkillCleanup  # type: ignore

        overrides_root = tmp_path / "overrides"
        _write_override(overrides_root, "code_generation", "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X", baseline_score=85.0)

        alerts_log = tmp_path / ".regression_alerts.jsonl"
        # Pre-seed the dedup log with a recent alert
        import datetime as _dt
        recent = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts_log.write_text(json.dumps({
            "override_id": "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X",
            "filed_at": recent,
            "issue_id": "iss_old",
        }) + "\n")

        paperclip = MagicMock()
        cleanup = MagicMock(spec=SkillCleanup)
        cleanup._paperclip = paperclip
        cleanup._overrides_root = overrides_root
        cleanup._alerts_log = alerts_log
        cleanup._human_triage_user_id = "human_1"
        cleanup._rolling_avg_for = lambda task_type, k: 70.0  # -15 drop

        SkillCleanup._check_override_regressions(cleanup)
        assert paperclip.create_issue.call_count == 0

    def test_missing_baseline_sidecar_skipped(self, tmp_path):
        from agents.skill_cleanup import SkillCleanup  # type: ignore

        overrides_root = tmp_path / "overrides"
        task_dir = overrides_root / "code_generation"
        task_dir.mkdir(parents=True)
        (task_dir / "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml").write_text("id: ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X\n")
        # No .baseline sidecar

        paperclip = MagicMock()
        cleanup = MagicMock(spec=SkillCleanup)
        cleanup._paperclip = paperclip
        cleanup._overrides_root = overrides_root
        cleanup._alerts_log = tmp_path / ".regression_alerts.jsonl"
        cleanup._rolling_avg_for = lambda task_type, k: 50.0

        SkillCleanup._check_override_regressions(cleanup)
        assert paperclip.create_issue.call_count == 0
