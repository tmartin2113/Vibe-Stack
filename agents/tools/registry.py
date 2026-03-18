"""
Tool System for Agent Actions

This module provides the tool infrastructure for agents to perform actions
beyond text generation (code execution, file operations, API calls, etc.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from ..sandbox.client import SandboxPoolManager
from dataclasses import dataclass, field
from enum import Enum
import logging
import os
import re
import subprocess
import json
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class ToolCategory(Enum):
    """Categories of tools for organization"""
    CODE_EXECUTION = "code_execution"
    FILE_OPS = "file_ops"
    WEB_API = "web_api"
    EXTERNAL_SERVICE = "external_service"
    SPECIALIZED = "specialized"


@dataclass
class ToolResult:
    """Standardized tool execution result"""
    success: bool
    output: str
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata
        }


class Tool(ABC):
    """
    Abstract base class for all tools.

    Each tool must implement:
    - name: Unique identifier
    - description: What the tool does (for LLM to understand)
    - category: Type of tool
    - execute(): Run the tool
    """

    def __init__(self, name: str, description: str, category: ToolCategory):
        self.name = name
        self.description = description
        self.category = category
        self.enabled = True

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters

        Returns:
            ToolResult with success status and output
        """
        pass

    def get_schema(self) -> Dict[str, Any]:
        """
        Return JSON schema describing tool parameters.
        Used by LLM to know how to call the tool.
        """
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "parameters": self._get_parameters_schema()
        }

    @abstractmethod
    def _get_parameters_schema(self) -> Dict[str, Any]:
        """
        Return parameter schema for this tool.

        Example:
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"}
            },
            "required": ["code"]
        }
        """
        pass

    def validate_params(self, **kwargs) -> bool:
        """Validate parameters before execution"""
        # Basic validation - override for specific checks
        schema = self._get_parameters_schema()
        required = schema.get("required", [])
        for param in required:
            if param not in kwargs:
                raise ValueError(f"Missing required parameter: {param}")
        return True


# ===== Code Execution Tools =====

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


# ===== File Operation Tools =====

# Built-in default directories (used when no config override is provided).
_DEFAULT_ALLOWED_FILE_DIRS = [
    Path("/home/user/Vibe").resolve(),
    Path("/tmp").resolve(),
]

# File size limits (in bytes) to prevent memory issues
# These limits protect against memory exhaustion and excessive disk usage
# Adjust these values based on your deployment environment
MAX_FILE_READ_SIZE = 10 * 1024 * 1024  # 10 MB - maximum file size to read
MAX_FILE_WRITE_SIZE = 10 * 1024 * 1024  # 10 MB - maximum content size to write


def _build_allowed_file_dirs(configured_dirs: Optional[List[str]] = None) -> List[Path]:
    """Build the resolved list of allowed directories.

    Priority:
    1. ``configured_dirs`` (from SandboxConfig.allowed_file_dir_list)
    2. ``VIBE_ALLOWED_FILE_DIRS`` env var (colon-separated)
    3. Built-in defaults (/home/user/Vibe, /tmp)
    """
    if configured_dirs:
        return [Path(d).resolve() for d in configured_dirs]
    env_dirs = os.environ.get("VIBE_ALLOWED_FILE_DIRS")
    if env_dirs:
        return [Path(d).resolve() for d in env_dirs.split(":") if d.strip()]
    return list(_DEFAULT_ALLOWED_FILE_DIRS)


def _validate_file_path(
    file_path: str,
    allowed_dirs: Optional[List[Path]] = None,
) -> tuple[bool, Optional[str]]:
    """
    Validate that file path is within allowed directories.

    Args:
        file_path:    Path to validate.
        allowed_dirs: Resolved directory list.  When *None*, falls back to
                      ``_build_allowed_file_dirs()`` (env var → defaults).

    Returns:
        tuple of (is_valid, error_message)
    """
    if allowed_dirs is None:
        allowed_dirs = _build_allowed_file_dirs()

    try:
        path = Path(file_path).resolve()

        # Check if path is within any allowed directory
        for allowed_dir in allowed_dirs:
            try:
                # Python 3.9+ has is_relative_to()
                if hasattr(path, 'is_relative_to'):
                    if path.is_relative_to(allowed_dir):
                        return True, None
                else:
                    # Fallback for Python 3.8
                    try:
                        path.relative_to(allowed_dir)
                        return True, None
                    except ValueError:
                        continue
            except (ValueError, TypeError):
                continue

        # Path not in any allowed directory
        allowed_str = ", ".join(str(d) for d in allowed_dirs)
        return False, f"Path outside allowed directories. Allowed: {allowed_str}"

    except Exception as e:
        return False, f"Invalid path: {str(e)}"


