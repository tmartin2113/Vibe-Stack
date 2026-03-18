"""
Shared test fixtures for the Vibe test suite.

Provides:
- Environment setup (disable remote skills)
- Mock configurations
- Mock adapter registries
- Pre-built AgentState objects
"""

import os

# Disable remote skill lookups in all tests (must be set before any agents import)
os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

import pytest
from unittest.mock import MagicMock

from agents.config import SystemConfig
from agents.adapters import PromptAdapter, AdapterRegistry
from agents.state import create_initial_state, AgentState


@pytest.fixture
def config():
    """SystemConfig with defaults (Mattermost disabled, spending disabled)."""
    cfg = SystemConfig()
    cfg.mattermost.enabled = False
    cfg.spending.enabled = False
    return cfg


@pytest.fixture
def mock_base_model():
    """Mock LLM backend whose generate() returns a controllable string."""
    model = MagicMock()
    model.generate.return_value = "mock LLM response"
    return model


@pytest.fixture
def mock_adapter_registry(mock_base_model):
    """
    AdapterRegistry pre-loaded with mock adapters for all workflow roles.

    Adapters registered: vibe, critic, refinement, code_expert,
    creative_writer, research_analyst, general.
    """
    registry = AdapterRegistry()
    adapter_names = [
        "vibe", "critic", "refinement",
        "code_expert", "creative_writer", "research_analyst", "general",
    ]
    for name in adapter_names:
        adapter = PromptAdapter(
            name=name,
            system_prompt=f"You are {name}.",
            base_model=mock_base_model,
        )
        registry.register(adapter)
    return registry


@pytest.fixture
def initial_state():
    """Fresh AgentState from create_initial_state()."""
    return create_initial_state(
        user_request="Write a Python function to sort a list",
        max_iterations=3,
        quality_threshold=85,
    )


@pytest.fixture
def completed_state(initial_state):
    """
    AgentState simulating a successful single-specialist workflow completion.

    All key fields populated so downstream tests can verify formatting,
    posting, finalization, etc.
    """
    state = dict(initial_state)
    state.update(
        intent="code_generation",
        intent_confidence=0.95,
        task_type="code",
        complexity_score=45.0,
        stakes="medium",
        specification="Implement a merge sort function in Python...",
        spec_critic_score=90,
        spec_critic_scores={"completeness": 90, "clarity": 92, "feasibility": 88},
        spec_critic_feedback="Specification is clear and complete.",
        routed_task_type="code",
        specialist_adapter="code_expert",
        routing_confidence=0.9,
        specialist_output="def merge_sort(lst):\n    if len(lst) <= 1:\n        return lst\n    ...",
        current_output="def merge_sort(lst):\n    if len(lst) <= 1:\n        return lst\n    ...",
        output_critic_score=88,
        output_critic_scores={"correctness": 90, "efficiency": 85, "readability": 90},
        output_critic_feedback="Good implementation. Minor efficiency concern.",
        critic_score=88,
        critic_scores={"correctness": 90, "efficiency": 85, "readability": 90},
        critic_feedback="Good implementation. Minor efficiency concern.",
        quality_gate_decision="pass",
        iteration_count=1,
    )
    return state
