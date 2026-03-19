"""Spider Web Scraping Tool — scrape web pages via a local Spider instance."""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class WebScrapeTool(Tool):
    """Scrape web pages using a self-hosted Spider instance.

    Spider handles JavaScript rendering via CDP and returns clean content.
    Use this instead of web_fetch when you need rendered page content
    from JavaScript-heavy sites.

    Requires ``SPIDER_URL`` environment variable pointing to the Spider
    instance (e.g. ``http://spider:3002``).
    """

    def __init__(self, base_url: Optional[str] = None):
        super().__init__(
            name="web_scrape",
            description=(
                "Scrape a web page and return its content. Handles JavaScript-rendered "
                "pages via headless browser. Returns clean text or markdown."
            ),
            category=ToolCategory.WEB_API,
        )
        self._base_url = (base_url or os.environ.get("SPIDER_URL", "")).rstrip("/")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to scrape (http or https)",
                },
                "return_format": {
                    "type": "string",
                    "description": "Output format: markdown, text, html, raw (default: markdown)",
                    "default": "markdown",
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
        return_format: str = "markdown",
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
        if not self._base_url:
            return ToolResult(
                success=False, output="",
                error="SPIDER_URL not set. Configure the Spider service URL.",
            )

        try:
            payload = json.dumps({
                "url": url,
                "return_format": return_format,
            }).encode()

            req = urllib.request.Request(
                f"{self._base_url}/crawl",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Vibe/1.0",
                },
                method="POST",
            )

            api_key = os.environ.get("SPIDER_API_KEY", "")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")

            with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
                data = json.loads(resp.read().decode())

            # Spider returns an array of results
            if isinstance(data, list) and data:
                content = data[0].get("content", "")
                page_url = data[0].get("url", url)
            elif isinstance(data, dict):
                content = data.get("content", data.get("markdown", ""))
                page_url = data.get("url", url)
            else:
                content = str(data)
                page_url = url

            if not content:
                return ToolResult(
                    success=True,
                    output="Page returned no content.",
                    metadata={"url": page_url},
                )

            return ToolResult(
                success=True,
                output=content,
                metadata={
                    "url": page_url,
                    "format": return_format,
                    "length": len(content),
                },
            )

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Spider scrape failed: {e}",
            )
