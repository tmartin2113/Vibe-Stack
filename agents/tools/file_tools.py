"""
File operation tools: FileReader, FileWriter, and path validation helpers.

Provides secure file I/O with configurable allowed-directory restrictions.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List
import difflib
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

# Default line cap: if no start_line/end_line is specified and the file
# exceeds this many lines, return only the first DEFAULT_LINE_CAP lines
# with a warning.  Agents should use start_line/end_line for targeted reads.
DEFAULT_LINE_CAP = int(os.environ.get("VIBE_FILE_READ_LINE_CAP", "200"))


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
    """Read file contents with optional line ranges and re-read detection.

    Token-saving features:
    - ``start_line``/``end_line`` parameters for targeted reads
    - Auto-caps large files at DEFAULT_LINE_CAP lines with a warning
    - Tracks previously-read (file, range) tuples per session and warns
      on redundant re-reads (does NOT block — just warns)
    """

    def __init__(self, allowed_dirs: Optional[List[Path]] = None):
        super().__init__(
            name="file_reader",
            description=(
                "Read contents of a file. Use start_line/end_line for targeted "
                "reads — large files are auto-capped at {cap} lines without them."
            ).format(cap=DEFAULT_LINE_CAP),
            category=ToolCategory.FILE_OPS
        )
        self._allowed_dirs = allowed_dirs
        # Track reads per session: set of (resolved_path, start, end) tuples
        self._read_history: set = set()

    def reset(self) -> None:
        """Reset read history (called at heartbeat start)."""
        self._read_history.clear()

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": f"Path to file to read (max size: {MAX_FILE_READ_SIZE // (1024*1024)}MB)"
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to read (1-indexed, inclusive). Omit to start from beginning.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read (1-indexed, inclusive). Omit to read to end (capped).",
                },
                "encoding": {
                    "type": "string",
                    "description": "File encoding",
                    "default": "utf-8"
                }
            },
            "required": ["file_path"]
        }

    def execute(
        self,
        file_path: str,
        start_line: int = 0,
        end_line: int = 0,
        encoding: str = "utf-8",
        **kwargs,
    ) -> ToolResult:
        """Read file contents with optional line range."""
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

            all_lines = content.splitlines(keepends=True)
            total_lines = len(all_lines)
            has_range = start_line > 0 or end_line > 0
            was_capped = False

            if has_range:
                # Apply line range (1-indexed, inclusive)
                sl = max(start_line, 1)
                el = end_line if end_line > 0 else total_lines
                selected = all_lines[sl - 1 : el]
                content = "".join(selected)
            elif total_lines > DEFAULT_LINE_CAP:
                # No range specified on a large file — cap and warn
                selected = all_lines[:DEFAULT_LINE_CAP]
                content = "".join(selected)
                was_capped = True

            # Re-read detection
            read_key = (str(path), start_line, end_line)
            is_reread = read_key in self._read_history
            self._read_history.add(read_key)

            warnings = []
            if was_capped:
                warnings.append(
                    f"File has {total_lines} lines but was capped at {DEFAULT_LINE_CAP}. "
                    f"Use start_line/end_line for targeted reads."
                )
            if is_reread:
                warnings.append(
                    "You already read this file (same range) in this session. "
                    "Use the content already in context instead of re-reading."
                )

            output = content
            if warnings:
                output = "⚠ " + " | ".join(warnings) + "\n\n" + content

            return ToolResult(
                success=True,
                output=output,
                metadata={
                    "size": len(content),
                    "lines": len(content.splitlines()),
                    "total_lines": total_lines,
                    "file_size_bytes": file_size,
                    "was_capped": was_capped,
                    "is_reread": is_reread,
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

    def _emit_file_edit_event(
        self,
        file_path: str,
        edit_type: str,
        old_content: str,
        new_content: str,
    ) -> None:
        """Best-effort emission of file.edit event to Paperclip."""
        try:
            import agents.tools.registry as _reg
            client = getattr(_reg, "_paperclip_client", None)
            if client is None:
                return

            workspace = os.environ.get("WORKSPACE_DIR", "")
            rel_path = file_path
            if workspace and file_path.startswith(workspace):
                rel_path = os.path.relpath(file_path, workspace)

            diff_lines = list(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=rel_path,
                tofile=rel_path,
                lineterm="",
            ))
            truncated = diff_lines[-50:] if len(diff_lines) > 50 else diff_lines

            added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

            client.emit_run_event(
                event_type="file.edit",
                data={
                    "filePath": rel_path,
                    "editType": edit_type,
                    "diff": "\n".join(truncated),
                    "linesAdded": added,
                    "linesRemoved": removed,
                    "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                },
                message=f"{edit_type.capitalize()} {rel_path} (+{added} -{removed})",
            )
        except Exception:
            pass  # Best-effort, never block file writes

    def execute(self, file_path: str, content: str, encoding: str = "utf-8", **kwargs) -> ToolResult:
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

            # Capture old content for diff (best-effort)
            old_content = ""
            edit_type = "create"
            if path.exists():
                try:
                    old_content = path.read_text(encoding=encoding)
                    edit_type = "modify"
                except (OSError, UnicodeDecodeError):
                    pass

            path.write_text(content, encoding=encoding)

            # Emit file.edit event (best-effort, non-blocking)
            self._emit_file_edit_event(str(path), edit_type, old_content, content)

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
