"""
Tests to improve coverage for agents/main.py, agents/tools/seo_tools.py,
and agents/daemon.py.

Covers:
- main.py: JsonFormatter, setup_logging, MultiAgentSystem lifecycle,
  _handle_command, main() CLI argument parsing, doctor/heartbeat/daemon modes,
  spending-status/spending-reset, request context helpers
- seo_tools.py: LighthouseSEOTool.execute, PageAnalyzerTool.execute,
  SEOChecklistTool.execute with various content combinations
- daemon.py: _initialize_messengers, _initialize_paperclip,
  _poll_mattermost_mentions, _poll_slack_mentions, _send_response,
  _create_issue_from_mention, _poll_issue_completion, _handle_mention,
  _handler_thread, _polling_loop, start/stop lifecycle, run_daemon
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue, Empty, Full
from unittest.mock import (
    MagicMock,
    PropertyMock,
    call,
    patch,
    ANY,
)

import pytest

os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"


# ====================================================================
# agents/main.py — Request context helpers
# ====================================================================


class TestRequestContext:
    """Test set_request_context, set_paperclip_context, clear_request_context."""

    def test_set_and_clear_request_context(self):
        from agents.main import (
            set_request_context,
            clear_request_context,
            _request_context,
        )

        set_request_context(request_id="req-1", session_id="sess-1")
        assert _request_context.request_id == "req-1"
        assert _request_context.session_id == "sess-1"

        clear_request_context()
        assert _request_context.request_id == ""
        assert _request_context.session_id == ""

    def test_set_paperclip_context(self):
        from agents.main import (
            set_paperclip_context,
            clear_request_context,
            _request_context,
        )

        set_paperclip_context(
            issue_id="iss-42",
            agent_id="agent-7",
            run_id="run-99",
            task_type="code",
        )
        assert _request_context.paperclip_issue_id == "iss-42"
        assert _request_context.paperclip_agent_id == "agent-7"
        assert _request_context.paperclip_run_id == "run-99"
        assert _request_context.paperclip_task_type == "code"

        clear_request_context()
        assert _request_context.paperclip_issue_id == ""
        assert _request_context.paperclip_task_type == ""


# ====================================================================
# agents/main.py — JsonFormatter
# ====================================================================


class TestJsonFormatter:
    """Test the structured JSON log formatter."""

    def test_basic_format(self):
        from agents.main import JsonFormatter

        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="agents.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Hello %s",
            args=("world",),
            exc_info=None,
        )
        output = fmt.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "Hello world"
        assert data["logger"] == "agents.test"
        assert "timestamp" in data

    def test_includes_paperclip_context(self):
        from agents.main import JsonFormatter, set_paperclip_context, clear_request_context

        set_paperclip_context(issue_id="iss-1", agent_id="a-1")
        try:
            fmt = JsonFormatter()
            record = logging.LogRecord(
                name="test", level=logging.INFO,
                pathname="t.py", lineno=1, msg="hi", args=(), exc_info=None,
            )
            data = json.loads(fmt.format(record))
            assert data["paperclip_issue_id"] == "iss-1"
            assert data["paperclip_agent_id"] == "a-1"
        finally:
            clear_request_context()

    def test_includes_request_context(self):
        from agents.main import JsonFormatter, set_request_context, clear_request_context

        set_request_context(request_id="r-1", session_id="s-1")
        try:
            fmt = JsonFormatter()
            record = logging.LogRecord(
                name="test", level=logging.INFO,
                pathname="t.py", lineno=1, msg="msg", args=(), exc_info=None,
            )
            data = json.loads(fmt.format(record))
            assert data["request_id"] == "r-1"
            assert data["session_id"] == "s-1"
        finally:
            clear_request_context()

    def test_includes_extra_fields(self):
        from agents.main import JsonFormatter

        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.WARNING,
            pathname="t.py", lineno=1, msg="oops", args=(), exc_info=None,
        )
        record.custom_field = "custom_value"
        data = json.loads(fmt.format(record))
        assert data["custom_field"] == "custom_value"

    def test_includes_exception_info(self):
        from agents.main import JsonFormatter

        fmt = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                name="test", level=logging.ERROR,
                pathname="t.py", lineno=1, msg="err", args=(),
                exc_info=sys.exc_info(),
            )
        data = json.loads(fmt.format(record))
        assert "exception" in data
        assert "ValueError" in data["exception"]


# ====================================================================
# agents/main.py — setup_logging
# ====================================================================


class TestSetupLogging:
    """Test setup_logging with JSON and Rich modes."""

    def test_json_mode(self):
        from agents.main import setup_logging
        from agents.config import SystemConfig

        cfg = SystemConfig()
        cfg.log_level = "DEBUG"

        with patch.dict(os.environ, {"LOG_FORMAT": "json"}):
            # Reset logging to avoid conflicts
            root = logging.getLogger()
            old_handlers = root.handlers[:]
            try:
                logger = setup_logging(cfg)
                assert logger.name == "agents"
                assert logger.level == logging.DEBUG
            finally:
                root.handlers = old_handlers

    def test_rich_mode(self):
        from agents.main import setup_logging
        from agents.config import SystemConfig

        cfg = SystemConfig()
        cfg.log_level = "INFO"

        with patch.dict(os.environ, {"LOG_FORMAT": ""}):
            root = logging.getLogger()
            old_handlers = root.handlers[:]
            try:
                logger = setup_logging(cfg)
                assert logger.name == "agents"
            finally:
                root.handlers = old_handlers


# ====================================================================
# agents/main.py — MultiAgentSystem
# ====================================================================


class TestMultiAgentSystem:
    """Test MultiAgentSystem init, initialize, run, interactive_mode, _handle_command."""

    @pytest.fixture
    def system(self):
        """Create a MultiAgentSystem with mocked logging."""
        from agents.config import SystemConfig
        from agents.main import MultiAgentSystem

        cfg = SystemConfig()
        cfg.log_level = "WARNING"  # quiet
        with patch("agents.main.setup_logging") as mock_log:
            mock_log.return_value = MagicMock()
            sys_obj = MultiAgentSystem(config=cfg)
        return sys_obj

    def test_init_defaults(self):
        from agents.main import MultiAgentSystem

        with patch("agents.main.setup_logging") as mock_log:
            mock_log.return_value = MagicMock()
            sys_obj = MultiAgentSystem()
        # Should get dev config by default
        assert sys_obj.config is not None
        assert sys_obj.graph is None
        assert sys_obj.base_model is None

    def test_initialize_success(self, system):
        mock_model = MagicMock()
        mock_registry = MagicMock()
        mock_graph = MagicMock()

        with patch.object(system, "_load_base_model", return_value=mock_model), \
             patch.object(system, "_setup_adapters", return_value=mock_registry), \
             patch("agents.main.create_agent_graph", return_value=mock_graph):
            mock_registry.list_adapters.return_value = ["a", "b"]
            system.config.validate = MagicMock(return_value=True)
            system.initialize()

        assert system.base_model is mock_model
        assert system.adapter_registry is mock_registry
        assert system.graph is mock_graph

    def test_initialize_validation_failure(self, system):
        system.config.validate = MagicMock(return_value=False)
        with pytest.raises(SystemExit):
            system.initialize()

    def test_load_base_model_success(self, system):
        mock_backend = MagicMock()
        with patch("agents.main.create_backend_from_config", return_value=mock_backend):
            result = system._load_base_model()
        assert result is mock_backend

    def test_load_base_model_failure(self, system):
        with patch("agents.main.create_backend_from_config", side_effect=RuntimeError("no llm")):
            with pytest.raises(RuntimeError, match="no llm"):
                system._load_base_model()

    def test_setup_adapters(self, system):
        system.base_model = MagicMock()
        registry = system._setup_adapters()
        adapters = registry.list_adapters()
        assert "vibe" in adapters
        assert "critic" in adapters
        assert "code" in adapters
        assert "creative" in adapters
        assert len(adapters) == 16  # 16 adapter definitions (including general)

    def test_run_not_initialized(self, system):
        with pytest.raises(RuntimeError, match="not initialized"):
            system.run("hello")

    def test_run_non_stream(self, system):
        system.graph = MagicMock()
        with patch("agents.main.run_workflow", return_value={"output": "done"}) as mock_run:
            result = system.run("test request", max_iterations=5, quality_threshold=90)
        mock_run.assert_called_once_with(system.graph, "test request", 5, 90, verbose=True)

    def test_run_stream(self, system):
        system.graph = MagicMock()
        with patch("agents.main.stream_workflow", return_value={"output": "streamed"}) as mock_stream:
            result = system.run("test request", stream=True)
        mock_stream.assert_called_once()

    def test_run_uses_config_defaults(self, system):
        system.graph = MagicMock()
        system.config.workflow.max_iterations = 7
        system.config.workflow.quality_threshold = 80
        with patch("agents.main.run_workflow", return_value={}) as mock_run:
            system.run("request")
        mock_run.assert_called_once_with(system.graph, "request", 7, 80, verbose=True)

    def test_handle_command_help(self, system):
        # Should not raise
        system._handle_command("/help")

    def test_handle_command_status(self, system):
        system.adapter_registry = MagicMock()
        system.adapter_registry.list_adapters.return_value = ["a"]
        system._handle_command("/status")

    def test_handle_command_config(self, system):
        system._handle_command("/config")

    def test_handle_command_graph(self, system):
        system.graph = MagicMock()
        with patch("agents.main.print_graph_structure") as mock_print:
            system._handle_command("/graph")
        mock_print.assert_called_once_with(system.graph)

    def test_handle_command_adapters(self, system):
        system.adapter_registry = MagicMock()
        system.adapter_registry.list_adapters.return_value = ["vibe", "critic"]
        system._handle_command("/adapters")

    def test_handle_command_adapters_none(self, system):
        system.adapter_registry = None
        system._handle_command("/adapters")

    def test_handle_command_unknown(self, system):
        system._handle_command("/foobar")

    def test_handle_command_status_no_adapters(self, system):
        system.adapter_registry = None
        system._handle_command("/status")


# ====================================================================
# agents/main.py — main() CLI entry point
# ====================================================================


class TestMainCLI:
    """Test the main() function with various CLI arguments.

    Since main() uses local imports (discover_system, compute_resource_plan,
    SandboxConfig, etc.), we patch at the source module level.
    """

    # Common patch targets for the local imports inside main()
    _RESOURCE_PATCHES = {
        "agents.resource_discovery.discover_system": "discover_system",
        "agents.resource_allocator.compute_resource_plan": "compute_resource_plan",
        "agents.sandbox.config.SandboxConfig": "SandboxConfig",
    }

    def _make_args(self, **overrides):
        """Build a mock args namespace with sensible defaults."""
        defaults = dict(
            dev=False,
            spending_status=False,
            spending_reset=False,
            heartbeat=False,
            doctor=False,
            daemon=False,
            show_graph=False,
            request=[],
            max_iterations=None,
            threshold=None,
            stream=False,
        )
        defaults.update(overrides)
        args = MagicMock()
        for k, v in defaults.items():
            setattr(args, k, v)
        return args

    def _make_config_mock(self):
        cfg = MagicMock()
        cfg.paperclip = MagicMock()
        cfg.paperclip.enabled = False
        cfg.paperclip.output_format = "json"
        cfg.spending = MagicMock()
        cfg.spending.db_path = None
        cfg.spending.window_seconds = 3600
        cfg.spending.max_cents_per_window = 500
        cfg.spending.max_heartbeats_per_window = 30
        cfg.spending.max_consecutive_non_idle = 10
        cfg.spending.cooldown_seconds = 300
        cfg.spending.max_cooldown_seconds = 7200
        cfg.spending.retention_days = 30
        return cfg

    def _enter_resource_patches(self):
        """Apply resource discovery/allocator/sandbox patches.

        Returns (mock_sandbox_config, list_of_patchers_to_stop).
        """
        mock_sandbox_config = MagicMock()
        mock_sandbox_config.apply_env_overrides = MagicMock()

        p1 = patch("agents.resource_discovery.discover_system", return_value=MagicMock())
        p2 = patch("agents.resource_allocator.compute_resource_plan", return_value=MagicMock())
        p3 = patch("agents.sandbox.config.SandboxConfig")
        p1.start()
        p2.start()
        mock_sc = p3.start()
        mock_sc.from_resource_plan.return_value = mock_sandbox_config
        return mock_sandbox_config, [p1, p2, p3]

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_doctor_mode(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(doctor=True)
        cfg = self._make_config_mock()

        mock_report = MagicMock()
        mock_report.fail_count = 0
        mock_report.format.return_value = "All checks passed"

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.doctor.run_doctor", return_value=mock_report):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_doctor_mode_with_failures(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(doctor=True)
        cfg = self._make_config_mock()

        mock_report = MagicMock()
        mock_report.fail_count = 2
        mock_report.format.return_value = "2 checks failed"

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.doctor.run_doctor", return_value=mock_report):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_heartbeat_mode_json(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(heartbeat=True)
        cfg = self._make_config_mock()
        cfg.paperclip.output_format = "json"

        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.to_json.return_value = '{"status": "idle"}'
        mock_result.summary = "No tasks"

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.main.setup_logging"), \
                 patch("agents.heartbeat.run_heartbeat", return_value=mock_result):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
                mock_result.to_json.assert_called_once()
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_heartbeat_mode_text(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(heartbeat=True)
        cfg = self._make_config_mock()
        cfg.paperclip.output_format = "text"

        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.summary = "Idle - no tasks"

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.main.setup_logging"), \
                 patch("agents.heartbeat.run_heartbeat", return_value=mock_result):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_daemon_mode(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(daemon=True)
        cfg = self._make_config_mock()

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.main.setup_logging"), \
                 patch("agents.daemon.run_daemon") as mock_daemon:
                main()
                mock_daemon.assert_called_once()
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_dev_flag_uses_dev_config(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(dev=True, doctor=True)
        cfg = self._make_config_mock()

        mock_report = MagicMock()
        mock_report.fail_count = 0
        mock_report.format.return_value = "OK"

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_dev_config", return_value=cfg) as mock_dev, \
                 patch("agents.doctor.run_doctor", return_value=mock_report):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
                mock_dev.assert_called_once()
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_spending_status(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(spending_status=True)
        cfg = self._make_config_mock()

        mock_status = MagicMock()
        mock_status.window_seconds = 3600
        mock_status.total_cost_cents = 100
        mock_status.non_idle_heartbeats = 5
        mock_status.consecutive_non_idle = 2
        mock_status.breaker = MagicMock()
        mock_status.breaker.state.value = "closed"

        mock_tracker = MagicMock()
        mock_tracker.get_status.return_value = mock_status
        mock_tracker.max_cents_per_window = 500
        mock_tracker.max_heartbeats_per_window = 30
        mock_tracker.max_consecutive_non_idle = 10

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.spending_tracker.SpendingTracker", return_value=mock_tracker):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_spending_status_tripped(self, mock_parse_args):
        """Test spending status display when breaker is tripped."""
        from agents.main import main

        mock_parse_args.return_value = self._make_args(spending_status=True)
        cfg = self._make_config_mock()

        mock_status = MagicMock()
        mock_status.window_seconds = 3600
        mock_status.total_cost_cents = 600
        mock_status.non_idle_heartbeats = 35
        mock_status.consecutive_non_idle = 12
        mock_status.breaker = MagicMock()
        mock_status.breaker.state.value = "open"
        mock_status.breaker.reason = "cost exceeded"
        mock_status.breaker.trip_count = 2
        mock_status.breaker.retry_after_seconds = 300

        mock_tracker = MagicMock()
        mock_tracker.get_status.return_value = mock_status
        mock_tracker.max_cents_per_window = 500
        mock_tracker.max_heartbeats_per_window = 30
        mock_tracker.max_consecutive_non_idle = 10

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.spending_tracker.SpendingTracker", return_value=mock_tracker):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_spending_reset(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(spending_reset=True)
        cfg = self._make_config_mock()

        mock_tracker = MagicMock()

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.spending_tracker.SpendingTracker", return_value=mock_tracker):
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 0
                mock_tracker.reset.assert_called_once()
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_show_graph(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(show_graph=True)
        cfg = self._make_config_mock()

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.main.MultiAgentSystem") as MockSystem, \
                 patch("agents.main.print_graph_structure") as mock_print:
                mock_sys = MagicMock()
                MockSystem.return_value = mock_sys

                main()
                mock_sys.initialize.assert_called_once()
                mock_print.assert_called_once()
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_single_request_mode(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args(
            request=["Write", "a", "hello", "world"],
            max_iterations=5,
            threshold=90,
            stream=True,
        )
        cfg = self._make_config_mock()

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.main.MultiAgentSystem") as MockSystem:
                mock_sys = MagicMock()
                MockSystem.return_value = mock_sys

                main()
                mock_sys.run.assert_called_once_with(
                    "Write a hello world",
                    max_iterations=5,
                    quality_threshold=90,
                    stream=True,
                )
        finally:
            for p in patchers:
                p.stop()

    @patch("agents.main.argparse.ArgumentParser.parse_args")
    def test_interactive_mode(self, mock_parse_args):
        from agents.main import main

        mock_parse_args.return_value = self._make_args()
        cfg = self._make_config_mock()

        _, patchers = self._enter_resource_patches()
        try:
            with patch("agents.main.get_production_config", return_value=cfg), \
                 patch("agents.main.MultiAgentSystem") as MockSystem:
                mock_sys = MagicMock()
                MockSystem.return_value = mock_sys

                main()
                mock_sys.interactive_mode.assert_called_once()
        finally:
            for p in patchers:
                p.stop()


# ====================================================================
# agents/tools/seo_tools.py — LighthouseSEOTool
# ====================================================================


class TestLighthouseSEOTool:
    """Test LighthouseSEOTool.execute with mocked subprocess."""

    def test_init(self):
        from agents.tools.seo_tools import LighthouseSEOTool

        tool = LighthouseSEOTool()
        assert tool.name == "lighthouse_seo"
        assert "Lighthouse" in tool.description

    def test_lighthouse_not_installed(self):
        from agents.tools.seo_tools import LighthouseSEOTool

        tool = LighthouseSEOTool()
        with patch("agents.tools.seo_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = tool.execute("https://example.com")
        assert result["success"] is False
        assert "not installed" in result["error"]

    def test_lighthouse_output_file_missing(self):
        from agents.tools.seo_tools import LighthouseSEOTool

        tool = LighthouseSEOTool()

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == "which":
                return MagicMock(returncode=0)
            # lighthouse run returns but no output file created
            return MagicMock(returncode=1, stderr="lighthouse error")

        with patch("agents.tools.seo_tools.subprocess.run", side_effect=side_effect), \
             patch("agents.tools.seo_tools.Path.exists", return_value=False), \
             patch("agents.tools.seo_tools.Path.unlink"):
            result = tool.execute("https://example.com")
        assert result["success"] is False
        assert "failed" in result["error"].lower()

    def test_lighthouse_success(self):
        from agents.tools.seo_tools import LighthouseSEOTool

        tool = LighthouseSEOTool()

        lighthouse_data = {
            "categories": {
                "seo": {"score": 0.85},
                "performance": {"score": 0.70},
                "accessibility": {"score": 0.95},
            },
            "audits": {
                "document-title": {
                    "score": 0.5,
                    "title": "Missing title",
                    "description": "Add a title",
                    "displayValue": "No title found",
                },
                "meta-description": {
                    "score": 1,
                    "title": "Meta description OK",
                    "description": "Good description",
                },
            },
        }

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == "which":
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stderr="")

        with patch("agents.tools.seo_tools.subprocess.run", side_effect=side_effect), \
             patch("builtins.open", MagicMock()), \
             patch("agents.tools.seo_tools.json.load", return_value=lighthouse_data), \
             patch("agents.tools.seo_tools.Path.exists", return_value=True), \
             patch("agents.tools.seo_tools.Path.unlink"):
            result = tool.execute("https://example.com")

        assert result["success"] is True
        assert result["seo_score"] == 85
        assert result["performance_score"] == 70
        assert result["accessibility_score"] == 95
        assert len(result["issues"]) == 1  # Only document-title has score < 1
        assert result["issues"][0]["issue"] == "Missing title"

    def test_lighthouse_no_audits(self):
        from agents.tools.seo_tools import LighthouseSEOTool

        tool = LighthouseSEOTool()
        lighthouse_data = {
            "categories": {
                "seo": {"score": 0.5},
                "performance": {"score": 0.5},
                "accessibility": {"score": 0.5},
            },
            "audits": {},
        }

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == "which":
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stderr="")

        with patch("agents.tools.seo_tools.subprocess.run", side_effect=side_effect), \
             patch("builtins.open", MagicMock()), \
             patch("agents.tools.seo_tools.json.load", return_value=lighthouse_data), \
             patch("agents.tools.seo_tools.Path.exists", return_value=True), \
             patch("agents.tools.seo_tools.Path.unlink"):
            result = tool.execute("https://example.com")
        assert result["success"] is False
        assert "no audit data" in result["error"].lower()

    def test_lighthouse_parse_error(self):
        from agents.tools.seo_tools import LighthouseSEOTool

        tool = LighthouseSEOTool()
        # categories with None score triggers TypeError
        lighthouse_data = {
            "categories": {
                "seo": {"score": None},
            },
        }

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == "which":
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stderr="")

        with patch("agents.tools.seo_tools.subprocess.run", side_effect=side_effect), \
             patch("builtins.open", MagicMock()), \
             patch("agents.tools.seo_tools.json.load", return_value=lighthouse_data), \
             patch("agents.tools.seo_tools.Path.exists", return_value=True), \
             patch("agents.tools.seo_tools.Path.unlink"):
            result = tool.execute("https://example.com")
        assert result["success"] is False
        assert "parse" in result["error"].lower() or "failed" in result["error"].lower()

    def test_lighthouse_timeout(self):
        from agents.tools.seo_tools import LighthouseSEOTool

        tool = LighthouseSEOTool()

        call_count = [0]

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == "which":
                return MagicMock(returncode=0)
            raise subprocess.TimeoutExpired(cmd="lighthouse", timeout=60)

        with patch("agents.tools.seo_tools.subprocess.run", side_effect=side_effect):
            result = tool.execute("https://example.com", timeout=60)
        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_lighthouse_general_exception(self):
        from agents.tools.seo_tools import LighthouseSEOTool

        tool = LighthouseSEOTool()

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == "which":
                return MagicMock(returncode=0)
            raise OSError("disk full")

        with patch("agents.tools.seo_tools.subprocess.run", side_effect=side_effect):
            result = tool.execute("https://example.com")
        assert result["success"] is False
        assert "disk full" in result["error"]


# ====================================================================
# agents/tools/seo_tools.py — PageAnalyzerTool
# ====================================================================


def _has_seo_deps() -> bool:
    try:
        import requests  # noqa: F401
        from bs4 import BeautifulSoup  # noqa: F401
        return True
    except ImportError:
        return False


_seo_deps_available = _has_seo_deps()


class TestPageAnalyzerTool:
    """Test PageAnalyzerTool.execute with mocked HTTP requests."""

    def test_init(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        assert tool.name == "page_analyzer"

    def test_requests_not_available(self):
        from agents.tools import seo_tools
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        original = seo_tools.REQUESTS_AVAILABLE
        try:
            seo_tools.REQUESTS_AVAILABLE = False
            result = tool.execute("https://example.com")
            assert result["success"] is False
            assert "required" in result["error"].lower()
        finally:
            seo_tools.REQUESTS_AVAILABLE = original


@pytest.mark.skipif(not _seo_deps_available, reason="requests and beautifulsoup4 not installed")
class TestPageAnalyzerToolWithDeps:
    """PageAnalyzerTool tests that require requests + beautifulsoup4."""

    def test_full_page_analysis(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()

        html = """
        <html>
        <head>
            <title>My Awesome Page Title That Is Just Right</title>
            <meta name="description" content="This is a meta description that is between 150 and 160 characters long. We need to make it exactly right for optimal SEO performance and search results.">
        </head>
        <body>
            <h1>Main Heading</h1>
            <h2>Sub Heading 1</h2>
            <h2>Sub Heading 2</h2>
            <h3>Sub Sub Heading</h3>
            <p>{content}</p>
            <img src="img1.jpg" alt="Image 1">
            <img src="img2.jpg">
            <a href="/internal1">Internal Link</a>
            <a href="/internal2">Internal Link 2</a>
            <a href="/internal3">Internal Link 3</a>
            <a href="https://external.com">External</a>
        </body>
        </html>
        """.format(content=" ".join(["word"] * 1500))

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com", target_keyword="Main Heading")

        assert result["success"] is True
        assert result["headers"]["h1_count"] == 1
        assert result["headers"]["h2_count"] == 2
        assert result["headers"]["h3_count"] == 1
        assert result["images"]["total"] == 2
        assert result["images"]["with_alt"] == 1
        assert result["images"]["without_alt"] == 1
        assert result["links"]["external"] == 1
        assert result["keyword_analysis"] is not None
        assert result["keyword_analysis"]["in_h1"] is True

    def test_page_missing_title(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = "<html><body><p>No title here</p></body></html>"

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        assert result["success"] is True
        # Should have critical issue for missing title
        issues = result["issues"]
        assert any(i["issue"] == "Missing title tag" for i in issues)

    def test_page_missing_meta_description(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = "<html><head><title>Title</title></head><body><p>body</p></body></html>"

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        assert result["success"] is True
        issues = result["issues"]
        assert any(i["issue"] == "Missing meta description" for i in issues)

    def test_page_missing_h1(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = """
        <html><head><title>Title</title></head>
        <body><h2>No H1</h2><p>Content</p></body></html>
        """

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        assert result["success"] is True
        issues = result["issues"]
        assert any(i["issue"] == "Missing H1 tag" for i in issues)

    def test_page_multiple_h1(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = """
        <html><head><title>Title</title></head>
        <body><h1>First</h1><h1>Second</h1></body></html>
        """

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        assert result["success"] is True
        issues = result["issues"]
        assert any(i["issue"] == "Multiple H1 tags" for i in issues)

    def test_short_title(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = "<html><head><title>Short</title></head><body><p>text</p></body></html>"

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        issues = result["issues"]
        assert any(i["issue"] == "Title too short" for i in issues)

    def test_long_title(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        long_title = "A" * 70
        html = f"<html><head><title>{long_title}</title></head><body><p>text</p></body></html>"

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        issues = result["issues"]
        assert any(i["issue"] == "Title too long" for i in issues)

    def test_short_meta_description(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = '<html><head><meta name="description" content="Too short"></head><body><p>text</p></body></html>'

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        issues = result["issues"]
        assert any(i["issue"] == "Meta description too short" for i in issues)

    def test_long_meta_description(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        long_meta = "A" * 200
        html = f'<html><head><meta name="description" content="{long_meta}"></head><body><p>text</p></body></html>'

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        issues = result["issues"]
        assert any(i["issue"] == "Meta description too long" for i in issues)

    def test_short_content(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = "<html><body><h1>Title</h1><p>Short content with just a few words.</p></body></html>"

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        issues = result["issues"]
        assert any("Content too short" in i["issue"] for i in issues)

    def test_medium_content(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        content = " ".join(["word"] * 500)
        html = f"<html><body><h1>Title</h1><p>{content}</p></body></html>"

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        issues = result["issues"]
        assert any("below recommended" in i["issue"] for i in issues)

    def test_keyword_not_in_h1(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = """
        <html><body>
            <h1>Something Else Entirely</h1>
            <p>The zebra appears here many times.</p>
        </body></html>
        """

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com", target_keyword="missing phrase")

        issues = result["issues"]
        assert any("Target keyword not in H1" in i["issue"] for i in issues)

    def test_keyword_not_in_intro(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        # keyword NOT in H1 and NOT in first 100 words, appears only late
        filler = " ".join(["filler"] * 200)
        html = f"""
        <html><body>
            <h1>Unrelated Heading</h1>
            <p>{filler}</p>
            <p>special unicorn phrase appears late</p>
        </body></html>
        """

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com", target_keyword="special unicorn phrase")

        issues = result["issues"]
        issue_texts = [i["issue"] for i in issues]
        assert "Target keyword not in introduction" in issue_texts

    def test_few_internal_links(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = """
        <html><body>
            <h1>Title</h1>
            <a href="/one">One</a>
        </body></html>
        """

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        issues = result["issues"]
        assert any("internal links" in i["issue"].lower() for i in issues)

    def test_request_exception(self):
        import requests as req_lib
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()

        with patch("agents.tools.seo_tools.requests.get", side_effect=req_lib.exceptions.ConnectionError("timeout")):
            result = tool.execute("https://example.com")

        assert result["success"] is False
        assert "fetch" in result["error"].lower()

    def test_general_exception(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()

        mock_response = MagicMock()
        mock_response.text = "<html></html>"
        mock_response.raise_for_status = MagicMock(side_effect=RuntimeError("unexpected"))

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        assert result["success"] is False

    def test_no_images_no_alt_issue(self):
        from agents.tools.seo_tools import PageAnalyzerTool

        tool = PageAnalyzerTool()
        html = "<html><body><h1>Title</h1><p>No images here</p></body></html>"

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()

        with patch("agents.tools.seo_tools.requests.get", return_value=mock_response):
            result = tool.execute("https://example.com")

        assert result["success"] is True
        assert result["images"]["total"] == 0
        assert result["images"]["alt_coverage_percent"] == 0


# ====================================================================
# agents/tools/seo_tools.py — SEOChecklistTool
# ====================================================================


class TestSEOChecklistTool:
    """Test SEOChecklistTool.execute with various content dictionaries."""

    def test_init(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        assert tool.name == "seo_checklist"

    def test_perfect_content(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        content = {
            "title": "A" * 55,  # 50-60 chars
            "meta_description": "B" * 155,  # 150-160 chars
            "h1": "Main Heading with keyword",
            "h2s": ["Sub 1", "Sub 2", "Sub 3", "Sub 4"],  # 3-6 h2s
            "content": " ".join(["keyword"] * 2000),  # >= 1500 words
            "images": [
                {"src": "a.jpg", "alt": "desc"},
                {"src": "b.jpg", "alt": "desc"},
            ],
            "links": ["/a", "/b", "/c", "/d", "/e"],  # 3-10 links
            "target_keyword": "keyword",
        }
        result = tool.execute(content)
        assert result["success"] is True
        assert result["overall_status"] in ("excellent", "good")
        assert result["overall_score"] >= 75

    def test_empty_content(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({})
        assert result["success"] is True
        assert result["overall_status"] == "poor"

    def test_missing_title(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"title": ""})
        assert result["success"] is True
        checks = result["checks"]
        title_check = next(c for c in checks if c["check"] == "Title length")
        assert title_check["status"] == "fail"

    def test_warning_title_length(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"title": "A" * 40})  # not in 50-60 range, not 0
        checks = result["checks"]
        title_check = next(c for c in checks if c["check"] == "Title length")
        assert title_check["status"] == "warning"

    def test_missing_meta_description(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"meta_description": ""})
        checks = result["checks"]
        meta_check = next(c for c in checks if c["check"] == "Meta description length")
        assert meta_check["status"] == "fail"

    def test_warning_meta_description(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"meta_description": "A" * 100})  # too short, not empty
        checks = result["checks"]
        meta_check = next(c for c in checks if c["check"] == "Meta description length")
        assert meta_check["status"] == "warning"

    def test_no_h2s(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"h2s": []})
        checks = result["checks"]
        h2_check = next(c for c in checks if c["check"] == "H2 structure")
        assert h2_check["status"] == "fail"

    def test_warning_h2_count(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"h2s": ["one", "two"]})
        checks = result["checks"]
        h2_check = next(c for c in checks if c["check"] == "H2 structure")
        assert h2_check["status"] == "warning"

    def test_medium_content_length(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"content": " ".join(["w"] * 1200)})
        checks = result["checks"]
        length_check = next(c for c in checks if c["check"] == "Content length")
        assert length_check["status"] == "warning"

    def test_short_content_length(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"content": "short text"})
        checks = result["checks"]
        length_check = next(c for c in checks if c["check"] == "Content length")
        assert length_check["status"] == "fail"

    def test_images_partial_alt(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({
            "images": [
                {"src": "a.jpg", "alt": "desc"},
                {"src": "b.jpg"},
            ],
        })
        checks = result["checks"]
        img_check = next(c for c in checks if c["check"] == "Image alt tags")
        assert img_check["status"] == "warning"

    def test_no_images(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"images": []})
        checks = result["checks"]
        img_check = next(c for c in checks if c["check"] == "Image alt tags")
        assert img_check["status"] == "info"

    def test_few_links(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"links": ["/a"]})
        checks = result["checks"]
        link_check = next(c for c in checks if c["check"] == "Internal links")
        assert link_check["status"] == "warning"

    def test_many_links(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({"links": [f"/l{i}" for i in range(15)]})
        checks = result["checks"]
        link_check = next(c for c in checks if c["check"] == "Internal links")
        assert link_check["status"] == "info"

    def test_keyword_checks_all_pass(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({
            "title": "A" * 20 + " keyword " + "B" * 20,
            "meta_description": "C" * 60 + " keyword " + "D" * 60,
            "h1": "keyword heading",
            "content": "keyword " + " ".join(["other"] * 200),
            "target_keyword": "keyword",
        })
        assert result["success"] is True
        checks = result["checks"]
        kw_title = next(c for c in checks if c["check"] == "Keyword in title")
        assert kw_title["status"] == "pass"
        kw_meta = next(c for c in checks if c["check"] == "Keyword in meta description")
        assert kw_meta["status"] == "pass"
        kw_h1 = next(c for c in checks if c["check"] == "Keyword in H1")
        assert kw_h1["status"] == "pass"
        kw_intro = next(c for c in checks if c["check"] == "Keyword in first 100 words")
        assert kw_intro["status"] == "pass"

    def test_keyword_checks_all_fail(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        result = tool.execute({
            "title": "No match here",
            "meta_description": "Nothing relevant",
            "h1": "Different heading",
            "content": " ".join(["filler"] * 200),
            "target_keyword": "unicorn",
        })
        checks = result["checks"]
        kw_title = next(c for c in checks if c["check"] == "Keyword in title")
        assert kw_title["status"] == "fail"
        kw_h1 = next(c for c in checks if c["check"] == "Keyword in H1")
        assert kw_h1["status"] == "fail"

    def test_needs_improvement_status(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        # Some pass, some fail — should be 'needs_improvement' or 'good'
        result = tool.execute({
            "title": "A" * 55,
            "meta_description": "",
            "h1": "heading",
            "h2s": [],
            "content": " ".join(["w"] * 1600),
            "images": [],
            "links": ["/a", "/b", "/c"],
        })
        assert result["success"] is True
        assert result["overall_status"] in ("good", "needs_improvement")

    def test_exception_handling(self):
        from agents.tools.seo_tools import SEOChecklistTool

        tool = SEOChecklistTool()
        # Force an exception by passing content_data that causes a crash
        # We mock the internals — an exception in processing should be caught
        with patch.object(tool, "execute", wraps=tool.execute):
            # content_data.get will work, but let's use a type that explodes
            result = tool.execute(None)  # type: ignore
        # Should get error because NoneType has no .get
        assert result["success"] is False


# ====================================================================
# agents/daemon.py — PaperclipBridge lifecycle and worker tests
# ====================================================================


class TestBridgeInitializeMessengers:
    """Test _initialize_messengers with various env var combinations."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.config = MagicMock()
            b.mattermost_client = None
            b.slack_client = None
            b.mattermost_bot_username = None
            b.slack_bot_user_id = None
            return b

    def test_no_messengers_raises(self, bridge):
        with patch.dict(os.environ, {}, clear=True), \
             patch.dict(os.environ, {"VIBE_DISABLE_REMOTE_SKILLS": "1"}):
            # Ensure MATTERMOST_URL, MATTERMOST_BOT_TOKEN, SLACK_BOT_TOKEN are not set
            os.environ.pop("MATTERMOST_URL", None)
            os.environ.pop("MATTERMOST_BOT_TOKEN", None)
            os.environ.pop("SLACK_BOT_TOKEN", None)
            with pytest.raises(RuntimeError, match="No messenger configured"):
                bridge._initialize_messengers()

    def test_mattermost_init_success(self, bridge):
        mock_mm = MagicMock()
        mock_mm.get_bot_username.return_value = "vibe-bot"

        with patch.dict(os.environ, {
            "MATTERMOST_URL": "http://mm:8065",
            "MATTERMOST_BOT_TOKEN": "tok",
        }):
            # Remove SLACK_BOT_TOKEN if present
            os.environ.pop("SLACK_BOT_TOKEN", None)
            with patch("agents.daemon.MattermostClient", return_value=mock_mm):
                bridge._initialize_messengers()

        assert bridge.mattermost_client is mock_mm
        assert bridge.mattermost_bot_username == "vibe-bot"

    def test_mattermost_init_failure_falls_through(self, bridge):
        """If Mattermost init fails but Slack works, no error."""
        mock_slack = MagicMock()
        mock_slack.get_bot_user_id.return_value = "U123"

        with patch.dict(os.environ, {
            "MATTERMOST_URL": "http://mm:8065",
            "MATTERMOST_BOT_TOKEN": "tok",
            "SLACK_BOT_TOKEN": "slack-tok",
        }):
            with patch("agents.daemon.MattermostClient", side_effect=RuntimeError("fail")), \
                 patch("agents.daemon.SlackClient", return_value=mock_slack):
                bridge._initialize_messengers()

        assert bridge.mattermost_client is None
        assert bridge.slack_client is mock_slack

    def test_slack_init_success(self, bridge):
        mock_slack = MagicMock()
        mock_slack.get_bot_user_id.return_value = "U999"

        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "slack-tok"}):
            os.environ.pop("MATTERMOST_URL", None)
            os.environ.pop("MATTERMOST_BOT_TOKEN", None)
            with patch("agents.daemon.SlackClient", return_value=mock_slack):
                bridge._initialize_messengers()

        assert bridge.slack_client is mock_slack
        assert bridge.slack_bot_user_id == "U999"

    def test_slack_init_failure(self, bridge):
        """If Slack init fails and no Mattermost, should raise."""
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "slack-tok"}):
            os.environ.pop("MATTERMOST_URL", None)
            os.environ.pop("MATTERMOST_BOT_TOKEN", None)
            with patch("agents.daemon.SlackClient", side_effect=RuntimeError("fail")):
                with pytest.raises(RuntimeError, match="No messenger configured"):
                    bridge._initialize_messengers()


