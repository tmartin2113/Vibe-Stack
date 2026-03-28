"""
Tool System for Agent Actions

This module provides the tool infrastructure for agents to perform actions
beyond text generation (code execution, file operations, API calls, etc.)

Classes and helpers have been split into submodules for maintainability:
- base.py         — ToolCategory, ToolResult, Tool ABC
- executors.py    — PythonExecutor, PytestRunner, BanditScanner, ShellExecutor
- file_tools.py   — FileReader, FileWriter, path validation helpers
- web_tools.py    — WebFetchTool, DevToolWrapper
- memory_tools.py — MemoryStoreTool, MemoryRecallTool, shared store singleton

This file retains the ToolRegistry class, role-based tool sets, parameter
schemas for extended tools, and the factory functions.  All public names are
re-exported here for backward compatibility.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from ..sandbox.client import SandboxPoolManager

import logging
import os
import re
import json
import subprocess  # re-imported for backward-compat patching in tests
from pathlib import Path  # re-imported for backward-compat patching in tests

# ── Re-exports from submodules (backward compat) ────────────────────────
from .base import ToolCategory, ToolResult, Tool
from .executors import PythonExecutor, PytestRunner, BanditScanner, ShellExecutor
from .file_tools import (
    _DEFAULT_ALLOWED_FILE_DIRS,
    _SELF_UPGRADE_DIR,
    MAX_FILE_READ_SIZE,
    MAX_FILE_WRITE_SIZE,
    _build_allowed_file_dirs,
    _validate_file_path,
    FileReader,
    FileWriter,
)
from .web_tools import DevToolWrapper, WebFetchTool
from . import memory_tools as _memory_tools_mod
from .memory_tools import MemoryStoreTool, MemoryRecallTool, _get_shared_memory_store

# Backward compat: expose _shared_memory_store at module level.
# The canonical singleton now lives in memory_tools; this alias lets
# tests that set ``agents.tools.registry._shared_memory_store = store``
# continue to work. _get_shared_memory_store() reads from memory_tools.
_shared_memory_store = None

logger = logging.getLogger(__name__)


# ===== Tool Registry =====

# ── Role-based tool filtering ─────────────────────────────────────────
# Maps agent roles to the tool names they should have access to.
# Tools not listed for a role are hidden from that role's registry view.
# 'None' (or unlisted roles) means no filtering — all tools visible.

ROLE_TOOL_SETS: Dict[str, Optional[frozenset]] = {
    # CTO sees everything — no filter
    "cto": None,

    "frontend_engineer": frozenset({
        # Code execution & analysis
        "python_executor", "pytest_runner", "shell_executor",
        "static_code_analyzer", "codebase_search_tool", "git_operations_tool", "data_parser_tool",
        # Web & browser
        "web_fetch", "web_search", "web_scrape", "browser_automation",
        # Design & visual
        "design", "image_generation",
        # SEO
        "lighthouse_seo", "page_analyzer", "seo_checklist",
        # Infrastructure
        "git_forge", "artifact_storage", "bulletin_board",
        # File ops & memory
        "file_reader", "file_writer", "memory_store", "memory_recall",
    }),

    "backend_engineer": frozenset({
        # Code execution & analysis
        "python_executor", "pytest_runner", "bandit_scanner", "shell_executor",
        "static_code_analyzer", "codebase_search_tool", "git_operations_tool", "data_parser_tool",
        # Web
        "web_fetch", "web_search", "web_scrape",
        # Database
        "database",
        # Security
        "dependency_scanner",
        # Infrastructure
        "container_inspect", "git_forge", "artifact_storage", "bulletin_board",
        # File ops & memory
        "file_reader", "file_writer", "memory_store", "memory_recall",
    }),

    "qa_engineer": frozenset({
        # Code execution & testing
        "python_executor", "pytest_runner", "shell_executor",
        "static_code_analyzer", "codebase_search_tool", "git_operations_tool", "data_parser_tool",
        # Web & browser (for E2E testing)
        "web_fetch", "web_search", "web_scrape", "browser_automation",
        # Security
        "bandit_scanner", "dependency_scanner",
        # SEO (for quality checks)
        "lighthouse_seo", "page_analyzer", "seo_checklist",
        # Infrastructure
        "container_inspect", "git_forge", "artifact_storage", "bulletin_board",
        # File ops & memory
        "file_reader", "file_writer", "memory_store", "memory_recall",
    }),

    "ux_engineer": frozenset({
        # Browser & design
        "web_fetch", "web_search", "web_scrape", "browser_automation",
        "design", "image_generation",
        # SEO (UX-relevant)
        "lighthouse_seo", "page_analyzer", "seo_checklist",
        # Light code support
        "shell_executor", "codebase_search_tool", "static_code_analyzer",
        # Infrastructure
        "git_forge", "artifact_storage", "bulletin_board",
        # File ops & memory
        "file_reader", "file_writer", "memory_store", "memory_recall",
    }),

    "security_engineer": frozenset({
        # Code execution & analysis
        "python_executor", "pytest_runner", "bandit_scanner", "shell_executor",
        "static_code_analyzer", "codebase_search_tool", "git_operations_tool", "data_parser_tool",
        # Security-specific
        "dependency_scanner",
        # Web (for security testing)
        "web_fetch", "web_search", "web_scrape", "browser_automation",
        # Infrastructure
        "container_inspect", "git_forge", "artifact_storage", "bulletin_board",
        # File ops & memory
        "file_reader", "file_writer", "memory_store", "memory_recall",
    }),
}


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

    def filter_for_role(self, role: str) -> "ToolRegistry":
        """Return a new ToolRegistry containing only tools allowed for *role*.

        Role names are normalized: lowercased, spaces replaced with underscores.
        Unknown roles or roles mapped to ``None`` get an unfiltered copy.
        """
        normalized = role.strip().lower().replace(" ", "_")
        allowed = ROLE_TOOL_SETS.get(normalized)
        if allowed is None:
            # No filtering — clone with all tools
            filtered = ToolRegistry()
            filtered.tools = dict(self.tools)
            return filtered

        filtered = ToolRegistry()
        for name, tool in self.tools.items():
            if name in allowed:
                filtered.tools[name] = tool
        logger.info(
            f"Filtered tools for role '{normalized}': "
            f"{len(filtered.tools)}/{len(self.tools)} tools"
        )
        return filtered

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

_LIGHTHOUSE_SEO_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "URL to audit for SEO"},
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Lighthouse categories to run (default: ['seo', 'performance', 'accessibility'])",
        },
    },
    "required": ["url"],
}

_PAGE_ANALYZER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "URL to analyze for SEO content quality"},
    },
    "required": ["url"],
}

_SEO_CHECKLIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "URL to validate against SEO best practices"},
        "checks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific checks to run (default: all). Options: meta, headers, content, technical",
        },
    },
    "required": ["url"],
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

    # --- Infrastructure service tools (env-gated) ---
    from .web_search import WebSearchTool
    from .web_scrape import WebScrapeTool
    from .browser_automation import BrowserAutomationTool
    from .design import DesignTool
    from .image_generation import ImageGenerationTool
    from .git_forge import GitForgeTool
    from .artifact_storage import ArtifactStorageTool

    if os.environ.get("SEARXNG_URL"):
        registry.register(WebSearchTool())
        logger.info("Tool registry: web_search enabled (SearXNG)")
    if os.environ.get("PLAYWRIGHT_WS_URL"):
        registry.register(WebScrapeTool())
        logger.info("Tool registry: web_scrape enabled (Playwright)")
        registry.register(BrowserAutomationTool())
        logger.info("Tool registry: browser_automation enabled (Playwright)")
    if os.environ.get("PENPOT_API_URL"):
        registry.register(DesignTool())
        logger.info("Tool registry: design enabled (Penpot)")
    if os.environ.get("COMFYUI_URL"):
        registry.register(ImageGenerationTool())
        logger.info("Tool registry: image_generation enabled (ComfyUI)")
    if os.environ.get("GITEA_URL"):
        registry.register(GitForgeTool())
        logger.info("Tool registry: git_forge enabled (Gitea)")
    if os.environ.get("MINIO_URL"):
        registry.register(ArtifactStorageTool())
        logger.info("Tool registry: artifact_storage enabled (MinIO)")
    if os.environ.get("MESSAGE_STORE_PATH") or os.environ.get("BULLETIN_PATH"):
        from .bulletin_board import BulletinBoardTool
        registry.register(BulletinBoardTool())
        logger.info("Tool registry: bulletin_board enabled")
    if os.environ.get("DATABASE_URL"):
        from .database import DatabaseTool
        registry.register(DatabaseTool())
        logger.info("Tool registry: database enabled")

    # --- Persistent memory tools (always-on) ---
    registry.register(MemoryStoreTool())
    registry.register(MemoryRecallTool())

    # --- File operation tools (always local) ---
    registry.register(FileReader(allowed_dirs=resolved_dirs))
    registry.register(FileWriter(allowed_dirs=resolved_dirs))

    # --- Dependency vulnerability scanner (always-on) ---
    from .dependency_scanner import DependencyScannerTool
    registry.register(DependencyScannerTool())
    logger.info("Tool registry: dependency_scanner enabled")

    # --- Container inspection (always-on, fails gracefully without Docker socket) ---
    from .container_inspect import ContainerInspectTool
    registry.register(ContainerInspectTool())
    logger.info("Tool registry: container_inspect enabled")

    # --- SEO tools (always-on, use Playwright/Lighthouse if available) ---
    from .seo_tools import LighthouseSEOTool, PageAnalyzerTool, SEOChecklistTool
    registry.register(DevToolWrapper(
        LighthouseSEOTool(), ToolCategory.WEB_API, _LIGHTHOUSE_SEO_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        PageAnalyzerTool(), ToolCategory.WEB_API, _PAGE_ANALYZER_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        SEOChecklistTool(), ToolCategory.SPECIALIZED, _SEO_CHECKLIST_SCHEMA,
    ))
    logger.info("Tool registry: SEO tools enabled (lighthouse_seo, page_analyzer, seo_checklist)")

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

    # --- Infrastructure service tools (env-gated) ---
    from .web_search import WebSearchTool
    from .web_scrape import WebScrapeTool
    from .browser_automation import BrowserAutomationTool
    from .design import DesignTool
    from .image_generation import ImageGenerationTool
    from .git_forge import GitForgeTool
    from .artifact_storage import ArtifactStorageTool

    if os.environ.get("SEARXNG_URL"):
        registry.register(WebSearchTool())
    if os.environ.get("PLAYWRIGHT_WS_URL"):
        registry.register(WebScrapeTool())
        registry.register(BrowserAutomationTool())
    if os.environ.get("PENPOT_API_URL"):
        registry.register(DesignTool())
    if os.environ.get("COMFYUI_URL"):
        registry.register(ImageGenerationTool())
    if os.environ.get("GITEA_URL"):
        registry.register(GitForgeTool())
    if os.environ.get("MINIO_URL"):
        registry.register(ArtifactStorageTool())
    if os.environ.get("MESSAGE_STORE_PATH") or os.environ.get("BULLETIN_PATH"):
        from .bulletin_board import BulletinBoardTool
        registry.register(BulletinBoardTool())
    if os.environ.get("DATABASE_URL"):
        from .database import DatabaseTool
        registry.register(DatabaseTool())

    # --- Persistent memory tools ---
    registry.register(MemoryStoreTool())
    registry.register(MemoryRecallTool())

    # --- File operation tools ---
    registry.register(FileReader(allowed_dirs=resolved_dirs))
    registry.register(FileWriter(allowed_dirs=resolved_dirs))

    # --- Dependency vulnerability scanner ---
    from .dependency_scanner import DependencyScannerTool
    registry.register(DependencyScannerTool())

    # --- Container inspection ---
    from .container_inspect import ContainerInspectTool
    registry.register(ContainerInspectTool())

    # --- SEO tools ---
    from .seo_tools import LighthouseSEOTool, PageAnalyzerTool, SEOChecklistTool
    registry.register(DevToolWrapper(
        LighthouseSEOTool(), ToolCategory.WEB_API, _LIGHTHOUSE_SEO_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        PageAnalyzerTool(), ToolCategory.WEB_API, _PAGE_ANALYZER_SCHEMA,
    ))
    registry.register(DevToolWrapper(
        SEOChecklistTool(), ToolCategory.SPECIALIZED, _SEO_CHECKLIST_SCHEMA,
    ))

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


__all__ = [
    # base.py
    "ToolCategory",
    "ToolResult",
    "Tool",
    # executors.py
    "PythonExecutor",
    "PytestRunner",
    "BanditScanner",
    "ShellExecutor",
    # file_tools.py
    "_DEFAULT_ALLOWED_FILE_DIRS",
    "_SELF_UPGRADE_DIR",
    "MAX_FILE_READ_SIZE",
    "MAX_FILE_WRITE_SIZE",
    "_build_allowed_file_dirs",
    "_validate_file_path",
    "FileReader",
    "FileWriter",
    # web_tools.py
    "DevToolWrapper",
    "WebFetchTool",
    # memory_tools.py
    "MemoryStoreTool",
    "MemoryRecallTool",
    "_get_shared_memory_store",
    # This file
    "ROLE_TOOL_SETS",
    "ToolRegistry",
    "create_default_tool_registry",
    "create_subprocess_tool_registry",
]
