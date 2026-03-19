"""
Tests for dev_tools.py uncovered methods.

Covers the 19 methods that were previously missing from coverage:
- StaticCodeAnalyzer: _check_tool_availability, _run_tool, _run_ruff,
  _run_pylint, _run_mypy, _run_eslint
- TestRunnerTool: _run_pytest, _run_unittest, _run_jest
- CodebaseSearchTool: _search_functions, _search_classes, _search_text
- GitOperationsTool: _git_blame, _git_history, _git_diff, _git_status,
  _git_branches
- DataParserTool: _xml_to_dict, _summarize_data

All subprocess calls are mocked. File-based tools use tmp_path fixtures.
"""

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from agents.tools.dev_tools import (
    StaticCodeAnalyzer,
    TestRunnerTool,
    CodebaseSearchTool,
    GitOperationsTool,
    DataParserTool,
    CodeIssue,
)


# ============================================================
# StaticCodeAnalyzer
# ============================================================


class TestStaticCodeAnalyzerCoverage:
    """Cover _check_tool_availability, _run_tool, _run_ruff,
    _run_pylint, _run_mypy, _run_eslint."""

    # -- _check_tool_availability ----------------------------

    def test_check_tool_availability_all_present(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            tools = analyzer._check_tool_availability()
            assert isinstance(tools, dict)
            for name in ("ruff", "pylint", "mypy", "pyflakes", "eslint"):
                assert tools[name] is True
            assert mock_run.call_count == 5

    def test_check_tool_availability_none_present(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            tools = analyzer._check_tool_availability()
            for name in ("ruff", "pylint", "mypy", "pyflakes", "eslint"):
                assert tools[name] is False

    def test_check_tool_availability_mixed(self):
        analyzer = StaticCodeAnalyzer()
        calls = iter([
            MagicMock(returncode=0),  # pylint
            MagicMock(returncode=0),  # ruff
            MagicMock(returncode=1),  # mypy
            MagicMock(returncode=1),  # pyflakes
            MagicMock(returncode=0),  # eslint
        ])
        with patch("subprocess.run", side_effect=calls):
            tools = analyzer._check_tool_availability()
            # The order is: pylint, ruff, mypy, pyflakes, eslint
            assert isinstance(tools, dict)
            # At least verify we got 5 entries
            assert len(tools) == 5

    # -- _run_tool dispatch ----------------------------------

    def test_run_tool_dispatches_ruff(self):
        analyzer = StaticCodeAnalyzer()
        with patch.object(analyzer, "_run_ruff", return_value=[]) as mock_ruff:
            issues = analyzer._run_tool("ruff", "/tmp/test.py", True)
            mock_ruff.assert_called_once_with("/tmp/test.py")
            assert issues == []

    def test_run_tool_dispatches_pylint(self):
        analyzer = StaticCodeAnalyzer()
        with patch.object(analyzer, "_run_pylint", return_value=[]) as mock_pylint:
            issues = analyzer._run_tool("pylint", "/tmp/test.py", False)
            mock_pylint.assert_called_once_with("/tmp/test.py", False)
            assert issues == []

    def test_run_tool_dispatches_mypy(self):
        analyzer = StaticCodeAnalyzer()
        with patch.object(analyzer, "_run_mypy", return_value=[]) as mock_mypy:
            issues = analyzer._run_tool("mypy", "/tmp/test.py", True)
            mock_mypy.assert_called_once_with("/tmp/test.py")
            assert issues == []

    def test_run_tool_dispatches_eslint(self):
        analyzer = StaticCodeAnalyzer()
        with patch.object(analyzer, "_run_eslint", return_value=[]) as mock_eslint:
            issues = analyzer._run_tool("eslint", "/tmp/app.js", True)
            mock_eslint.assert_called_once_with("/tmp/app.js")
            assert issues == []

    def test_run_tool_unknown_tool_returns_empty(self):
        analyzer = StaticCodeAnalyzer()
        issues = analyzer._run_tool("unknown_tool", "/tmp/test.py", True)
        assert issues == []

    def test_run_tool_catches_exception(self):
        analyzer = StaticCodeAnalyzer()
        with patch.object(analyzer, "_run_ruff", side_effect=RuntimeError("boom")):
            issues = analyzer._run_tool("ruff", "/tmp/test.py", True)
            assert issues == []

    # -- _run_ruff -------------------------------------------

    def test_run_ruff_parses_issues(self):
        analyzer = StaticCodeAnalyzer()
        ruff_output = json.dumps([
            {
                "code": "E501",
                "filename": "test.py",
                "location": {"row": 10, "column": 80},
                "message": "Line too long",
                "fix": {"message": "Split the line"},
            },
            {
                "code": "W291",
                "filename": "test.py",
                "location": {"row": 15, "column": 1},
                "message": "Trailing whitespace",
                "fix": None,
            },
        ])
        mock_result = MagicMock(stdout=ruff_output, returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_ruff("/tmp/test.py")
            assert len(issues) == 2
            assert issues[0].severity == "error"  # E501 starts with E
            assert issues[0].line == 10
            assert issues[0].column == 80
            assert issues[0].suggestion == "Split the line"
            assert issues[1].severity == "warning"  # W291 starts with W
            assert issues[1].suggestion is None

    def test_run_ruff_empty_output(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_ruff("/tmp/test.py")
            assert issues == []

    def test_run_ruff_invalid_json(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="not valid json!", returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_ruff("/tmp/test.py")
            assert issues == []

    def test_run_ruff_no_fix_field(self):
        """Ruff item with fix=None should yield suggestion=None."""
        analyzer = StaticCodeAnalyzer()
        ruff_output = json.dumps([
            {
                "code": "F401",
                "filename": "test.py",
                "location": {"row": 1, "column": 1},
                "message": "Unused import",
                "fix": None,
            },
        ])
        mock_result = MagicMock(stdout=ruff_output, returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_ruff("/tmp/test.py")
            assert len(issues) == 1
            assert issues[0].suggestion is None

    # -- _run_pylint -----------------------------------------

    def test_run_pylint_parses_issues(self):
        analyzer = StaticCodeAnalyzer()
        pylint_output = json.dumps([
            {
                "type": "error",
                "path": "test.py",
                "line": 5,
                "column": 0,
                "message": "Undefined variable 'x'",
                "message-id": "E0602",
            },
            {
                "type": "warning",
                "path": "test.py",
                "line": 10,
                "column": 4,
                "message": "Unused variable 'y'",
                "message-id": "W0612",
            },
            {
                "type": "convention",
                "path": "test.py",
                "line": 1,
                "column": 0,
                "message": "Missing module docstring",
                "message-id": "C0114",
            },
            {
                "type": "refactor",
                "path": "test.py",
                "line": 20,
                "column": 0,
                "message": "Too many branches",
                "message-id": "R0912",
            },
        ])
        mock_result = MagicMock(stdout=pylint_output, returncode=2)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_pylint("/tmp/test.py", include_style=True)
            assert len(issues) == 4
            assert issues[0].severity == "error"
            assert issues[0].rule == "E0602"
            assert issues[1].severity == "warning"
            assert issues[2].severity == "info"  # convention -> info
            assert issues[3].severity == "info"  # refactor -> info

    def test_run_pylint_without_style(self):
        """When include_style=False, --disable=C should appear in command."""
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="[]", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            analyzer._run_pylint("/tmp/test.py", include_style=False)
            cmd = mock_run.call_args[0][0]
            assert "--disable=C" in cmd

    def test_run_pylint_with_style(self):
        """When include_style=True, --disable=C should NOT appear."""
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="[]", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            analyzer._run_pylint("/tmp/test.py", include_style=True)
            cmd = mock_run.call_args[0][0]
            assert "--disable=C" not in cmd

    def test_run_pylint_empty_output(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_pylint("/tmp/test.py", True)
            assert issues == []

    def test_run_pylint_invalid_json(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="{not json", returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_pylint("/tmp/test.py", True)
            assert issues == []

    def test_run_pylint_unknown_type(self):
        """Unknown pylint type should map to 'info'."""
        analyzer = StaticCodeAnalyzer()
        pylint_output = json.dumps([
            {
                "type": "fatal",
                "path": "test.py",
                "line": 1,
                "column": 0,
                "message": "Something fatal",
                "message-id": "F0001",
            },
        ])
        mock_result = MagicMock(stdout=pylint_output, returncode=2)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_pylint("/tmp/test.py", True)
            assert len(issues) == 1
            assert issues[0].severity == "info"

    # -- _run_mypy -------------------------------------------

    def test_run_mypy_parses_issues(self):
        analyzer = StaticCodeAnalyzer()
        mypy_output = (
            "test.py:10:5: error: Incompatible return type\n"
            "test.py:20:1: warning: Unused import\n"
            "test.py:30:10: note: See docs\n"
        )
        mock_result = MagicMock(stdout=mypy_output, returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_mypy("/tmp/test.py")
            assert len(issues) == 3
            assert issues[0].severity == "error"
            assert issues[0].file == "test.py"
            assert issues[0].line == 10
            assert issues[0].column == 5
            assert issues[0].message == "Incompatible return type"
            assert issues[0].rule == "mypy"
            assert issues[1].severity == "warning"
            assert issues[2].severity == "warning"  # note -> warning

    def test_run_mypy_no_issues(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_mypy("/tmp/test.py")
            assert issues == []

    def test_run_mypy_ignores_non_matching_lines(self):
        analyzer = StaticCodeAnalyzer()
        mypy_output = (
            "Success: no issues found in 1 source file\n"
            "test.py:10:5: error: Real issue\n"
        )
        mock_result = MagicMock(stdout=mypy_output, returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_mypy("/tmp/test.py")
            assert len(issues) == 1
            assert issues[0].message == "Real issue"

    # -- _run_eslint -----------------------------------------

    def test_run_eslint_parses_issues(self):
        analyzer = StaticCodeAnalyzer()
        eslint_output = json.dumps([
            {
                "filePath": "/tmp/app.js",
                "messages": [
                    {
                        "severity": 2,
                        "line": 5,
                        "column": 10,
                        "message": "Unexpected var",
                        "ruleId": "no-var",
                        "fix": {"text": "let"},
                    },
                    {
                        "severity": 1,
                        "line": 12,
                        "column": 1,
                        "message": "Missing semicolon",
                        "ruleId": "semi",
                        "fix": None,
                    },
                ],
            },
        ])
        mock_result = MagicMock(stdout=eslint_output, returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_eslint("/tmp/app.js")
            assert len(issues) == 2
            assert issues[0].severity == "error"
            assert issues[0].file == "/tmp/app.js"
            assert issues[0].rule == "no-var"
            assert issues[0].suggestion == "let"
            assert issues[1].severity == "warning"
            assert issues[1].suggestion is None

    def test_run_eslint_empty_output(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_eslint("/tmp/app.js")
            assert issues == []

    def test_run_eslint_invalid_json(self):
        analyzer = StaticCodeAnalyzer()
        mock_result = MagicMock(stdout="not json", returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_eslint("/tmp/app.js")
            assert issues == []

    def test_run_eslint_no_messages(self):
        analyzer = StaticCodeAnalyzer()
        eslint_output = json.dumps([
            {"filePath": "/tmp/app.js", "messages": []},
        ])
        mock_result = MagicMock(stdout=eslint_output, returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_eslint("/tmp/app.js")
            assert issues == []

    def test_run_eslint_multiple_files(self):
        analyzer = StaticCodeAnalyzer()
        eslint_output = json.dumps([
            {
                "filePath": "/tmp/a.js",
                "messages": [
                    {"severity": 2, "line": 1, "column": 1, "message": "err1", "ruleId": "r1"},
                ],
            },
            {
                "filePath": "/tmp/b.js",
                "messages": [
                    {"severity": 1, "line": 2, "column": 3, "message": "warn1", "ruleId": "r2"},
                ],
            },
        ])
        mock_result = MagicMock(stdout=eslint_output, returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            issues = analyzer._run_eslint("/tmp/dir")
            assert len(issues) == 2
            assert issues[0].file == "/tmp/a.js"
            assert issues[1].file == "/tmp/b.js"


# ============================================================
# TestRunnerTool
# ============================================================


class TestTestRunnerToolCoverage:
    """Cover _run_pytest, _run_unittest, _run_jest."""

    # -- _run_pytest -----------------------------------------

    def test_run_pytest_all_pass(self):
        runner = TestRunnerTool()
        stdout = (
            "test_one.py ...\n"
            "test_two.py ..\n"
            "5 passed in 0.42s\n"
        )
        mock_result = MagicMock(stdout=stdout, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_pytest("/tmp/tests", None, False, False, 300)
            assert result["success"] is True
            assert result["framework"] == "pytest"
            assert result["passed"] == 5
            assert result["failed"] == 0
            assert result["tests_run"] == 5
            assert "5 passed" in result["summary"]

    def test_run_pytest_mixed_results(self):
        runner = TestRunnerTool()
        stdout = (
            "test_one.py .F.\n"
            "FAILURES\n"
            "  ...\n"
            "3 passed, 2 failed, 1 skipped in 1.5s\n"
        )
        mock_result = MagicMock(stdout=stdout, stderr="assertion error", returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_pytest("/tmp/tests", None, False, False, 300)
            assert result["success"] is False
            assert result["passed"] == 3
            assert result["failed"] == 2
            assert result["skipped"] == 1
            assert result["tests_run"] == 5  # passed + failed
            assert result["errors"] == "assertion error"

    def test_run_pytest_with_pattern(self):
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="1 passed in 0.1s\n", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner._run_pytest("/tmp/tests", "test_foo", False, False, 300)
            cmd = mock_run.call_args[0][0]
            assert "-k" in cmd
            idx = cmd.index("-k")
            assert cmd[idx + 1] == "test_foo"

    def test_run_pytest_with_coverage_flag(self):
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="1 passed in 0.1s\n", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner._run_pytest("/tmp/tests", None, True, False, 300)
            cmd = mock_run.call_args[0][0]
            assert "--cov" in cmd
            assert "--cov-report=json" in cmd

    def test_run_pytest_verbose_flag(self):
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="1 passed in 0.1s\n", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner._run_pytest("/tmp/tests", None, False, True, 300)
            cmd = mock_run.call_args[0][0]
            assert "-v" in cmd
            assert "-q" not in cmd

    def test_run_pytest_quiet_flag(self):
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="1 passed in 0.1s\n", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner._run_pytest("/tmp/tests", None, False, False, 300)
            cmd = mock_run.call_args[0][0]
            assert "-q" in cmd
            assert "-v" not in cmd

    def test_run_pytest_no_summary_line(self):
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="no relevant output\n", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_pytest("/tmp/tests", None, False, False, 300)
            assert result["passed"] == 0
            assert result["failed"] == 0
            assert result["summary"] == ""

    def test_run_pytest_coverage_data_loaded(self, tmp_path):
        """When .coverage and coverage.json exist, coverage data is returned."""
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="10 passed in 1.0s\n", stderr="", returncode=0)

        coverage_data = {
            "totals": {
                "percent_covered": 85.5,
                "covered_lines": 171,
                "num_statements": 200,
            }
        }
        # Create .coverage and coverage.json in cwd (which we control via chdir)
        orig_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            (tmp_path / ".coverage").write_text("")
            (tmp_path / "coverage.json").write_text(json.dumps(coverage_data))

            with patch("subprocess.run", return_value=mock_result):
                result = runner._run_pytest(str(tmp_path), None, True, False, 300)
                assert result["coverage"] is not None
                assert result["coverage"]["total_coverage"] == 85.5
                assert result["coverage"]["lines_covered"] == 171
                assert result["coverage"]["lines_total"] == 200
        finally:
            os.chdir(orig_cwd)

    # -- _run_unittest ---------------------------------------

    def test_run_unittest_ok(self):
        runner = TestRunnerTool()
        stderr_output = (
            "....\n"
            "----------------------------------------------------------------------\n"
            "Ran 4 tests in 0.002s\n"
            "\n"
            "OK\n"
        )
        mock_result = MagicMock(stdout="", stderr=stderr_output, returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_unittest("/tmp/tests", None, False, 300)
            assert result["success"] is True
            assert result["framework"] == "unittest"
            assert result["tests_run"] == 4
            assert result["passed"] == 4
            assert result["failed"] == 0

    def test_run_unittest_failures(self):
        runner = TestRunnerTool()
        stderr_output = (
            "..F.\n"
            "----------------------------------------------------------------------\n"
            "Ran 4 tests in 0.005s\n"
            "\n"
            "FAILED (failures=1)\n"
        )
        mock_result = MagicMock(stdout="", stderr=stderr_output, returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_unittest("/tmp/tests", None, False, 300)
            assert result["success"] is False
            assert result["tests_run"] == 4
            assert result["failed"] == 1
            assert result["passed"] == 3

    def test_run_unittest_with_pattern(self):
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="", stderr="Ran 0 tests in 0.0s\n\nOK\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner._run_unittest("/tmp/tests", "test_foo*.py", False, 300)
            cmd = mock_run.call_args[0][0]
            assert "-p" in cmd
            idx = cmd.index("-p")
            assert cmd[idx + 1] == "test_foo*.py"

    def test_run_unittest_with_coverage(self):
        runner = TestRunnerTool()
        # The main run
        unittest_result = MagicMock(
            stdout="", stderr="Ran 2 tests in 0.01s\n\nOK\n", returncode=0
        )
        # The coverage report run
        cov_result = MagicMock(stdout="78.5\n", stderr="", returncode=0)

        with patch("subprocess.run", side_effect=[unittest_result, cov_result]) as mock_run:
            result = runner._run_unittest("/tmp/tests", None, True, 300)
            # Should have called subprocess.run twice
            assert mock_run.call_count == 2
            # First call should use coverage
            first_cmd = mock_run.call_args_list[0][0][0]
            assert "coverage" in first_cmd
            # Coverage data parsed
            assert result["coverage"] is not None
            assert result["coverage"]["total_coverage"] == 78.5

    def test_run_unittest_coverage_bad_output(self):
        runner = TestRunnerTool()
        unittest_result = MagicMock(
            stdout="", stderr="Ran 1 tests in 0.01s\n\nOK\n", returncode=0
        )
        cov_result = MagicMock(stdout="not a number\n", stderr="", returncode=0)

        with patch("subprocess.run", side_effect=[unittest_result, cov_result]):
            result = runner._run_unittest("/tmp/tests", None, True, 300)
            # Coverage failed to parse but shouldn't crash
            assert result["coverage"] is None

    def test_run_unittest_coverage_command_fails(self):
        runner = TestRunnerTool()
        unittest_result = MagicMock(
            stdout="", stderr="Ran 1 tests in 0.01s\n\nOK\n", returncode=0
        )
        cov_result = MagicMock(stdout="", stderr="error", returncode=1)

        with patch("subprocess.run", side_effect=[unittest_result, cov_result]):
            result = runner._run_unittest("/tmp/tests", None, True, 300)
            assert result["coverage"] is None

    def test_run_unittest_empty_stderr(self):
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_unittest("/tmp/tests", None, False, 300)
            assert result["tests_run"] == 0
            assert result["summary"] == ""

    # -- _run_jest -------------------------------------------

    def test_run_jest_success(self):
        runner = TestRunnerTool()
        jest_output = json.dumps({
            "success": True,
            "numTotalTests": 10,
            "numPassedTests": 10,
            "numFailedTests": 0,
            "numPendingTests": 0,
        })
        mock_result = MagicMock(stdout=jest_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_jest("/tmp/tests", False, 300)
            assert result["success"] is True
            assert result["framework"] == "jest"
            assert result["tests_run"] == 10
            assert result["passed"] == 10
            assert result["failed"] == 0
            assert result["skipped"] == 0
            assert "10/10 tests passed" in result["summary"]

    def test_run_jest_with_failures(self):
        runner = TestRunnerTool()
        jest_output = json.dumps({
            "success": False,
            "numTotalTests": 8,
            "numPassedTests": 5,
            "numFailedTests": 3,
            "numPendingTests": 0,
        })
        mock_result = MagicMock(stdout=jest_output, stderr="", returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_jest("/tmp/tests", False, 300)
            assert result["success"] is False
            assert result["failed"] == 3
            assert result["passed"] == 5

    def test_run_jest_with_coverage(self):
        runner = TestRunnerTool()
        jest_output = json.dumps({
            "success": True,
            "numTotalTests": 5,
            "numPassedTests": 5,
            "numFailedTests": 0,
            "numPendingTests": 0,
            "coverageMap": {"total": {"lines": 85}},
        })
        mock_result = MagicMock(stdout=jest_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner._run_jest("/tmp/tests", True, 300)
            cmd = mock_run.call_args[0][0]
            assert "--coverage" in cmd
            assert result["coverage"] == {"lines": 85}

    def test_run_jest_invalid_json(self):
        runner = TestRunnerTool()
        mock_result = MagicMock(stdout="not json at all", stderr="", returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            result = runner._run_jest("/tmp/tests", False, 300)
            assert result["success"] is False
            assert "Failed to parse Jest output" in result["error"]

    def test_run_jest_without_coverage(self):
        runner = TestRunnerTool()
        jest_output = json.dumps({
            "success": True,
            "numTotalTests": 3,
            "numPassedTests": 3,
            "numFailedTests": 0,
            "numPendingTests": 1,
        })
        mock_result = MagicMock(stdout=jest_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner._run_jest("/tmp/tests", False, 300)
            cmd = mock_run.call_args[0][0]
            assert "--coverage" not in cmd
            assert result["coverage"] is None
            assert result["skipped"] == 1


# ============================================================
# CodebaseSearchTool
# ============================================================


class TestCodebaseSearchToolCoverage:
    """Cover _search_functions, _search_classes, _search_text with
    real temp files (no mocking needed)."""

    # -- _search_functions -----------------------------------

    def test_search_functions_ast(self, tmp_path):
        code = (
            'def hello_world():\n'
            '    """Greet the world."""\n'
            '    pass\n'
            '\n'
            'def goodbye_world(name):\n'
            '    pass\n'
            '\n'
            'def unrelated():\n'
            '    pass\n'
        )
        py_file = tmp_path / "module.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_functions("hello", [py_file], max_results=20)
        assert len(results) == 1
        assert results[0]["type"] == "function"
        assert results[0]["name"] == "hello_world"
        assert results[0]["line"] == 1
        assert results[0]["docstring"] == "Greet the world."
        assert results[0]["args"] == []

    def test_search_functions_with_args(self, tmp_path):
        code = "def process(data, options, verbose=False):\n    pass\n"
        py_file = tmp_path / "proc.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_functions("process", [py_file], max_results=20)
        assert len(results) == 1
        assert set(results[0]["args"]) == {"data", "options", "verbose"}

    def test_search_functions_case_insensitive(self, tmp_path):
        code = "def MyFunction():\n    pass\n"
        py_file = tmp_path / "case.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_functions("myfunction", [py_file], max_results=20)
        assert len(results) == 1
        assert results[0]["name"] == "MyFunction"

    def test_search_functions_max_results(self, tmp_path):
        code = "\n".join(f"def func_{i}():\n    pass\n" for i in range(10))
        py_file = tmp_path / "many.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_functions("func", [py_file], max_results=3)
        assert len(results) == 3

    def test_search_functions_no_match(self, tmp_path):
        code = "def something_else():\n    pass\n"
        py_file = tmp_path / "no_match.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_functions("nonexistent", [py_file], max_results=20)
        assert results == []

    def test_search_functions_syntax_error(self, tmp_path):
        code = "def broken(\n    pass\n"  # invalid syntax
        py_file = tmp_path / "broken.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_functions("broken", [py_file], max_results=20)
        # Should not crash, just skip the file
        assert results == []

    def test_search_functions_non_python_regex_fallback(self, tmp_path):
        code = (
            "function helloWorld() {\n"
            "  console.log('hello');\n"
            "}\n"
            "\n"
            "function otherFunc() {}\n"
        )
        js_file = tmp_path / "app.js"
        js_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_functions("hello", [js_file], max_results=20)
        assert len(results) == 1
        assert results[0]["name"] == "helloWorld"
        assert results[0]["line"] == 1

    # -- _search_classes -------------------------------------

    def test_search_classes_ast(self, tmp_path):
        code = (
            'class MyWidget:\n'
            '    """A custom widget."""\n'
            '    def render(self):\n'
            '        pass\n'
            '    def update(self):\n'
            '        pass\n'
            '\n'
            'class OtherThing:\n'
            '    pass\n'
        )
        py_file = tmp_path / "widgets.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_classes("widget", [py_file], max_results=20)
        assert len(results) == 1
        assert results[0]["type"] == "class"
        assert results[0]["name"] == "MyWidget"
        assert results[0]["docstring"] == "A custom widget."
        assert "render" in results[0]["methods"]
        assert "update" in results[0]["methods"]

    def test_search_classes_case_insensitive(self, tmp_path):
        code = "class FooBar:\n    pass\n"
        py_file = tmp_path / "foo.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_classes("foobar", [py_file], max_results=20)
        assert len(results) == 1
        assert results[0]["name"] == "FooBar"

    def test_search_classes_methods_limited_to_10(self, tmp_path):
        methods = "\n".join(f"    def method_{i}(self):\n        pass" for i in range(15))
        code = f"class BigClass:\n{methods}\n"
        py_file = tmp_path / "big.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_classes("BigClass", [py_file], max_results=20)
        assert len(results) == 1
        assert len(results[0]["methods"]) == 10

    def test_search_classes_no_match(self, tmp_path):
        code = "class SomeClass:\n    pass\n"
        py_file = tmp_path / "some.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_classes("Nonexistent", [py_file], max_results=20)
        assert results == []

    def test_search_classes_syntax_error(self, tmp_path):
        code = "class Broken(\n    pass\n"
        py_file = tmp_path / "broken.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_classes("Broken", [py_file], max_results=20)
        assert results == []

    def test_search_classes_non_python_regex_fallback(self, tmp_path):
        code = (
            "class MyComponent {\n"
            "  constructor() {}\n"
            "}\n"
            "\n"
            "interface MyInterface {\n"
            "  value: string;\n"
            "}\n"
        )
        ts_file = tmp_path / "comp.ts"
        ts_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_classes("My", [ts_file], max_results=20)
        assert len(results) == 2
        names = [r["name"] for r in results]
        assert "MyComponent" in names
        assert "MyInterface" in names

    def test_search_classes_max_results(self, tmp_path):
        code = "\n".join(f"class Cls{i}:\n    pass\n" for i in range(10))
        py_file = tmp_path / "many_cls.py"
        py_file.write_text(code)

        search = CodebaseSearchTool()
        results = search._search_classes("Cls", [py_file], max_results=3)
        assert len(results) == 3

    # -- _search_text ----------------------------------------

    def test_search_text_basic(self, tmp_path):
        content = (
            "line one\n"
            "line two\n"
            "the target phrase here\n"
            "line four\n"
            "line five\n"
        )
        py_file = tmp_path / "data.py"
        py_file.write_text(content)

        search = CodebaseSearchTool()
        results = search._search_text("target phrase", [py_file], max_results=20)
        assert len(results) == 1
        assert results[0]["type"] == "text"
        assert results[0]["line"] == 3
        assert "target phrase" in results[0]["match"]
        # Context should include surrounding lines
        assert "line one" in results[0]["context"] or "line two" in results[0]["context"]

    def test_search_text_case_insensitive(self, tmp_path):
        content = "Hello World\nGoodbye\n"
        py_file = tmp_path / "greet.py"
        py_file.write_text(content)

        search = CodebaseSearchTool()
        results = search._search_text("hello world", [py_file], max_results=20)
        assert len(results) == 1
        assert "Hello World" in results[0]["match"]

    def test_search_text_multiple_matches(self, tmp_path):
        content = "apple\nbanana\napple pie\norange\napple sauce\n"
        py_file = tmp_path / "fruits.py"
        py_file.write_text(content)

        search = CodebaseSearchTool()
        results = search._search_text("apple", [py_file], max_results=20)
        assert len(results) == 3

    def test_search_text_max_results(self, tmp_path):
        content = "\n".join(f"match line {i}" for i in range(10))
        py_file = tmp_path / "many.py"
        py_file.write_text(content)

        search = CodebaseSearchTool()
        results = search._search_text("match", [py_file], max_results=3)
        assert len(results) == 3

    def test_search_text_no_match(self, tmp_path):
        content = "nothing relevant here\n"
        py_file = tmp_path / "empty.py"
        py_file.write_text(content)

        search = CodebaseSearchTool()
        results = search._search_text("zzzzz", [py_file], max_results=20)
        assert results == []

    def test_search_text_context_boundaries(self, tmp_path):
        """Context at the very start/end of file should not error."""
        content = "match here\nend\n"
        py_file = tmp_path / "boundary.py"
        py_file.write_text(content)

        search = CodebaseSearchTool()
        results = search._search_text("match", [py_file], max_results=20)
        assert len(results) == 1
        # Should include context without IndexError
        assert "match here" in results[0]["context"]

    def test_search_text_skips_unreadable_files(self, tmp_path):
        # Create a binary file that will cause a read error
        bin_file = tmp_path / "binary.py"
        bin_file.write_bytes(b"\x80\x81\x82\x83")

        search = CodebaseSearchTool()
        # Should not crash
        results = search._search_text("anything", [bin_file], max_results=20)
        assert isinstance(results, list)

    def test_search_across_multiple_files(self, tmp_path):
        (tmp_path / "a.py").write_text("target in file a\n")
        (tmp_path / "b.py").write_text("target in file b\n")
        (tmp_path / "c.py").write_text("no match here\n")

        files = [tmp_path / "a.py", tmp_path / "b.py", tmp_path / "c.py"]
        search = CodebaseSearchTool()
        results = search._search_text("target", files, max_results=20)
        assert len(results) == 2
        matched_files = {r["file"] for r in results}
        assert str(tmp_path / "a.py") in matched_files
        assert str(tmp_path / "b.py") in matched_files


# ============================================================
# GitOperationsTool
# ============================================================


class TestGitOperationsToolCoverage:
    """Cover _git_blame, _git_history, _git_diff, _git_status,
    _git_branches with mocked subprocess."""

    # -- _git_blame ------------------------------------------

    def test_git_blame_parses_porcelain(self, tmp_path):
        git_tool = GitOperationsTool()
        # Create a real file so the is_file() check passes
        target = tmp_path / "test.py"
        target.write_text("print('hello')\nprint('world')\n")

        blame_output = (
            "abc1234567890 1 1 1\n"
            "author Alice\n"
            "author-time 1700000000\n"
            "summary Initial commit\n"
            "\tprint('hello')\n"
            "def4567890abc 2 2 1\n"
            "author Bob\n"
            "author-time 1700100000\n"
            "summary Second commit\n"
            "\tprint('world')\n"
        )
        mock_result = MagicMock(stdout=blame_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_blame(str(target))
            assert result["success"] is True
            assert result["total_lines"] == 2
            assert result["lines"][0]["author"] == "Alice"
            assert result["lines"][0]["timestamp"] == 1700000000
            assert result["lines"][0]["message"] == "Initial commit"
            assert result["lines"][0]["code"] == "print('hello')"
            assert result["lines"][1]["author"] == "Bob"

    def test_git_blame_with_line_range(self, tmp_path):
        git_tool = GitOperationsTool()
        target = tmp_path / "test.py"
        target.write_text("a\nb\nc\n")

        blame_output = (
            "aaa 1 1 1\nauthor A\nauthor-time 100\nsummary s1\n\tline1\n"
            "bbb 2 2 1\nauthor B\nauthor-time 200\nsummary s2\n\tline2\n"
            "ccc 3 3 1\nauthor C\nauthor-time 300\nsummary s3\n\tline3\n"
        )
        mock_result = MagicMock(stdout=blame_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_blame(str(target), line_range=(2, 3))
            assert result["success"] is True
            # line_range (2,3) means blame_data[1:3]
            assert len(result["lines"]) == 2
            assert result["lines"][0]["author"] == "B"
            assert result["lines"][1]["author"] == "C"

    def test_git_blame_not_a_file(self, tmp_path):
        git_tool = GitOperationsTool()
        result = git_tool._git_blame(str(tmp_path))  # directory, not file
        assert result["success"] is False
        assert "Not a file" in result["error"]

    def test_git_blame_git_error(self, tmp_path):
        git_tool = GitOperationsTool()
        target = tmp_path / "test.py"
        target.write_text("x\n")

        mock_result = MagicMock(stdout="", stderr="fatal: not a git repo", returncode=128)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_blame(str(target))
            assert result["success"] is False
            assert "fatal" in result["error"]

    # -- _git_history ----------------------------------------

    def test_git_history_parses_commits(self):
        git_tool = GitOperationsTool()
        log_output = (
            "abc12345|Alice|alice@example.com|1700000000|Initial commit\n"
            "def67890|Bob|bob@example.com|1700100000|Fix bug\n"
        )
        mock_result = MagicMock(stdout=log_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_history(".", 10)
            assert result["success"] is True
            assert result["commits_found"] == 2
            assert result["commits"][0]["hash"] == "abc12345"
            assert result["commits"][0]["author"] == "Alice"
            assert result["commits"][0]["email"] == "alice@example.com"
            assert result["commits"][0]["timestamp"] == 1700000000
            assert result["commits"][0]["message"] == "Initial commit"
            assert result["commits"][1]["hash"] == "def67890"

    def test_git_history_no_commits(self):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_history(".", 10)
            assert result["success"] is True
            assert result["commits_found"] == 0
            assert result["commits"] == []

    def test_git_history_error(self):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="", stderr="fatal: bad default revision", returncode=128)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_history(".", 10)
            assert result["success"] is False

    def test_git_history_max_commits_in_command(self):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            git_tool._git_history(".", 5)
            cmd = mock_run.call_args[0][0]
            assert "-5" in cmd

    def test_git_history_message_with_pipes(self):
        """Commit messages may contain pipe characters."""
        git_tool = GitOperationsTool()
        log_output = "abc12345|Alice|alice@ex.com|1700000000|Fix: use a|b pattern\n"
        mock_result = MagicMock(stdout=log_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_history(".", 10)
            assert result["commits"][0]["message"] == "Fix: use a|b pattern"

    # -- _git_diff -------------------------------------------

    def test_git_diff_working_tree(self):
        git_tool = GitOperationsTool()
        diff_output = "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        stat_output = " file.py | 2 +-\n 1 file changed\n"

        mock_diff = MagicMock(stdout=diff_output, stderr="", returncode=0)
        mock_stat = MagicMock(stdout=stat_output, stderr="", returncode=0)

        with patch("subprocess.run", side_effect=[mock_diff, mock_stat]):
            result = git_tool._git_diff(".", None, None)
            assert result["success"] is True
            assert result["has_changes"] is True
            assert "file.py" in result["diff"]
            assert result["stats"] is not None

    def test_git_diff_with_commits(self):
        git_tool = GitOperationsTool()
        mock_diff = MagicMock(stdout="some diff", stderr="", returncode=0)
        mock_stat = MagicMock(stdout="stat output", stderr="", returncode=0)

        with patch("subprocess.run", side_effect=[mock_diff, mock_stat]) as mock_run:
            result = git_tool._git_diff(".", "abc123", "def456")
            cmd = mock_run.call_args_list[0][0][0]
            assert "abc123" in cmd
            assert "def456" in cmd
            assert result["success"] is True

    def test_git_diff_with_specific_file(self):
        git_tool = GitOperationsTool()
        mock_diff = MagicMock(stdout="", stderr="", returncode=0)
        mock_stat = MagicMock(stdout="", stderr="", returncode=0)

        with patch("subprocess.run", side_effect=[mock_diff, mock_stat]) as mock_run:
            result = git_tool._git_diff("file.py", None, None)
            cmd = mock_run.call_args_list[0][0][0]
            assert "--" in cmd
            assert "file.py" in cmd
            assert result["has_changes"] is False

    def test_git_diff_error(self):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="", stderr="fatal: error", returncode=128)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_diff(".", None, None)
            assert result["success"] is False

    def test_git_diff_stat_fails(self):
        git_tool = GitOperationsTool()
        mock_diff = MagicMock(stdout="some diff\n", stderr="", returncode=0)
        mock_stat = MagicMock(stdout="", stderr="error", returncode=1)

        with patch("subprocess.run", side_effect=[mock_diff, mock_stat]):
            result = git_tool._git_diff(".", None, None)
            assert result["success"] is True
            assert result["stats"] is None

    def test_git_diff_only_commit1(self):
        git_tool = GitOperationsTool()
        mock_diff = MagicMock(stdout="diff", stderr="", returncode=0)
        mock_stat = MagicMock(stdout="stat", stderr="", returncode=0)

        with patch("subprocess.run", side_effect=[mock_diff, mock_stat]) as mock_run:
            git_tool._git_diff(".", "HEAD~1", None)
            cmd = mock_run.call_args_list[0][0][0]
            assert "HEAD~1" in cmd

    # -- _git_status -----------------------------------------

    def test_git_status_parses_porcelain(self):
        git_tool = GitOperationsTool()
        status_output = (
            " M agents/config.py\n"
            "A  agents/new_file.py\n"
            " D agents/old_file.py\n"
            "?? untracked.txt\n"
        )
        mock_result = MagicMock(stdout=status_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_status(".")
            assert result["success"] is True
            assert "agents/config.py" in result["modified"]
            assert "agents/new_file.py" in result["added"]
            assert "agents/old_file.py" in result["deleted"]
            assert "untracked.txt" in result["untracked"]
            assert result["clean"] is False

    def test_git_status_clean(self):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_status(".")
            assert result["success"] is True
            assert result["clean"] is True
            assert result["modified"] == []
            assert result["added"] == []
            assert result["deleted"] == []
            assert result["untracked"] == []

    def test_git_status_error(self):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="", stderr="fatal: not a git repo", returncode=128)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_status(".")
            assert result["success"] is False

    def test_git_status_modified_both_staged_and_unstaged(self):
        git_tool = GitOperationsTool()
        status_output = "MM agents/config.py\n"
        mock_result = MagicMock(stdout=status_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_status(".")
            assert "agents/config.py" in result["modified"]

    # -- _git_branches ---------------------------------------

    def test_git_branches_parses_output(self):
        git_tool = GitOperationsTool()
        branch_output = (
            "* main\n"
            "  feature/new-stuff\n"
            "  bugfix/fix-123\n"
            "  remotes/origin/main\n"
        )
        mock_result = MagicMock(stdout=branch_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_branches(".")
            assert result["success"] is True
            assert result["current_branch"] == "main"
            assert "main" in result["branches"]
            assert "feature/new-stuff" in result["branches"]
            assert "bugfix/fix-123" in result["branches"]
            assert "remotes/origin/main" in result["branches"]
            assert result["total_branches"] == 4

    def test_git_branches_no_current(self):
        git_tool = GitOperationsTool()
        branch_output = "  detached-head\n  feature/x\n"
        mock_result = MagicMock(stdout=branch_output, stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_branches(".")
            assert result["success"] is True
            assert result["current_branch"] is None
            assert result["total_branches"] == 2

    def test_git_branches_error(self):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="", stderr="fatal: not a git repo", returncode=128)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_branches(".")
            assert result["success"] is False

    def test_git_branches_uses_cwd_for_directory(self, tmp_path):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="* main\n", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            git_tool._git_branches(str(tmp_path))
            kwargs = mock_run.call_args[1]
            assert kwargs["cwd"] == str(tmp_path)

    def test_git_branches_uses_dot_for_file_path(self, tmp_path):
        git_tool = GitOperationsTool()
        target_file = tmp_path / "test.py"
        target_file.write_text("x\n")
        mock_result = MagicMock(stdout="* main\n", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            git_tool._git_branches(str(target_file))
            kwargs = mock_run.call_args[1]
            assert kwargs["cwd"] == "."

    def test_git_branches_empty_output(self):
        git_tool = GitOperationsTool()
        mock_result = MagicMock(stdout="", stderr="", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = git_tool._git_branches(".")
            assert result["success"] is True
            assert result["branches"] == []
            assert result["current_branch"] is None
            assert result["total_branches"] == 0


# ============================================================
# DataParserTool
# ============================================================


class TestDataParserToolCoverage:
    """Cover _xml_to_dict and _summarize_data."""

    # -- _xml_to_dict ----------------------------------------

    def test_xml_to_dict_simple(self):
        parser = DataParserTool()
        xml_str = "<root><name>Alice</name><age>30</age></root>"
        root = ET.fromstring(xml_str)
        result = parser._xml_to_dict(root)
        assert result["name"]["@text"] == "Alice"
        assert result["age"]["@text"] == "30"

    def test_xml_to_dict_with_attributes(self):
        parser = DataParserTool()
        xml_str = '<root><item id="1" type="book">Title</item></root>'
        root = ET.fromstring(xml_str)
        result = parser._xml_to_dict(root)
        assert result["item"]["@attributes"]["id"] == "1"
        assert result["item"]["@attributes"]["type"] == "book"
        assert result["item"]["@text"] == "Title"

    def test_xml_to_dict_duplicate_children(self):
        parser = DataParserTool()
        xml_str = "<root><item>A</item><item>B</item><item>C</item></root>"
        root = ET.fromstring(xml_str)
        result = parser._xml_to_dict(root)
        # Should become a list
        assert isinstance(result["item"], list)
        assert len(result["item"]) == 3
        assert result["item"][0]["@text"] == "A"
        assert result["item"][1]["@text"] == "B"
        assert result["item"][2]["@text"] == "C"

    def test_xml_to_dict_nested(self):
        parser = DataParserTool()
        xml_str = "<root><parent><child>Value</child></parent></root>"
        root = ET.fromstring(xml_str)
        result = parser._xml_to_dict(root)
        assert result["parent"]["child"]["@text"] == "Value"

    def test_xml_to_dict_empty_element(self):
        parser = DataParserTool()
        xml_str = "<root><empty/></root>"
        root = ET.fromstring(xml_str)
        result = parser._xml_to_dict(root)
        assert result["empty"] == {}

    def test_xml_to_dict_mixed_content(self):
        parser = DataParserTool()
        xml_str = '<root attr="val"><child>text</child></root>'
        root = ET.fromstring(xml_str)
        result = parser._xml_to_dict(root)
        assert result["@attributes"]["attr"] == "val"
        assert result["child"]["@text"] == "text"

    def test_xml_to_dict_whitespace_text_ignored(self):
        parser = DataParserTool()
        xml_str = "<root>   \n   </root>"
        root = ET.fromstring(xml_str)
        result = parser._xml_to_dict(root)
        # Whitespace-only text should not produce @text key
        assert "@text" not in result

    def test_xml_to_dict_two_duplicate_children_becomes_list(self):
        """Two children with the same tag: first time converts to list."""
        parser = DataParserTool()
        xml_str = "<root><tag>first</tag><tag>second</tag></root>"
        root = ET.fromstring(xml_str)
        result = parser._xml_to_dict(root)
        assert isinstance(result["tag"], list)
        assert len(result["tag"]) == 2

    # -- _summarize_data -------------------------------------

    def test_summarize_dict(self):
        parser = DataParserTool()
        summary = parser._summarize_data({"a": 1, "b": 2, "c": 3})
        assert summary["type"] == "object"
        assert summary["total_keys"] == 3
        assert set(summary["keys"]) == {"a", "b", "c"}

    def test_summarize_dict_many_keys(self):
        parser = DataParserTool()
        big_dict = {f"key_{i}": i for i in range(30)}
        summary = parser._summarize_data(big_dict)
        assert summary["type"] == "object"
        assert summary["total_keys"] == 30
        assert len(summary["keys"]) == 20  # limited to 20

    def test_summarize_list(self):
        parser = DataParserTool()
        summary = parser._summarize_data([1, 2, 3])
        assert summary["type"] == "array"
        assert summary["length"] == 3
        assert summary["item_type"] == "int"

    def test_summarize_empty_list(self):
        parser = DataParserTool()
        summary = parser._summarize_data([])
        assert summary["type"] == "array"
        assert summary["length"] == 0
        assert summary["item_type"] == "unknown"

    def test_summarize_list_of_dicts(self):
        parser = DataParserTool()
        summary = parser._summarize_data([{"a": 1}, {"b": 2}])
        assert summary["type"] == "array"
        assert summary["item_type"] == "dict"

    def test_summarize_string(self):
        parser = DataParserTool()
        summary = parser._summarize_data("hello world")
        assert summary["type"] == "str"
        assert summary["value"] == "hello world"

    def test_summarize_int(self):
        parser = DataParserTool()
        summary = parser._summarize_data(42)
        assert summary["type"] == "int"
        assert summary["value"] == "42"

    def test_summarize_float(self):
        parser = DataParserTool()
        summary = parser._summarize_data(3.14)
        assert summary["type"] == "float"
        assert summary["value"] == "3.14"

    def test_summarize_bool(self):
        parser = DataParserTool()
        summary = parser._summarize_data(True)
        assert summary["type"] == "bool"
        assert summary["value"] == "True"

    def test_summarize_none(self):
        parser = DataParserTool()
        summary = parser._summarize_data(None)
        assert summary["type"] == "NoneType"
        assert summary["value"] == "None"

    def test_summarize_long_string_truncated(self):
        parser = DataParserTool()
        long_str = "x" * 200
        summary = parser._summarize_data(long_str)
        assert len(summary["value"]) == 100
