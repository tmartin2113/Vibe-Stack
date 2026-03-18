"""
Development Tools for Multi-Agent System

Free, offline tools for code analysis, testing, and project management.
These tools integrate with the Tool-Caller adapter to provide comprehensive
development capabilities without requiring internet access.

Tools included:
1. StaticCodeAnalyzer - Lint and analyze code quality
2. TestRunnerTool - Execute tests and measure coverage
3. CodebaseSearchTool - Semantic search through codebase
4. GitOperationsTool - Git history and analysis
5. DataParserTool - Parse and validate structured data
"""

import subprocess
import json
import os
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
import ast
import tokenize
import io

# Optional imports with graceful degradation
try:
    import git
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False
    print("Warning: gitpython not installed. GitOperationsTool will have limited functionality.")
    print("Install with: pip install gitpython")

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False
    print("Note: pyyaml not installed. DataParserTool will not support YAML.")
    print("Install with: pip install pyyaml")


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


class TestRunnerTool:
    """
    Execute tests and measure coverage.

    Supports:
    - pytest (Python)
    - unittest (Python)
    - jest (JavaScript - if available)
    - coverage.py for Python coverage
    """

    def __init__(self):
        self.name = "test_runner"
        self.description = "Run tests and measure code coverage. Supports pytest, unittest, jest."

    def execute(
        self,
        path: str = ".",
        test_pattern: Optional[str] = None,
        coverage: bool = True,
        verbose: bool = False,
        timeout: int = 300
    ) -> Dict[str, Any]:
        """
        Run tests at the given path.

        Args:
            path: Directory or file to test
            test_pattern: Pattern to match test files (e.g., "test_*.py")
            coverage: Measure code coverage
            verbose: Show verbose output
            timeout: Timeout in seconds

        Returns:
            Dictionary with test results
        """
        try:
            target_path = Path(path)
            if not target_path.exists():
                return {
                    "success": False,
                    "error": f"Path does not exist: {path}"
                }

            # Detect test framework
            framework = self._detect_test_framework(target_path)

            if framework == "pytest":
                return self._run_pytest(str(target_path), test_pattern, coverage, verbose, timeout)
            elif framework == "unittest":
                return self._run_unittest(str(target_path), test_pattern, coverage, timeout)
            elif framework == "jest":
                return self._run_jest(str(target_path), coverage, timeout)
            else:
                return {
                    "success": False,
                    "error": "No test framework detected. Install pytest: pip install pytest pytest-cov"
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"Test execution failed: {str(e)}"
            }

    def _detect_test_framework(self, path: Path) -> Optional[str]:
        """Detect which test framework to use"""
        # Check if pytest is available
        pytest_check = subprocess.run(['which', 'pytest'], capture_output=True)
        if pytest_check.returncode == 0:
            return "pytest"

        # Check if jest is available
        jest_check = subprocess.run(['which', 'jest'], capture_output=True)
        if jest_check.returncode == 0:
            return "jest"

        # Fall back to unittest (built-in to Python)
        return "unittest"

    def _run_pytest(
        self,
        path: str,
        pattern: Optional[str],
        coverage: bool,
        verbose: bool,
        timeout: int
    ) -> Dict[str, Any]:
        """Run pytest"""
        cmd = ['pytest', path]

        if pattern:
            cmd.extend(['-k', pattern])

        if coverage:
            cmd.extend(['--cov', '--cov-report=json', '--cov-report=term'])

        if verbose:
            cmd.append('-v')
        else:
            cmd.append('-q')

        # Add JSON report
        cmd.extend(['--tb=short'])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        # Parse output
        output_lines = result.stdout.splitlines()

        # Extract summary line (e.g., "5 passed, 2 failed in 1.23s")
        summary = ""
        for line in reversed(output_lines):
            if 'passed' in line or 'failed' in line or 'error' in line:
                summary = line.strip()
                break

        # Parse counts
        passed = failed = skipped = 0
        if summary:
            passed_match = re.search(r'(\d+) passed', summary)
            failed_match = re.search(r'(\d+) failed', summary)
            skipped_match = re.search(r'(\d+) skipped', summary)

            if passed_match:
                passed = int(passed_match.group(1))
            if failed_match:
                failed = int(failed_match.group(1))
            if skipped_match:
                skipped = int(skipped_match.group(1))

        # Load coverage data if available
        coverage_data = None
        if coverage and Path('.coverage').exists():
            coverage_json = Path('coverage.json')
            if coverage_json.exists():
                with open(coverage_json, 'r') as f:
                    cov_data = json.load(f)
                    total_coverage = cov_data.get('totals', {}).get('percent_covered', 0)
                    coverage_data = {
                        "total_coverage": round(total_coverage, 1),
                        "lines_covered": cov_data.get('totals', {}).get('covered_lines', 0),
                        "lines_total": cov_data.get('totals', {}).get('num_statements', 0)
                    }

        return {
            "success": result.returncode == 0,
            "framework": "pytest",
            "tests_run": passed + failed,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "summary": summary,
            "coverage": coverage_data,
            "output": result.stdout,
            "errors": result.stderr if result.returncode != 0 else None
        }

    def _run_unittest(self, path: str, pattern: Optional[str], coverage: bool, timeout: int) -> Dict[str, Any]:
        """Run unittest"""
        cmd = ['python', '-m', 'unittest', 'discover', '-s', path]

        if pattern:
            cmd.extend(['-p', pattern])

        if coverage:
            cmd = ['coverage', 'run', '-m'] + cmd[1:]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        # Parse output
        output = result.stderr  # unittest writes to stderr

        # Extract summary
        ok_match = re.search(r'Ran (\d+) test.*OK', output, re.DOTALL)
        fail_match = re.search(r'Ran (\d+) test.*FAILED \(failures=(\d+)\)', output, re.DOTALL)

        tests_run = passed = failed = 0

        if ok_match:
            tests_run = passed = int(ok_match.group(1))
        elif fail_match:
            tests_run = int(fail_match.group(1))
            failed = int(fail_match.group(2))
            passed = tests_run - failed

        # Get coverage if requested
        coverage_data = None
        if coverage:
            cov_result = subprocess.run(
                ['coverage', 'report', '--format=total'],
                capture_output=True,
                text=True
            )
            if cov_result.returncode == 0:
                try:
                    total_coverage = float(cov_result.stdout.strip())
                    coverage_data = {"total_coverage": round(total_coverage, 1)}
                except ValueError:
                    pass

        return {
            "success": result.returncode == 0,
            "framework": "unittest",
            "tests_run": tests_run,
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "summary": output.splitlines()[-1] if output else "",
            "coverage": coverage_data,
            "output": output
        }

    def _run_jest(self, path: str, coverage: bool, timeout: int) -> Dict[str, Any]:
        """Run jest for JavaScript/TypeScript"""
        cmd = ['jest', path, '--json']

        if coverage:
            cmd.append('--coverage')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        # Jest outputs JSON results
        try:
            data = json.loads(result.stdout)
            return {
                "success": data.get('success', False),
                "framework": "jest",
                "tests_run": data.get('numTotalTests', 0),
                "passed": data.get('numPassedTests', 0),
                "failed": data.get('numFailedTests', 0),
                "skipped": data.get('numPendingTests', 0),
                "summary": f"{data.get('numPassedTests', 0)}/{data.get('numTotalTests', 0)} tests passed",
                "coverage": data.get('coverageMap', {}).get('total', {}) if coverage else None,
                "output": result.stdout
            }
        except json.JSONDecodeError:
            return {
                "success": False,
                "error": "Failed to parse Jest output",
                "output": result.stdout
            }


