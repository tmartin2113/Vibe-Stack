"""Penpot Design Tool — interact with Penpot design projects via its REST API."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class DesignTool(Tool):
    """Interact with a self-hosted Penpot instance for UI/UX design.

    Supports listing projects, creating files, listing components, and
    exporting assets.  Penpot provides a full design system with
    real-time collaboration, components, and design tokens.

    Requires ``PENPOT_API_URL`` environment variable pointing to the
    Penpot backend (e.g. ``http://penpot-backend:6060``).
    """

    def __init__(self, api_url: Optional[str] = None):
        super().__init__(
            name="design",
            description=(
                "Interact with Penpot design tool: list projects, create design files, "
                "list components, and export assets. Use for UI/UX design tasks."
            ),
            category=ToolCategory.EXTERNAL_SERVICE,
        )
        self._api_url = (api_url or os.environ.get("PENPOT_API_URL", "")).rstrip("/")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Design action: list_projects, get_project, create_file, "
                        "list_components, export_asset"
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": "Project UUID (for project-specific actions)",
                },
                "file_id": {
                    "type": "string",
                    "description": "File UUID (for file-specific actions)",
                },
                "name": {
                    "type": "string",
                    "description": "Name for new file or project",
                },
                "component_id": {
                    "type": "string",
                    "description": "Component UUID (for export_asset)",
                },
                "format": {
                    "type": "string",
                    "description": "Export format: svg, png, pdf (default: svg)",
                    "default": "svg",
                },
            },
            "required": ["action"],
        }

    def execute(
        self,
        action: str,
        project_id: str = "",
        file_id: str = "",
        name: str = "",
        component_id: str = "",
        format: str = "svg",
        **kwargs: Any,
    ) -> ToolResult:
        if not action or not action.strip():
            return ToolResult(success=False, output="", error="No action provided")
        if not self._api_url:
            return ToolResult(
                success=False, output="",
                error="PENPOT_API_URL not set. Configure the Penpot backend URL.",
            )

        valid_actions = {"list_projects", "get_project", "create_file", "list_components", "export_asset"}
        if action not in valid_actions:
            return ToolResult(
                success=False, output="",
                error=f"Invalid action '{action}'. Valid: {', '.join(sorted(valid_actions))}",
            )

        try:
            if action == "list_projects":
                return self._rpc("get-projects", {})

            elif action == "get_project":
                if not project_id:
                    return ToolResult(success=False, output="", error="project_id required")
                return self._rpc("get-project", {"id": project_id})

            elif action == "create_file":
                if not project_id:
                    return ToolResult(success=False, output="", error="project_id required")
                if not name:
                    return ToolResult(success=False, output="", error="name required")
                return self._rpc("create-file", {
                    "project-id": project_id,
                    "name": name,
                })

            elif action == "list_components":
                if not file_id:
                    return ToolResult(success=False, output="", error="file_id required")
                return self._rpc("get-file-components", {"file-id": file_id})

            elif action == "export_asset":
                if not file_id or not component_id:
                    return ToolResult(
                        success=False, output="",
                        error="file_id and component_id required",
                    )
                return self._rpc("export", {
                    "file-id": file_id,
                    "object-id": component_id,
                    "type": format,
                })

            return ToolResult(success=False, output="", error=f"Unhandled action: {action}")

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Penpot API call failed: {e}",
            )

    def _rpc(self, cmd: str, params: Dict[str, Any]) -> ToolResult:
        """Call a Penpot RPC command."""
        payload = json.dumps(params).encode()
        req = urllib.request.Request(
            f"{self._api_url}/api/rpc/command/{cmd}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Vibe/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        output = json.dumps(data, indent=2, default=str)
        return ToolResult(
            success=True,
            output=output,
            metadata={"command": cmd},
        )