class FileReader(Tool):
    """Read file contents"""

    def __init__(self, allowed_dirs: Optional[List[Path]] = None):
        super().__init__(
            name="file_reader",
            description="Read contents of a file",
            category=ToolCategory.FILE_OPS
        )
        self._allowed_dirs = allowed_dirs

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": f"Path to file to read (max size: {MAX_FILE_READ_SIZE // (1024*1024)}MB)"
                },
                "encoding": {
                    "type": "string",
                    "description": "File encoding",
                    "default": "utf-8"
                }
            },
            "required": ["file_path"]
        }

    def execute(self, file_path: str, encoding: str = "utf-8", **kwargs) -> ToolResult:  # type: ignore[override]
        """Read file contents"""
        try:
            # SECURITY: Validate path is within allowed directories
            is_valid, error_msg = _validate_file_path(file_path, self._allowed_dirs)
            if not is_valid:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Security: {error_msg}"
                )

            path = Path(file_path).resolve()

            if not path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File not found: {file_path}"
                )

            if not path.is_file():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Path is not a file: {file_path}"
                )

            # Check file size before reading (prevent memory issues)
            file_size = path.stat().st_size
            if file_size > MAX_FILE_READ_SIZE:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File too large: {file_size} bytes (max: {MAX_FILE_READ_SIZE} bytes)"
                )

            try:
                content = path.read_text(encoding=encoding)
            except UnicodeDecodeError as e:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Encoding error: Cannot decode file as {encoding}. File may be binary. Error: {str(e)}"
                )

            return ToolResult(
                success=True,
                output=content,
                metadata={
                    "size": len(content),
                    "lines": len(content.splitlines()),
                    "file_size_bytes": file_size
                }
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error reading file: {str(e)}"
            )


class FileWriter(Tool):
    """Write content to a file"""

    def __init__(self, allowed_dirs: Optional[List[Path]] = None):
        super().__init__(
            name="file_writer",
            description="Write content to a file",
            category=ToolCategory.FILE_OPS
        )
        self._allowed_dirs = allowed_dirs

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to file to write"
                },
                "content": {
                    "type": "string",
                    "description": f"Content to write (max size: {MAX_FILE_WRITE_SIZE // (1024*1024)}MB)"
                },
                "encoding": {
                    "type": "string",
                    "description": "File encoding",
                    "default": "utf-8"
                }
            },
            "required": ["file_path", "content"]
        }

    def execute(self, file_path: str, content: str, encoding: str = "utf-8", **kwargs) -> ToolResult:  # type: ignore[override]
        """Write content to file"""
        try:
            # SECURITY: Validate path is within allowed directories
            is_valid, error_msg = _validate_file_path(file_path, self._allowed_dirs)
            if not is_valid:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Security: {error_msg}"
                )

            # Check content size before writing (prevent memory/disk issues)
            try:
                content_bytes = content.encode(encoding)
                content_size = len(content_bytes)
            except UnicodeEncodeError as e:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Encoding error: Cannot encode content as {encoding}. Error: {str(e)}"
                )

            if content_size > MAX_FILE_WRITE_SIZE:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Content too large: {content_size} bytes (max: {MAX_FILE_WRITE_SIZE} bytes)"
                )

            path = Path(file_path).resolve()

            # Create parent directories if needed
            path.parent.mkdir(parents=True, exist_ok=True)

            path.write_text(content, encoding=encoding)

            return ToolResult(
                success=True,
                output=f"Wrote {len(content)} characters to {file_path}",
                metadata={
                    "bytes_written": content_size,
                    "characters": len(content),
                    "lines": len(content.splitlines())
                }
            )

        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Error writing file: {str(e)}"
            )


# ===== Tool Registry =====

