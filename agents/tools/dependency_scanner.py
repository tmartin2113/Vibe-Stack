"""Dependency Vulnerability Scanner — audit Python and Node.js dependencies for known CVEs."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class DependencyScannerTool(Tool):
    """Scan project dependencies for known security vulnerabilities.

    Supports Python (pip-audit) and Node.js (npm audit) projects.
    Automatically detects project type from lock/manifest files.
    Falls back to available scanners when one is not installed.
    """

    def __init__(self):
        super().__init__(
            name="dependency_scanner",
            description=(
                "Scan project dependencies for known security vulnerabilities (CVEs). "
                "Supports Python (pip-audit) and Node.js (npm audit). Provide a project "
                "directory path to scan."
            ),
            category=ToolCategory.CODE_EXECUTION,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the project directory to scan",
                },
                "ecosystem": {
                    "type": "string",
                    "description": "Force ecosystem: 'python', 'node', or 'auto' (default: auto-detect)",
                    "default": "auto",
                },
                "fix": {
                    "type": "boolean",
                    "description": "Attempt to auto-fix vulnerabilities (default: false)",
                    "default": False,
                },
            },
            "required": ["path"],
        }

    def execute(
        self,
        path: str,
        ecosystem: str = "auto",
        fix: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        if not path or not path.strip():
            return ToolResult(success=False, output="", error="No path provided")

        path = os.path.abspath(path)
        if not os.path.isdir(path):
            return ToolResult(success=False, output="", error=f"Directory not found: {path}")

        results = []
        ecosystems = self._detect_ecosystems(path) if ecosystem == "auto" else [ecosystem]

        if not ecosystems:
            return ToolResult(
                success=True,
                output="No Python or Node.js project files found in the directory.",
                metadata={"path": path, "ecosystems_checked": []},
            )

        total_vulns = 0

        for eco in ecosystems:
            if eco == "python":
                result = self._scan_python(path, fix)
            elif eco == "node":
                result = self._scan_node(path, fix)
            else:
                result = {"ecosystem": eco, "error": f"Unknown ecosystem: {eco}"}

            results.append(result)
            total_vulns += result.get("vulnerability_count", 0)

        output_lines = []
        for r in results:
            eco = r.get("ecosystem", "unknown")
            if "error" in r:
                output_lines.append(f"## {eco.title()}\n\nError: {r['error']}")
            elif r.get("vulnerability_count", 0) == 0:
                output_lines.append(f"## {eco.title()}\n\nNo known vulnerabilities found.")
            else:
                count = r["vulnerability_count"]
                output_lines.append(f"## {eco.title()}\n\n**{count} vulnerabilities found:**\n")
                output_lines.append(r.get("details", ""))

        return ToolResult(
            success=True,
            output="\n\n".join(output_lines),
            metadata={
                "path": path,
                "ecosystems": ecosystems,
                "total_vulnerabilities": total_vulns,
            },
        )

    def _detect_ecosystems(self, path: str) -> list[str]:
        """Detect which ecosystems are present based on project files."""
        found = []
        python_markers = ("requirements.txt", "pyproject.toml", "Pipfile.lock", "setup.py", "setup.cfg")
        node_markers = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml")

        for marker in python_markers:
            if os.path.exists(os.path.join(path, marker)):
                found.append("python")
                break
        for marker in node_markers:
            if os.path.exists(os.path.join(path, marker)):
                found.append("node")
                break
        return found

    def _scan_python(self, path: str, fix: bool) -> dict:
        """Run pip-audit on a Python project."""
        cmd = [sys.executable, "-m", "pip_audit", "--format", "json", "--desc"]
        if fix:
            cmd.append("--fix")

        # Check for requirements file
        req_file = None
        for candidate in ("requirements.txt", "requirements-production.lock"):
            full = os.path.join(path, candidate)
            if os.path.exists(full):
                req_file = full
                break

        if req_file:
            cmd.extend(["--requirement", req_file])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=path,
            )

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                # pip-audit may output non-JSON on error
                if result.returncode != 0 and result.stderr:
                    return {"ecosystem": "python", "error": result.stderr.strip()}
                return {
                    "ecosystem": "python",
                    "vulnerability_count": 0,
                    "details": result.stdout or "No output from pip-audit",
                }

            vulns = data.get("dependencies", [])
            vuln_count = sum(len(dep.get("vulns", [])) for dep in vulns if dep.get("vulns"))
            details_lines = []
            for dep in vulns:
                for vuln in dep.get("vulns", []):
                    details_lines.append(
                        f"- **{dep['name']}** {dep.get('version', '?')}: "
                        f"{vuln.get('id', 'unknown')} — {vuln.get('description', 'no description')}"
                        f" (fix: {vuln.get('fix_versions', ['unknown'])})"
                    )

            return {
                "ecosystem": "python",
                "vulnerability_count": vuln_count,
                "details": "\n".join(details_lines) if details_lines else "No vulnerabilities.",
            }

        except FileNotFoundError:
            return {"ecosystem": "python", "error": "pip-audit not installed. Install with: pip install pip-audit"}
        except subprocess.TimeoutExpired:
            return {"ecosystem": "python", "error": "pip-audit timed out after 120s"}
        except Exception as e:
            return {"ecosystem": "python", "error": str(e)}

    def _scan_node(self, path: str, fix: bool) -> dict:
        """Run npm audit on a Node.js project."""
        cmd = ["npm", "audit", "--json"]
        if fix:
            cmd.append("--fix")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=path,
            )

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                if result.stderr:
                    return {"ecosystem": "node", "error": result.stderr.strip()}
                return {
                    "ecosystem": "node",
                    "vulnerability_count": 0,
                    "details": result.stdout or "No output from npm audit",
                }

            # npm audit v2+ format
            vulns_meta = data.get("metadata", {}).get("vulnerabilities", {})
            vuln_count = sum(vulns_meta.get(sev, 0) for sev in ("low", "moderate", "high", "critical"))

            details_lines = []
            advisories = data.get("vulnerabilities", {})
            for name, info in advisories.items():
                severity = info.get("severity", "unknown")
                via = info.get("via", [])
                title = via[0].get("title", "unknown") if via and isinstance(via[0], dict) else str(via)
                fix_avail = info.get("fixAvailable", False)
                details_lines.append(
                    f"- **{name}** [{severity}]: {title}"
                    f" (fix available: {fix_avail})"
                )

            return {
                "ecosystem": "node",
                "vulnerability_count": vuln_count,
                "details": "\n".join(details_lines) if details_lines else "No vulnerabilities.",
            }

        except FileNotFoundError:
            return {"ecosystem": "node", "error": "npm not found in PATH"}
        except subprocess.TimeoutExpired:
            return {"ecosystem": "node", "error": "npm audit timed out after 120s"}
        except Exception as e:
            return {"ecosystem": "node", "error": str(e)}
