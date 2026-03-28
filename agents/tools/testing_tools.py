"""
Test Runner Tool

Execute tests and measure coverage across multiple frameworks.
Supports pytest, unittest (Python), and jest (JavaScript).
"""

import subprocess
import json
import re
from pathlib import Path
from typing import Dict, Any, List, Optional


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
