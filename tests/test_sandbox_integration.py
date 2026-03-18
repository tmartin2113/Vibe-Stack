"""Tests for agents.sandbox — OpenSandbox integration."""

import os
import queue
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from agents.sandbox.config import SandboxConfig, _parse_bool
from agents.sandbox.tools import (
    SandboxedPythonExecutor,
    SandboxedPytestRunner,
    SandboxedBanditScanner,
    SandboxedShellExecutor,
)
from agents.tools import ToolResult


# ── SandboxConfig tests ────────────────────────────────────────────

class TestSandboxConfig:
    def test_defaults(self):
        c = SandboxConfig()
        assert c.backend == "opensandbox"
        assert c.server_url == "http://opensandbox:8080"
        assert c.api_key == ""
        assert c.pool_size == 2
        assert c.gpu_enabled is False
        assert c.network_egress is False

    def test_from_resource_plan(self):
        from agents.resource_allocator import SandboxPoolPlan, ResourcePlan, ServiceBudget
        from agents.resource_discovery import SystemProfile

        pool_plan = SandboxPoolPlan(
            pool_size=3,
            per_sandbox_cpu="1000m",
            per_sandbox_memory="1Gi",
            gpu_enabled=True,
            gpu_device_ids=[1, 2],
            sandbox_image="vibe/sandbox-gpu:latest",
        )
        profile = SystemProfile(
            cpu_count=8, cpu_threads=16, cpu_model="test",
            total_ram_mb=65536, available_ram_mb=40000,
        )
        plan = ResourcePlan(
            vllm=ServiceBudget(cpu_cores=8.0, memory_mb=20000),
            vibe=ServiceBudget(cpu_cores=2.0, memory_mb=1024),
            opensandbox_server=ServiceBudget(cpu_cores=1.0, memory_mb=512),
            sandbox_pool=pool_plan,
            profile=profile,
            strategy="multi_gpu",
        )

        config = SandboxConfig.from_resource_plan(plan)
        assert config.pool_size == 3
        assert config.cpu_limit == "1000m"
        assert config.memory_limit == "1Gi"
        assert config.gpu_enabled is True
        assert config.gpu_device_ids == "1,2"
        assert config.sandbox_image == "vibe/sandbox-gpu:latest"

    def test_env_overrides(self):
        c = SandboxConfig()
        env = {
            "VIBE_SANDBOX_POOL_SIZE": "5",
            "VIBE_SANDBOX_GPU": "true",
            "VIBE_SANDBOX_GPU_IDS": "2,3",
        }
        with patch.dict(os.environ, env, clear=False):
            c.apply_env_overrides()

        assert c.backend == "opensandbox"
        assert c.pool_size == 5
        assert c.gpu_enabled is True
        assert c.gpu_device_ids == "2,3"

    def test_env_overrides_partial(self):
        """Only set env vars override; others keep defaults."""
        c = SandboxConfig()
        with patch.dict(os.environ, {"VIBE_SANDBOX_POOL_SIZE": "8"}, clear=False):
            c.apply_env_overrides()
        assert c.pool_size == 8
        assert c.backend == "opensandbox"  # Always opensandbox

    def test_gpu_device_id_list(self):
        c = SandboxConfig(gpu_device_ids="0,1,2")
        assert c.gpu_device_id_list == [0, 1, 2]

    def test_gpu_device_id_list_empty(self):
        c = SandboxConfig(gpu_device_ids="")
        assert c.gpu_device_id_list == []


class TestParseBool:
    @pytest.mark.parametrize("val,expected", [
        ("true", True), ("True", True), ("TRUE", True),
        ("1", True), ("yes", True), ("Yes", True),
        ("false", False), ("0", False), ("no", False),
        ("anything", False),
    ])
    def test_values(self, val, expected):
        assert _parse_bool(val) is expected


# ── SandboxPoolManager tests (mocked SDK) ─────────────────────────

