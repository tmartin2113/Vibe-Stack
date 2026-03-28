"""Gitea Git Forge Tool — manage repositories and code via a local Gitea instance."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class GitForgeTool(Tool):
    """Interact with a self-hosted Gitea instance for Git operations.

    Supports creating repos, listing repos, reading files, creating
    commits, managing branches, and creating issues/PRs via the
    Gitea REST API.

    Requires ``GITEA_URL`` environment variable pointing to the Gitea
    instance (e.g. ``http://gitea:3000``).
    """

    def __init__(self, base_url: Optional[str] = None):
        super().__init__(
            name="git_forge",
            description=(
                "Interact with Gitea git forge: create repos, list repos, read files, "
                "create commits, manage branches, create issues and pull requests."
            ),
            category=ToolCategory.EXTERNAL_SERVICE,
        )
        self._base_url = (base_url or os.environ.get("GITEA_URL", "")).rstrip("/")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Git forge action: list_repos, create_repo, get_file, "
                        "create_file, update_file, list_branches, create_branch, "
                        "create_issue, list_issues, create_pull_request"
                    ),
                },
                "owner": {
                    "type": "string",
                    "description": "Repository owner/organization name",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository name",
                },
                "path": {
                    "type": "string",
                    "description": "File path within the repository",
                },
                "content": {
                    "type": "string",
                    "description": "File content (for create_file/update_file) or issue/PR body",
                },
                "message": {
                    "type": "string",
                    "description": "Commit message (for create_file/update_file)",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch name (default: main)",
                    "default": "main",
                },
                "title": {
                    "type": "string",
                    "description": "Title for issue or pull request",
                },
                "name": {
                    "type": "string",
                    "description": "Name for new repo or branch",
                },
                "base": {
                    "type": "string",
                    "description": "Base branch for pull request (default: main)",
                    "default": "main",
                },
                "head": {
                    "type": "string",
                    "description": "Head branch for pull request",
                },
                "private": {
                    "type": "boolean",
                    "description": "Whether new repo should be private (default: false)",
                    "default": False,
                },
            },
            "required": ["action"],
        }

    def execute(
        self,
        action: str,
        owner: str = "",
        repo: str = "",
        path: str = "",
        content: str = "",
        message: str = "",
        branch: str = "main",
        title: str = "",
        name: str = "",
        base: str = "main",
        head: str = "",
        private: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        if not action or not action.strip():
            return ToolResult(success=False, output="", error="No action provided")
        if not self._base_url:
            return ToolResult(
                success=False, output="",
                error="GITEA_URL not set. Configure the Gitea service URL.",
            )

        valid_actions = {
            "list_repos", "create_repo", "get_file", "create_file",
            "update_file", "list_branches", "create_branch",
            "create_issue", "list_issues", "create_pull_request",
        }
        if action not in valid_actions:
            return ToolResult(
                success=False, output="",
                error=f"Invalid action '{action}'. Valid: {', '.join(sorted(valid_actions))}",
            )

        try:
            if action == "list_repos":
                return self._api_get("/api/v1/repos/search?limit=50")

            elif action == "create_repo":
                if not name:
                    return ToolResult(success=False, output="", error="name required")
                return self._api_post("/api/v1/user/repos", {
                    "name": name,
                    "private": private,
                    "auto_init": True,
                    "default_branch": "main",
                })

            elif action == "get_file":
                if not owner or not repo or not path:
                    return ToolResult(success=False, output="", error="owner, repo, and path required")
                return self._api_get(f"/api/v1/repos/{owner}/{repo}/contents/{path}?ref={branch}")

            elif action == "create_file":
                if not owner or not repo or not path or not content:
                    return ToolResult(success=False, output="", error="owner, repo, path, and content required")
                import base64
                return self._api_post(f"/api/v1/repos/{owner}/{repo}/contents/{path}", {
                    "content": base64.b64encode(content.encode()).decode(),
                    "message": message or f"Create {path}",
                    "branch": branch,
                })

            elif action == "update_file":
                if not owner or not repo or not path or not content:
                    return ToolResult(success=False, output="", error="owner, repo, path, and content required")
                # Get current file SHA first
                existing = self._api_get_raw(f"/api/v1/repos/{owner}/{repo}/contents/{path}?ref={branch}")
                sha = existing.get("sha", "") if isinstance(existing, dict) else ""
                import base64
                return self._api_put(f"/api/v1/repos/{owner}/{repo}/contents/{path}", {
                    "content": base64.b64encode(content.encode()).decode(),
                    "message": message or f"Update {path}",
                    "branch": branch,
                    "sha": sha,
                })

            elif action == "list_branches":
                if not owner or not repo:
                    return ToolResult(success=False, output="", error="owner and repo required")
                return self._api_get(f"/api/v1/repos/{owner}/{repo}/branches")

            elif action == "create_branch":
                if not owner or not repo or not name:
                    return ToolResult(success=False, output="", error="owner, repo, and name required")
                return self._api_post(f"/api/v1/repos/{owner}/{repo}/branches", {
                    "new_branch_name": name,
                    "old_branch_name": branch,
                })

            elif action == "create_issue":
                if not owner or not repo or not title:
                    return ToolResult(success=False, output="", error="owner, repo, and title required")
                return self._api_post(f"/api/v1/repos/{owner}/{repo}/issues", {
                    "title": title,
                    "body": content,
                })

            elif action == "list_issues":
                if not owner or not repo:
                    return ToolResult(success=False, output="", error="owner and repo required")
                return self._api_get(f"/api/v1/repos/{owner}/{repo}/issues?state=open&limit=20")

            elif action == "create_pull_request":
                if not owner or not repo or not title or not head:
                    return ToolResult(success=False, output="", error="owner, repo, title, and head required")
                return self._api_post(f"/api/v1/repos/{owner}/{repo}/pulls", {
                    "title": title,
                    "body": content,
                    "base": base,
                    "head": head,
                })

            return ToolResult(success=False, output="", error=f"Unhandled action: {action}")

        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Gitea API call failed: {e}",
            )

    def _get_token(self) -> str:
        """Get Gitea API token from environment."""
        return os.environ.get("GITEA_API_TOKEN", "")

    def _api_get(self, path: str) -> ToolResult:
        """Make a GET request to the Gitea API."""
        data = self._api_get_raw(path)
        output = json.dumps(data, indent=2, default=str)
        return ToolResult(success=True, output=output, metadata={"path": path})

    def _api_get_raw(self, path: str) -> Any:
        """Make a GET request and return raw parsed JSON."""
        req = urllib.request.Request(
            f"{self._base_url}{path}",
            headers={"User-Agent": "Vibe/1.0"},
        )
        token = self._get_token()
        if token:
            req.add_header("Authorization", f"token {token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def _api_post(self, path: str, data: Dict[str, Any]) -> ToolResult:
        """Make a POST request to the Gitea API."""
        return self._api_mutate("POST", path, data)

    def _api_put(self, path: str, data: Dict[str, Any]) -> ToolResult:
        """Make a PUT request to the Gitea API."""
        return self._api_mutate("PUT", path, data)

    def _api_mutate(self, method: str, path: str, data: Dict[str, Any]) -> ToolResult:
        """Make a POST/PUT request to the Gitea API."""
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self._base_url}{path}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Vibe/1.0",
            },
            method=method,
        )
        token = self._get_token()
        if token:
            req.add_header("Authorization", f"token {token}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        output = json.dumps(result, indent=2, default=str)
        return ToolResult(success=True, output=output, metadata={"path": path, "method": method})
