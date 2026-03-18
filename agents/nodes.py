"""
Workflow Node Implementations

Each node is a function that:
1. Takes the current state
2. Performs its specific task (using adapters)
3. Returns updated state

Nodes are designed to be composable and testable.

This module composes AgentNodes from focused mixins:
- CriticNodesMixin: All evaluation/critic logic
- SpecialistNodesMixin: Specialist execution with tool calling
- OutputNodesMixin: Formatting and posting

Decision functions and the conversational handler are re-exported
from decision_functions.py for backward compatibility.
"""

from typing import Optional
import logging

from .state import AgentState
from .adapters import AdapterRegistry
from .tools import ToolRegistry, create_default_tool_registry

# Import mixins
from .critic_nodes import CriticNodesMixin
from .specialist_nodes import SpecialistNodesMixin
from .output_nodes import OutputNodesMixin

# Re-export decision functions for backward compatibility
# (graph.py imports these from .nodes)
from .decision_functions import (  # noqa: F401
    should_approve_output,
    should_approve_sub_specification,
    should_approve_sub_output,
    has_more_subtasks,
    should_decompose,
    should_use_llm_critic,
)

logger = logging.getLogger(__name__)

# Explicit re-exports for mypy (no_implicit_reexport = true)
__all__ = [
    "AgentNodes",
    "should_approve_output",
    "should_approve_sub_specification",
    "should_approve_sub_output",
    "has_more_subtasks",
    "should_decompose",
    "MAX_TOOL_CALLING_ITERATIONS",
]

# Re-export for backward compatibility
MAX_TOOL_CALLING_ITERATIONS = 3


class AgentNodes(CriticNodesMixin, SpecialistNodesMixin, OutputNodesMixin):
    """
    Collection of agent node functions.

    This class holds all the node implementations and provides them
    access to the adapter registry and tool registry.

    Composed from:
    - CriticNodesMixin: evaluate_specification, evaluate_output, evaluate_sub_*,
      evaluate_aggregated_output, _parse_critic_output, _build_refinement_history
    - SpecialistNodesMixin: execute_with_specialist, execute_sub_task,
      plan_refinement, _get_specialist_config
    - OutputNodesMixin: format_for_mattermost, post_to_mattermost
    """

    def __init__(
        self,
        adapter_registry: AdapterRegistry,
        tool_registry: Optional[ToolRegistry] = None,
        config = None
    ):
        """
        Initialize agent nodes.

        Args:
            adapter_registry: Registry of LLM adapters
            tool_registry: Registry of tools (optional)
            config: SystemConfig (optional)
        """
        self.adapters = adapter_registry
        self.tool_registry = tool_registry or create_default_tool_registry()
        self.config = config

    # ===== SAFE PARSING UTILITIES =====

    @staticmethod
    def _safe_split_after(text: str, delimiter: str, default: str = "") -> str:
        """Safely extract text after a delimiter."""
        if delimiter not in text:
            return default
        parts = text.split(delimiter, 1)
        return parts[1].strip() if len(parts) > 1 and parts[1].strip() else default

    @staticmethod
    def _safe_split_before(text: str, delimiter: str, default: str = "") -> str:
        """Safely extract text before a delimiter."""
        if delimiter not in text:
            return default
        parts = text.split(delimiter, 1)
        return parts[0].strip() if parts[0].strip() else default

    @staticmethod
    def _safe_split_between(text: str, start_delim: str, end_delim: str, default: str = "") -> str:
        """Safely extract text between two delimiters."""
        if start_delim not in text or end_delim not in text:
            return default
        after_start = text.split(start_delim, 1)
        if len(after_start) < 2:
            return default
        before_end = after_start[1].split(end_delim, 1)
        return before_end[0].strip() if before_end[0].strip() else default

    @staticmethod
    def _safe_find_line_with(text: str, substring: str, default: str = "") -> str:
        """Safely find first line containing a substring."""
        for line in text.split('\n'):
            if substring in line:
                return line.strip()
        return default

