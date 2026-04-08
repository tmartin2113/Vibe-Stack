"""Classifier tests for the Tier 1a rule in SelfUpgradeDispatcher."""

from agents.self_upgrade_dispatcher import SelfUpgradeDispatcher, Tier
from agents.self_upgrade_trigger import UpgradeSignal


def _sig(task_type="code_generation", detail="some feedback", score=50):
    return UpgradeSignal(
        category="low_score",
        task_type=task_type,
        detail=detail,
        score=score,
        source_node="critic",
    )


class TestTier1aClassification:
    def test_single_signal_stays_tier_zero(self):
        d = SelfUpgradeDispatcher()
        result = d.classify_signals([_sig()])
        assert result == Tier.ZERO

    def test_three_signals_same_detail_same_type_stays_tier1b(self):
        d = SelfUpgradeDispatcher()
        signals = [_sig(detail="identical") for _ in range(3)]
        result = d.classify_signals(signals)
        assert result == Tier.ONE_B

    def test_three_signals_varied_detail_same_type_goes_tier1a(self):
        d = SelfUpgradeDispatcher()
        signals = [
            _sig(detail="Missing validation"),
            _sig(detail="Bad error handling"),
            _sig(detail="Unclear naming"),
        ]
        result = d.classify_signals(signals)
        assert result == Tier.ONE_A

    def test_three_signals_varied_detail_different_types_goes_tier3(self):
        d = SelfUpgradeDispatcher()
        signals = [
            _sig(task_type="code_generation", detail="a"),
            _sig(task_type="test_generation", detail="b"),
            _sig(task_type="code_generation", detail="c"),
        ]
        result = d.classify_signals(signals)
        assert result == Tier.THREE

    def test_two_signals_insufficient_for_tier1a(self):
        d = SelfUpgradeDispatcher()
        signals = [
            _sig(detail="a"),
            _sig(detail="b"),
        ]
        result = d.classify_signals(signals)
        assert result == Tier.THREE  # falls through

    def test_tier1b_ordered_before_tier1a(self):
        # Cluster that matches both rules: same-detail subset of same-type.
        # Tier 1b should win because it's checked first.
        d = SelfUpgradeDispatcher()
        signals = [_sig(detail="x") for _ in range(5)]
        result = d.classify_signals(signals)
        assert result == Tier.ONE_B, \
            "Tier 1b must be evaluated before Tier 1a for same-detail clusters"
