"""
File operation tools: FileReader, FileWriter, and path validation helpers.

Provides secure file I/O with configurable allowed-directory restrictions.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List
import logging
import os
import sys
from pathlib import Path

from .base import Tool, ToolCategory, ToolResult

# Module self-reference: FileReader/FileWriter look up _validate_file_path
# through this so that unittest.mock.patch("agents.tools.file_tools._validate_file_path")
# or ("agents.tools.registry._validate_file_path") both work correctly.
_this_module = sys.modules[__name__]

logger = logging.getLogger(__name__)


# ===== File Operation Tools =====

# Built-in default directories (used when no config override is provided).
_DEFAULT_ALLOWED_FILE_DIRS = [
    Path("/home/user/Vibe").resolve(),
    Path("/tmp").resolve(),
]

# Project root for self-upgrade mode (agents/ source directory)
_SELF_UPGRADE_DIR = Path(__file__).resolve().parent.parent  # agents/ parent = Vibe-Stack

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

    The self-upgrade project root is appended only when using env-var
    or built-in defaults (not when explicit dirs are passed), so that
    callers who pass a specific allow-list get exactly those dirs.
    """
    if configured_dirs:
        return [Path(d).resolve() for d in configured_dirs]

    if (env_dirs := os.environ.get("VIBE_ALLOWED_FILE_DIRS")):
        dirs = [Path(d).resolve() for d in env_dirs.split(":") if d.strip()]
    else:
        dirs = list(_DEFAULT_ALLOWED_FILE_DIRS)

    # When self-upgrade is explicitly enabled via env var, grant access to
    # the project root.  We check for explicit presence (not a default) so
    # that unit tests with cleared environments get predictable results and
    # production deployments opt in via their .env / docker-compose config.
    su_val = os.environ.get("VIBE_SELF_UPGRADE_ENABLED")
    if su_val is not None and su_val.lower() not in ("false", "0", "no"):
        project_root = _SELF_UPGRADE_DIR.resolve()
        if project_root not in dirs:
            dirs.append(project_root)

    return dirs


def _validate_file_path(
    file_path: str,
    allowed_dirs: Optional[List[Path]] = None,
) -> tuple[bool, Optional[str]]:
    """
    Validate that file path is within allowed directories.

    Args:
        file_path:    Path to validate.
        allowed_dirs: Resolved directory list.  When *None*, falls back to
                      ``_build_allowed_file_dirs()`` (env var -> defaults).

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
            # Look up via registry module for backward-compat with test patches
            import agents.tools.registry as _reg
            is_valid, error_msg = _reg._validate_file_path(file_path, self._allowed_dirs)
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
            # Look up via registry module for backward-compat with test patches
            import agents.tools.registry as _reg
            is_valid, error_msg = _reg._validate_file_path(file_path, self._allowed_dirs)
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


__all__ = [
    "_DEFAULT_ALLOWED_FILE_DIRS",
    "_SELF_UPGRADE_DIR",
    "MAX_FILE_READ_SIZE",
    "MAX_FILE_WRITE_SIZE",
    "_build_allowed_file_dirs",
    "_validate_file_path",
    "FileReader",
    "FileWriter",
]
