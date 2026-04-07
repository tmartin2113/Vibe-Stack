"""Integration test: graph_nodes invokes the dispatcher at end-of-run."""
from unittest.mock import MagicMock, patch

from agents.self_upgrade_dispatcher import DispatchResult
from agents.self_upgrade_trigger import SelfUpgradeTrigger, UpgradeSignal


def test_run_self_upgrade_dispatch_with_signals():
    """When signals exist, the helper invokes the dispatcher and returns its result."""
    from agents.graph_nodes import _run_self_upgrade_dispatch

    fake_trigger = MagicMock(spec=SelfUpgradeTrigger)
    fake_trigger.get_accumulated_signals.return_value = [
        UpgradeSignal(
            category="low_score", task_type="t", detail="d", score=40,
        ),
    ]

    with patch("agents.graph_nodes.SelfUpgradeDispatcher") as dispatcher_cls:
        dispatcher_cls.return_value.dispatch.return_value = DispatchResult.Rejected(
            reason="stub", signal_refs=["sig_1"],
        )

        result = _run_self_upgrade_dispatch(fake_trigger, task_type="t")

        dispatcher_cls.return_value.dispatch.assert_called_once()
        assert isinstance(result, DispatchResult.Rejected)


def test_run_self_upgrade_dispatch_no_signals_short_circuits():
    """When no accumulated signals exist, the dispatcher is not called."""
    from agents.graph_nodes import _run_self_upgrade_dispatch

    fake_trigger = MagicMock(spec=SelfUpgradeTrigger)
    fake_trigger.get_accumulated_signals.return_value = []

    with patch("agents.graph_nodes.SelfUpgradeDispatcher") as dispatcher_cls:
        result = _run_self_upgrade_dispatch(fake_trigger, task_type="t")

        dispatcher_cls.return_value.dispatch.assert_not_called()
        assert isinstance(result, DispatchResult.Rejected)
        assert "no signals" in result.reason
