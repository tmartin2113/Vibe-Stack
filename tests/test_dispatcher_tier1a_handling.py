"""Dispatcher → Tier1aBuilder hand-off tests with a mocked builder."""

from pathlib import Path
from unittest.mock import MagicMock

from agents.self_upgrade_dispatcher import (
    SelfUpgradeDispatcher, DispatchResult, Tier,
)
from agents.self_upgrade_trigger import UpgradeSignal
from agents.self_upgrade.tier1a_builder import Tier1aResult
from agents.self_upgrade.tier3_builder import Tier3Result


def _varied_cluster():
    return [
        UpgradeSignal(
            category="low_score",
            task_type="code_generation",
            detail=f"feedback {i}",
            score=50,
            source_node="critic",
        )
        for i in range(3)
    ]


class TestDispatcherTier1aHandling:
    def test_no_builder_returns_rejected(self):
        d = SelfUpgradeDispatcher()  # no tier1a_builder wired
        result = d.dispatch(_varied_cluster())
        assert isinstance(result, DispatchResult.Rejected)
        assert "tier1a dependencies not wired" in result.reason

    def test_builder_low_confidence_falls_through_to_tier3(self):
        tier1a = MagicMock()
        tier1a.build = MagicMock(return_value=Tier1aResult.LowConfidence(
            reason="no matching skill",
            signal_refs=["sig_1", "sig_2", "sig_3"],
        ))

        # Return Dropped so the dispatcher's _handle_tier3 exits early
        # without calling render_report (which can't handle MagicMock data).
        tier3 = MagicMock()
        tier3.build = MagicMock(return_value=Tier3Result.Dropped(
            reason="test drop", signal_refs=["sig_1", "sig_2", "sig_3"],
        ))
        paperclip = MagicMock()

        d = SelfUpgradeDispatcher(
            tier1a_builder=tier1a,
            tier3_builder=tier3,
            paperclip_client=paperclip,
            human_triage_user_id="user_1",
        )

        signals = _varied_cluster()
        result = d.dispatch(signals)

        # Fall-through path: Tier1aBuilder returned LowConfidence, so
        # dispatcher should have routed to Tier3Builder.build
        tier1a.build.assert_called_once()
        tier3.build.assert_called_once()
        # Tier3 returned Dropped → dispatcher returns Rejected
        assert isinstance(result, DispatchResult.Rejected)

    def test_builder_success_returns_tier1a_queued(self, tmp_path):
        v2_path = tmp_path / "myCodeSkill__v2"
        tier1a = MagicMock()
        tier1a.build = MagicMock(return_value=Tier1aResult.CandidateWritten(
            skill_name="myCodeSkill",
            v2_path=v2_path,
            signal_refs=["sig_1", "sig_2", "sig_3"],
        ))

        d = SelfUpgradeDispatcher(tier1a_builder=tier1a)

        signals = _varied_cluster()
        result = d.dispatch(signals)

        assert isinstance(result, DispatchResult.Tier1aQueued)
        assert result.refinement_id == "myCodeSkill__v2"
        assert result.signal_refs == ["sig_1", "sig_2", "sig_3"]

    def test_builder_called_with_author_ids(self, tmp_path):
        tier1a = MagicMock()
        tier1a.build = MagicMock(return_value=Tier1aResult.CandidateWritten(
            skill_name="myCodeSkill",
            v2_path=tmp_path / "myCodeSkill__v2",
            signal_refs=[],
        ))

        d = SelfUpgradeDispatcher(tier1a_builder=tier1a)
        d.dispatch(
            _varied_cluster(),
            author_agent_id="agent_x",
            author_run_id="run_y",
        )

        tier1a.build.assert_called_once()
        kwargs = tier1a.build.call_args.kwargs
        assert kwargs["author_agent_id"] == "agent_x"
        assert kwargs["author_run_id"] == "run_y"
