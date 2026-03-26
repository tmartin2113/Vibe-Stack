"""
Tests for the Tool System (agents/tools/).

Covers:
- ToolResult dataclass and serialization
- Tool base class (schema, validation)
- PythonExecutor (execution, timeout, temp file cleanup)
- PytestRunner (path validation)
- BanditScanner (path validation, severity mapping)
- FileReader (path validation, size limits, encoding)
- FileWriter (path validation, size limits, auto-mkdir)
- _validate_file_path() security boundary
- ToolRegistry (register, get, list, execute, schemas, parse_tool_call)
- create_default_tool_registry()
- StaticCodeAnalyzer (language detection, deduplication)
- CodebaseSearchTool (function/class/text search, auto-detect)
- GitOperationsTool (status, branches, history)
- DataParserTool (JSON, CSV, XML, auto-detect)
- SEOChecklistTool (full checklist validation)
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

import pytest

from agents.tools.registry import (
    ToolResult,
    ToolCategory,
    Tool,
    ToolRegistry,
    PythonExecutor,
    PytestRunner,
    BanditScanner,
    FileReader,
    FileWriter,
    ShellExecutor,
    WebFetchTool,
    DevToolWrapper,
    _validate_file_path,
    _build_allowed_file_dirs,
    _DEFAULT_ALLOWED_FILE_DIRS,
    create_default_tool_registry,
    MAX_FILE_READ_SIZE,
    MAX_FILE_WRITE_SIZE,
)
from agents.tools.dev_tools import (
    StaticCodeAnalyzer,
    CodebaseSearchTool,
    GitOperationsTool,
    DataParserTool,
    CodeIssue,
)
from agents.tools.seo_tools import SEOChecklistTool, SEOIssue

# Resolve project root from this file's location so tests work both
# inside the Docker container (/home/user/Vibe) and on dev machines.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# ToolResult
# ============================================================


class TestToolResult:
    def test_success_result(self):
        r = ToolResult(success=True, output="hello")
        assert r.success is True
        assert r.output == "hello"
        assert r.error is None
        assert r.metadata == {}

    def test_error_result(self):
        r = ToolResult(success=False, output="", error="boom", metadata={"code": 1})
        assert r.success is False
        assert r.error == "boom"
        assert r.metadata["code"] == 1

    def test_to_dict(self):
        r = ToolResult(success=True, output="ok", metadata={"k": "v"})
        d = r.to_dict()
        assert d == {
            "success": True,
            "output": "ok",
            "error": None,
            "metadata": {"k": "v"},
        }

    def test_to_dict_roundtrip(self):
        r = ToolResult(success=False, output="x", error="e", metadata={"a": 1})
        d = r.to_dict()
        assert json.dumps(d)  # serializable


# ============================================================
# Tool base class
# ============================================================


class ConcreteTool(Tool):
    """Minimal concrete tool for testing the abstract base class."""

    def __init__(self):
        super().__init__(
            name="test_tool",
            description="A test tool",
            category=ToolCategory.CODE_EXECUTION,
        )

    def execute(self, code: str = "", **kwargs) -> ToolResult:
        return ToolResult(success=True, output=code)

    def _get_parameters_schema(self):
        return {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        }


class TestToolBase:
    def test_get_schema(self):
        t = ConcreteTool()
        schema = t.get_schema()
        assert schema["name"] == "test_tool"
        assert schema["category"] == "code_execution"
        assert "parameters" in schema

    def test_validate_params_ok(self):
        t = ConcreteTool()
        assert t.validate_params(code="x") is True

    def test_validate_params_missing_required(self):
        t = ConcreteTool()
        with pytest.raises(ValueError, match="Missing required parameter: code"):
            t.validate_params()

    def test_enabled_default(self):
        t = ConcreteTool()
        assert t.enabled is True


# ============================================================
# _validate_file_path
# ============================================================


class TestValidateFilePath:
    def test_allowed_path_project(self):
        ok, err = _validate_file_path(
            str(_PROJECT_ROOT / "agents" / "nodes.py"),
            allowed_dirs=[_PROJECT_ROOT],
        )
        assert ok is True
        assert err is None

    def test_allowed_path_tmp(self):
        ok, err = _validate_file_path("/tmp/somefile.txt")
        assert ok is True
        assert err is None

    def test_disallowed_path_root(self):
        ok, err = _validate_file_path("/etc/passwd")
        assert ok is False
        assert "outside allowed directories" in err.lower()

    def test_disallowed_path_home_other_user(self):
        ok, err = _validate_file_path("/home/other/secret.txt")
        assert ok is False

    def test_relative_path_resolves(self):
        """Relative paths resolve against cwd — allowed when cwd is in allowed_dirs."""
        ok, err = _validate_file_path(
            "agents/tools/registry.py",
            allowed_dirs=[_PROJECT_ROOT],
        )
        assert ok is True


class TestConfigurableFileDirs:
    """Test user-configurable allowed file directories."""

    def test_custom_dirs_allow_new_path(self, tmp_path):
        custom = [Path(str(tmp_path)).resolve()]
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")
        ok, err = _validate_file_path(str(test_file), allowed_dirs=custom)
        assert ok is True

    def test_custom_dirs_block_old_defaults(self, tmp_path):
        custom = [Path(str(tmp_path)).resolve()]
        ok, err = _validate_file_path(str(_PROJECT_ROOT / "agents" / "nodes.py"), allowed_dirs=custom)
        assert ok is False
        assert "outside allowed" in err.lower()

    def test_build_from_config_list(self, tmp_path):
        dirs = _build_allowed_file_dirs([str(tmp_path), "/opt/data"])
        assert Path(str(tmp_path)).resolve() in dirs
        assert Path("/opt/data").resolve() in dirs

    def test_build_falls_back_to_env(self):
        with patch.dict(os.environ, {"VIBE_ALLOWED_FILE_DIRS": "/srv/data:/opt/models"}):
            dirs = _build_allowed_file_dirs(None)
        assert Path("/srv/data").resolve() in dirs
        assert Path("/opt/models").resolve() in dirs

    def test_build_falls_back_to_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            dirs = _build_allowed_file_dirs(None)
        assert dirs == _DEFAULT_ALLOWED_FILE_DIRS

    def test_file_reader_uses_custom_dirs(self, tmp_path):
        custom = [Path(str(tmp_path)).resolve()]
        reader = FileReader(allowed_dirs=custom)
        # File inside custom dir — should work
        test_file = tmp_path / "readable.txt"
        test_file.write_text("content")
        result = reader.execute(file_path=str(test_file))
        assert result.success is True
        assert result.output == "content"

    def test_file_reader_blocks_outside_custom_dirs(self, tmp_path):
        custom = [Path(str(tmp_path)).resolve()]
        reader = FileReader(allowed_dirs=custom)
        result = reader.execute(file_path="/etc/hostname")
        assert result.success is False
        assert "security" in result.error.lower()

    def test_file_writer_uses_custom_dirs(self, tmp_path):
        custom = [Path(str(tmp_path)).resolve()]
        writer = FileWriter(allowed_dirs=custom)
        out_file = tmp_path / "writable.txt"
        result = writer.execute(file_path=str(out_file), content="hello")
        assert result.success is True
        assert out_file.read_text() == "hello"

    def test_file_writer_blocks_outside_custom_dirs(self, tmp_path):
        custom = [Path(str(tmp_path)).resolve()]
        writer = FileWriter(allowed_dirs=custom)
        result = writer.execute(file_path="/etc/evil.txt", content="bad")
        assert result.success is False

    def test_sandbox_config_allowed_file_dir_list(self):
        from agents.sandbox.config import SandboxConfig
        cfg = SandboxConfig(allowed_file_dirs="/home/user/project:/data/models")
        dirs = cfg.allowed_file_dir_list
        assert "/home/user/project" in dirs
        assert "/data/models" in dirs

    def test_sandbox_config_empty_allowed_file_dirs(self):
        from agents.sandbox.config import SandboxConfig
        cfg = SandboxConfig()
        assert cfg.allowed_file_dir_list == []

    def test_sandbox_config_env_override(self):
        from agents.sandbox.config import SandboxConfig
        cfg = SandboxConfig()
        with patch.dict(os.environ, {"VIBE_ALLOWED_FILE_DIRS": "/opt/data:/srv/files"}):
            cfg.apply_env_overrides()
        assert cfg.allowed_file_dirs == "/opt/data:/srv/files"
        dirs = cfg.allowed_file_dir_list
        assert "/opt/data" in dirs
        assert "/srv/files" in dirs

    def test_registry_passes_dirs_to_tools(self, tmp_path):
        custom = [str(tmp_path)]
        reg = create_default_tool_registry(sandbox_pool=MagicMock(), allowed_file_dirs=custom)
        reader = reg.get("file_reader")
        writer = reg.get("file_writer")
        assert reader._allowed_dirs is not None
        assert writer._allowed_dirs is not None
        assert Path(str(tmp_path)).resolve() in reader._allowed_dirs
        assert Path(str(tmp_path)).resolve() in writer._allowed_dirs


# ============================================================
# PythonExecutor
# ============================================================


class TestPythonExecutor:
    def test_successful_execution(self):
        exe = PythonExecutor()
        result = exe.execute(code="print('hello world')")
        assert result.success is True
        assert "hello world" in result.output

    def test_execution_with_error(self):
        exe = PythonExecutor()
        result = exe.execute(code="raise ValueError('boom')")
        assert result.success is False
        assert "boom" in result.error

    def test_timeout(self):
        exe = PythonExecutor()
        result = exe.execute(code="import time; time.sleep(10)", timeout=1)
        assert result.success is False
        assert "timed out" in result.error.lower()

    def test_temp_file_cleanup(self):
        """Temp file should be cleaned up even on success."""
        exe = PythonExecutor()
        # We'll intercept to capture the temp file path
        temp_files_before = set(Path(tempfile.gettempdir()).glob("tmp*.py"))
        exe.execute(code="print(1)")
        temp_files_after = set(Path(tempfile.gettempdir()).glob("tmp*.py"))
        # No new .py temp files should linger
        new_files = temp_files_after - temp_files_before
        assert len(new_files) == 0

    def test_schema(self):
        exe = PythonExecutor()
        schema = exe._get_parameters_schema()
        assert "code" in schema["properties"]
        assert "code" in schema["required"]

    def test_multiline_code(self):
        exe = PythonExecutor()
        code = "x = 5\ny = 10\nprint(x + y)"
        result = exe.execute(code=code)
        assert result.success is True
        assert "15" in result.output


# ============================================================
# PytestRunner
# ============================================================


class TestPytestRunner:
    def test_path_validation_blocked(self):
        runner = PytestRunner()
        result = runner.execute(test_file="/etc/passwd")
        assert result.success is False
        assert "Security" in result.error

    def test_file_not_found(self):
        runner = PytestRunner()
        result = runner.execute(test_file="/tmp/nonexistent_test_file_12345.py")
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_schema(self):
        runner = PytestRunner()
        schema = runner._get_parameters_schema()
        assert "test_file" in schema["required"]


# ============================================================
# BanditScanner
# ============================================================


class TestBanditScanner:
    def test_path_validation_blocked(self):
        scanner = BanditScanner()
        result = scanner.execute(target="/etc/shadow")
        assert result.success is False
        assert "Security" in result.error

    def test_target_not_found(self):
        scanner = BanditScanner()
        result = scanner.execute(target="/tmp/nonexistent_dir_12345")
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_schema(self):
        scanner = BanditScanner()
        schema = scanner._get_parameters_schema()
        assert "target" in schema["required"]

    def test_severity_mapping(self):
        """Verify severity flag mapping works."""
        scanner = BanditScanner(allowed_dirs=[_PROJECT_ROOT])
        # We test the internal mapping by checking the execute path
        # with a mocked subprocess
        with patch("agents.tools.registry.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"results": []}),
                stderr="",
            )
            result = scanner.execute(
                target=str(_PROJECT_ROOT / "agents" / "__init__.py"),
                severity_level="high",
            )
            # high severity means no flag appended (empty string)
            call_args = mock_run.call_args[0][0]
            assert "-l" not in call_args
            assert "-ll" not in call_args


# ============================================================
# FileReader
# ============================================================


class TestFileReader:
    def test_read_existing_file(self):
        reader = FileReader(allowed_dirs=[_PROJECT_ROOT])
        # Read a known file in the project
        result = reader.execute(file_path=str(_PROJECT_ROOT / "agents" / "tools" / "__init__.py"))
        assert result.success is True
        assert "ToolRegistry" in result.output
        assert result.metadata["lines"] > 0

    def test_path_blocked(self):
        reader = FileReader()
        result = reader.execute(file_path="/etc/hosts")
        assert result.success is False
        assert "Security" in result.error

    def test_file_not_found(self):
        reader = FileReader()
        result = reader.execute(file_path="/tmp/does_not_exist_abc.txt")
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_directory_not_file(self):
        reader = FileReader(allowed_dirs=[_PROJECT_ROOT])
        result = reader.execute(file_path=str(_PROJECT_ROOT / "agents"))
        assert result.success is False
        assert "not a file" in result.error.lower()

    def test_file_too_large(self):
        reader = FileReader(allowed_dirs=[_PROJECT_ROOT])
        with patch("agents.tools.registry.Path.stat") as mock_stat:
            mock_stat.return_value = MagicMock(st_size=MAX_FILE_READ_SIZE + 1)
            with patch("agents.tools.registry.Path.exists", return_value=True):
                with patch("agents.tools.registry.Path.is_file", return_value=True):
                    big_path = str(_PROJECT_ROOT / "big.bin")
                    with patch("agents.tools.registry.Path.resolve", return_value=Path(big_path)):
                        result = reader.execute(file_path=big_path)
                        assert result.success is False
                        assert "too large" in result.error.lower()

    def test_read_tmp_file(self):
        reader = FileReader()
        # Create a temp file in /tmp (allowed directory)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", dir="/tmp", delete=False
        ) as f:
            f.write("test content 123")
            path = f.name
        try:
            result = reader.execute(file_path=path)
            assert result.success is True
            assert result.output == "test content 123"
        finally:
            os.unlink(path)


# ============================================================
# FileWriter
# ============================================================


class TestFileWriter:
    def test_write_new_file(self):
        writer = FileWriter()
        path = f"/tmp/test_write_{os.getpid()}.txt"
        try:
            result = writer.execute(file_path=path, content="hello world")
            assert result.success is True
            assert "hello world" in Path(path).read_text()
            assert result.metadata["characters"] == 11
        finally:
            Path(path).unlink(missing_ok=True)

    def test_path_blocked(self):
        writer = FileWriter()
        result = writer.execute(file_path="/etc/test.txt", content="x")
        assert result.success is False
        assert "Security" in result.error

    def test_content_too_large(self):
        writer = FileWriter()
        big_content = "x" * (MAX_FILE_WRITE_SIZE + 1)
        result = writer.execute(file_path="/tmp/big.txt", content=big_content)
        assert result.success is False
        assert "too large" in result.error.lower()

    def test_auto_mkdir(self):
        writer = FileWriter()
        dir_path = f"/tmp/test_auto_mkdir_{os.getpid()}"
        file_path = f"{dir_path}/sub/file.txt"
        try:
            result = writer.execute(file_path=file_path, content="nested")
            assert result.success is True
            assert Path(file_path).read_text() == "nested"
        finally:
            import shutil
            shutil.rmtree(dir_path, ignore_errors=True)

    def test_schema(self):
        writer = FileWriter()
        schema = writer._get_parameters_schema()
        assert "file_path" in schema["required"]
        assert "content" in schema["required"]


# ============================================================
# ToolRegistry
# ============================================================


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = ConcreteTool()
        reg.register(tool)
        assert reg.get("test_tool") is tool

    def test_get_missing(self):
        reg = ToolRegistry()
        assert reg.get("nonexistent") is None

    def test_list_tools(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        assert "test_tool" in reg.list_tools()

    def test_get_tools_by_category(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        code_tools = reg.get_tools_by_category(ToolCategory.CODE_EXECUTION)
        assert len(code_tools) == 1
        assert code_tools[0].name == "test_tool"

        file_tools = reg.get_tools_by_category(ToolCategory.FILE_OPS)
        assert len(file_tools) == 0

    def test_execute_tool(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        result = reg.execute_tool("test_tool", code="abc")
        assert result.success is True
        assert result.output == "abc"

    def test_execute_missing_tool(self):
        reg = ToolRegistry()
        result = reg.execute_tool("ghost")
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_execute_disabled_tool(self):
        reg = ToolRegistry()
        tool = ConcreteTool()
        tool.enabled = False
        reg.register(tool)
        result = reg.execute_tool("test_tool", code="x")
        assert result.success is False
        assert "disabled" in result.error.lower()

    def test_execute_validation_error(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        result = reg.execute_tool("test_tool")  # missing required 'code'
        assert result.success is False
        assert "Invalid parameters" in result.error

    def test_get_all_schemas(self):
        reg = ToolRegistry()
        reg.register(ConcreteTool())
        schemas = reg.get_all_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "test_tool"

    def test_schemas_skip_disabled(self):
        reg = ToolRegistry()
        tool = ConcreteTool()
        tool.enabled = False
        reg.register(tool)
        assert len(reg.get_all_schemas()) == 0

    def test_parse_tool_call_valid(self):
        reg = ToolRegistry()
        output = 'Some text <tool_call name="my_tool">{"arg": "val"}</tool_call> more text'
        parsed = reg.parse_tool_call(output)
        assert parsed is not None
        assert parsed["name"] == "my_tool"
        assert parsed["params"] == {"arg": "val"}

    def test_parse_tool_call_single_quotes(self):
        reg = ToolRegistry()
        output = "<tool_call name='my_tool'>{\"x\": 1}</tool_call>"
        parsed = reg.parse_tool_call(output)
        assert parsed is not None
        assert parsed["name"] == "my_tool"

    def test_parse_tool_call_no_match(self):
        reg = ToolRegistry()
        assert reg.parse_tool_call("no tool call here") is None

    def test_parse_tool_call_invalid_json(self):
        reg = ToolRegistry()
        output = '<tool_call name="t">not json</tool_call>'
        assert reg.parse_tool_call(output) is None


# ============================================================
# create_default_tool_registry
# ============================================================


class TestDefaultRegistry:
    def test_default_tools_registered(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        tools = reg.list_tools()
        # Core tools
        assert "python_executor" in tools
        assert "pytest_runner" in tools
        assert "bandit" in tools
        assert "file_reader" in tools
        assert "file_writer" in tools
        # New tools
        assert "shell_executor" in tools
        assert "static_code_analyzer" in tools
        assert "codebase_search" in tools
        assert "git_operations" in tools
        assert "data_parser" in tools
        assert "dependency_scanner" in tools
        assert "container_inspect" in tools
        assert "lighthouse_seo" in tools
        assert "page_analyzer" in tools
        assert "seo_checklist" in tools
        assert len(tools) == 17

    def test_default_tools_have_schemas(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        schemas = reg.get_all_schemas()
        assert len(schemas) == 17
        names = {s["name"] for s in schemas}
        assert "python_executor" in names
        assert "shell_executor" in names
        assert "static_code_analyzer" in names
        assert "codebase_search" in names
        assert "git_operations" in names
        assert "data_parser" in names


# ============================================================
# StaticCodeAnalyzer
# ============================================================


class TestStaticCodeAnalyzer:
    def test_detect_language_python(self):
        analyzer = StaticCodeAnalyzer()
        assert analyzer._detect_language(Path("test.py")) == "python"

    def test_detect_language_javascript(self):
        analyzer = StaticCodeAnalyzer()
        assert analyzer._detect_language(Path("app.js")) == "javascript"
        assert analyzer._detect_language(Path("app.tsx")) == "typescript"

    def test_detect_language_unknown(self):
        analyzer = StaticCodeAnalyzer()
        assert analyzer._detect_language(Path("data.bin")) == "unknown"

    def test_nonexistent_path(self):
        analyzer = StaticCodeAnalyzer()
        result = analyzer.execute("/nonexistent/path")
        assert result["success"] is False
        assert "does not exist" in result["error"]

    def test_deduplicate_issues(self):
        analyzer = StaticCodeAnalyzer()
        issues = [
            CodeIssue("error", "f.py", 10, 1, "unused import os", "F401"),
            CodeIssue("error", "f.py", 10, 1, "unused import os", "W0611"),  # dup
            CodeIssue("warning", "f.py", 20, 1, "different issue", "W001"),
        ]
        unique = analyzer._deduplicate_issues(issues)
        assert len(unique) == 2

    def test_no_tools_available(self):
        analyzer = StaticCodeAnalyzer()
        with patch.object(
            analyzer,
            "_check_tool_availability",
            return_value={"ruff": False, "pylint": False, "mypy": False, "pyflakes": False, "eslint": False},
        ):
            # Use a real existing path
            result = analyzer.execute(str(_PROJECT_ROOT / "agents" / "__init__.py"))
            assert result["success"] is False
            assert "No analysis tools available" in result["error"]


# ============================================================
# CodebaseSearchTool
# ============================================================


class TestCodebaseSearchTool:
    _tools_path = str(_PROJECT_ROOT / "agents" / "tools")

    def test_auto_detect_class(self):
        """CamelCase query should auto-detect as class search."""
        search = CodebaseSearchTool()
        result = search.execute("ToolRegistry", path=self._tools_path, max_results=5)
        assert result["success"] is True
        assert result["search_type"] == "class"
        assert result["results_found"] >= 1
        # Should find ToolRegistry class definition
        names = [r["name"] for r in result["results"]]
        assert "ToolRegistry" in names

    def test_auto_detect_function(self):
        """snake_case query should auto-detect as function search."""
        search = CodebaseSearchTool()
        result = search.execute("create_default_tool_registry", path=self._tools_path, max_results=5)
        assert result["success"] is True
        assert result["search_type"] == "function"
        assert result["results_found"] >= 1

    def test_text_search(self):
        search = CodebaseSearchTool()
        result = search.execute("ALLOWED_FILE_DIRS", path=self._tools_path, search_type="text", max_results=5)
        assert result["success"] is True
        assert result["results_found"] >= 1

    def test_nonexistent_path(self):
        search = CodebaseSearchTool()
        result = search.execute("anything", path="/nonexistent")
        assert result["success"] is False

    def test_no_results(self):
        search = CodebaseSearchTool()
        result = search.execute("zzz_nonexistent_symbol_xyz", path=self._tools_path, max_results=5)
        assert result["success"] is True
        assert result["results_found"] == 0

    def test_explicit_function_search(self):
        search = CodebaseSearchTool()
        result = search.execute("execute", path=self._tools_path, search_type="function", max_results=5)
        assert result["success"] is True
        assert result["search_type"] == "function"
        # "execute" is defined in many tool classes
        assert result["results_found"] >= 1


# ============================================================
# GitOperationsTool
# ============================================================


class TestGitOperationsTool:
    _root = str(_PROJECT_ROOT)

    def test_status(self):
        git_tool = GitOperationsTool()
        result = git_tool.execute("status", path=self._root)
        assert result["success"] is True
        assert "modified" in result
        assert "untracked" in result

    def test_branches(self):
        git_tool = GitOperationsTool()
        result = git_tool.execute("branches", path=self._root)
        assert result["success"] is True
        assert result["current_branch"] is not None
        assert len(result["branches"]) >= 1

    def test_history(self):
        git_tool = GitOperationsTool()
        result = git_tool.execute("history", path=self._root, max_commits=3)
        assert result["success"] is True
        assert len(result["commits"]) <= 3
        if result["commits"]:
            assert "hash" in result["commits"][0]
            assert "author" in result["commits"][0]
            assert "message" in result["commits"][0]

    def test_unknown_operation(self):
        git_tool = GitOperationsTool()
        result = git_tool.execute("rebase")
        assert result["success"] is False
        assert "Unknown operation" in result["error"]

    def test_diff(self):
        git_tool = GitOperationsTool()
        result = git_tool.execute("diff", path=self._root)
        assert result["success"] is True
        assert "has_changes" in result


# ============================================================
# DataParserTool
# ============================================================


class TestDataParserTool:
    def test_parse_json_string(self):
        parser = DataParserTool()
        result = parser.execute('{"name": "test", "value": 42}')
        assert result["success"] is True
        assert result["format"] == "json"
        assert result["data"]["name"] == "test"
        assert result["data"]["value"] == 42

    def test_parse_json_array(self):
        parser = DataParserTool()
        result = parser.execute('[1, 2, 3]')
        assert result["success"] is True
        assert result["data"] == [1, 2, 3]
        assert result["summary"]["type"] == "array"
        assert result["summary"]["length"] == 3

    def test_parse_csv(self):
        parser = DataParserTool()
        csv_data = "name,age\nAlice,30\nBob,25"
        result = parser.execute(csv_data, format_type="csv")
        assert result["success"] is True
        assert result["format"] == "csv"
        assert len(result["data"]) == 2
        assert result["data"][0]["name"] == "Alice"

    def test_parse_xml(self):
        parser = DataParserTool()
        # XML must be > 500 chars to bypass the file-path heuristic
        # (short strings containing '/' get misidentified as file paths)
        items = "".join(f"<item id=\"{i}\">value {i}</item>" for i in range(30))
        xml_data = f"<root>{items}</root>"
        assert len(xml_data) > 500
        result = parser.execute(xml_data, format_type="xml")
        assert result["success"] is True
        assert result["format"] == "xml"
        assert "item" in result["data"]

    def test_auto_detect_json(self):
        parser = DataParserTool()
        # Must be > 500 chars to bypass file-path heuristic
        big_json = json.dumps({"auto": True, "padding": "x" * 500})
        result = parser.execute(big_json)
        assert result["format"] == "json"

    def test_auto_detect_xml(self):
        parser = DataParserTool()
        # Must be > 500 chars to bypass file-path heuristic
        items = "".join(f"<item>{i}</item>" for i in range(50))
        xml_data = f"<root>{items}</root>"
        result = parser.execute(xml_data, format_type="auto")
        assert result["format"] == "xml"

    def test_invalid_json(self):
        parser = DataParserTool()
        result = parser.execute("{bad json", format_type="json")
        assert result["success"] is False
        assert "parse error" in result["error"].lower()

    def test_unsupported_format(self):
        parser = DataParserTool()
        result = parser.execute("data", format_type="protobuf")
        assert result["success"] is False
        assert "Unsupported format" in result["error"]

    def test_parse_json_file(self):
        parser = DataParserTool()
        path = f"/tmp/test_parser_{os.getpid()}.json"
        try:
            Path(path).write_text('{"file": true}')
            result = parser.execute(path)
            assert result["success"] is True
            assert result["data"]["file"] is True
        finally:
            Path(path).unlink(missing_ok=True)

    def test_summarize_dict(self):
        parser = DataParserTool()
        summary = parser._summarize_data({"a": 1, "b": 2})
        assert summary["type"] == "object"
        assert summary["total_keys"] == 2

    def test_summarize_list(self):
        parser = DataParserTool()
        summary = parser._summarize_data([1, 2, 3])
        assert summary["type"] == "array"
        assert summary["length"] == 3

    def test_summarize_scalar(self):
        parser = DataParserTool()
        summary = parser._summarize_data("hello")
        assert summary["type"] == "str"


# ============================================================
# SEOChecklistTool
# ============================================================


class TestSEOChecklistTool:
    def _make_content(self, **overrides):
        """Helper to create content_data with defaults."""
        defaults = {
            "title": "Best Python Testing Frameworks 2026 Guide",  # 48 chars
            "meta_description": (
                "Discover the top Python testing frameworks in 2026. "
                "Compare pytest, unittest, and more with detailed reviews "
                "and recommendations for your next project."
            ),  # 157 chars
            "h1": "Best Python Testing Frameworks",
            "h2s": ["Overview", "Top Frameworks", "Comparison", "Conclusion"],
            "content": " ".join(["word"] * 1600),
            "images": [
                {"src": "img1.jpg", "alt": "pytest logo"},
                {"src": "img2.jpg", "alt": "unittest example"},
            ],
            "links": ["/link1", "/link2", "/link3", "/link4"],
        }
        defaults.update(overrides)
        return defaults

    def test_excellent_score(self):
        checklist = SEOChecklistTool()
        content = self._make_content()
        result = checklist.execute(content)
        assert result["success"] is True
        assert result["overall_score"] >= 75
        assert result["overall_status"] in ("excellent", "good")

    def test_missing_title_fails(self):
        checklist = SEOChecklistTool()
        content = self._make_content(title="")
        result = checklist.execute(content)
        assert result["success"] is True
        title_check = next(c for c in result["checks"] if c["check"] == "Title length")
        assert title_check["status"] == "fail"

    def test_missing_h1_fails(self):
        checklist = SEOChecklistTool()
        content = self._make_content(h1="")
        result = checklist.execute(content)
        h1_check = next(c for c in result["checks"] if c["check"] == "H1 tag present")
        assert h1_check["status"] == "fail"

    def test_short_content_fails(self):
        checklist = SEOChecklistTool()
        content = self._make_content(content="short text only")
        result = checklist.execute(content)
        length_check = next(c for c in result["checks"] if c["check"] == "Content length")
        assert length_check["status"] == "fail"

    def test_no_images_info(self):
        checklist = SEOChecklistTool()
        content = self._make_content(images=[])
        result = checklist.execute(content)
        img_check = next(c for c in result["checks"] if c["check"] == "Image alt tags")
        assert img_check["status"] == "info"

    def test_images_without_alt_warning(self):
        checklist = SEOChecklistTool()
        content = self._make_content(
            images=[{"src": "a.jpg", "alt": "yes"}, {"src": "b.jpg"}]
        )
        result = checklist.execute(content)
        img_check = next(c for c in result["checks"] if c["check"] == "Image alt tags")
        assert img_check["status"] == "warning"

    def test_keyword_checks(self):
        checklist = SEOChecklistTool()
        content = self._make_content(
            title="Best Python Testing Frameworks 2026 Guide",
            h1="Best Python Testing Frameworks",
            content="Python testing " + " ".join(["word"] * 1600),
            target_keyword="Python testing",
        )
        result = checklist.execute(content)
        keyword_checks = [c for c in result["checks"] if "Keyword" in c["check"]]
        assert len(keyword_checks) == 4  # title, meta, h1, first 100 words

    def test_keyword_not_in_title(self):
        checklist = SEOChecklistTool()
        content = self._make_content(
            title="A Great Guide to Frameworks",
            target_keyword="Python testing",
        )
        result = checklist.execute(content)
        kw_title = next(c for c in result["checks"] if c["check"] == "Keyword in title")
        assert kw_title["status"] == "fail"

    def test_poor_score(self):
        checklist = SEOChecklistTool()
        content = {
            "title": "",
            "meta_description": "",
            "h1": "",
            "h2s": [],
            "content": "tiny",
            "images": [],
            "links": [],
        }
        result = checklist.execute(content)
        assert result["overall_status"] == "poor"
        assert result["overall_score"] < 60

    def test_title_too_long_warning(self):
        checklist = SEOChecklistTool()
        content = self._make_content(title="x" * 80)
        result = checklist.execute(content)
        title_check = next(c for c in result["checks"] if c["check"] == "Title length")
        assert title_check["status"] == "warning"

    def test_meta_too_short_warning(self):
        checklist = SEOChecklistTool()
        content = self._make_content(meta_description="Short meta.")
        result = checklist.execute(content)
        meta_check = next(
            c for c in result["checks"] if c["check"] == "Meta description length"
        )
        assert meta_check["status"] == "warning"

    def test_few_h2s_warning(self):
        checklist = SEOChecklistTool()
        content = self._make_content(h2s=["Only One"])
        result = checklist.execute(content)
        h2_check = next(c for c in result["checks"] if c["check"] == "H2 structure")
        assert h2_check["status"] == "warning"


# ============================================================
# SEOIssue dataclass
# ============================================================


class TestSEOIssue:
    def test_creation(self):
        issue = SEOIssue(
            severity="critical",
            category="meta",
            issue="Missing title",
            recommendation="Add a title",
        )
        assert issue.severity == "critical"
        assert issue.current_value is None
        assert issue.target_value is None

    def test_with_values(self):
        issue = SEOIssue(
            severity="important",
            category="content",
            issue="Short content",
            recommendation="Add more",
            current_value="100 words",
            target_value="1000+ words",
        )
        assert issue.current_value == "100 words"


# ============================================================
# Integration: ToolRegistry execute with real tools
# ============================================================


class TestRegistryIntegration:
    def test_execute_python_executor_via_registry(self):
        pool = MagicMock()
        pool.execute_in_sandbox.return_value = ToolResult(success=True, output="4\n")
        reg = create_default_tool_registry(sandbox_pool=pool)
        result = reg.execute_tool("python_executor", code="print(2+2)")
        assert result.success is True
        assert "4" in result.output

    def test_execute_file_reader_via_registry(self):
        reg = create_default_tool_registry(
            sandbox_pool=MagicMock(),
            allowed_file_dirs=[str(_PROJECT_ROOT)],
        )
        result = reg.execute_tool(
            "file_reader",
            file_path=str(_PROJECT_ROOT / "agents" / "tools" / "__init__.py"),
        )
        assert result.success is True
        assert "ToolRegistry" in result.output

    def test_execute_file_writer_via_registry(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        path = f"/tmp/test_registry_write_{os.getpid()}.txt"
        try:
            result = reg.execute_tool(
                "file_writer", file_path=path, content="registry test"
            )
            assert result.success is True
            assert Path(path).read_text() == "registry test"
        finally:
            Path(path).unlink(missing_ok=True)

    def test_unexpected_execution_error(self):
        """Registry should catch unexpected errors and return ToolResult."""
        reg = ToolRegistry()
        tool = ConcreteTool()

        # Monkey-patch execute to raise
        def bad_execute(**kwargs):
            raise RuntimeError("unexpected")

        tool.execute = bad_execute
        reg.register(tool)

        result = reg.execute_tool("test_tool", code="x")
        assert result.success is False
        assert "RuntimeError" in result.error


# ============================================================
# ShellExecutor
# ============================================================


class TestShellExecutor:
    def test_simple_command(self):
        se = ShellExecutor()
        result = se.execute(command="echo hello")
        assert result.success is True
        assert "hello" in result.output

    def test_empty_command_rejected(self):
        se = ShellExecutor()
        result = se.execute(command="")
        assert result.success is False
        assert "No command" in result.error

    def test_timeout(self):
        se = ShellExecutor()
        result = se.execute(command="sleep 10", timeout=1)
        assert result.success is False
        assert "timed out" in result.error

    def test_failing_command(self):
        se = ShellExecutor()
        result = se.execute(command="false")
        assert result.success is False
        assert result.metadata["returncode"] != 0

    def test_schema(self):
        se = ShellExecutor()
        schema = se.get_schema()
        assert schema["name"] == "shell_executor"
        assert "command" in schema["parameters"]["properties"]

    def test_env_stripped(self):
        """ShellExecutor should strip environment variables."""
        se = ShellExecutor()
        # SECRET_VAR should not be visible inside the shell
        with patch.dict(os.environ, {"SECRET_VAR": "leaked"}):
            result = se.execute(command="echo $SECRET_VAR")
            assert result.success is True
            # echo with empty var produces just a newline
            assert "leaked" not in result.output


# ============================================================
# DevToolWrapper
# ============================================================


class TestDevToolWrapper:
    def test_wraps_dict_result(self):
        """Wrapper converts Dict return to ToolResult."""
        inner = MagicMock()
        inner.name = "mock_tool"
        inner.description = "A mock tool"
        inner.execute.return_value = {
            "success": True,
            "output": "analysis done",
            "issues": [],
        }
        wrapper = DevToolWrapper(inner, ToolCategory.SPECIALIZED)
        result = wrapper.execute(path="/some/file")
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert "analysis done" in result.output
        inner.execute.assert_called_once_with(path="/some/file")

    def test_wraps_failed_dict(self):
        inner = MagicMock()
        inner.name = "mock_tool"
        inner.description = "A mock tool"
        inner.execute.return_value = {
            "success": False,
            "error": "file not found",
        }
        wrapper = DevToolWrapper(inner)
        result = wrapper.execute(path="/bad")
        assert result.success is False
        assert result.error == "file not found"

    def test_passthrough_tool_result(self):
        """If inner tool already returns ToolResult, pass it through."""
        inner = MagicMock()
        inner.name = "nice_tool"
        inner.description = "already conforms"
        expected = ToolResult(success=True, output="direct")
        inner.execute.return_value = expected
        wrapper = DevToolWrapper(inner)
        result = wrapper.execute()
        assert result is expected

    def test_schema_from_params(self):
        inner = MagicMock()
        inner.name = "tool_x"
        inner.description = "desc"
        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
        wrapper = DevToolWrapper(inner, parameters_schema=schema)
        full = wrapper.get_schema()
        assert full["name"] == "tool_x"
        assert full["parameters"]["properties"]["x"]["type"] == "string"

    def test_enabled_by_default(self):
        inner = MagicMock()
        inner.name = "t"
        inner.description = "d"
        wrapper = DevToolWrapper(inner)
        assert wrapper.enabled is True

    def test_validate_params_required(self):
        inner = MagicMock()
        inner.name = "t"
        inner.description = "d"
        schema = {"type": "object", "properties": {}, "required": ["needed"]}
        wrapper = DevToolWrapper(inner, parameters_schema=schema)
        with pytest.raises(ValueError, match="Missing required parameter"):
            wrapper.validate_params(other="x")


# ============================================================
# Extended tools via registry
# ============================================================


class TestExtendedToolsViaRegistry:
    def test_static_analyzer_registered(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        tool = reg.get("static_code_analyzer")
        assert tool is not None
        assert isinstance(tool, DevToolWrapper)

    def test_codebase_search_registered(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        tool = reg.get("codebase_search")
        assert tool is not None

    def test_git_operations_registered(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        tool = reg.get("git_operations")
        assert tool is not None

    def test_data_parser_registered(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        tool = reg.get("data_parser")
        assert tool is not None

    def test_data_parser_via_execute(self):
        """DataParserTool should parse JSON via the registry wrapper."""
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        result = reg.execute_tool("data_parser", data='{"key": "value"}', format_type="json")
        assert result.success is True

    def test_shell_executor_registered(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        tool = reg.get("shell_executor")
        assert tool is not None

    def test_shell_executor_via_registry(self):
        pool = MagicMock()
        pool.run_command.return_value = ToolResult(success=True, output="registry_test\n")
        reg = create_default_tool_registry(sandbox_pool=pool)
        result = reg.execute_tool("shell_executor", command="echo registry_test")
        assert result.success is True
        assert "registry_test" in result.output

    def test_codebase_search_nonexistent_path(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        result = reg.execute_tool("codebase_search", query="foo", path="/nonexistent")
        assert result.success is False

    def test_git_status(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        result = reg.execute_tool("git_operations", operation="status", path=".")
        assert result.success is True


# ============================================================
# SandboxedShellExecutor
# ============================================================


class TestSandboxedShellExecutor:
    def test_empty_command_rejected(self):
        from agents.sandbox.tools import SandboxedShellExecutor
        pool = MagicMock()
        tool = SandboxedShellExecutor(pool)
        result = tool.execute(command="")
        assert result.success is False
        assert "No command" in result.error
        pool.run_command.assert_not_called()

    def test_delegates_to_pool(self):
        from agents.sandbox.tools import SandboxedShellExecutor
        pool = MagicMock()
        pool.run_command.return_value = ToolResult(success=True, output="ok")
        tool = SandboxedShellExecutor(pool)
        result = tool.execute(command="ls -la", timeout=15)
        assert result.success is True
        assert result.output == "ok"
        pool.run_command.assert_called_once_with("ls -la", timeout=15)

    def test_schema(self):
        from agents.sandbox.tools import SandboxedShellExecutor
        pool = MagicMock()
        tool = SandboxedShellExecutor(pool)
        schema = tool.get_schema()
        assert schema["name"] == "shell_executor"
        assert "command" in schema["parameters"]

    def test_name_matches_local(self):
        from agents.sandbox.tools import SandboxedShellExecutor
        pool = MagicMock()
        assert SandboxedShellExecutor(pool).name == ShellExecutor().name


# ============================================================
# Specialist tool enablement
# ============================================================


class TestSpecialistToolEnablement:
    """Verify that all code-producing specialists get tool access."""

    def test_expanded_specialist_set(self):
        """The tool_enabled_specialists set must include all code-producing roles."""
        expected = {
            "test_generator", "security_auditor", "data_specialist",
            "database_specialist", "code_reviewer",
            "vibe", "code", "api_generator", "performance_optimizer",
            "debugging_assistant", "doc_generator", "general",
        }
        # Parse the full module file to find all tool_enabled_specialists sets
        import ast
        from agents import specialist_nodes
        module_file = specialist_nodes.__file__
        with open(module_file, "r") as f:
            tree = ast.parse(f.read())
        found_sets = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Set):
                elts = {e.value for e in node.elts if isinstance(e, ast.Constant)}
                if "test_generator" in elts:
                    found_sets.append(elts)
        assert len(found_sets) >= 2, (
            f"Expected 2 tool_enabled_specialists sets (single + multi specialist paths), "
            f"found {len(found_sets)}"
        )
        for i, found in enumerate(found_sets):
            assert found == expected, (
                f"tool_enabled_specialists set #{i} mismatch: "
                f"missing={expected - found}, extra={found - expected}"
            )


# ============================================================
# WebFetchTool
# ============================================================


class TestWebFetchTool:
    def test_empty_url_rejected(self):
        tool = WebFetchTool()
        result = tool.execute(url="")
        assert result.success is False
        assert "No URL" in result.error

    def test_bad_scheme_rejected(self):
        tool = WebFetchTool()
        result = tool.execute(url="ftp://example.com")
        assert result.success is False
        assert "http" in result.error.lower()

    def test_schema(self):
        tool = WebFetchTool()
        schema = tool.get_schema()
        assert schema["name"] == "web_fetch"
        assert "url" in schema["parameters"]["properties"]

    def test_category_is_web_api(self):
        tool = WebFetchTool()
        assert tool.category == ToolCategory.WEB_API

    @patch("subprocess.run")
    def test_successful_fetch(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="<html>Hello</html>", stderr=""
        )
        tool = WebFetchTool()
        result = tool.execute(url="https://example.com")
        assert result.success is True
        assert "Hello" in result.output
        assert result.metadata["url"] == "https://example.com"

    @patch("subprocess.run")
    def test_fetch_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="404 Not Found"
        )
        tool = WebFetchTool()
        result = tool.execute(url="https://example.com/missing")
        assert result.success is False
        assert "404" in result.error

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="", timeout=15))
    def test_fetch_timeout(self, mock_run):
        tool = WebFetchTool()
        result = tool.execute(url="https://slow.example.com", timeout=15)
        assert result.success is False
        assert "timed out" in result.error


# ============================================================
# WebFetchTool — registry gating
# ============================================================


class TestWebFetchRegistryGating:
    def test_not_registered_by_default(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        assert reg.get("web_fetch") is None

    def test_registered_when_egress_enabled(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=True)
        tool = reg.get("web_fetch")
        assert tool is not None
        assert tool.name == "web_fetch"

    def test_tool_count_without_egress(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock())
        assert len(reg.list_tools()) == 17

    def test_tool_count_with_egress(self):
        reg = create_default_tool_registry(sandbox_pool=MagicMock(), network_egress=True)
        assert len(reg.list_tools()) == 18

    def test_sandboxed_web_fetch_when_pool_and_egress(self):
        from agents.sandbox.tools import SandboxedWebFetchTool
        pool = MagicMock()
        reg = create_default_tool_registry(sandbox_pool=pool, network_egress=True)
        tool = reg.get("web_fetch")
        assert tool is not None
        assert tool.name == "web_fetch"

    def test_no_web_fetch_when_pool_but_no_egress(self):
        pool = MagicMock()
        reg = create_default_tool_registry(sandbox_pool=pool, network_egress=False)
        assert reg.get("web_fetch") is None


# ============================================================
# SandboxedWebFetchTool
# ============================================================


class TestSandboxedWebFetchTool:
    def test_empty_url_rejected(self):
        from agents.sandbox.tools import SandboxedWebFetchTool
        pool = MagicMock()
        tool = SandboxedWebFetchTool(pool)
        result = tool.execute(url="")
        assert result.success is False
        assert "No URL" in result.error
        pool.run_command.assert_not_called()

    def test_bad_scheme_rejected(self):
        from agents.sandbox.tools import SandboxedWebFetchTool
        pool = MagicMock()
        tool = SandboxedWebFetchTool(pool)
        result = tool.execute(url="ftp://x.com")
        assert result.success is False
        pool.run_command.assert_not_called()

    def test_delegates_to_pool(self):
        from agents.sandbox.tools import SandboxedWebFetchTool
        pool = MagicMock()
        pool.run_command.return_value = ToolResult(success=True, output="<html/>")
        tool = SandboxedWebFetchTool(pool)
        result = tool.execute(url="https://example.com", timeout=10)
        assert result.success is True
        assert result.output == "<html/>"
        pool.run_command.assert_called_once()
        # Timeout passed to pool should be timeout + 5 headroom
        call_args = pool.run_command.call_args
        assert call_args[1]["timeout"] == 15

    def test_schema(self):
        from agents.sandbox.tools import SandboxedWebFetchTool
        pool = MagicMock()
        schema = SandboxedWebFetchTool(pool).get_schema()
        assert schema["name"] == "web_fetch"
        assert "url" in schema["parameters"]

    def test_name_matches_local(self):
        from agents.sandbox.tools import SandboxedWebFetchTool
        pool = MagicMock()
        assert SandboxedWebFetchTool(pool).name == WebFetchTool().name