class TestSandboxPoolManager:
    """Test pool manager lifecycle with mocked opensandbox SDK."""

    @patch("agents.sandbox.client._check_sdk", return_value=True)
    def test_init(self, mock_sdk):
        from agents.sandbox.client import SandboxPoolManager
        config = SandboxConfig(pool_size=2)
        pool = SandboxPoolManager(config)
        assert pool._started is False
        assert pool._pool.qsize() == 0

    @patch("agents.sandbox.client._check_sdk", return_value=False)
    def test_start_without_sdk_raises(self, mock_sdk):
        from agents.sandbox.client import SandboxPoolManager
        pool = SandboxPoolManager(SandboxConfig())
        with pytest.raises(RuntimeError, match="opensandbox SDK"):
            pool.start()

    @patch("agents.sandbox.client._check_sdk", return_value=True)
    def test_stop_when_not_started(self, mock_sdk):
        from agents.sandbox.client import SandboxPoolManager
        pool = SandboxPoolManager(SandboxConfig())
        pool.stop()  # Should not raise

    @patch("agents.sandbox.client._check_sdk", return_value=True)
    def test_double_start(self, mock_sdk):
        from agents.sandbox.client import SandboxPoolManager
        pool = SandboxPoolManager(SandboxConfig(pool_size=0))
        pool._started = True
        pool.start()  # Should return early


# ── SandboxHandle tests ───────────────────────────────────────────

class TestSandboxHandle:
    def test_age(self):
        from agents.sandbox.client import SandboxHandle
        handle = SandboxHandle(sandbox_id="test", sandbox=MagicMock())
        time.sleep(0.05)
        assert handle.age_seconds >= 0.04

    def test_touch(self):
        from agents.sandbox.client import SandboxHandle
        handle = SandboxHandle(sandbox_id="test", sandbox=MagicMock())
        old_used = handle.last_used
        time.sleep(0.05)
        handle.touch()
        assert handle.last_used > old_used


# ── Sandboxed tool tests (mocked pool) ────────────────────────────

class TestSandboxedPythonExecutor:
    def _make_pool(self, result=None):
        pool = MagicMock()
        pool.execute_in_sandbox.return_value = result or ToolResult(
            success=True, output="42\n", error=None
        )
        return pool

    def test_execute_success(self):
        pool = self._make_pool()
        executor = SandboxedPythonExecutor(pool)
        result = executor.execute("print(42)")
        assert result.success is True
        assert result.output == "42\n"
        pool.execute_in_sandbox.assert_called_once_with("print(42)", timeout=30)

    def test_execute_with_timeout(self):
        pool = self._make_pool()
        executor = SandboxedPythonExecutor(pool)
        executor.execute("print(42)", timeout=60)
        pool.execute_in_sandbox.assert_called_once_with("print(42)", timeout=60)

    def test_execute_empty_code(self):
        pool = self._make_pool()
        executor = SandboxedPythonExecutor(pool)
        result = executor.execute("")
        assert result.success is False
        assert "No code provided" in result.error
        pool.execute_in_sandbox.assert_not_called()

    def test_execute_failure(self):
        pool = MagicMock()
        pool.execute_in_sandbox.return_value = ToolResult(
            success=False, output="", error="SyntaxError"
        )
        executor = SandboxedPythonExecutor(pool)
        result = executor.execute("invalid python {{{")
        assert result.success is False

    def test_schema(self):
        executor = SandboxedPythonExecutor(MagicMock())
        schema = executor.get_schema()
        assert schema["name"] == "python_executor"
        assert "code" in schema["parameters"]

    def test_name_matches_original(self):
        assert SandboxedPythonExecutor.name == "python_executor"


class TestSandboxedPytestRunner:
    def _make_pool(self, result=None):
        pool = MagicMock()
        pool.run_command.return_value = result or ToolResult(
            success=True, output="1 passed", error=None
        )
        return pool

    def test_execute_success(self):
        pool = self._make_pool()
        runner = SandboxedPytestRunner(pool)
        result = runner.execute("tests/test_example.py")
        assert result.success is True
        cmd = pool.run_command.call_args[0][0]
        assert "pytest" in cmd
        assert "tests/test_example.py" in cmd

    def test_execute_with_coverage(self):
        pool = self._make_pool()
        runner = SandboxedPytestRunner(pool)
        runner.execute("test.py", coverage=True)
        cmd = pool.run_command.call_args[0][0]
        assert "--cov" in cmd

    def test_execute_without_coverage(self):
        pool = self._make_pool()
        runner = SandboxedPytestRunner(pool)
        runner.execute("test.py", coverage=False)
        cmd = pool.run_command.call_args[0][0]
        assert "--cov" not in cmd

    def test_execute_verbose(self):
        pool = self._make_pool()
        runner = SandboxedPytestRunner(pool)
        runner.execute("test.py", verbose=True)
        cmd = pool.run_command.call_args[0][0]
        assert "-v" in cmd

    def test_execute_empty_file(self):
        pool = self._make_pool()
        runner = SandboxedPytestRunner(pool)
        result = runner.execute("")
        assert result.success is False
        pool.run_command.assert_not_called()

    def test_schema(self):
        runner = SandboxedPytestRunner(MagicMock())
        schema = runner.get_schema()
        assert schema["name"] == "pytest_runner"

    def test_name_matches_original(self):
        assert SandboxedPytestRunner.name == "pytest_runner"


