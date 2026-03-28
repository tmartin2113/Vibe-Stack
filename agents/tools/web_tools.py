"""
Web tools: WebFetchTool and DevToolWrapper.

WebFetchTool fetches URLs via subprocess isolation.
DevToolWrapper bridges extended dev/seo tools to the Tool ABC interface.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import json
import logging
import subprocess

from .base import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


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

    def execute(self, **kwargs) -> ToolResult:
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

    Uses a Python subprocess with urllib (stdlib -- no extra deps).
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

    def execute(self, url: str, timeout: int = 15, **kwargs) -> ToolResult:
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


__all__ = [
    "DevToolWrapper",
    "WebFetchTool",
]
