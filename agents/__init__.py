"""
Multi-Agent System with Iterative Refinement

Specialized prompt-based adapters in an iterative refinement loop,
orchestrated by a lightweight custom state machine (zero external dependencies).

Key Components:
- State: AgentState with full context tracking
- Adapters: Prompt-based specialists with per-task generation configs
- Skills: Dynamic task specialization via discoverable SKILL.md files
- Nodes: Individual agent implementations (Genesia, Critic, Refinement, etc.)
- Graph: Workflow state machine with quality gate and iteration loop
- Config: Centralized configuration management

Quick Start:
    >>> from agents import MultiAgentSystem
    >>> system = MultiAgentSystem()
    >>> system.initialize()
    >>> result = system.run("Write a Python script to analyze CSV files")
"""

__version__ = "0.1.0"

from .main import MultiAgentSystem
from .config import (
    SystemConfig,
    ModelConfig,
    AdapterConfig,
    WorkflowConfig,
    get_dev_config,
    get_production_config
)
from .state import AgentState, create_initial_state
from .adapters import (
    AdapterRegistry,
    PromptAdapter,
)
from .graph import (
    create_agent_graph,
    run_workflow,
    stream_workflow,
    print_graph_structure
)

__all__ = [
    "MultiAgentSystem",
    "SystemConfig",
    "ModelConfig",
    "AdapterConfig",
    "WorkflowConfig",
    "get_dev_config",
    "get_production_config",
    "AgentState",
    "create_initial_state",
    "AdapterRegistry",
    "PromptAdapter",
    "create_agent_graph",
    "run_workflow",
    "stream_workflow",
    "print_graph_structure",
]
