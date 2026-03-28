"""
Base classes and types for the tool system.

Provides ToolCategory enum, ToolResult dataclass, and Tool ABC that all
tool implementations inherit from.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from dataclasses import dataclass, field
from enum import Enum


class ToolCategory(Enum):
    """Categories of tools for organization"""
    CODE_EXECUTION = "code_execution"
    FILE_OPS = "file_ops"
    WEB_API = "web_api"
    EXTERNAL_SERVICE = "external_service"
    SPECIALIZED = "specialized"


@dataclass
class ToolResult:
    """Standardized tool execution result"""
    success: bool
    output: str
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata
        }


class Tool(ABC):
    """
    Abstract base class for all tools.

    Each tool must implement:
    - name: Unique identifier
    - description: What the tool does (for LLM to understand)
    - category: Type of tool
    - execute(): Run the tool
    """

    def __init__(self, name: str, description: str, category: ToolCategory):
        self.name = name
        self.description = description
        self.category = category
        self.enabled = True

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """
        Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters

        Returns:
            ToolResult with success status and output
        """
        pass

    def get_schema(self) -> Dict[str, Any]:
        """
        Return JSON schema describing tool parameters.
        Used by LLM to know how to call the tool.
        """
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "parameters": self._get_parameters_schema()
        }

    @abstractmethod
    def _get_parameters_schema(self) -> Dict[str, Any]:
        """
        Return parameter schema for this tool.

        Example:
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"}
            },
            "required": ["code"]
        }
        """
        pass

    def validate_params(self, **kwargs) -> bool:
        """Validate parameters before execution"""
        # Basic validation - override for specific checks
        schema = self._get_parameters_schema()
        required = schema.get("required", [])
        for param in required:
            if param not in kwargs:
                raise ValueError(f"Missing required parameter: {param}")
        return True


__all__ = [
    "ToolCategory",
    "ToolResult",
    "Tool",
]
