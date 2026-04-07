"""Web Scraping Tool — scrape web pages via a remote Playwright server."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class WebScrapeTool(Tool):
    """Scrape web pages and return clean content using Playwright.

    Handles JavaScript-rendered pages via a remote Playwright server.
    Use this instead of web_fetch when you need rendered page content
    from JavaScript-heavy sites.

    Requires ``PLAYWRIGHT_WS_URL`` environment variable pointing to the
    Playwright server (e.g. ``ws://playwright:3003``).
    """

    def __init__(self, ws_url: Optional[str] = None):
        super().__init__(
            name="web_scrape",
            description=(
                "Scrape a web page and return its content. Handles JavaScript-rendered "
                "pages via headless browser. Returns clean text or markdown."
            ),
            category=ToolCategory.WEB_API,
        )
        # Only fall back to env when caller didn't pass anything. Empty string is
        # an explicit "no URL" — preserves it so the missing-URL error path fires.
        self._ws_url = ws_url if ws_url is not None else os.environ.get("PLAYWRIGHT_WS_URL", "")

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
                    "description": "Output format: markdown, text, html (default: markdown)",
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

    def execute(
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
        if not self._ws_url:
            return ToolResult(
                success=False, output="",
                error="PLAYWRIGHT_WS_URL not set. Configure the Playwright server URL.",
            )

        script = self._build_script(url, return_format, timeout)

        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=timeout + 15,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", "/tmp"),
                    "PLAYWRIGHT_WS_URL": self._ws_url,
                },
            )

            if result.returncode == 0:
                output = result.stdout.strip()
                try:
                    data = json.loads(output)
                    return ToolResult(
                        success=True,
                        output=data.get("content", output),
                        metadata=data.get("metadata", {"url": url}),
                    )
                except json.JSONDecodeError:
                    return ToolResult(
                        success=True,
                        output=output,
                        metadata={"url": url},
                    )

            return ToolResult(
                success=False,
                output=result.stdout,
                error=result.stderr,
            )

        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False, output="",
                error=f"Web scrape of {url} timed out after {timeout}s",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Web scrape failed: {e}",
            )

    def _build_script(self, url: str, return_format: str, timeout: int) -> str:
        """Build a self-contained Python script for scraping via Playwright."""
        url_json = json.dumps(url)
        fmt_json = json.dumps(return_format)
        timeout_ms = timeout * 1000

        return f"""
import asyncio, json, os, re

def html_to_markdown(html):
    \"\"\"Lightweight HTML to markdown conversion.\"\"\"
    import re as _re
    text = html
    # Remove script/style
    text = _re.sub(r'<(script|style)[^>]*>.*?</\\1>', '', text, flags=_re.DOTALL | _re.IGNORECASE)
    # Headers
    for i in range(1, 7):
        text = _re.sub(rf'<h{{i}}[^>]*>(.*?)</h{{i}}>', rf'{{"#" * i}} \\1\\n', text, flags=_re.DOTALL | _re.IGNORECASE)
    # Paragraphs / divs / br
    text = _re.sub(r'<br\\s*/?>', '\\n', text, flags=_re.IGNORECASE)
    text = _re.sub(r'</(p|div|li|tr)>', '\\n', text, flags=_re.IGNORECASE)
    # Links
    text = _re.sub(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', r'[\\2](\\1)', text, flags=_re.DOTALL | _re.IGNORECASE)
    # Bold / italic
    text = _re.sub(r'<(b|strong)[^>]*>(.*?)</\\1>', r'**\\2**', text, flags=_re.DOTALL | _re.IGNORECASE)
    text = _re.sub(r'<(i|em)[^>]*>(.*?)</\\1>', r'*\\2*', text, flags=_re.DOTALL | _re.IGNORECASE)
    # Strip remaining tags
    text = _re.sub(r'<[^>]+>', '', text)
    # Collapse whitespace
    text = _re.sub(r'[ \\t]+', ' ', text)
    text = _re.sub(r'\\n{{3,}}', '\\n\\n', text)
    return text.strip()

async def main():
    from playwright.async_api import async_playwright
    ws_url = os.environ["PLAYWRIGHT_WS_URL"]
    async with async_playwright() as p:
        browser = await p.chromium.connect(ws_url)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout({timeout_ms})

        resp = await page.goto({url_json}, wait_until="networkidle")
        title = await page.title()
        page_url = page.url

        fmt = {fmt_json}
        if fmt == "html":
            content = await page.content()
        elif fmt == "text":
            content = await page.evaluate("() => document.body.innerText")
        else:
            # markdown
            html = await page.evaluate("() => document.body.innerHTML")
            content = html_to_markdown(html)

        await context.close()
        await browser.close()

        metadata = {{
            "url": page_url,
            "title": title,
            "format": fmt,
            "length": len(content),
            "status": resp.status if resp else None,
        }}
        print(json.dumps({{"content": content, "metadata": metadata}}))

asyncio.run(main())
"""
