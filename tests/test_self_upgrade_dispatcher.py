from agents.self_upgrade_dispatcher import (
    DispatchResult,
    SelfUpgradeDispatcher,
    Tier,
)
from agents.self_upgrade_trigger import UpgradeSignal


def _make_signal(category="low_score", task_type="code_generation", detail="", score=40):
    return UpgradeSignal(
        category=category, task_type=task_type, detail=detail,
        score=score, source_node="critic",
    )


def test_classifier_routes_single_actionable_low_score_to_tier0():
    dispatcher = SelfUpgradeDispatcher()
    signals = [_make_signal(detail="Missing error handling around DB calls")]
    tier = dispatcher.classify_signals(signals)
    assert tier == Tier.ZERO


def test_classifier_routes_repeated_pattern_to_tier1b():
    dispatcher = SelfUpgradeDispatcher()
    signals = [
        _make_signal(detail="Missing request validation"),
        _make_signal(detail="Missing request validation"),
        _make_signal(detail="Missing request validation"),
    ]
    tier = dispatcher.classify_signals(signals)
    assert tier == Tier.ONE_B


def test_classifier_routes_empty_feedback_to_tier3():
    dispatcher = SelfUpgradeDispatcher()
    signals = [_make_signal(detail=""), _make_signal(detail=""), _make_signal(detail="")]
    tier = dispatcher.classify_signals(signals)
    assert tier == Tier.THREE


def test_dispatch_stub_returns_rejected_for_every_tier_in_m0():
    """In M0, all builders are stubs — every dispatch returns Rejected."""
    dispatcher = SelfUpgradeDispatcher()
    signals = [_make_signal()]
    result = dispatcher.dispatch(signals)
    assert isinstance(result, DispatchResult.Rejected)
    assert "stub" in result.reason.lower()
