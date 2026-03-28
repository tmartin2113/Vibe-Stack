"""
Static Code Analysis Tool

Lint and analyze code quality using available static analysis tools.
Supports Python (ruff, pylint, mypy, pyflakes) and JavaScript/TypeScript (eslint).
"""

import subprocess
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass


@dataclass
class CodeIssue:
    """Represents a code quality issue"""
    severity: str  # "error", "warning", "info"
    file: str
    line: int
    column: int
    message: str
    rule: str
    suggestion: Optional[str] = None


class StaticCodeAnalyzer:
    """
    Analyze code quality using static analysis tools.

    Supports:
    - Python: pylint, ruff, mypy, pyflakes
    - JavaScript/TypeScript: eslint (if available)
    - Multiple files or directories

    All tools are optional - will use whatever is installed.
    """

    def __init__(self):
        self.name = "static_code_analyzer"
        self.description = "Run static code analysis to find bugs, style issues, and type errors. Supports Python, JavaScript, TypeScript."

    def _check_tool_availability(self) -> Dict[str, bool]:
        """Check which linting tools are available"""
        tools = {}
        for tool in ['pylint', 'ruff', 'mypy', 'pyflakes', 'eslint']:
            result = subprocess.run(['which', tool], capture_output=True)
            tools[tool] = result.returncode == 0
        return tools

    def execute(
        self,
        path: str,
        tools: Optional[List[str]] = None,
        include_style: bool = True,
        max_issues: int = 50
    ) -> Dict[str, Any]:
        """
        Analyze code at the given path.

        Args:
            path: File or directory to analyze
            tools: List of tools to use (None = auto-detect)
            include_style: Include style issues or only errors/warnings
            max_issues: Maximum number of issues to return

        Returns:
            Dictionary with analysis results
        """
        try:
            target_path = Path(path)
            if not target_path.exists():
                return {
                    "success": False,
                    "error": f"Path does not exist: {path}"
                }

            available_tools = self._check_tool_availability()

            # Determine language
            if target_path.is_file():
                language = self._detect_language(target_path)
            else:
                language = "mixed"

            # Select appropriate tools
            if tools is None:
                if language == "python":
                    tools = ['ruff', 'pylint', 'mypy']  # Prefer ruff (fastest)
                elif language in ["javascript", "typescript"]:
                    tools = ['eslint']
                else:
                    tools = ['ruff', 'pylint']  # Default to Python tools

            # Filter to only available tools
            tools_to_run = [t for t in tools if available_tools.get(t, False)]

            if not tools_to_run:
                return {
                    "success": False,
                    "error": f"No analysis tools available. Install one of: {', '.join(tools)}",
                    "install_command": "pip install ruff pylint mypy"
                }

            # Run each tool and collect issues
            all_issues = []
            tool_results = {}

            for tool in tools_to_run:
                issues = self._run_tool(tool, str(target_path), include_style)
                all_issues.extend(issues)
                tool_results[tool] = {
                    "issues_found": len(issues),
                    "ran": True
                }

            # Deduplicate issues (same line/message from different tools)
            unique_issues = self._deduplicate_issues(all_issues)

            # Sort by severity and limit
            unique_issues.sort(key=lambda x: (
                0 if x.severity == "error" else 1 if x.severity == "warning" else 2,
                x.file,
                x.line
            ))

            limited_issues = unique_issues[:max_issues]

            # Categorize issues
            errors = [i for i in limited_issues if i.severity == "error"]
            warnings = [i for i in limited_issues if i.severity == "warning"]
            info = [i for i in limited_issues if i.severity == "info"]

            return {
                "success": True,
                "path": path,
                "language": language,
                "tools_used": tools_to_run,
                "total_issues": len(unique_issues),
                "shown_issues": len(limited_issues),
                "truncated": len(unique_issues) > max_issues,
                "summary": {
                    "errors": len(errors),
                    "warnings": len(warnings),
                    "info": len(info)
                },
                "issues": [
                    {
                        "severity": i.severity,
                        "file": i.file,
                        "line": i.line,
                        "column": i.column,
                        "message": i.message,
                        "rule": i.rule,
                        "suggestion": i.suggestion
                    } for i in limited_issues
                ],
                "tool_results": tool_results
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Analysis failed: {str(e)}"
            }

    def _detect_language(self, path: Path) -> str:
        """Detect programming language from file extension"""
        ext = path.suffix.lower()
        lang_map = {
            '.py': 'python',
            '.js': 'javascript',
            '.jsx': 'javascript',
            '.ts': 'typescript',
            '.tsx': 'typescript',
            '.mjs': 'javascript',
            '.cjs': 'javascript'
        }
        return lang_map.get(ext, 'unknown')

    def _run_tool(self, tool: str, path: str, include_style: bool) -> List[CodeIssue]:
        """Run a specific linting tool"""
        issues = []

        try:
            if tool == 'ruff':
                issues = self._run_ruff(path)
            elif tool == 'pylint':
                issues = self._run_pylint(path, include_style)
            elif tool == 'mypy':
                issues = self._run_mypy(path)
            elif tool == 'eslint':
                issues = self._run_eslint(path)
        except Exception as e:
            # Tool failed, but don't crash - just skip it
            pass

        return issues

    def _run_ruff(self, path: str) -> List[CodeIssue]:
        """Run ruff linter"""
        result = subprocess.run(
            ['ruff', 'check', path, '--output-format=json'],
            capture_output=True,
            text=True,
            timeout=30
        )

        issues = []
        if result.stdout:
            try:
                data = json.loads(result.stdout)
                for item in data:
                    issues.append(CodeIssue(
                        severity="error" if item.get('code', '').startswith('E') else "warning",
                        file=item.get('filename', path),
                        line=item.get('location', {}).get('row', 0),
                        column=item.get('location', {}).get('column', 0),
                        message=item.get('message', ''),
                        rule=item.get('code', 'ruff'),
                        suggestion=item.get('fix', {}).get('message') if item.get('fix') else None
                    ))
            except json.JSONDecodeError:
                pass

        return issues

    def _run_pylint(self, path: str, include_style: bool) -> List[CodeIssue]:
        """Run pylint"""
        cmd = ['pylint', path, '--output-format=json']
        if not include_style:
            cmd.extend(['--disable=C'])  # Disable convention messages

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )

        issues = []
        if result.stdout:
            try:
                data = json.loads(result.stdout)
                for item in data:
                    severity_map = {
                        'error': 'error',
                        'warning': 'warning',
                        'refactor': 'info',
                        'convention': 'info',
                        'info': 'info'
                    }
                    issues.append(CodeIssue(
                        severity=severity_map.get(item.get('type', 'info'), 'info'),
                        file=item.get('path', path),
                        line=item.get('line', 0),
                        column=item.get('column', 0),
                        message=item.get('message', ''),
                        rule=item.get('message-id', 'pylint'),
                        suggestion=None
                    ))
            except json.JSONDecodeError:
                pass

        return issues

    def _run_mypy(self, path: str) -> List[CodeIssue]:
        """Run mypy type checker"""
        result = subprocess.run(
            ['mypy', path, '--show-column-numbers', '--no-error-summary'],
            capture_output=True,
            text=True,
            timeout=60
        )

        issues = []
        # Parse mypy output: file:line:col: error: message
        pattern = r'^(.+?):(\d+):(\d+): (error|warning|note): (.+)$'

        for line in result.stdout.splitlines():
            match = re.match(pattern, line)
            if match:
                file_path, line_num, col, severity, message = match.groups()
                issues.append(CodeIssue(
                    severity="error" if severity == "error" else "warning",
                    file=file_path,
                    line=int(line_num),
                    column=int(col),
                    message=message,
                    rule='mypy',
                    suggestion=None
                ))

        return issues

    def _run_eslint(self, path: str) -> List[CodeIssue]:
        """Run eslint for JavaScript/TypeScript"""
        result = subprocess.run(
            ['eslint', path, '--format=json'],
            capture_output=True,
            text=True,
            timeout=30
        )

        issues = []
        if result.stdout:
            try:
                data = json.loads(result.stdout)
                for file_result in data:
                    for msg in file_result.get('messages', []):
                        severity_map = {1: 'warning', 2: 'error'}
                        issues.append(CodeIssue(
                            severity=severity_map.get(msg.get('severity', 1), 'warning'),
                            file=file_result.get('filePath', path),
                            line=msg.get('line', 0),
                            column=msg.get('column', 0),
                            message=msg.get('message', ''),
                            rule=msg.get('ruleId', 'eslint'),
                            suggestion=msg.get('fix', {}).get('text') if msg.get('fix') else None
                        ))
            except json.JSONDecodeError:
                pass

        return issues

    def _deduplicate_issues(self, issues: List[CodeIssue]) -> List[CodeIssue]:
        """Remove duplicate issues from different tools"""
        seen = set()
        unique = []

        for issue in issues:
            # Create key from file, line, and normalized message
            key = (issue.file, issue.line, issue.message[:50])
            if key not in seen:
                seen.add(key)
                unique.append(issue)

        return unique
