"""Rate-limited web search wrapper for senior engineers.

Senior engineers get this instead of unrestricted web_search/web_fetch.
Enforces a per-session call limit to prevent engineers from doing
broad research that should be delegated to DeerFlow assistants.

DeerFlow assistants (running on free local vLLM) retain full
web_search/web_fetch access.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)

# Default limit per heartbeat session
_DEFAULT_MAX_LOOKUPS = 1


class QuickLookupTool(Tool):
    """Rate-limited web search for senior engineers.

    Wraps an underlying WebSearchTool but enforces a per-session call limit.
    When the limit is reached, returns an error directing the engineer to
    delegate further research to their DeerFlow assistant.

    Args:
        search_tool: The underlying WebSearchTool instance to delegate to.
        max_lookups: Maximum searches allowed per session (default: 1).
    """

    def __init__(
        self,
        search_tool: Tool,
        max_lookups: int = _DEFAULT_MAX_LOOKUPS,
    ):
        super().__init__(
            name="quick_lookup",
            description=(
                "One-shot web search for a specific error message, API signature, "
                "or version check. Limited to {limit} use(s) per session — for "
                "broader research, create a subtask for your DeerFlow assistant."
            ).format(limit=max_lookups),
            category=ToolCategory.WEB_API,
        )
        self._search_tool = search_tool
        self._max_lookups = max_lookups
        self._call_count = 0
        self._lock = threading.Lock()

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — be specific (error message, API name, version)",
                },
            },
            "required": ["query"],
        }

    def reset(self) -> None:
        """Reset the call counter (called at heartbeat start)."""
        with self._lock:
            self._call_count = 0

    def execute(self, query: str = "", **kwargs: Any) -> ToolResult:
        if not query or not query.strip():
            return ToolResult(success=False, output="", error="No query provided")

        with self._lock:
            if self._call_count >= self._max_lookups:
                return ToolResult(
                    success=False,
                    output="",
                    error=(
                        f"Lookup limit reached ({self._max_lookups} per session). "
                        "Create a research subtask for your DeerFlow assistant instead."
                    ),
                )
            self._call_count += 1
            current = self._call_count

        logger.info(
            "QuickLookup %d/%d: %s",
            current, self._max_lookups, query[:80],
        )

        return self._search_tool.execute(
            query=query,
            categories="general",
            limit=5,
            **kwargs,
        )