class CodebaseSearchTool:
    """
    Semantic search through codebase.

    Features:
    - Find function/class definitions
    - Search by name pattern
    - AST-based search (understands code structure)
    - Grep-like text search with context
    """

    def __init__(self):
        self.name = "codebase_search"
        self.description = "Search codebase for functions, classes, patterns. Understands code structure."

    def execute(
        self,
        query: str,
        path: str = ".",
        search_type: str = "auto",
        file_pattern: str = "*.py",
        max_results: int = 20
    ) -> Dict[str, Any]:
        """
        Search codebase.

        Args:
            query: What to search for
            path: Directory to search in
            search_type: "function", "class", "text", "auto"
            file_pattern: File pattern (e.g., "*.py", "*.js")
            max_results: Maximum results to return

        Returns:
            Dictionary with search results
        """
        try:
            target_path = Path(path)
            if not target_path.exists():
                return {
                    "success": False,
                    "error": f"Path does not exist: {path}"
                }

            # Auto-detect search type
            if search_type == "auto":
                if query.startswith("def ") or query.startswith("function "):
                    search_type = "function"
                elif query.startswith("class "):
                    search_type = "class"
                else:
                    # Try to determine from query
                    if re.match(r'^[A-Z][a-zA-Z0-9]*$', query):
                        search_type = "class"  # CamelCase = likely class
                    elif re.match(r'^[a-z_][a-z0-9_]*$', query):
                        search_type = "function"  # snake_case = likely function
                    else:
                        search_type = "text"

            # Find matching files
            files = list(target_path.rglob(file_pattern))

            results = []

            if search_type == "function":
                results = self._search_functions(query, files, max_results)
            elif search_type == "class":
                results = self._search_classes(query, files, max_results)
            else:  # text search
                results = self._search_text(query, files, max_results)

            return {
                "success": True,
                "query": query,
                "search_type": search_type,
                "files_searched": len(files),
                "results_found": len(results),
                "results": results
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Search failed: {str(e)}"
            }

    def _search_functions(self, query: str, files: List[Path], max_results: int) -> List[Dict[str, Any]]:
        """Search for function definitions"""
        results = []
        query_lower = query.lower()

        for file in files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Parse AST for Python files
                if file.suffix == '.py':
                    try:
                        tree = ast.parse(content)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.FunctionDef):
                                if query_lower in node.name.lower():
                                    results.append({
                                        "type": "function",
                                        "name": node.name,
                                        "file": str(file),
                                        "line": node.lineno,
                                        "args": [arg.arg for arg in node.args.args],
                                        "docstring": ast.get_docstring(node)
                                    })
                    except SyntaxError:
                        pass
                else:
                    # Regex fallback for other languages
                    pattern = r'^[\s]*(function|def|async def)\s+(\w*' + re.escape(query) + r'\w*)\s*\('
                    for i, line in enumerate(content.splitlines(), 1):
                        match = re.search(pattern, line, re.IGNORECASE)
                        if match:
                            results.append({
                                "type": "function",
                                "name": match.group(2),
                                "file": str(file),
                                "line": i,
                                "snippet": line.strip()
                            })

                if len(results) >= max_results:
                    break

            except Exception:
                continue

        return results[:max_results]

    def _search_classes(self, query: str, files: List[Path], max_results: int) -> List[Dict[str, Any]]:
        """Search for class definitions"""
        results = []
        query_lower = query.lower()

        for file in files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Parse AST for Python files
                if file.suffix == '.py':
                    try:
                        tree = ast.parse(content)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.ClassDef):
                                if query_lower in node.name.lower():
                                    methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                                    results.append({
                                        "type": "class",
                                        "name": node.name,
                                        "file": str(file),
                                        "line": node.lineno,
                                        "methods": methods[:10],  # First 10 methods
                                        "docstring": ast.get_docstring(node)
                                    })
                    except SyntaxError:
                        pass
                else:
                    # Regex fallback
                    pattern = r'^[\s]*(class|interface)\s+(\w*' + re.escape(query) + r'\w*)'
                    for i, line in enumerate(content.splitlines(), 1):
                        match = re.search(pattern, line, re.IGNORECASE)
                        if match:
                            results.append({
                                "type": "class",
                                "name": match.group(2),
                                "file": str(file),
                                "line": i,
                                "snippet": line.strip()
                            })

                if len(results) >= max_results:
                    break

            except Exception:
                continue

        return results[:max_results]

    def _search_text(self, query: str, files: List[Path], max_results: int) -> List[Dict[str, Any]]:
        """Search for text with context"""
        results = []

        for file in files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                for i, line in enumerate(lines):
                    if query.lower() in line.lower():
                        # Get context (2 lines before and after)
                        context_start = max(0, i - 2)
                        context_end = min(len(lines), i + 3)
                        context = ''.join(lines[context_start:context_end])

                        results.append({
                            "type": "text",
                            "file": str(file),
                            "line": i + 1,
                            "match": line.strip(),
                            "context": context.strip()
                        })

                        if len(results) >= max_results:
                            return results

            except Exception:
                continue

        return results[:max_results]