class TestSandboxedBanditScanner:
    def _make_pool(self, output="", success=True):
        pool = MagicMock()
        pool.run_command.return_value = ToolResult(
            success=success, output=output, error=None if success else "error"
        )
        return pool

    def test_execute_no_issues(self):
        output = '{"results": [], "metrics": {}}'
        pool = self._make_pool(output=output)
        scanner = SandboxedBanditScanner(pool)
        result = scanner.execute("src/")
        assert result.success is True
        assert "No issues found" in result.output

    def test_execute_with_issues(self):
        output = '{"results": [{"issue_severity": "HIGH"}, {"issue_severity": "MEDIUM"}]}'
        pool = self._make_pool(output=output)
        scanner = SandboxedBanditScanner(pool)
        result = scanner.execute("src/")
        assert result.success is True
        assert result.metadata["issue_count"] == 2

    def test_execute_severity_flag(self):
        pool = self._make_pool(output='{"results": []}')
        scanner = SandboxedBanditScanner(pool)
        scanner.execute("src/", severity_level="high")
        cmd = pool.run_command.call_args[0][0]
        assert "-h" in cmd

    def test_execute_empty_target(self):
        pool = self._make_pool()
        scanner = SandboxedBanditScanner(pool)
        result = scanner.execute("")
        assert result.success is False
        pool.run_command.assert_not_called()

    def test_execute_malformed_json(self):
        pool = self._make_pool(output="not json")
        scanner = SandboxedBanditScanner(pool)
        result = scanner.execute("src/")
        # Should return raw result when JSON parsing fails
        assert result.output == "not json"

    def test_schema(self):
        scanner = SandboxedBanditScanner(MagicMock())
        schema = scanner.get_schema()
        assert schema["name"] == "bandit"

    def test_name_matches_original(self):
        assert SandboxedBanditScanner.name == "bandit"


# ── Registry toggle tests ─────────────────────────────────────────

class TestRegistryToggle:
    def test_sandbox_mode(self):
        """Registry uses sandboxed tools via sandbox_pool."""
        from agents.tools.registry import create_default_tool_registry
        pool = MagicMock()
        registry = create_default_tool_registry(sandbox_pool=pool)
        tools = registry.list_tools()
        assert "python_executor" in tools
        assert "pytest_runner" in tools
        assert "bandit" in tools

    def test_file_ops_always_local(self):
        """FileReader/FileWriter are always registered alongside sandboxed tools."""
        from agents.tools.registry import create_default_tool_registry
        registry = create_default_tool_registry(sandbox_pool=MagicMock())
        tools = registry.list_tools()
        assert "file_reader" in tools
        assert "file_writer" in tools


# ── Doctor integration ────────────────────────────────────────────

class TestDoctorChecks:
    def test_check_hardware(self):
        from agents.doctor import check_hardware
        result = check_hardware()
        # Should succeed even in environments without GPU
        assert result.status in ("ok", "warn")
        assert result.name == "Hardware"

    def test_check_sandbox_missing_config(self):
        """Missing sandbox config should be a failure."""
        from agents.doctor import check_sandbox
        from unittest.mock import MagicMock
        config = MagicMock(spec=[])  # No sandbox attribute
        result = check_sandbox(config)
        assert result.status == "fail"

    def test_check_docker_gpu(self):
        from agents.doctor import check_docker_gpu
        result = check_docker_gpu()
        # Should not raise, returns ok or warn
        assert result.status in ("ok", "warn")
