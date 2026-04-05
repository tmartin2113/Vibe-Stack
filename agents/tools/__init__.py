"""
Tools Package for Multi-Agent System

This package contains tool implementations that agents can use to perform
actions beyond text generation.

Available tool categories:
- Core: ToolRegistry, Tool base class, default tool factory
- SEO Tools: Lighthouse, PageAnalyzer, SEOChecklist
- Development Tools: StaticCodeAnalyzer, TestRunner, CodebaseSearch, GitOperations, DataParser
- Infrastructure Tools: WebSearch, WebScrape, BrowserAutomation, Design, ImageGeneration, GitForge, ArtifactStorage
"""

from .registry import ToolRegistry, create_default_tool_registry, Tool, ToolResult, ToolCategory, ROLE_TOOL_SETS
from .seo_tools import LighthouseSEOTool, PageAnalyzerTool, SEOChecklistTool
from .dependency_scanner import DependencyScannerTool
from .database import DatabaseTool
from .container_inspect import ContainerInspectTool
from .dev_tools import (
    StaticCodeAnalyzer,
    TestRunnerTool,
    CodebaseSearchTool,
    GitOperationsTool,
    DataParserTool
)
from .web_search import WebSearchTool
from .web_scrape import WebScrapeTool
from .browser_automation import BrowserAutomationTool
from .design import DesignTool
from .image_generation import ImageGenerationTool
from .git_forge import GitForgeTool
from .artifact_storage import ArtifactStorageTool
from .bulletin_board import BulletinBoardTool
from .quick_lookup import QuickLookupTool

__all__ = [
    # Core
    'ToolRegistry',
    'create_default_tool_registry',
    'Tool',
    'ToolResult',
    'ToolCategory',
    'ROLE_TOOL_SETS',
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
    # Infrastructure Tools
    'WebSearchTool',
    'WebScrapeTool',
    'BrowserAutomationTool',
    'DesignTool',
    'ImageGenerationTool',
    'GitForgeTool',
    'ArtifactStorageTool',
    'BulletinBoardTool',
    'DependencyScannerTool',
    'DatabaseTool',
    'ContainerInspectTool',
    'QuickLookupTool',
]