class TestBridgeInitializePaperclip:
    """Test _initialize_paperclip with various env var combinations."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.config = MagicMock()
            b.config.paperclip = MagicMock()
            b.config.paperclip.api_url = ""
            b.config.paperclip.api_key = ""
            b.paperclip_client = None
            return b

    def test_no_api_url_raises(self, bridge):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PAPERCLIP_API_URL", None)
            os.environ.pop("PAPERCLIP_API_KEY", None)
            os.environ.pop("PAPERCLIP_AGENT_ID", None)
            os.environ.pop("PAPERCLIP_COMPANY_ID", None)
            with pytest.raises(RuntimeError, match="Paperclip not configured"):
                bridge._initialize_paperclip()

    def test_success(self, bridge):
        mock_client = MagicMock()
        with patch.dict(os.environ, {
            "PAPERCLIP_API_URL": "http://paperclip:3000",
            "PAPERCLIP_API_KEY": "key",
            "PAPERCLIP_AGENT_ID": "agent-1",
            "PAPERCLIP_COMPANY_ID": "company-1",
        }):
            with patch("agents.daemon.PaperclipClient", return_value=mock_client):
                bridge._initialize_paperclip()
        assert bridge.paperclip_client is mock_client


class TestBridgeSignalHandlers:
    """Test signal handling setup and handler."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.running = True
            b.shutdown_event = threading.Event()
            return b

    def test_setup_signal_handlers(self, bridge):
        with patch("agents.daemon.signal.signal") as mock_sig:
            bridge._setup_signal_handlers()
        assert mock_sig.call_count == 2

    def test_signal_handler_stops(self, bridge):
        bridge.stop = MagicMock()
        bridge._signal_handler(signal.SIGTERM, None)
        bridge.stop.assert_called_once()