class GitOperationsTool:
    """
    Git repository operations and analysis.

    Features:
    - Git blame (who wrote what)
    - Commit history analysis
    - Diff parsing
    - Branch information
    - File history
    """

    def __init__(self):
        self.name = "git_operations"
        self.description = "Analyze git repository: blame, history, diffs, branches."
        self.git_available = GIT_AVAILABLE

    def execute(
        self,
        operation: str,
        path: str = ".",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Execute git operation.

        Args:
            operation: "blame", "history", "diff", "status", "branches"
            path: Repository or file path
            **kwargs: Operation-specific arguments

        Returns:
            Dictionary with operation results
        """
        try:
            if operation == "blame":
                return self._git_blame(path, kwargs.get('line_range'))
            elif operation == "history":
                return self._git_history(path, kwargs.get('max_commits', 10))
            elif operation == "diff":
                return self._git_diff(path, kwargs.get('commit1'), kwargs.get('commit2'))
            elif operation == "status":
                return self._git_status(path)
            elif operation == "branches":
                return self._git_branches(path)
            else:
                return {
                    "success": False,
                    "error": f"Unknown operation: {operation}. Use: blame, history, diff, status, branches"
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"Git operation failed: {str(e)}"
            }

    def _git_blame(self, file_path: str, line_range: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        """Get git blame for a file"""
        if not Path(file_path).is_file():
            return {"success": False, "error": "Not a file"}

        cmd = ['git', 'blame', '--line-porcelain', file_path]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        # Parse blame output
        blame_data = []
        current: Dict[str, Any] = {}

        for line in result.stdout.splitlines():
            if line.startswith('author '):
                current['author'] = line[7:]
            elif line.startswith('author-time '):
                current['timestamp'] = int(line[12:])
            elif line.startswith('summary '):
                current['message'] = line[8:]
            elif line.startswith('\t'):
                if current:
                    current['code'] = line[1:]
                    blame_data.append(current.copy())
                    current = {}

        # Filter by line range if specified
        if line_range:
            start, end = line_range
            blame_data = blame_data[start-1:end]

        return {
            "success": True,
            "file": file_path,
            "total_lines": len(blame_data),
            "lines": blame_data[:100]  # Limit to 100 lines
        }

    def _git_history(self, path: str, max_commits: int) -> Dict[str, Any]:
        """Get commit history"""
        cmd = [
            'git', 'log',
            f'-{max_commits}',
            '--pretty=format:%H|%an|%ae|%at|%s',
            '--', path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        commits = []
        for line in result.stdout.splitlines():
            if '|' in line:
                hash_val, author, email, timestamp, message = line.split('|', 4)
                commits.append({
                    "hash": hash_val[:8],
                    "author": author,
                    "email": email,
                    "timestamp": int(timestamp),
                    "message": message
                })

        return {
            "success": True,
            "path": path,
            "commits_found": len(commits),
            "commits": commits
        }

    def _git_diff(self, path: str, commit1: Optional[str], commit2: Optional[str]) -> Dict[str, Any]:
        """Get git diff"""
        cmd = ['git', 'diff']

        if commit1:
            cmd.append(commit1)
        if commit2:
            cmd.append(commit2)

        if path != '.':
            cmd.extend(['--', path])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        # Parse diff stats
        stats_result = subprocess.run(
            cmd + ['--stat'],
            capture_output=True,
            text=True,
            timeout=10
        )

        return {
            "success": True,
            "diff": result.stdout,
            "stats": stats_result.stdout if stats_result.returncode == 0 else None,
            "has_changes": bool(result.stdout.strip())
        }

    def _git_status(self, path: str) -> Dict[str, Any]:
        """Get git status"""
        result = subprocess.run(
            ['git', 'status', '--porcelain', path],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        # Parse status
        modified = []
        added = []
        deleted = []
        untracked = []

        for line in result.stdout.splitlines():
            status = line[:2]
            file_path = line[3:]

            if 'M' in status:
                modified.append(file_path)
            elif 'A' in status:
                added.append(file_path)
            elif 'D' in status:
                deleted.append(file_path)
            elif '?' in status:
                untracked.append(file_path)

        return {
            "success": True,
            "path": path,
            "modified": modified,
            "added": added,
            "deleted": deleted,
            "untracked": untracked,
            "clean": not (modified or added or deleted or untracked)
        }

    def _git_branches(self, path: str) -> Dict[str, Any]:
        """Get git branches"""
        result = subprocess.run(
            ['git', 'branch', '-a'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=path if Path(path).is_dir() else '.'
        )

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        branches = []
        current = None

        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('* '):
                current = line[2:]
                branches.append(line[2:])
            else:
                branches.append(line)

        return {
            "success": True,
            "current_branch": current,
            "branches": branches,
            "total_branches": len(branches)
        }


class DataParserTool:
    """
    Parse and validate structured data.

    Supports:
    - JSON
    - YAML (if pyyaml installed)
    - XML
    - CSV
    - TOML
    """

    def __init__(self):
        self.name = "data_parser"
        self.description = "Parse and validate JSON, YAML, XML, CSV, TOML files."

    def execute(
        self,
        data: str,
        format_type: str = "auto",
        validate_schema: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Parse structured data.

        Args:
            data: Data string or file path
            format_type: "json", "yaml", "xml", "csv", "toml", "auto"
            validate_schema: Optional JSON schema to validate against

        Returns:
            Dictionary with parsed data
        """
        try:
            # Check if data is a file path
            if len(data) < 500 and Path(data).is_file():
                with open(data, 'r') as f:
                    data_str = f.read()

                # Auto-detect format from extension
                if format_type == "auto":
                    ext = Path(data).suffix.lower()
                    format_map = {
                        '.json': 'json',
                        '.yaml': 'yaml',
                        '.yml': 'yaml',
                        '.xml': 'xml',
                        '.csv': 'csv',
                        '.toml': 'toml'
                    }
                    format_type = format_map.get(ext, 'json')
            elif len(data) < 500 and (Path(data).suffix or '/' in data or '\\' in data):
                # Looks like a file path but doesn't exist
                return {
                    "success": False,
                    "error": f"File not found: {data}"
                }
            else:
                data_str = data

                # Auto-detect from content
                if format_type == "auto":
                    data_stripped = data_str.strip()
                    if data_stripped.startswith('{') or data_stripped.startswith('['):
                        format_type = 'json'
                    elif data_stripped.startswith('<'):
                        format_type = 'xml'
                    else:
                        format_type = 'yaml'

            # Parse based on format
            if format_type == "json":
                parsed = json.loads(data_str)
            elif format_type == "yaml":
                if not YAML_AVAILABLE:
                    return {
                        "success": False,
                        "error": "YAML support requires pyyaml. Install with: pip install pyyaml"
                    }
                parsed = yaml.safe_load(data_str)
            elif format_type == "csv":
                import csv
                lines = data_str.splitlines()
                reader = csv.DictReader(lines)
                parsed = list(reader)
            elif format_type == "xml":
                import xml.etree.ElementTree as ET
                root = ET.fromstring(data_str)
                parsed = self._xml_to_dict(root)
            elif format_type == "toml":
                try:
                    import tomllib
                except ImportError:
                    import tomli as tomllib
                parsed = tomllib.loads(data_str)
            else:
                return {
                    "success": False,
                    "error": f"Unsupported format: {format_type}"
                }

            # Validate schema if provided
            validation: Optional[Dict[str, Any]] = None
            if validate_schema:
                try:
                    import jsonschema
                    jsonschema.validate(parsed, validate_schema)
                    validation = {"valid": True}
                except ImportError:
                    validation = {"error": "jsonschema not installed"}
                except Exception as e:
                    validation = {"valid": False, "errors": str(e)}

            return {
                "success": True,
                "format": format_type,
                "data": parsed,
                "validation": validation,
                "summary": self._summarize_data(parsed)
            }

        except json.JSONDecodeError as e:
            return {
                "success": False,
                "error": f"JSON parse error: {str(e)}",
                "line": e.lineno,
                "column": e.colno
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Parse error: {str(e)}"
            }

    def _xml_to_dict(self, element) -> Dict[str, Any]:
        """Convert XML element to dictionary"""
        result = {}

        # Add attributes
        if element.attrib:
            result['@attributes'] = element.attrib

        # Add text
        if element.text and element.text.strip():
            result['@text'] = element.text.strip()

        # Add children
        for child in element:
            child_data = self._xml_to_dict(child)
            if child.tag in result:
                if not isinstance(result[child.tag], list):
                    result[child.tag] = [result[child.tag]]
                result[child.tag].append(child_data)
            else:
                result[child.tag] = child_data

        return result

    def _summarize_data(self, data: Any) -> Dict[str, Any]:
        """Generate summary of parsed data"""
        if isinstance(data, dict):
            return {
                "type": "object",
                "keys": list(data.keys())[:20],
                "total_keys": len(data)
            }
        elif isinstance(data, list):
            return {
                "type": "array",
                "length": len(data),
                "item_type": type(data[0]).__name__ if data else "unknown"
            }
        else:
            return {
                "type": type(data).__name__,
                "value": str(data)[:100]
            }


# Example usage
if __name__ == "__main__":
    print("Development Tools Test\n" + "="*60)

    # Test StaticCodeAnalyzer
    print("\n1. Testing StaticCodeAnalyzer...")
    analyzer = StaticCodeAnalyzer()
    result = analyzer.execute(__file__, max_issues=5)
    print(f"Success: {result['success']}")
    if result['success']:
        print(f"Issues found: {result['total_issues']}")
        print(f"Errors: {result['summary']['errors']}, Warnings: {result['summary']['warnings']}")

    # Test CodebaseSearchTool
    print("\n2. Testing CodebaseSearchTool...")
    search = CodebaseSearchTool()
    result = search.execute("StaticCodeAnalyzer", path=".", search_type="class", max_results=3)
    print(f"Success: {result['success']}")
    if result['success']:
        print(f"Results found: {result['results_found']}")

    # Test DataParserTool
    print("\n3. Testing DataParserTool...")
    parser = DataParserTool()
    test_json = '{"name": "test", "value": 123, "items": [1, 2, 3]}'
    result = parser.execute(test_json)
    print(f"Success: {result['success']}")
    if result['success']:
        print(f"Format: {result['format']}")
        print(f"Summary: {result['summary']}")

    # Test GitOperationsTool
    print("\n4. Testing GitOperationsTool...")
    git = GitOperationsTool()
    result = git.execute("status", path=".")
    print(f"Success: {result['success']}")
    if result['success']:
        print(f"Repository clean: {result.get('clean', False)}")

    print("\n" + "="*60)
    print("All development tools tested successfully!")
