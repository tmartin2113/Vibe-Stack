"""Playwright Browser Automation Tool — automate browsers via a remote Playwright server."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class BrowserAutomationTool(Tool):
    """Automate browser interactions via a remote Playwright server.

    Connects to a Playwright run-server instance over WebSocket CDP.
    Agents can navigate pages, fill forms, click elements, take screenshots,
    and extract content from rendered pages.

    Requires ``PLAYWRIGHT_WS_URL`` environment variable pointing to the
    Playwright server (e.g. ``ws://playwright:3003``).
    """

    def __init__(self, ws_url: Optional[str] = None):
        super().__init__(
            name="browser_automation",
            description=(
                "Automate a web browser: navigate to URLs, click elements, fill forms, "
                "extract text, and take screenshots. Use for interactive web tasks "
                "that require JavaScript rendering or user interaction simulation."
            ),
            category=ToolCategory.WEB_API,
        )
        self._ws_url = ws_url or os.environ.get("PLAYWRIGHT_WS_URL", "")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Browser action: navigate, click, fill, get_text, screenshot, "
                        "evaluate, wait_for_selector"
                    ),
                },
                "url": {
                    "type": "string",
                    "description": "URL to navigate to (for 'navigate' action)",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the target element (for click, fill, get_text, wait_for_selector)",
                },
                "value": {
                    "type": "string",
                    "description": "Value to fill (for 'fill' action) or JS expression (for 'evaluate')",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Action timeout in milliseconds (default 30000)",
                    "default": 30000,
                },
            },
            "required": ["action"],
        }

    def execute(
        self,
        action: str,
        url: str = "",
        selector: str = "",
        value: str = "",
        timeout: int = 30000,
        **kwargs: Any,
    ) -> ToolResult:
        if not action or not action.strip():
            return ToolResult(success=False, output="", error="No action provided")
        if not self._ws_url:
            return ToolResult(
                success=False, output="",
                error="PLAYWRIGHT_WS_URL not set. Configure the Playwright server URL.",
            )

        valid_actions = {"navigate", "click", "fill", "get_text", "screenshot", "evaluate", "wait_for_selector"}
        if action not in valid_actions:
            return ToolResult(
                success=False, output="",
                error=f"Invalid action '{action}'. Valid: {', '.join(sorted(valid_actions))}",
            )

        # Build a Playwright script that connects to the remote server
        script = self._build_script(action, url, selector, value, timeout)

        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=(timeout // 1000) + 15,
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
                        output=data.get("result", output),
                        metadata=data.get("metadata", {"action": action}),
                    )
                except json.JSONDecodeError:
                    return ToolResult(
                        success=True,
                        output=output,
                        metadata={"action": action},
                    )

            return ToolResult(
                success=False,
                output=result.stdout,
                error=result.stderr,
                metadata={"action": action},
            )

        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False, output="",
                error=f"Browser action '{action}' timed out",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Browser automation failed: {e}",
            )

    def _build_script(
        self, action: str, url: str, selector: str, value: str, timeout: int
    ) -> str:
        """Build a self-contained Python script for the Playwright action."""
        # JSON-encode values to prevent injection
        url_json = json.dumps(url)
        selector_json = json.dumps(selector)
        value_json = json.dumps(value)
        timeout_json = json.dumps(timeout)

        return f"""
import asyncio, json, os

async def main():
    from playwright.async_api import async_playwright
    ws_url = os.environ["PLAYWRIGHT_WS_URL"]
    async with async_playwright() as p:
        browser = await p.chromium.connect(ws_url)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout({timeout_json})

        action = {json.dumps(action)}
        result = ""
        metadata = {{"action": action}}

        if action == "navigate":
            resp = await page.goto({url_json}, wait_until="domcontentloaded")
            result = f"Navigated to {{page.url}}"
            metadata["status"] = resp.status if resp else None
            metadata["title"] = await page.title()

        elif action == "click":
            await page.click({selector_json})
            result = f"Clicked {{repr({selector_json})}}"

        elif action == "fill":
            await page.fill({selector_json}, {value_json})
            result = f"Filled {{repr({selector_json})}} with value"

        elif action == "get_text":
            if {selector_json}:
                el = await page.query_selector({selector_json})
                result = await el.text_content() if el else "Element not found"
            else:
                result = await page.content()

        elif action == "screenshot":
            buf = await page.screenshot(full_page=True)
            import base64
            result = base64.b64encode(buf).decode()
            metadata["format"] = "base64-png"

        elif action == "evaluate":
            result = str(await page.evaluate({value_json}))

        elif action == "wait_for_selector":
            await page.wait_for_selector({selector_json})
            result = f"Selector {{repr({selector_json})}} found"

        await context.close()
        await browser.close()
        print(json.dumps({{"result": result, "metadata": metadata}}))

asyncio.run(main())
"""