class TestBridgePollMattermostMentions:
    """Test _poll_mattermost_mentions."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.mattermost_client = None
            b.slack_client = None
            b.mattermost_bot_username = "vibe-bot"
            return b

    def test_no_client_returns_empty(self, bridge):
        result = bridge._poll_mattermost_mentions()
        assert result == []

    def test_with_mentions(self, bridge):
        mock_client = MagicMock()
        bridge.mattermost_client = mock_client

        now_ms = int(datetime.now().timestamp() * 1000)
        mock_client._get_bot_user_id.return_value = "bot-id"
        mock_client.search_posts.return_value = [
            {
                "id": "post-1",
                "create_at": now_ms,
                "user_id": "user-1",
                "channel_id": "ch-1",
                "message": "@vibe-bot do something",
            },
        ]
        mock_client.get_channels_for_user.return_value = []

        result = bridge._poll_mattermost_mentions()
        assert len(result) == 1
        assert result[0]["id"] == "post-1"
        assert result[0]["platform"] == "mattermost"

    def test_skips_old_posts(self, bridge):
        mock_client = MagicMock()
        bridge.mattermost_client = mock_client

        old_ms = int((datetime.now() - timedelta(hours=1)).timestamp() * 1000)
        mock_client._get_bot_user_id.return_value = "bot-id"
        mock_client.search_posts.return_value = [
            {
                "id": "old-post",
                "create_at": old_ms,
                "user_id": "user-1",
                "channel_id": "ch-1",
                "message": "@vibe-bot old message",
            },
        ]
        mock_client.get_channels_for_user.return_value = []

        result = bridge._poll_mattermost_mentions()
        assert len(result) == 0

    def test_skips_bot_posts(self, bridge):
        mock_client = MagicMock()
        bridge.mattermost_client = mock_client

        now_ms = int(datetime.now().timestamp() * 1000)
        mock_client._get_bot_user_id.return_value = "bot-id"
        mock_client.search_posts.return_value = [
            {
                "id": "bot-post",
                "create_at": now_ms,
                "user_id": "bot-id",
                "channel_id": "ch-1",
                "message": "@vibe-bot self-mention",
            },
        ]
        mock_client.get_channels_for_user.return_value = []

        result = bridge._poll_mattermost_mentions()
        assert len(result) == 0

    def test_channel_mentions(self, bridge):
        mock_client = MagicMock()
        bridge.mattermost_client = mock_client

        now_ms = int(datetime.now().timestamp() * 1000)
        mock_client._get_bot_user_id.return_value = "bot-id"
        mock_client.search_posts.return_value = []
        mock_client.get_channels_for_user.return_value = [{"id": "ch-1"}]
        mock_client.get_recent_messages.return_value = [
            {
                "id": "ch-msg-1",
                "create_at": now_ms,
                "user_id": "user-1",
                "message": "@vibe-bot from channel",
            },
        ]

        result = bridge._poll_mattermost_mentions()
        assert len(result) == 1
        assert result[0]["id"] == "ch-msg-1"

    def test_channel_at_all(self, bridge):
        mock_client = MagicMock()
        bridge.mattermost_client = mock_client

        now_ms = int(datetime.now().timestamp() * 1000)
        mock_client._get_bot_user_id.return_value = "bot-id"
        mock_client.search_posts.return_value = []
        mock_client.get_channels_for_user.return_value = [{"id": "ch-1"}]
        mock_client.get_recent_messages.return_value = [
            {
                "id": "at-all-msg",
                "create_at": now_ms,
                "user_id": "user-1",
                "message": "@all important announcement",
            },
        ]

        result = bridge._poll_mattermost_mentions()
        assert len(result) == 1

    def test_exception_returns_empty(self, bridge):
        mock_client = MagicMock()
        bridge.mattermost_client = mock_client
        mock_client._get_bot_user_id.side_effect = RuntimeError("oops")

        result = bridge._poll_mattermost_mentions()
        assert result == []


class TestBridgePollSlackMentions:
    """Test _poll_slack_mentions."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.slack_client = None
            b.slack_bot_user_id = "U123BOT"
            return b

    def test_no_client_returns_empty(self, bridge):
        result = bridge._poll_slack_mentions()
        assert result == []

    def test_with_search_results(self, bridge):
        mock_client = MagicMock()
        bridge.slack_client = mock_client

        now_ts = str(datetime.now().timestamp())
        mock_client.search_messages.return_value = [
            {
                "ts": now_ts,
                "user": "U999",
                "channel": {"id": "C123"},
                "text": "<@U123BOT> hello",
            },
        ]
        mock_client.get_conversations_list.return_value = []

        result = bridge._poll_slack_mentions()
        assert len(result) == 1
        assert result[0]["platform"] == "slack"

    def test_skips_old_messages(self, bridge):
        mock_client = MagicMock()
        bridge.slack_client = mock_client

        old_ts = str((datetime.now() - timedelta(hours=1)).timestamp())
        mock_client.search_messages.return_value = [
            {
                "ts": old_ts,
                "user": "U999",
                "channel": {"id": "C123"},
                "text": "<@U123BOT> old",
            },
        ]
        mock_client.get_conversations_list.return_value = []

        result = bridge._poll_slack_mentions()
        assert len(result) == 0

    def test_skips_bot_messages(self, bridge):
        mock_client = MagicMock()
        bridge.slack_client = mock_client

        now_ts = str(datetime.now().timestamp())
        mock_client.search_messages.return_value = [
            {
                "ts": now_ts,
                "bot_id": "B123",
                "channel": {"id": "C123"},
                "text": "<@U123BOT> bot message",
            },
        ]
        mock_client.get_conversations_list.return_value = []

        result = bridge._poll_slack_mentions()
        assert len(result) == 0

    def test_conversation_mentions(self, bridge):
        mock_client = MagicMock()
        bridge.slack_client = mock_client

        now_ts = str(datetime.now().timestamp())
        mock_client.search_messages.return_value = []
        mock_client.get_conversations_list.return_value = [{"id": "C456"}]
        mock_client.get_conversation_history.return_value = [
            {
                "ts": now_ts,
                "user": "U999",
                "text": "<@U123BOT> from conv",
            },
        ]

        result = bridge._poll_slack_mentions()
        assert len(result) == 1
        assert result[0]["channel_id"] == "C456"

    def test_conversation_at_channel(self, bridge):
        mock_client = MagicMock()
        bridge.slack_client = mock_client

        now_ts = str(datetime.now().timestamp())
        mock_client.search_messages.return_value = []
        mock_client.get_conversations_list.return_value = [{"id": "C456"}]
        mock_client.get_conversation_history.return_value = [
            {
                "ts": now_ts,
                "user": "U999",
                "text": "@channel everyone check this",
            },
        ]

        result = bridge._poll_slack_mentions()
        assert len(result) == 1

    def test_exception_returns_empty(self, bridge):
        mock_client = MagicMock()
        bridge.slack_client = mock_client
        mock_client.search_messages.side_effect = RuntimeError("api down")

        result = bridge._poll_slack_mentions()
        assert result == []