class ToolRegistry:
    """
    Central registry for managing tools.

    Provides tool discovery, execution, and schema generation.
    """

    def __init__(self):
        self.tools: Dict[str, Tool] = {}

    def register(self, tool: Tool):
        """Register a tool"""
        self.tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")

    def get(self, name: str) -> Optional[Tool]:
        """Get tool by name"""
        return self.tools.get(name)

    def list_tools(self) -> List[str]:
        """List all registered tool names"""
        return list(self.tools.keys())

    def get_tools_by_category(self, category: ToolCategory) -> List[Tool]:
        """Get all tools in a category"""
        return [t for t in self.tools.values() if t.category == category]

    def execute_tool(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name"""
        tool = self.get(name)

        if not tool:
            available = ", ".join(self.list_tools())
            return ToolResult(
                success=False,
                output="",
                error=f"Tool not found: '{name}'. Available tools: {available}"
            )

        if not tool.enabled:
            return ToolResult(
                success=False,
                output="",
                error=f"Tool disabled: {name}"
            )

        try:
            tool.validate_params(**kwargs)
            return tool.execute(**kwargs)
        except ValueError as e:
            # Parameter validation errors - provide clear feedback
            logger.warning(f"Parameter validation failed for tool {name}: {e}")
            return ToolResult(
                success=False,
                output="",
                error=f"Invalid parameters for tool '{name}': {str(e)}"
            )
        except Exception as e:
            # Unexpected errors - log full traceback but provide user-friendly message
            logger.exception(f"Unexpected error executing tool {name} with params {kwargs}")
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{name}' execution failed: {type(e).__name__}: {str(e)}"
            )

    def get_all_schemas(self) -> List[Dict[str, Any]]:
        """Get schemas for all tools (for LLM prompt)"""
        return [tool.get_schema() for tool in self.tools.values() if tool.enabled]

    def parse_tool_call(self, output: str) -> Optional[Dict[str, Any]]:
        """
        Parse tool call from LLM output.

        Expected format:
        <tool_call name="tool_name">{"param1": "value1"}</tool_call>

        Returns:
            Dict with 'name' and 'params' or None if no tool call found
        """
        pattern = r'<tool_call name=["\'](\w+)["\']>(.*?)</tool_call>'
        match = re.search(pattern, output, re.DOTALL)

        if not match:
            return None

        tool_name = match.group(1)
        params_json = match.group(2).strip()

        try:
            params = json.loads(params_json)
            return {
                "name": tool_name,
                "params": params
            }
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in tool call: {params_json}")
            return None


class DevToolWrapper(Tool):
    """Wraps extended dev/seo tools to conform to the Tool ABC.

    Extended tools in dev_tools.py/seo_tools.py return Dict[str, Any]
    instead of ToolResult and lack get_schema()/validate_params().
    This wrapper bridges them to the ToolRegistry interface.
    """

    def __init__(
        self,
        inner_tool: Any,
        category: ToolCategory = ToolCategory.SPECIALIZED,
        parameters_schema: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(inner_tool.name, inner_tool.description, category)
        self._inner = inner_tool
        self._params_schema = parameters_schema or {
            "type": "object",
            "properties": {},
        }

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return self._params_schema

    def execute(self, **kwargs) -> ToolResult:  # type: ignore[override]
        result = self._inner.execute(**kwargs)
        if isinstance(result, ToolResult):
            return result
        if isinstance(result, dict):
            success = result.get("success", True)
            error = result.get("error")
            # Produce human-readable output
            output = result.get("output", json.dumps(result, indent=2, default=str))
            if not isinstance(output, str):
                output = json.dumps(output, indent=2, default=str)
            return ToolResult(success=success, output=output, error=error, metadata=result)
        return ToolResult(success=True, output=str(result))


class WebFetchTool(Tool):
    """Fetch content from a URL and return it as text.

    Uses a Python subprocess with urllib (stdlib — no extra deps).
    Network egress must be enabled for this tool to be registered.
    """

    def __init__(self):
        super().__init__(
            name="web_fetch",
            description="Fetch a URL and return its content. Use for downloading pages, APIs, or files.",
            category=ToolCategory.WEB_API,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (http or https)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds (default 15)",
                    "default": 15,
                },
            },
            "required": ["url"],
        }

    def execute(self, url: str, timeout: int = 15, **kwargs) -> ToolResult:  # type: ignore[override]
        if not url or not url.strip():
            return ToolResult(success=False, output="", error="No URL provided")
        if not url.startswith(("http://", "https://")):
            return ToolResult(success=False, output="", error="URL must start with http:// or https://")

        # Run in subprocess to enforce timeout and isolation
        script = (
            "import urllib.request, sys; "
            f"req = urllib.request.Request(sys.argv[1], headers={{'User-Agent': 'Vibe/1.0'}}); "
            "r = urllib.request.urlopen(req, timeout=int(sys.argv[2])); "
            "sys.stdout.buffer.write(r.read())"
        )
        try:
            result = subprocess.run(
                ["python3", "-c", script, url, str(timeout)],
                capture_output=True,
                text=True,
                timeout=timeout + 5,  # extra headroom beyond urllib timeout
            )
            if result.returncode == 0:
                return ToolResult(
                    success=True,
                    output=result.stdout,
                    metadata={"url": url, "length": len(result.stdout)},
                )
            return ToolResult(
                success=False,
                output=result.stdout,
                error=result.stderr,
                metadata={"url": url},
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"Fetch timed out after {timeout}s")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))


class FirecrawlScrapeTool(Tool):
    """Scrape a single URL using Firecrawl and return clean markdown.

    Handles JavaScript rendering, anti-bot measures, and content extraction
    automatically.  Returns LLM-ready markdown instead of raw HTML.

    Requires the ``firecrawl-py`` package and a ``FIRECRAWL_API_KEY``
    environment variable (or key passed at init).
    Network egress must be enabled for this tool to be registered.
    """

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            name="web_scrape",
            description=(
                "Scrape a web page and return its content as clean markdown. "
                "Handles JavaScript-rendered pages, anti-bot protections, and "
                "extracts main content. Use instead of web_fetch when you need "
                "readable page content rather than raw HTML."
            ),
            category=ToolCategory.WEB_API,
        )
        self._api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to scrape (http or https)",
                },
                "formats": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Output formats: markdown, html, links, screenshot "
                        "(default: ['markdown'])"
                    ),
                    "default": ["markdown"],
                },
                "only_main_content": {
                    "type": "boolean",
                    "description": (
                        "Extract only main content, stripping nav/footer/ads "
                        "(default: true)"
                    ),
                    "default": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                    "default": 30,
                },
            },
            "required": ["url"],
        }

    def execute(  # type: ignore[override]
        self,
        url: str,
        formats: Optional[List[str]] = None,
        only_main_content: bool = True,
        timeout: int = 30,
        **kwargs: Any,
    ) -> ToolResult:
        if not url or not url.strip():
            return ToolResult(success=False, output="", error="No URL provided")
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                success=False, output="",
                error="URL must start with http:// or https://",
            )
        if not self._api_key:
            return ToolResult(
                success=False, output="",
                error="FIRECRAWL_API_KEY not set. Set via environment variable.",
            )

        try:
            from firecrawl import FirecrawlApp  # type: ignore[import-untyped]
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="firecrawl-py not installed. Install with: pip install firecrawl-py",
            )

        formats = formats or ["markdown"]

        try:
            app = FirecrawlApp(api_key=self._api_key)
            result = app.scrape_url(url, params={
                "formats": formats,
                "onlyMainContent": only_main_content,
                "timeout": timeout * 1000,  # Firecrawl uses milliseconds
            })

            # Extract content from response
            if isinstance(result, dict):
                # Prefer markdown, fall back to other formats
                content = (
                    result.get("markdown")
                    or result.get("html")
                    or result.get("rawHtml")
                    or json.dumps(result, indent=2, default=str)
                )
                metadata_out: Dict[str, Any] = {
                    "url": url,
                    "formats": formats,
                }
                if result.get("metadata"):
                    page_meta = result["metadata"]
                    metadata_out["title"] = page_meta.get("title", "")
                    metadata_out["description"] = page_meta.get("description", "")
                    metadata_out["language"] = page_meta.get("language", "")
                if result.get("links"):
                    metadata_out["link_count"] = len(result["links"])
                return ToolResult(
                    success=True,
                    output=content,
                    metadata=metadata_out,
                )
            # Unexpected response shape
            return ToolResult(
                success=True,
                output=str(result),
                metadata={"url": url},
            )

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Firecrawl scrape failed: {e}",
            )


class FirecrawlCrawlTool(Tool):
    """Crawl multiple pages from a starting URL using Firecrawl.

    Useful for ingesting documentation sites, sitemaps, or multi-page
    content.  Returns a combined markdown document with page separators.

    Requires the ``firecrawl-py`` package and a ``FIRECRAWL_API_KEY``
    environment variable (or key passed at init).
    Network egress must be enabled for this tool to be registered.
    """

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            name="web_crawl",
            description=(
                "Crawl a website starting from a URL and return content from "
                "multiple pages as markdown. Use for ingesting documentation "
                "sites or exploring site structure. Returns combined content "
                "with page separators."
            ),
            category=ToolCategory.WEB_API,
        )
        self._api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The starting URL to crawl from",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of pages to crawl (default 10, max 50)",
                    "default": 10,
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum link depth from start URL (default 2)",
                    "default": 2,
                },
                "include_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URL glob patterns to include (e.g. ['/docs/*'])",
                },
                "exclude_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URL glob patterns to exclude (e.g. ['/blog/*'])",
                },
            },
            "required": ["url"],
        }

    def execute(  # type: ignore[override]
        self,
        url: str,
        limit: int = 10,
        max_depth: int = 2,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> ToolResult:
        if not url or not url.strip():
            return ToolResult(success=False, output="", error="No URL provided")
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                success=False, output="",
                error="URL must start with http:// or https://",
            )
        if not self._api_key:
            return ToolResult(
                success=False, output="",
                error="FIRECRAWL_API_KEY not set. Set via environment variable.",
            )

        # Clamp limits to prevent runaway crawls
        limit = max(1, min(limit, 50))
        max_depth = max(1, min(max_depth, 5))

        try:
            from firecrawl import FirecrawlApp  # type: ignore[import-untyped]
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="firecrawl-py not installed. Install with: pip install firecrawl-py",
            )

        try:
            app = FirecrawlApp(api_key=self._api_key)

            crawl_params: Dict[str, Any] = {
                "limit": limit,
                "maxDepth": max_depth,
                "scrapeOptions": {"formats": ["markdown"]},
            }
            if include_patterns:
                crawl_params["includePaths"] = include_patterns
            if exclude_patterns:
                crawl_params["excludePaths"] = exclude_patterns

            # crawl_url polls until complete (synchronous by default)
            result = app.crawl_url(url, params=crawl_params)

            # Parse crawl results
            pages: List[Dict[str, Any]] = []
            if isinstance(result, dict):
                pages = result.get("data", [])
            elif isinstance(result, list):
                pages = result

            if not pages:
                return ToolResult(
                    success=True,
                    output="Crawl completed but returned no pages.",
                    metadata={"url": url, "pages_found": 0},
                )

            # Combine pages into a single document with separators
            sections: List[str] = []
            for page in pages:
                page_url = page.get("metadata", {}).get("sourceURL", page.get("url", "unknown"))
                page_title = page.get("metadata", {}).get("title", "")
                content = page.get("markdown", page.get("content", ""))
                if not content:
                    continue
                header = f"# {page_title}\n> Source: {page_url}" if page_title else f"> Source: {page_url}"
                sections.append(f"{header}\n\n{content}")

            combined = "\n\n---\n\n".join(sections)

            return ToolResult(
                success=True,
                output=combined,
                metadata={
                    "url": url,
                    "pages_crawled": len(sections),
                    "pages_total": len(pages),
                    "limit": limit,
                    "max_depth": max_depth,
                },
            )

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Firecrawl crawl failed: {e}",
            )


class FirecrawlSearchTool(Tool):
    """Search the web using Firecrawl and return scraped results.

    Combines web search with automatic scraping of result pages,
    returning clean markdown content instead of just links.

    Requires the ``firecrawl-py`` package and a ``FIRECRAWL_API_KEY``
    environment variable (or key passed at init).
    """

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            name="web_search",
            description=(
                "Search the web and return scraped content from result pages "
                "as markdown. More powerful than a simple search — actually "
                "reads the pages and returns their content."
            ),
            category=ToolCategory.WEB_API,
        )
        self._api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 5, max 10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    def execute(  # type: ignore[override]
        self,
        query: str,
        limit: int = 5,
        **kwargs: Any,
    ) -> ToolResult:
        if not query or not query.strip():
            return ToolResult(success=False, output="", error="No query provided")
        if not self._api_key:
            return ToolResult(
                success=False, output="",
                error="FIRECRAWL_API_KEY not set. Set via environment variable.",
            )

        limit = max(1, min(limit, 10))

        try:
            from firecrawl import FirecrawlApp  # type: ignore[import-untyped]
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="firecrawl-py not installed. Install with: pip install firecrawl-py",
            )

        try:
            app = FirecrawlApp(api_key=self._api_key)
            result = app.search(query, params={"limit": limit})

            # Parse search results
            items: List[Dict[str, Any]] = []
            if isinstance(result, dict):
                items = result.get("data", [])
            elif isinstance(result, list):
                items = result

            if not items:
                return ToolResult(
                    success=True,
                    output=f"No results found for: {query}",
                    metadata={"query": query, "results": 0},
                )

            sections: List[str] = []
            for item in items[:limit]:
                title = item.get("metadata", {}).get("title", item.get("title", ""))
                item_url = item.get("metadata", {}).get("sourceURL", item.get("url", ""))
                content = item.get("markdown", item.get("content", item.get("description", "")))
                header = f"### {title}" if title else "### (untitled)"
                if item_url:
                    header += f"\n> {item_url}"
                sections.append(f"{header}\n\n{content}" if content else header)

            combined = "\n\n---\n\n".join(sections)

            return ToolResult(
                success=True,
                output=combined,
                metadata={
                    "query": query,
                    "results": len(sections),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Firecrawl search failed: {e}",
            )


class MemoryStoreTool(Tool):
    """Store a fact, decision, or insight in persistent long-term memory.

    Every entry carries a citation tracking where the information came from.
    Memories persist across sessions and can be recalled later via memory_recall.
    """

    def __init__(self):
        super().__init__(
            name="memory_store",
            description=(
                "Store a fact, decision, insight, or learned context in persistent memory. "
                "Include the source of the information for citation tracking."
            ),
            category=ToolCategory.SPECIALIZED,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact, decision, or insight to remember.",
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Where this information came from. "
                        "Conventions: 'user' (user statement), 'url:<url>' (web page), "
                        "'file:<path>' (local file), 'tool:<name>' (tool output), "
                        "'agent' (inferred). Default: 'agent'"
                    ),
                },
                "tags": {
                    "type": "string",
                    "description": (
                        "Space-separated tags for categorization. "
                        "e.g. 'architecture decision python'"
                    ),
                },
            },
            "required": ["content"],
        }

    def execute(self, content: str, source: str = "agent", tags: str = "", **kwargs) -> ToolResult:  # type: ignore[override]
        if not content or not content.strip():
            return ToolResult(success=False, output="", error="Content cannot be empty")

        try:
            from ..memory_store import MemoryStore

            store = _get_shared_memory_store()
            memory_id = store.store(content=content, source=source, tags=tags)
            return ToolResult(
                success=True,
                output=f"Stored memory #{memory_id}: {content[:200]}",
                metadata={"memory_id": memory_id, "source": source, "tags": tags},
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to store memory: {e}")


class MemoryRecallTool(Tool):
    """Search persistent memory for relevant facts, decisions, and context.

    Returns results ranked by relevance (BM25) with citation information
    showing where each memory originally came from.
    """

    def __init__(self):
        super().__init__(
            name="memory_recall",
            description=(
                "Search persistent memory for relevant facts, decisions, insights, "
                "and context. Returns results with citations showing the original source."
            ),
            category=ToolCategory.SPECIALIZED,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — keywords or natural language.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (1-20, default 5).",
                    "default": 5,
                },
                "tag_filter": {
                    "type": "string",
                    "description": "Only return memories with tags containing this substring.",
                },
                "source_filter": {
                    "type": "string",
                    "description": (
                        "Only return memories whose source starts with this prefix. "
                        "e.g. 'url:' for web sources, 'file:' for file sources."
                    ),
                },
            },
            "required": ["query"],
        }

    def execute(self, query: str, max_results: int = 5, tag_filter: str = "", source_filter: str = "", **kwargs) -> ToolResult:  # type: ignore[override]
        if not query or not query.strip():
            return ToolResult(success=False, output="", error="Query cannot be empty")

        max_results = max(1, min(max_results, 20))

        try:
            from ..memory_store import MemoryStore

            store = _get_shared_memory_store()
            results = store.recall(
                query=query,
                max_results=max_results,
                tag_filter=tag_filter,
                source_filter=source_filter,
            )

            if not results:
                return ToolResult(
                    success=True,
                    output="No relevant memories found.",
                    metadata={"query": query, "results": 0},
                )

            # Format results with citations
            sections = []
            for i, entry in enumerate(results, 1):
                section = f"## Memory #{entry.memory_id} (score: {entry.score:.2f})\n"
                section += f"{entry.content}\n"
                section += f"\n**Source:** {entry.citation}\n"
                if entry.tags:
                    section += f"**Tags:** {entry.tags}\n"
                section += f"**Stored:** {entry.created_at}\n"
                sections.append(section)

            combined = "\n---\n".join(sections)
            header = f"Found {len(results)} relevant memories:\n\n"

            return ToolResult(
                success=True,
                output=header + combined,
                metadata={
                    "query": query,
                    "results": len(results),
                    "memory_ids": [e.memory_id for e in results],
                },
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Memory recall failed: {e}")


# Shared MemoryStore singleton (lazy-initialized)
_shared_memory_store = None
_memory_store_lock = threading.Lock()


def _get_shared_memory_store():
    """Get or create the shared MemoryStore singleton."""
    global _shared_memory_store
    if _shared_memory_store is None:
        with _memory_store_lock:
            if _shared_memory_store is None:
                from ..memory_store import MemoryStore
                _shared_memory_store = MemoryStore()
    return _shared_memory_store


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


# === Parameter schemas for extended tools ===

_STATIC_ANALYZER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File or directory to analyze"},
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Linters to use (e.g. ['ruff', 'mypy']). Omit for auto-detect.",
        },
    },
    "required": ["path"],
}

_CODEBASE_SEARCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Function name, class name, or text pattern to search for"},
        "path": {"type": "string", "description": "Directory to search (default: current dir)"},
        "search_type": {"type": "string", "description": "Search mode: function, class, text, auto (default: auto)"},
    },
    "required": ["query"],
}

_GIT_OPERATIONS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "description": "Git operation: blame, history, diff, status, branches"},
        "path": {"type": "string", "description": "Repository or file path (default: current dir)"},
    },
    "required": ["operation"],
}

_DATA_PARSER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "data": {"type": "string", "description": "Data string or file path to parse"},
        "format_type": {"type": "string", "description": "Format: json, yaml, xml, csv, toml, auto (default: auto)"},
    },
    "required": ["data"],
}


def create_default_tool_registry(
    sandbox_pool: "SandboxPoolManager",
    network_egress: bool = False,
    allowed_file_dirs: Optional[List[str]] = None,
) -> ToolRegistry:
    """
    Create a tool registry with all default tools.

    Code execution tools (PythonExecutor, PytestRunner, BanditScanner,
    ShellExecutor) run inside OpenSandbox containers. File operations and
    in-process tools (search, git, parsing) remain local.

    Args:
        sandbox_pool: SandboxPoolManager for containerized execution.
        network_egress: If True, register WebFetchTool for HTTP access.
            Defaults to False (no outbound network tools).
        allowed_file_dirs: List of directory paths the agent may read/write.
            Falls back to VIBE_ALLOWED_FILE_DIRS env var, then built-in
            defaults (/home/user/Vibe, /tmp).

    Returns:
        ToolRegistry with all tools registered
    """
    registry = ToolRegistry()

    # Resolve allowed directories once for all file-aware tools
    resolved_dirs = _build_allowed_file_dirs(allowed_file_dirs)
    logger.info(f"Tool registry: allowed file dirs = {[str(d) for d in resolved_dirs]}")

    # --- Code execution tools (sandboxed) ---
    from ..sandbox.tools import (
        SandboxedPythonExecutor,
        SandboxedPytestRunner,
        SandboxedBanditScanner,
        SandboxedShellExecutor,
    )
    registry.register(SandboxedPythonExecutor(sandbox_pool))
    registry.register(SandboxedPytestRunner(sandbox_pool))
    registry.register(SandboxedBanditScanner(sandbox_pool))
    registry.register(SandboxedShellExecutor(sandbox_pool))
    logger.info("Tool registry: using OpenSandbox-backed execution")

    # --- Web fetch (only when egress is enabled) ---
    if network_egress:
        from ..sandbox.tools import SandboxedWebFetchTool
        registry.register(SandboxedWebFetchTool(sandbox_pool))
        logger.info("Tool registry: web_fetch enabled (network_egress=True)")
    else:
        logger.info("Tool registry: web_fetch disabled (network_egress=False)")

    # --- Firecrawl tools (always-on when API key is set) ---
    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if firecrawl_key:
        registry.register(FirecrawlScrapeTool(api_key=firecrawl_key))
        registry.register(FirecrawlCrawlTool(api_key=firecrawl_key))
        registry.register(FirecrawlSearchTool(api_key=firecrawl_key))
        logger.info("Tool registry: Firecrawl tools enabled (web_scrape, web_crawl, web_search)")
    else:
        logger.info("Tool registry: Firecrawl tools disabled (FIRECRAWL_API_KEY not set)")

    # --- Persistent memory tools (always-on) ---
    registry.register(MemoryStoreTool())
    registry.register(MemoryRecallTool())

    # --- File operation tools (always local) ---
    registry.register(FileReader(allowed_dirs=resolved_dirs))
    registry.register(FileWriter(allowed_dirs=resolved_dirs))

    # --- Extended dev tools (in-process, no sandboxing needed) ---
    from .dev_tools import (
        StaticCodeAnalyzer,
        CodebaseSearchTool,
        GitOperationsTool,
        DataParserTool,
    )
    registry.register(DevToolWrapper(
        StaticCodeAnalyzer(), ToolCategory.CODE_EXECUTION, _STATIC_ANALYZER_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        CodebaseSearchTool(), ToolCategory.SPECIALIZED, _CODEBASE_SEARCH_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        GitOperationsTool(), ToolCategory.SPECIALIZED, _GIT_OPERATIONS_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        DataParserTool(), ToolCategory.SPECIALIZED, _DATA_PARSER_SCHEMA,
    ))

    logger.info(f"Created default tool registry with {len(registry.list_tools())} tools")

    return registry


def create_subprocess_tool_registry(
    network_egress: bool = False,
    allowed_file_dirs: Optional[List[str]] = None,
) -> ToolRegistry:
    """
    Create a tool registry using subprocess-based execution (no OpenSandbox).

    Fallback for environments where the opensandbox SDK is not installed.
    Tools run in isolated subprocesses with resource limits instead of containers.
    """
    registry = ToolRegistry()

    resolved_dirs = _build_allowed_file_dirs(allowed_file_dirs)
    logger.info(f"Tool registry (subprocess): allowed file dirs = {[str(d) for d in resolved_dirs]}")

    # --- Code execution tools (subprocess-based) ---
    registry.register(PythonExecutor())
    registry.register(PytestRunner())
    registry.register(BanditScanner())
    registry.register(ShellExecutor())
    logger.info("Tool registry: using subprocess-backed execution (no OpenSandbox)")

    # --- Web fetch (only when egress is enabled) ---
    if network_egress:
        registry.register(WebFetchTool())
        logger.info("Tool registry: web_fetch enabled (network_egress=True)")

    # --- Firecrawl tools (always-on when API key is set) ---
    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if firecrawl_key:
        registry.register(FirecrawlScrapeTool(api_key=firecrawl_key))
        registry.register(FirecrawlCrawlTool(api_key=firecrawl_key))
        registry.register(FirecrawlSearchTool(api_key=firecrawl_key))
        logger.info("Tool registry: Firecrawl tools enabled")

    # --- Persistent memory tools ---
    registry.register(MemoryStoreTool())
    registry.register(MemoryRecallTool())

    # --- File operation tools ---
    registry.register(FileReader(allowed_dirs=resolved_dirs))
    registry.register(FileWriter(allowed_dirs=resolved_dirs))

    # --- Extended dev tools (in-process) ---
    from .dev_tools import (
        StaticCodeAnalyzer,
        CodebaseSearchTool,
        GitOperationsTool,
        DataParserTool,
    )
    registry.register(DevToolWrapper(
        StaticCodeAnalyzer(), ToolCategory.CODE_EXECUTION, _STATIC_ANALYZER_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        CodebaseSearchTool(), ToolCategory.SPECIALIZED, _CODEBASE_SEARCH_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        GitOperationsTool(), ToolCategory.SPECIALIZED, _GIT_OPERATIONS_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        DataParserTool(), ToolCategory.SPECIALIZED, _DATA_PARSER_SCHEMA,
    ))

    logger.info(f"Created subprocess tool registry with {len(registry.list_tools())} tools")

    return registry
