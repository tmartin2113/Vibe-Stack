"""Tests for the orchestrator main entry point."""

import signal
from unittest.mock import MagicMock, patch, call

import pytest
from agents.orchestrator_main import run_orchestrator
from agents.config import SystemConfig, SchedulerConfig, PaperclipConfig


def _agent(role, agent_id):
    from agents.paperclip_client import AgentInfo
    return AgentInfo(id=agent_id, company_id="c1", name=role, role=role)


@pytest.fixture
def config():
    cfg = SystemConfig()
    cfg.paperclip = PaperclipConfig(enabled=True, api_url="http://localhost:3100")
    cfg.scheduler = SchedulerConfig(scheduler_interval=1, max_concurrent_agents=2)
    return cfg


class TestRunOrchestrator:

    @patch("agents.orchestrator_main.set_scheduler_status_fn")
    @patch("agents.orchestrator_main.Scheduler")
    @patch("agents.orchestrator_main.AgentRegistry")
    @patch("agents.orchestrator_main.calculate_concurrency_budget", return_value=4)
    @patch("agents.orchestrator_main.discover_system")
    @patch("agents.orchestrator_main.PaperclipClient")
    @patch("agents.orchestrator_main.start_health_server")
    def test_wiring(self, mock_health, mock_client_cls, mock_discover,
                    mock_budget, mock_registry_cls, mock_scheduler_cls,
                    mock_set_status, config):
        mock_registry = mock_registry_cls.return_value
        mock_registry.resolve_all.return_value = {"cto": "uuid-1", "backend-engineer": "uuid-2"}

        mock_scheduler = mock_scheduler_cls.return_value
        mock_scheduler.run.side_effect = KeyboardInterrupt

        with pytest.raises(SystemExit):
            run_orchestrator(config)

        mock_discover.assert_called_once()
        mock_registry.resolve_all.assert_called_once()
        mock_scheduler_cls.assert_called_once()
        mock_scheduler.run.assert_called_once()

    @patch("agents.orchestrator_main.set_scheduler_status_fn")
    @patch("agents.orchestrator_main.Scheduler")
    @patch("agents.orchestrator_main.AgentRegistry")
    @patch("agents.orchestrator_main.calculate_concurrency_budget", return_value=4)
    @patch("agents.orchestrator_main.discover_system")
    @patch("agents.orchestrator_main.PaperclipClient")
    @patch("agents.orchestrator_main.start_health_server")
    def test_override_max_concurrent(self, mock_health, mock_client_cls, mock_discover,
                                      mock_budget, mock_registry_cls, mock_scheduler_cls,
                                      mock_set_status, config):
        config.scheduler.max_concurrent_agents = 3
        mock_registry = mock_registry_cls.return_value
        mock_registry.resolve_all.return_value = {"cto": "uuid-1"}
        mock_scheduler = mock_scheduler_cls.return_value
        mock_scheduler.run.side_effect = KeyboardInterrupt

        with pytest.raises(SystemExit):
            run_orchestrator(config)

        mock_budget.assert_called_once()
        call_kwargs = mock_budget.call_args
        # Check override_max was passed
        assert call_kwargs.kwargs.get("override_max") == 3 or \
               (len(call_kwargs.args) >= 4 and call_kwargs.args[3] == 3)

    @patch("agents.orchestrator_main.set_scheduler_status_fn")
    @patch("agents.orchestrator_main.Scheduler")
    @patch("agents.orchestrator_main.AgentRegistry")
    @patch("agents.orchestrator_main.calculate_concurrency_budget", return_value=2)
    @patch("agents.orchestrator_main.discover_system")
    @patch("agents.orchestrator_main.PaperclipClient")
    @patch("agents.orchestrator_main.start_health_server")
    def test_graceful_shutdown_on_keyboard_interrupt(
        self, mock_health, mock_client_cls, mock_discover,
        mock_budget, mock_registry_cls, mock_scheduler_cls,
        mock_set_status, config
    ):
        mock_registry = mock_registry_cls.return_value
        mock_registry.resolve_all.return_value = {"cto": "uuid-1"}
        mock_scheduler = mock_scheduler_cls.return_value
        mock_scheduler.run.side_effect = KeyboardInterrupt

        with pytest.raises(SystemExit) as exc_info:
            run_orchestrator(config)

        mock_scheduler.stop.assert_called_once()
        assert exc_info.value.code == 0

    @patch("agents.orchestrator_main.PaperclipClient")
    @patch("agents.orchestrator_main.start_health_server")
    @patch("agents.orchestrator_main.discover_system")
    @patch("agents.orchestrator_main.calculate_concurrency_budget", return_value=2)
    def test_exits_if_no_agents_resolved(self, mock_budget, mock_discover,
                                          mock_health, mock_client_cls, config):
        with patch("agents.orchestrator_main.AgentRegistry") as mock_reg_cls:
            mock_reg = mock_reg_cls.return_value
            mock_reg.resolve_all.return_value = {}

            with pytest.raises(SystemExit) as exc_info:
                run_orchestrator(config)
            assert exc_info.value.code == 1
