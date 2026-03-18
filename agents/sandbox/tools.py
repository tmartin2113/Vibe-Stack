"""
Sandboxed tool implementations for Vibe.

Drop-in replacements for PythonExecutor, PytestRunner, BanditScanner, and
ShellExecutor that execute inside OpenSandbox containers instead of local
subprocesses.

These classes have identical names, schemas, and ToolResult shapes to the
originals so the tool calling loop in specialist_nodes.py requires zero changes.
"""

import json
import logging
from typing import Any, Dict, Optional

from ..tools import ToolResult
from .client import SandboxPoolManager

logger = logging.getLogger(__name__)


class _SandboxedToolMixin:
    """Provide the enabled/validate_params interface expected by ToolRegistry."""

    enabled = True

    def validate_params(self, **kwargs) -> bool:
        schema = self.get_schema().get("parameters", {})
        for param, spec in schema.items():
            if spec.get("required") and param not in kwargs:
                raise ValueError(f"Missing required parameter: {param}")
        return True


class SandboxedPythonExecutor(_SandboxedToolMixin):
    """Execute Python code inside an OpenSandbox container.

    Replaces agents.tools.registry.PythonExecutor.
    Same name, schema, and return type.
    """

    name = "python_executor"
    description = "Execute Python code in an isolated sandbox container"

    def __init__(self, pool: SandboxPoolManager):
        self._pool = pool

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                    "required": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default: 30)",
                    "required": False,
                },
            },
        }

    def execute(
        self,
        code: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute Python code inside a sandbox."""
        if not code or not code.strip():
            return ToolResult(
                success=False,
                output="",
                error="No code provided",
            )

        logger.info(f"Executing Python code in sandbox (timeout={timeout}s)")
        return self._pool.execute_in_sandbox(code, timeout=timeout)


class SandboxedPytestRunner(_SandboxedToolMixin):
    """Run pytest inside an OpenSandbox container.

    Replaces agents.tools.registry.PytestRunner.
    Same name, schema, and return type.
    """

    name = "pytest_runner"
    description = "Run pytest test suite in an isolated sandbox container"

    def __init__(self, pool: SandboxPoolManager):
        self._pool = pool

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "test_file": {
                    "type": "string",
                    "description": "Path to test file or directory",
                    "required": True,
                },
                "coverage": {
                    "type": "boolean",
                    "description": "Enable coverage reporting (default: true)",
                    "required": False,
                },
                "verbose": {
                    "type": "boolean",
                    "description": "Enable verbose output (default: true)",
                    "required": False,
                },
            },
        }

    def execute(
        self,
        test_file: str,
        coverage: bool = True,
        verbose: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        """Run pytest inside a sandbox."""
        if not test_file or not test_file.strip():
            return ToolResult(
                success=False,
                output="",
                error="No test file specified",
            )

        # Build pytest command
        cmd_parts = ["python3", "-m", "pytest"]
        if verbose:
            cmd_parts.append("-v")
        if coverage:
            cmd_parts.extend(["--cov", "--cov-report=term-missing"])
        cmd_parts.append(test_file)

        command = " ".join(cmd_parts)
        logger.info(f"Running pytest in sandbox: {command}")

        return self._pool.run_command(command, timeout=60)


class SandboxedBanditScanner(_SandboxedToolMixin):
    """Run bandit security scanner inside an OpenSandbox container.

    Replaces agents.tools.registry.BanditScanner.
    Same name, schema, and return type.
    """

    name = "bandit"
    description = "Run bandit security scanner in an isolated sandbox container"

    def __init__(self, pool: SandboxPoolManager):
        self._pool = pool

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "target": {
                    "type": "string",
                    "description": "File or directory to scan",
                    "required": True,
                },
                "severity_level": {
                    "type": "string",
                    "description": "Minimum severity: low, medium, high (default: medium)",
                    "required": False,
                },
            },
        }

    def execute(
        self,
        target: str,
        severity_level: str = "medium",
        **kwargs: Any,
    ) -> ToolResult:
        """Run bandit inside a sandbox."""
        if not target or not target.strip():
            return ToolResult(
                success=False,
                output="",
                error="No target specified",
            )

        # Map severity levels
        severity_map = {"low": "l", "medium": "m", "high": "h"}
        sev_flag = severity_map.get(severity_level.lower(), "m")

        command = f"bandit -r -f json -ll -{sev_flag} {target}"
        logger.info(f"Running bandit in sandbox: {command}")

        result = self._pool.run_command(command, timeout=30)

        # Parse JSON output if bandit succeeded (exit code 0 or 1)
        # bandit returns 1 when issues are found (not an error)
        if result.output:
            try:
                findings = json.loads(result.output)
                issue_count = len(findings.get("results", []))
                severity_counts: Dict[str, int] = {}
                for issue in findings.get("results", []):
                    sev = issue.get("issue_severity", "UNKNOWN")
                    severity_counts[sev] = severity_counts.get(sev, 0) + 1

                summary = (
                    f"Found {issue_count} issue(s): "
                    + ", ".join(
                        f"{count} {sev}"
                        for sev, count in sorted(severity_counts.items())
                    )
                    if issue_count
                    else "No issues found"
                )

                return ToolResult(
                    success=True,
                    output=f"{summary}\n\n{result.output}",
                    error=None,
                    metadata={
                        "issue_count": issue_count,
                        "severity_counts": severity_counts,
                        "sandboxed": True,
                    },
                )
            except json.JSONDecodeError:
                pass  # Fall through to return raw result

        return result


class SandboxedShellExecutor(_SandboxedToolMixin):
    """Execute shell commands inside an OpenSandbox container.

    Replaces agents.tools.registry.ShellExecutor.
    Same name, schema, and return type.
    """

    name = "shell_executor"
    description = "Execute a shell command in an isolated sandbox container"

    def __init__(self, pool: SandboxPoolManager):
        self._pool = pool

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                    "required": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30)",
                    "required": False,
                },
            },
        }

    def execute(
        self,
        command: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> ToolResult:
        """Run a shell command inside a sandbox."""
        if not command or not command.strip():
            return ToolResult(
                success=False,
                output="",
                error="No command provided",
            )

        logger.info(f"Running shell command in sandbox: {command}")
        return self._pool.run_command(command, timeout=timeout)


class SandboxedWebFetchTool(_SandboxedToolMixin):
    """Fetch a URL inside an OpenSandbox container.

    Replaces agents.tools.registry.WebFetchTool.
    Same name, schema, and return type.
    """

    name = "web_fetch"
    description = "Fetch a URL in an isolated sandbox container"

    def __init__(self, pool: SandboxPoolManager):
        self._pool = pool

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (http or https)",
                    "required": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds (default: 15)",
                    "required": False,
                },
            },
        }

    def execute(
        self,
        url: str,
        timeout: int = 15,
        **kwargs: Any,
    ) -> ToolResult:
        """Fetch a URL inside a sandbox."""
        if not url or not url.strip():
            return ToolResult(
                success=False,
                output="",
                error="No URL provided",
            )
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                success=False,
                output="",
                error="URL must start with http:// or https://",
            )

        # Use Python urllib inside the sandbox (always available in stdlib)
        script = (
            f"python3 -c \""
            f"import urllib.request, sys; "
            f"req = urllib.request.Request('{url}', headers={{'User-Agent': 'Vibe/1.0'}}); "
            f"r = urllib.request.urlopen(req, timeout={timeout}); "
            f"sys.stdout.buffer.write(r.read())\""
        )

        logger.info(f"Fetching URL in sandbox: {url}")
        return self._pool.run_command(script, timeout=timeout + 5)
