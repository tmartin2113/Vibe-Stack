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
    """With no deps wired, every dispatch returns Rejected (M0 backward compat)."""
    dispatcher = SelfUpgradeDispatcher()
    signals = [_make_signal()]
    result = dispatcher.dispatch(signals)
    assert isinstance(result, DispatchResult.Rejected)


# ────────────────────────────────────────────────────────────────────────────
# Task 21: Tier 0 + Tier 3 dispatch with real dependencies
# ────────────────────────────────────────────────────────────────────────────

from unittest.mock import MagicMock

from agents.self_upgrade.reports import IssueReport, EvidenceRow
from agents.self_upgrade.tier0_builder import Tier0Result
from agents.self_upgrade.tier3_builder import Tier3Result


def test_dispatch_tier0_writes_lesson_via_store():
    """Classifier routes single actionable signal to Tier 0 → writes lesson to store."""
    fake_store = MagicMock()
    fake_store.add.return_value = "lesson_xyz"

    fake_tier0 = MagicMock()
    fake_tier0.build.return_value = Tier0Result.LessonDrafted(
        lesson="use validation",
        role="backend",
        task_type="code_generation",
        tag="",
        signal_refs=["sig_1"],
    )

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=fake_store,
        tier0_builder=fake_tier0,
        tier3_builder=MagicMock(),
        paperclip_client=MagicMock(),
        human_triage_user_id="user_prime",
    )

    signals = [_make_signal(detail="missing input validation")]
    result = dispatcher.dispatch(
        signals, author_agent_id="a", author_run_id="r1", role="backend",
    )

    assert isinstance(result, DispatchResult.Tier0Written)
    assert result.lesson_id == "lesson_xyz"
    fake_store.add.assert_called_once()
    fake_tier0.build.assert_called_once()


def test_dispatch_tier0_returns_rejected_when_deps_missing():
    """No-deps dispatcher returns Rejected with clear reason (backward compat with M0)."""
    dispatcher = SelfUpgradeDispatcher()
    signals = [_make_signal(detail="single actionable")]
    result = dispatcher.dispatch(signals)

    assert isinstance(result, DispatchResult.Rejected)
    assert "not wired" in result.reason.lower() or "dependencies" in result.reason.lower()


def test_dispatch_tier3_files_paperclip_issue():
    """Classifier routes empty-feedback cluster to Tier 3 → files Paperclip issue."""
    fake_client = MagicMock()
    fake_client.create_issue.return_value = MagicMock(id="iss_42")

    fake_report = IssueReport(
        report_id="report_1", title="T", signal_refs=["sig_1", "sig_2", "sig_3"],
        evidence=[EvidenceRow(run_id="", task_type="t", score=0, excerpt="")],
        hypothesis="", suggested_change="", suggested_change_kind="code",
        confidence=0.8, author_agent_id="", author_role="", created_at="",
    )
    fake_tier3 = MagicMock()
    fake_tier3.build.return_value = Tier3Result.ReportDrafted(report=fake_report)

    dispatcher = SelfUpgradeDispatcher(
        lesson_store=MagicMock(),
        tier0_builder=MagicMock(),
        tier3_builder=fake_tier3,
        paperclip_client=fake_client,
        human_triage_user_id="user_prime",
    )

    # 3 empty-feedback signals → classifier picks Tier 3
    signals = [_make_signal(detail="") for _ in range(3)]
    result = dispatcher.dispatch(signals)

    assert isinstance(result, DispatchResult.Tier3Filed)
    assert result.issue_id == "iss_42"
    fake_client.create_issue.assert_called_once()

    call_kwargs = fake_client.create_issue.call_args.kwargs
    assert call_kwargs["assignee_user_id"] == "user_prime"
    assert "self-upgrade" in call_kwargs["labels"]
    assert "tier-3" in call_kwargs["labels"]
    assert "auto-generated" in call_kwargs["labels"]


def test_dispatch_tier3_rejected_when_builder_drops():
    """When Tier3Builder returns Dropped, dispatcher returns Rejected with reason."""
    fake_tier3 = MagicMock()
    fake_tier3.build.return_value = Tier3Result.Dropped(
        reason="self-critique failed", signal_refs=["sig_1"],
    )

    dispatcher = SelfUpgradeDispatcher(
        tier3_builder=fake_tier3,
        paperclip_client=MagicMock(),
    )

    signals = [_make_signal(detail="") for _ in range(3)]
    result = dispatcher.dispatch(signals)

    assert isinstance(result, DispatchResult.Rejected)
    assert "self-critique" in result.reason or "dropped" in result.reason.lower()


def test_dispatch_passes_none_assignee_when_triage_user_empty():
    """Empty human_triage_user_id should result in assignee_user_id=None, not empty string."""
    fake_client = MagicMock()
    fake_client.create_issue.return_value = MagicMock(id="iss_1")

    fake_report = IssueReport(
        report_id="r", title="T", signal_refs=[],
        evidence=[], hypothesis="", suggested_change="",
        suggested_change_kind="code", confidence=0.8,
        author_agent_id="", author_role="", created_at="",
    )
    fake_tier3 = MagicMock()
    fake_tier3.build.return_value = Tier3Result.ReportDrafted(report=fake_report)

    dispatcher = SelfUpgradeDispatcher(
        tier3_builder=fake_tier3,
        paperclip_client=fake_client,
        human_triage_user_id="",  # no triage user
    )

    signals = [_make_signal(detail="") for _ in range(3)]
    dispatcher.dispatch(signals)

    call_kwargs = fake_client.create_issue.call_args.kwargs
    assert call_kwargs["assignee_user_id"] is None