class TestBridgeSendResponse:
    """Test _send_response for both platforms."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.mattermost_client = MagicMock()
            b.slack_client = MagicMock()
            return b

    def test_send_mattermost(self, bridge):
        bridge.mattermost_client.send_channel_message.return_value = "post-id-1"

        result = bridge._send_response("mattermost", "ch-1", "hello")
        assert result == "post-id-1"
        bridge.mattermost_client.send_channel_message.assert_called_once()

    def test_send_slack(self, bridge):
        bridge.slack_client.send_channel_message.return_value = "ts-1"

        result = bridge._send_response("slack", "C123", "hello")
        assert result == "ts-1"
        bridge.slack_client.send_channel_message.assert_called_once()

    def test_send_with_thread(self, bridge):
        bridge.mattermost_client.send_channel_message.return_value = "post-2"

        result = bridge._send_response("mattermost", "ch-1", "reply", thread_id="parent-1")
        bridge.mattermost_client.send_channel_message.assert_called_once_with(
            "ch-1", "reply", root_id="parent-1",
        )

    def test_send_exception(self, bridge):
        bridge.mattermost_client.send_channel_message.side_effect = RuntimeError("fail")

        result = bridge._send_response("mattermost", "ch-1", "hello")
        assert result is None


class TestBridgeCreateIssueFromMention:
    """Test _create_issue_from_mention."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.paperclip_client = MagicMock()
            return b

    def test_success(self, bridge):
        mock_issue = MagicMock()
        mock_issue.id = "issue-42"
        bridge.paperclip_client.create_issue.return_value = mock_issue

        mention = {
            "platform": "mattermost",
            "user_id": "user-1",
            "channel_id": "ch-1",
        }
        result = bridge._create_issue_from_mention(mention, "Build a REST API")
        assert result == "issue-42"

    def test_no_client(self, bridge):
        bridge.paperclip_client = None
        result = bridge._create_issue_from_mention({}, "text")
        assert result is None

    def test_api_error(self, bridge):
        from agents.paperclip_client import PaperclipAPIError
        bridge.paperclip_client.create_issue.side_effect = PaperclipAPIError(500, "fail")

        result = bridge._create_issue_from_mention(
            {"platform": "slack", "user_id": "u1", "channel_id": "c1"}, "text",
        )
        assert result is None


