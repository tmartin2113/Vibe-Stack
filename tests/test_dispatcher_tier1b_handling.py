"""Tests for SelfUpgradeDispatcher._handle_tier1b — Tier 1b handling + fall-through."""

from unittest.mock import MagicMock

import pytest

from agents.self_upgrade_dispatcher import (
    DispatchResult,
    SelfUpgradeDispatcher,
)
from agents.self_upgrade.tier1b_builder import Tier1bResult
from agents.self_upgrade.tier3_builder import Tier3Result
from agents.self_upgrade_trigger import UpgradeSignal


def _same_detail_cluster(n=3, task_type="code_generation", detail="use response_model"):
    return [
        UpgradeSignal(
            category="critic_pattern",
            task_type=task_type,
            detail=detail,
            score=60,
            source_node="critic",
        )
        for _ in range(n)
    ]


class TestDispatcherTier1bHandling:
    def test_tier1b_builder_none_returns_rejected(self):
        dispatcher = SelfUpgradeDispatcher()
        result = dispatcher.dispatch(
            _same_detail_cluster(),
            author_agent_id="x",
            author_run_id="y",
        )
        assert isinstance(result, DispatchResult.Rejected)
        assert "tier1b" in result.reason.lower() or "tier 1b" in result.reason.lower()

    def test_tier1b_override_committed_returns_tier1b_committed(self):
        tier1b_builder = MagicMock()
        tier1b_builder.build.return_value = Tier1bResult.OverrideCommitted(
            override_id="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X",
            task_type="code_generation",
            branch="vibe/self-upgrade/tier1b-ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X",
            commit="abc123",
            pr_url="https://github.com/x/y/pull/42",
            issue_id="iss_7",
            signal_refs=["sig_1", "sig_2", "sig_3"],
        )
        dispatcher = SelfUpgradeDispatcher(tier1b_builder=tier1b_builder)
        result = dispatcher.dispatch(
            _same_detail_cluster(),
            author_agent_id="x",
            author_run_id="y",
        )
        assert isinstance(result, DispatchResult.Tier1bCommitted)
        assert result.branch.startswith("vibe/self-upgrade/tier1b-")
        assert result.pr_url == "https://github.com/x/y/pull/42"
        assert result.issue_id == "iss_7"

    def test_tier1b_low_confidence_falls_through_to_tier3(self):
        tier1b_builder = MagicMock()
        tier1b_builder.build.return_value = Tier1bResult.LowConfidence(
            reason="no fixtures yet for adapter: vibe",
            signal_refs=["sig_1", "sig_2", "sig_3"],
        )
        # Use Tier3Result.Dropped so render_report is never called
        # (avoids needing a full IssueReport mock). The fall-through
        # behavior is verified by tier3_builder.build being called.
        tier3_builder = MagicMock()
        tier3_builder.build.return_value = Tier3Result.Dropped(
            reason="test",
            signal_refs=["sig_1"],
        )
        dispatcher = SelfUpgradeDispatcher(
            tier1b_builder=tier1b_builder,
            tier3_builder=tier3_builder,
            paperclip_client=MagicMock(),
            human_triage_user_id="human_1",
        )
        result = dispatcher.dispatch(
            _same_detail_cluster(),
            author_agent_id="x",
            author_run_id="y",
        )
        # Tier3 Dropped → dispatcher returns Rejected, but tier3_builder.build
        # WAS called — proving the fall-through happened.
        tier3_builder.build.assert_called_once()

    def test_tier1b_gate_failed_falls_through_to_tier3(self):
        tier1b_builder = MagicMock()
        tier1b_builder.build.return_value = Tier1bResult.GateFailed(
            gate="smoke_test",
            detail="fixture can_02 dropped from 85 to 78",
            signal_refs=["sig_1", "sig_2", "sig_3"],
        )
        tier3_builder = MagicMock()
        tier3_builder.build.return_value = Tier3Result.Dropped(
            reason="test",
            signal_refs=["sig_1"],
        )
        dispatcher = SelfUpgradeDispatcher(
            tier1b_builder=tier1b_builder,
            tier3_builder=tier3_builder,
            paperclip_client=MagicMock(),
            human_triage_user_id="human_1",
        )
        result = dispatcher.dispatch(
            _same_detail_cluster(),
            author_agent_id="x",
            author_run_id="y",
        )
        tier3_builder.build.assert_called_once()
