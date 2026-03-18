"""
Tools Package for Multi-Agent System

This package contains tool implementations that agents can use to perform
actions beyond text generation.

Available tool categories:
- Core: ToolRegistry, Tool base class, default tool factory
- SEO Tools: Lighthouse, PageAnalyzer, SEOChecklist
- Development Tools: StaticCodeAnalyzer, TestRunner, CodebaseSearch, GitOperations, DataParser
"""

from .registry import ToolRegistry, create_default_tool_registry, Tool, ToolResult, ToolCategory
from .seo_tools import LighthouseSEOTool, PageAnalyzerTool, SEOChecklistTool
from .dev_tools import (
    StaticCodeAnalyzer,
    TestRunnerTool,
    CodebaseSearchTool,
    GitOperationsTool,
    DataParserTool
)

__all__ = [
    # Core
    'ToolRegistry',
    'create_default_tool_registry',
    'Tool',
    'ToolResult',
    'ToolCategory',
    # SEO Tools
    'LighthouseSEOTool',
    'PageAnalyzerTool',
    'SEOChecklistTool',
    # Development Tools
    'StaticCodeAnalyzer',
    'TestRunnerTool',
    'CodebaseSearchTool',
    'GitOperationsTool',
    'DataParserTool',
]