class TestBridgePollIssueCompletion:
    """Test _poll_issue_completion."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.paperclip_client = MagicMock()
            b.shutdown_event = threading.Event()
            return b

    def test_no_client(self, bridge):
        bridge.paperclip_client = None
        result = bridge._poll_issue_completion("issue-1")
        assert result is None

    def test_issue_completes(self, bridge):
        mock_issue = MagicMock()
        mock_issue.status = "done"
        bridge.paperclip_client.get_issue.return_value = mock_issue

        mock_comment = MagicMock()
        mock_comment.body = "## Completed\nOutput here"
        bridge.paperclip_client.get_comments.return_value = [mock_comment]

        result = bridge._poll_issue_completion("issue-1")
        assert result is not None
        assert result["status"] == "done"
        assert "Completed" in result["output"]

    def test_issue_blocked(self, bridge):
        mock_issue = MagicMock()
        mock_issue.status = "blocked"
        bridge.paperclip_client.get_issue.return_value = mock_issue

        mock_comment = MagicMock()
        mock_comment.body = "## Blocked\nReason here"
        bridge.paperclip_client.get_comments.return_value = [mock_comment]

        result = bridge._poll_issue_completion("issue-1")
        assert result["status"] == "blocked"

    def test_issue_no_matching_comment(self, bridge):
        mock_issue = MagicMock()
        mock_issue.status = "done"
        bridge.paperclip_client.get_issue.return_value = mock_issue

        mock_comment = MagicMock()
        mock_comment.body = "Just a regular comment"
        bridge.paperclip_client.get_comments.return_value = [mock_comment]

        result = bridge._poll_issue_completion("issue-1")
        assert result["output"] == "Just a regular comment"

    def test_issue_no_comments(self, bridge):
        mock_issue = MagicMock()
        mock_issue.status = "done"
        bridge.paperclip_client.get_issue.return_value = mock_issue
        bridge.paperclip_client.get_comments.return_value = []

        result = bridge._poll_issue_completion("issue-1")
        assert result["output"] == ""

    def test_timeout(self, bridge):
        import agents.daemon as daemon_mod
        original_timeout = daemon_mod.COMPLETION_TIMEOUT
        original_poll = daemon_mod.COMPLETION_POLL_INTERVAL
        try:
            daemon_mod.COMPLETION_TIMEOUT = 0  # immediate timeout
            daemon_mod.COMPLETION_POLL_INTERVAL = 0

            mock_issue = MagicMock()
            mock_issue.status = "in_progress"
            bridge.paperclip_client.get_issue.return_value = mock_issue

            result = bridge._poll_issue_completion("issue-1")
            assert result["status"] == "timeout"
        finally:
            daemon_mod.COMPLETION_TIMEOUT = original_timeout
            daemon_mod.COMPLETION_POLL_INTERVAL = original_poll

    def test_shutdown_during_poll(self, bridge):
        bridge.shutdown_event.set()

        mock_issue = MagicMock()
        mock_issue.status = "in_progress"
        bridge.paperclip_client.get_issue.return_value = mock_issue

        result = bridge._poll_issue_completion("issue-1")
        assert result is None

    def test_api_error_during_poll(self, bridge):
        import agents.daemon as daemon_mod
        from agents.paperclip_client import PaperclipAPIError

        original_timeout = daemon_mod.COMPLETION_TIMEOUT
        original_poll = daemon_mod.COMPLETION_POLL_INTERVAL
        try:
            daemon_mod.COMPLETION_TIMEOUT = 0
            daemon_mod.COMPLETION_POLL_INTERVAL = 0

            bridge.paperclip_client.get_issue.side_effect = PaperclipAPIError(500, "fail")

            result = bridge._poll_issue_completion("issue-1")
            assert result["status"] == "timeout"
        finally:
            daemon_mod.COMPLETION_TIMEOUT = original_timeout
            daemon_mod.COMPLETION_POLL_INTERVAL = original_poll


class TestBridgeHandleMention:
    """Test _handle_mention end-to-end."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.config = MagicMock()
            b.paperclip_client = MagicMock()
            b.mattermost_client = MagicMock()
            b.slack_client = None
            b.shutdown_event = threading.Event()
            b.inflight = {}
            b.inflight_lock = threading.Lock()
            b.metrics_lock = threading.Lock()
            b.metrics = {
                "requests_created": 0,
                "requests_completed": 0,
                "requests_failed": 0,
                "start_time": None,
            }
            return b

    def test_empty_request_skipped(self, bridge):
        """When extract returns None, _handle_mention returns early."""
        mention = {
            "platform": "mattermost",
            "channel_id": "ch-1",
            "text": "",
        }
        # _extract_request_from_message returns None for empty text
        bridge._handle_mention(mention)
        # No issue should be created
        bridge.paperclip_client.create_issue.assert_not_called()

    def test_issue_creation_failure(self, bridge):
        mention = {
            "platform": "mattermost",
            "channel_id": "ch-1",
            "text": "Please build a REST API for users",
        }
        bridge.mattermost_client.send_channel_message.return_value = "thread-1"

        from agents.paperclip_client import PaperclipAPIError
        bridge.paperclip_client.create_issue.side_effect = PaperclipAPIError(500, "fail")

        bridge._handle_mention(mention)
        assert bridge.metrics["requests_failed"] == 1

    def test_successful_mention_handling(self, bridge):
        mention = {
            "platform": "mattermost",
            "channel_id": "ch-1",
            "text": "Please build a REST API for users",
        }
        bridge.mattermost_client.send_channel_message.return_value = "thread-1"

        mock_issue = MagicMock()
        mock_issue.id = "issue-99"
        bridge.paperclip_client.create_issue.return_value = mock_issue

        # Mock _poll_issue_completion to return a result
        mock_done_issue = MagicMock()
        mock_done_issue.status = "done"
        bridge.paperclip_client.get_issue.return_value = mock_done_issue

        mock_comment = MagicMock()
        mock_comment.body = "## Completed\nHere is your API"
        bridge.paperclip_client.get_comments.return_value = [mock_comment]

        bridge._handle_mention(mention)
        assert bridge.metrics["requests_created"] == 1
        assert bridge.metrics["requests_completed"] == 1
        assert "issue-99" not in bridge.inflight

    def test_poll_returns_none(self, bridge):
        """When _poll_issue_completion returns None (shutdown), send interrupted msg."""
        mention = {
            "platform": "mattermost",
            "channel_id": "ch-1",
            "text": "Please build a REST API for users",
        }
        bridge.mattermost_client.send_channel_message.return_value = "thread-1"

        mock_issue = MagicMock()
        mock_issue.id = "issue-100"
        bridge.paperclip_client.create_issue.return_value = mock_issue

        bridge.shutdown_event = threading.Event()
        bridge.shutdown_event.set()  # Force immediate None return

        bridge._handle_mention(mention)
        assert bridge.metrics["requests_created"] == 1
        # "interrupted" message should be sent
        calls = bridge.mattermost_client.send_channel_message.call_args_list
        assert any("interrupted" in str(c).lower() for c in calls)


