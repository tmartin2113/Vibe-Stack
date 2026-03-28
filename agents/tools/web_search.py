"""SearXNG Web Search Tool — search the web via a local SearXNG instance."""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class WebSearchTool(Tool):
    """Search the web using a self-hosted SearXNG instance.

    Returns structured search results (title, URL, snippet) in a format
    suitable for LLM consumption.  SearXNG aggregates results from
    multiple engines (Google, DuckDuckGo, GitHub, StackOverflow, etc.).

    Requires ``SEARXNG_URL`` environment variable pointing to the SearXNG
    instance (e.g. ``http://searxng:8080``).
    """

    def __init__(self, base_url: Optional[str] = None):
        super().__init__(
            name="web_search",
            description=(
                "Search the web and return results with titles, URLs, and snippets. "
                "Use for finding documentation, answers, libraries, or any web content."
            ),
            category=ToolCategory.WEB_API,
        )
        self._base_url = (base_url or os.environ.get("SEARXNG_URL", "")).rstrip("/")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "categories": {
                    "type": "string",
                    "description": (
                        "Comma-separated search categories: general, science, it, "
                        "files, images, news, social_media (default: general)"
                    ),
                    "default": "general",
                },
                "engines": {
                    "type": "string",
                    "description": (
                        "Comma-separated engine names to use (e.g. 'google,github'). "
                        "Leave empty for default engines."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 10, max 20)",
                    "default": 10,
                },
            },
            "required": ["query"],
        }

    def execute(
        self,
        query: str,
        categories: str = "general",
        engines: str = "",
        limit: int = 10,
        **kwargs: Any,
    ) -> ToolResult:
        if not query or not query.strip():
            return ToolResult(success=False, output="", error="No query provided")
        if not self._base_url:
            return ToolResult(
                success=False, output="",
                error="SEARXNG_URL not set. Configure the SearXNG service URL.",
            )

        limit = max(1, min(limit, 20))

        params: Dict[str, str] = {
            "q": query,
            "format": "json",
            "categories": categories,
        }
        if engines:
            params["engines"] = engines

        url = f"{self._base_url}/search?{urllib.parse.urlencode(params)}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Vibe/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            results: List[Dict[str, Any]] = data.get("results", [])[:limit]

            if not results:
                return ToolResult(
                    success=True,
                    output=f"No results found for: {query}",
                    metadata={"query": query, "results": 0},
                )

            sections = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "(untitled)")
                result_url = r.get("url", "")
                snippet = r.get("content", "")
                engine = r.get("engine", "")
                section = f"### {i}. {title}"
                if result_url:
                    section += f"\n> {result_url}"
                if engine:
                    section += f" ({engine})"
                if snippet:
                    section += f"\n\n{snippet}"
                sections.append(section)

            combined = "\n\n---\n\n".join(sections)

            return ToolResult(
                success=True,
                output=combined,
                metadata={
                    "query": query,
                    "results": len(results),
                    "categories": categories,
                },
            )

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"SearXNG search failed: {e}",
            )
