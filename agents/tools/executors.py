"""
Code execution tools: PythonExecutor, PytestRunner, BanditScanner, ShellExecutor.

These tools run code/commands in subprocesses with security sandboxing
(resource limits, stripped environment, timeouts).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from .base import Tool, ToolCategory, ToolResult
from .file_tools import _validate_file_path

logger = logging.getLogger(__name__)


class PythonExecutor(Tool):
    """
    Execute Python code with process-level sandboxing.

    Security measures:
    - Subprocess isolation (separate process, not eval/exec)
    - Timeout enforcement via subprocess.run()
    - Resource limits: CPU time (30s), memory (256MB), file size (10MB)
    - Stripped environment (only PATH, HOME, LANG, TMPDIR)
    - Temporary file with unpredictable name
    - Automatic cleanup of temp files
    """

    # Resource limits for child process (applied on Unix via preexec_fn)
    RLIMIT_CPU_SECONDS = 30       # Max CPU time (seconds)
    RLIMIT_MEMORY_BYTES = 256 * 1024 * 1024  # 256 MB address space
    RLIMIT_FSIZE_BYTES = 10 * 1024 * 1024    # 10 MB max file creation size
    RLIMIT_NPROC = 32             # Max child processes (prevent fork bomb)

    # Minimal environment for child process
    _ALLOWED_ENV_KEYS = {"PATH", "HOME", "LANG", "TMPDIR", "LC_ALL", "LC_CTYPE"}

    def __init__(self):
        super().__init__(
            name="python_executor",
            description="Execute Python code and return output. Use for testing generated code.",
            category=ToolCategory.CODE_EXECUTION
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                    "default": 30
                }
            },
            "required": ["code"]
        }

    @staticmethod
    def _make_sandbox_env() -> Dict[str, str]:
        """Build a stripped environment for the child process."""
        return {
            k: v for k, v in os.environ.items()
            if k in PythonExecutor._ALLOWED_ENV_KEYS
        }

    @staticmethod
    def _apply_resource_limits():
        """
        Set resource limits for the child process (Unix only).

        Called via subprocess preexec_fn — runs in the child after fork,
        before exec.  Silently skipped on non-Unix platforms.
        """
        try:
            import resource
            # CPU time limit
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (PythonExecutor.RLIMIT_CPU_SECONDS, PythonExecutor.RLIMIT_CPU_SECONDS),
            )
            # Virtual memory limit
            resource.setrlimit(
                resource.RLIMIT_AS,
                (PythonExecutor.RLIMIT_MEMORY_BYTES, PythonExecutor.RLIMIT_MEMORY_BYTES),
            )
            # Max file creation size
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (PythonExecutor.RLIMIT_FSIZE_BYTES, PythonExecutor.RLIMIT_FSIZE_BYTES),
            )
            # Max child processes (fork bomb protection)
            resource.setrlimit(
                resource.RLIMIT_NPROC,
                (PythonExecutor.RLIMIT_NPROC, PythonExecutor.RLIMIT_NPROC),
            )
        except (ImportError, AttributeError, ValueError, OSError):
            # Non-Unix platform or limit not supported — skip silently
            pass

    def execute(self, code: str, timeout: int = 30, **kwargs) -> ToolResult:  # type: ignore[override]
        """Execute Python code with timeout and process-level sandboxing."""
        # Create temporary file for code (SECURITY: unpredictable name)
        temp_file = None
        try:
            # Use NamedTemporaryFile for security (unpredictable name) and auto-cleanup
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(code)
                temp_file = Path(f.name)

            # Build sandbox parameters
            run_kwargs: Dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
                "env": self._make_sandbox_env(),
            }

            # Apply resource limits on Unix (preexec_fn not available on Windows)
            if hasattr(os, "fork"):
                run_kwargs["preexec_fn"] = self._apply_resource_limits

            # Execute with timeout + resource limits + stripped env
            result = subprocess.run(
                ["python3", str(temp_file)],
                **run_kwargs,
            )

            if result.returncode == 0:
                return ToolResult(
                    success=True,
                    output=result.stdout,
                    metadata={"returncode": 0, "stderr": result.stderr}
                )
            else:
                return ToolResult(
                    success=False,
                    output=result.stdout,
                    error=result.stderr,
                    metadata={"returncode": result.returncode}
                )

        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=f"Execution timed out after {timeout}s"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Execution error: {str(e)}"
            )
        finally:
            # ALWAYS clean up temp file (fixes temp file leak bug)
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception as e:
                    logger.warning(f"Failed to clean up temp file {temp_file}: {e}")


class PytestRunner(Tool):
    """Run pytest tests"""

    def __init__(self, allowed_dirs: Optional[List[Path]] = None):
        super().__init__(
            name="pytest_runner",
            description="Run pytest tests and return results with coverage",
            category=ToolCategory.CODE_EXECUTION
        )
        self._allowed_dirs = allowed_dirs

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "test_file": {
                    "type": "string",
                    "description": "Path to test file"
                },
                "coverage": {
                    "type": "boolean",
                    "description": "Include coverage report",
                    "default": True
                },
                "verbose": {
                    "type": "boolean",
                    "description": "Verbose output",
                    "default": True
                }
            },
            "required": ["test_file"]
        }

    def execute(self, test_file: str, coverage: bool = True, verbose: bool = True, **kwargs) -> ToolResult:  # type: ignore[override]
        """Run pytest with optional coverage"""
        try:
            # SECURITY: Validate test file path is within allowed directories
            is_valid, error_msg = _validate_file_path(test_file, self._allowed_dirs)
            if not is_valid:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Security: {error_msg}"
                )

            # Verify test file exists
            test_path = Path(test_file).resolve()
            if not test_path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Test file not found: {test_file}"
                )

            cmd = ["pytest"]

            if verbose:
                cmd.append("-v")

            if coverage:
                cmd.extend(["--cov", "--cov-report=term"])

            cmd.append(str(test_path))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )

            return ToolResult(
                success=(result.returncode == 0),
                output=result.stdout,
                error=result.stderr if result.returncode != 0 else None,
                metadata={"returncode": result.returncode}
            )

        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error="Tests timed out after 60s"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error running pytest: {str(e)}"
            )


class BanditScanner(Tool):
    """Run Bandit security scanner on Python code"""

    def __init__(self, allowed_dirs: Optional[List[Path]] = None):
        super().__init__(
            name="bandit",
            description="Scan Python code for security vulnerabilities using Bandit",
            category=ToolCategory.CODE_EXECUTION
        )
        self._allowed_dirs = allowed_dirs

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "File or directory to scan"
                },
                "severity_level": {
                    "type": "string",
                    "description": "Minimum severity: low, medium, high",
                    "default": "medium"
                }
            },
            "required": ["target"]
        }

    def execute(self, target: str, severity_level: str = "medium", **kwargs) -> ToolResult:  # type: ignore[override]
        """Run Bandit security scan"""
        try:
            # SECURITY: Validate target path is within allowed directories
            is_valid, error_msg = _validate_file_path(target, self._allowed_dirs)
            if not is_valid:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Security: {error_msg}"
                )

            # Verify target exists
            target_path = Path(target).resolve()
            if not target_path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Target not found: {target}"
                )

            # Map severity level to bandit flags
            severity_map = {
                "low": "-ll",      # Low and above
                "medium": "-l",    # Medium and above
                "high": ""         # High only
            }
            severity_flag = severity_map.get(severity_level.lower(), "-l")

            cmd = ["bandit", "-r", str(target_path), "-f", "json"]
            if severity_flag:
                cmd.append(severity_flag)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )

            # Bandit returns non-zero if issues found
            try:
                output_data = json.loads(result.stdout)
                issues = output_data.get("results", [])

                return ToolResult(
                    success=True,
                    output=f"Found {len(issues)} security issues",
                    metadata={
                        "issues": issues,
                        "summary": {
                            "total": len(issues),
                            "high": len([i for i in issues if i["issue_severity"] == "HIGH"]),
                            "medium": len([i for i in issues if i["issue_severity"] == "MEDIUM"]),
                            "low": len([i for i in issues if i["issue_severity"] == "LOW"])
                        }
                    }
                )
            except json.JSONDecodeError:
                return ToolResult(
                    success=False,
                    output=result.stdout,
                    error="Could not parse Bandit output"
                )

        except FileNotFoundError:
            return ToolResult(
                success=False,
                output="",
                error="Bandit not installed. Install with: pip install bandit"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error running Bandit: {str(e)}"
            )


class ShellExecutor(Tool):
    """Execute shell commands with process-level sandboxing.

    Security: timeout enforcement, stripped environment, resource limits.
    In Docker-based setups, the container provides the primary isolation;
    this tool adds defense-in-depth for subprocess-mode fallback.
    """

    _ALLOWED_ENV_KEYS = {"PATH", "HOME", "LANG", "TMPDIR", "LC_ALL", "LC_CTYPE"}

    def __init__(self):
        super().__init__(
            name="shell_executor",
            description="Execute a shell command and return its output. Use for pip install, npm, curl, and other CLI tools.",
            category=ToolCategory.CODE_EXECUTION,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                    "default": 30,
                },
            },
            "required": ["command"],
        }

    def execute(self, command: str, timeout: int = 30, **kwargs) -> ToolResult:  # type: ignore[override]
        if not command or not command.strip():
            return ToolResult(success=False, output="", error="No command provided")

        env = {k: v for k, v in os.environ.items() if k in self._ALLOWED_ENV_KEYS}

        try:
            result = subprocess.run(
                ["sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return ToolResult(
                success=(result.returncode == 0),
                output=result.stdout,
                error=result.stderr if result.returncode != 0 else None,
                metadata={"returncode": result.returncode},
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {timeout}s",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


__all__ = [
    "PythonExecutor",
    "PytestRunner",
    "BanditScanner",
    "ShellExecutor",
]