class TestBridgeHandlerThread:
    """Test _handler_thread processing."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.running = True
            b.shutdown_event = threading.Event()
            b.request_queue = Queue(maxsize=10)
            return b

    def test_processes_items_from_queue(self, bridge):
        mention = {
            "platform": "mattermost",
            "channel_id": "ch-1",
            "text": "Hello world test request",
        }
        bridge.request_queue.put(mention)

        bridge._handle_mention = MagicMock()

        # Stop after processing one item
        def stop_after_handle(*args, **kwargs):
            bridge.running = False
            bridge.shutdown_event.set()

        bridge._handle_mention.side_effect = stop_after_handle

        bridge._handler_thread()
        bridge._handle_mention.assert_called_once_with(mention)

    def test_handles_exception(self, bridge):
        mention = {"platform": "test", "channel_id": "ch"}
        bridge.request_queue.put(mention)

        def raise_and_stop(*args, **kwargs):
            bridge.running = False
            bridge.shutdown_event.set()
            raise RuntimeError("handler error")

        bridge._handle_mention = MagicMock(side_effect=raise_and_stop)

        # Should not raise
        bridge._handler_thread()

    def test_empty_queue_timeout(self, bridge):
        """Handler should loop on Empty queue until shutdown."""
        # Immediately signal shutdown
        bridge.shutdown_event.set()
        bridge.running = False

        bridge._handler_thread()  # Should return without error


class TestBridgePollingLoop:
    """Test _polling_loop."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.running = True
            b.shutdown_event = threading.Event()
            b.request_queue = Queue(maxsize=10)
            b.mattermost_client = MagicMock()
            b.slack_client = MagicMock()
            b.processed_messages = {}
            b.message_lock = threading.Lock()
            return b

    def test_polling_queues_mentions(self, bridge):
        import agents.daemon as daemon_mod
        original_poll = daemon_mod.POLL_INTERVAL
        try:
            daemon_mod.POLL_INTERVAL = 0

            call_count = [0]

            def stop_on_second(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] >= 2:
                    bridge.running = False
                    bridge.shutdown_event.set()
                return []

            bridge._poll_mattermost_mentions = MagicMock(side_effect=stop_on_second)
            bridge._poll_slack_mentions = MagicMock(return_value=[])

            bridge._polling_loop()
        finally:
            daemon_mod.POLL_INTERVAL = original_poll

    def test_polling_with_real_mention(self, bridge):
        import agents.daemon as daemon_mod
        original_poll = daemon_mod.POLL_INTERVAL
        try:
            daemon_mod.POLL_INTERVAL = 0

            call_count = [0]

            def provide_mention(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return [{
                        "id": "msg-1",
                        "platform": "mattermost",
                        "channel_id": "ch-1",
                        "text": "Please build something useful for me",
                    }]
                bridge.running = False
                bridge.shutdown_event.set()
                return []

            bridge._poll_mattermost_mentions = MagicMock(side_effect=provide_mention)
            bridge._poll_slack_mentions = MagicMock(return_value=[])

            bridge._polling_loop()

            assert bridge.request_queue.qsize() == 1
        finally:
            daemon_mod.POLL_INTERVAL = original_poll

    def test_polling_dedup_skips(self, bridge):
        import agents.daemon as daemon_mod
        original_poll = daemon_mod.POLL_INTERVAL
        try:
            daemon_mod.POLL_INTERVAL = 0

            # Pre-mark message as processed
            bridge._mark_message_processed("msg-dup")

            call_count = [0]

            def provide_dup(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return [{
                        "id": "msg-dup",
                        "platform": "mattermost",
                        "channel_id": "ch-1",
                        "text": "Hello world duplicate message",
                    }]
                bridge.running = False
                bridge.shutdown_event.set()
                return []

            bridge._poll_mattermost_mentions = MagicMock(side_effect=provide_dup)
            bridge._poll_slack_mentions = MagicMock(return_value=[])

            bridge._polling_loop()

            # Should not be queued (deduped)
            assert bridge.request_queue.qsize() == 0
        finally:
            daemon_mod.POLL_INTERVAL = original_poll

    def test_polling_queue_full(self, bridge):
        import agents.daemon as daemon_mod
        original_poll = daemon_mod.POLL_INTERVAL
        try:
            daemon_mod.POLL_INTERVAL = 0
            bridge.request_queue = Queue(maxsize=1)
            bridge.request_queue.put({"id": "blocker"})  # Fill the queue

            call_count = [0]

            def provide_mention(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return [{
                        "id": "msg-full",
                        "platform": "slack",
                        "channel_id": "C1",
                        "text": "Please build something that overflows the queue",
                    }]
                bridge.running = False
                bridge.shutdown_event.set()
                return []

            bridge._poll_mattermost_mentions = MagicMock(side_effect=provide_mention)
            bridge._poll_slack_mentions = MagicMock(return_value=[])
            bridge._send_response = MagicMock()

            with patch("agents.daemon.metrics") as mock_metrics:
                bridge._polling_loop()

            # Should have sent "at capacity" response
            bridge._send_response.assert_called()
        finally:
            daemon_mod.POLL_INTERVAL = original_poll

    def test_polling_exception_handling(self, bridge):
        import agents.daemon as daemon_mod
        original_poll = daemon_mod.POLL_INTERVAL
        try:
            daemon_mod.POLL_INTERVAL = 0

            call_count = [0]

            def raise_then_stop(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("poll error")
                bridge.running = False
                bridge.shutdown_event.set()
                return []

            bridge._poll_mattermost_mentions = MagicMock(side_effect=raise_then_stop)
            bridge._poll_slack_mentions = MagicMock(return_value=[])

            # Should handle exception and continue
            bridge._polling_loop()
        finally:
            daemon_mod.POLL_INTERVAL = original_poll


class TestBridgeStartStop:
    """Test start() and stop() lifecycle."""

    @pytest.fixture
    def bridge(self):
        with patch("agents.daemon.get_production_config"):
            from agents.daemon import PaperclipBridge
            b = PaperclipBridge.__new__(PaperclipBridge)
            b.config = MagicMock()
            b.running = False
            b.shutdown_event = threading.Event()
            b.mattermost_client = None
            b.slack_client = None
            b.mattermost_bot_username = None
            b.slack_bot_user_id = None
            b.paperclip_client = None
            b.request_queue = Queue(maxsize=100)
            b.processed_messages = {}
            b.message_lock = threading.Lock()
            b.inflight = {}
            b.inflight_lock = threading.Lock()
            b.metrics_lock = threading.Lock()
            b.metrics = {
                "requests_created": 0,
                "requests_completed": 0,
                "requests_failed": 0,
                "start_time": None,
            }
            return b

    def test_start_already_running(self, bridge):
        bridge.running = True
        # Should just log and return
        bridge.start()

    def test_stop_not_running(self, bridge):
        bridge.running = False
        # Should just return
        bridge.stop()

    def test_stop_with_metrics(self, bridge):
        bridge.running = True
        bridge.metrics["start_time"] = datetime.now() - timedelta(minutes=5)
        bridge.metrics["requests_created"] = 3
        bridge.metrics["requests_completed"] = 2
        bridge.metrics["requests_failed"] = 1

        mock_handler = MagicMock()
        mock_handler.join = MagicMock()
        bridge._handler = mock_handler

        mock_health = MagicMock()
        bridge._health_server = mock_health

        bridge.stop()
        assert bridge.running is False
        assert bridge.shutdown_event.is_set()
        mock_handler.join.assert_called_once_with(timeout=10.0)
        mock_health.shutdown.assert_called_once()

    def test_stop_without_handler(self, bridge):
        bridge.running = True
        bridge.metrics["start_time"] = datetime.now()
        # No _handler or _health_server attributes
        bridge.stop()
        assert bridge.running is False

    def test_start_initializes_and_runs(self, bridge):
        """Test that start() calls init methods and enters polling loop."""
        bridge._setup_signal_handlers = MagicMock()
        bridge._initialize_messengers = MagicMock()
        bridge._initialize_paperclip = MagicMock()

        # Make _polling_loop return immediately
        def mock_polling():
            bridge.running = False

        bridge._polling_loop = MagicMock(side_effect=mock_polling)

        with patch("agents.daemon.start_health_server", return_value=MagicMock()):
            bridge.start()

        bridge._setup_signal_handlers.assert_called_once()
        bridge._initialize_messengers.assert_called_once()
        bridge._initialize_paperclip.assert_called_once()
        bridge._polling_loop.assert_called_once()


class TestRunDaemon:
    """Test the run_daemon() module-level function."""

    def test_run_daemon_normal(self):
        from agents.daemon import run_daemon

        with patch("agents.daemon.PaperclipBridge") as MockBridge:
            mock_bridge = MagicMock()
            MockBridge.return_value = mock_bridge

            run_daemon(config=MagicMock())
            mock_bridge.start.assert_called_once()
            mock_bridge.stop.assert_called_once()

    def test_run_daemon_keyboard_interrupt(self):
        from agents.daemon import run_daemon

        with patch("agents.daemon.PaperclipBridge") as MockBridge:
            mock_bridge = MagicMock()
            mock_bridge.start.side_effect = KeyboardInterrupt()
            MockBridge.return_value = mock_bridge

            run_daemon(config=MagicMock())
            mock_bridge.stop.assert_called_once()

    def test_run_daemon_no_config(self):
        from agents.daemon import run_daemon

        with patch("agents.daemon.PaperclipBridge") as MockBridge:
            mock_bridge = MagicMock()
            MockBridge.return_value = mock_bridge

            run_daemon()
            MockBridge.assert_called_once_with(None)
            mock_bridge.start.assert_called_once()


# ====================================================================
# agents/tools/seo_tools.py — SEOIssue dataclass
# ====================================================================


class TestSEOIssue:
    """Test the SEOIssue dataclass."""

    def test_creation(self):
        from agents.tools.seo_tools import SEOIssue

        issue = SEOIssue(
            severity="critical",
            category="meta",
            issue="Missing title",
            recommendation="Add a title",
        )
        assert issue.severity == "critical"
        assert issue.current_value is None
        assert issue.target_value is None

    def test_creation_with_optional_fields(self):
        from agents.tools.seo_tools import SEOIssue

        issue = SEOIssue(
            severity="minor",
            category="content",
            issue="Short content",
            recommendation="Write more",
            current_value="100 words",
            target_value="1000+ words",
        )
        assert issue.current_value == "100 words"
        assert issue.target_value == "1000+ words"
