"""
Workflow factory with cached setup for Paperclip heartbeat mode.

Avoids re-creating the LLM backend, adapter registry, and sandbox pool
on every heartbeat invocation.  These are expensive to initialise
(especially the sandbox pool which pre-warms Docker containers) and
their configuration is immutable within a single container lifecycle.

Usage:
    factory = WorkflowFactory(config)
    # Deferred — nothing heavy happens until run_workflow()
    final_state = factory.run_workflow(user_request, task_type, ...)
"""

import logging
from typing import Any, Callable, Dict, Optional

from .adapters import (
    AdapterRegistry,
    PromptAdapter,
    API_GENERATOR_PROMPT,
    CODE_REVIEWER_PROMPT,
    CODE_SYSTEM_PROMPT,
    CREATIVE_SYSTEM_PROMPT,
    CRITIC_SYSTEM_PROMPT,
    DATABASE_SPECIALIST_PROMPT,
    DATA_SPECIALIST_PROMPT,
    SELF_UPGRADE_PROMPT,
    VIBE_SYSTEM_PROMPT,
    REFINEMENT_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
)
from .cancellation import CancellationToken
from .config import SystemConfig
from .graph import create_agent_graph
from .llm_backend import create_backend_from_config
from .simulation import register_simulation_adapters
from .state import create_initial_state

logger = logging.getLogger(__name__)

# All adapter definitions — order does not matter.
_ADAPTER_DEFS = [
    ("vibe", VIBE_SYSTEM_PROMPT),
    ("critic", CRITIC_SYSTEM_PROMPT),
    ("refinement", REFINEMENT_SYSTEM_PROMPT),
    ("code", CODE_SYSTEM_PROMPT),
    ("creative", CREATIVE_SYSTEM_PROMPT),
    ("research", RESEARCH_SYSTEM_PROMPT),
    ("general", RESEARCH_SYSTEM_PROMPT),
    ("data_specialist", DATA_SPECIALIST_PROMPT),
    ("api_generator", API_GENERATOR_PROMPT),
    ("database_specialist", DATABASE_SPECIALIST_PROMPT),
    ("code_reviewer", CODE_REVIEWER_PROMPT),
    ("test_generator", CODE_SYSTEM_PROMPT),
    ("security_auditor", CODE_SYSTEM_PROMPT),
    ("doc_generator", CODE_SYSTEM_PROMPT),
    ("performance_optimizer", CODE_SYSTEM_PROMPT),
    ("debugging_assistant", CODE_SYSTEM_PROMPT),
    ("self_upgrade", SELF_UPGRADE_PROMPT),
]


class WorkflowFactory:
    """Lazily-initialised, reusable workflow components.

    Defers all expensive work (LLM health-check, sandbox pool warm-up)
    to the first ``run_workflow`` call.  Subsequent calls reuse the
    already-initialised backend, adapters, and graph.
    """

    def __init__(self, config: SystemConfig) -> None:
        self._config = config
        self._base_model: Any = None
        self._adapter_registry: Optional[AdapterRegistry] = None
        self._initialised = False

    # ── lazy init ──────────────────────────────────────────────

    def _ensure_initialised(self) -> None:
        """One-time setup: LLM backend + adapter registry."""
        if self._initialised:
            return

        self._base_model = create_backend_from_config(self._config)

        registry = AdapterRegistry()
        for name, prompt in _ADAPTER_DEFS:
            adapter = PromptAdapter(
                name,
                prompt,
                self._base_model,
                config=self._config.generation.get_config(name),
            )
            registry.register(adapter)
        self._adapter_registry = registry

        # Register simulation adapters (MiroFish-style integration prediction).
        # These are lightweight PromptAdapter wrappers on the same base_model.
        register_simulation_adapters(registry, self._base_model)

        self._initialised = True
        logger.info(
            "WorkflowFactory initialised: backend=%s, adapters=%d",
            self._config.model.backend,
            len(registry.list_adapters()),
        )

    # ── public API ─────────────────────────────────────────────

    def run_workflow(
        self,
        user_request: str,
        task_type: str,
        complexity_tier: str = "",
        cancellation_token: Optional[CancellationToken] = None,
        progress_callback: "Optional[Callable[[str, Dict[str, Any]], None]]" = None,
        partial_state: Optional[Dict[str, Any]] = None,
        clarification_reply: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the Vibe workflow graph on the given request.

        Reuses the cached LLM backend and adapter registry across calls.
        The compiled graph is recreated each time because it carries
        per-run state (cancellation token, node closures).

        Args:
            user_request: The full user request text.
            task_type: Pre-resolved task type (skips router classification).
            complexity_tier: Orchestrator complexity hint (fast/standard/full).
            cancellation_token: Cooperative cancellation for long workflows.
            progress_callback: Called after key nodes with (node_name, state).
            partial_state: Mutable dict kept in sync for SIGTERM handler.
            clarification_reply: If set, clears clarification flags so the
                specialist won't re-trigger on the resumed invocation.

        Returns:
            Final workflow state dict.
        """
        self._ensure_initialised()
        if self._adapter_registry is None:
            raise ValueError("WorkflowFactory failed to initialise adapter registry")

        initial_state = create_initial_state(
            user_request=user_request,
            max_iterations=self._config.workflow.max_iterations,
            quality_threshold=self._config.workflow.quality_threshold,
        )

        # Pre-set task type if specified (skips router classification)
        if task_type:
            initial_state["routed_task_type"] = task_type

        # Pre-set complexity tier (from orchestrator or bridge)
        if complexity_tier:
            initial_state["complexity_tier"] = complexity_tier
            tier_thresholds = {"fast": 70, "standard": 75, "full": 85}
            initial_state["effective_quality_threshold"] = tier_thresholds.get(
                complexity_tier, 85
            )

        # ── Clarification resume: clear the flag so the specialist doesn't
        #    re-trigger clarification on the same request.
        if clarification_reply:
            initial_state["clarification_needed"] = False
            initial_state["clarification_questions"] = []
            logger.info(
                "Clarification resume: cleared clarification flags"
            )

        # Graph must be recreated per-run (it closes over cancellation token
        # and per-invocation node wrappers like training-data collection).
        graph = create_agent_graph(
            self._adapter_registry,
            config=self._config,
            base_model=self._base_model,
            cancellation_token=cancellation_token,
        )

        # Stream through graph nodes, collecting final state.
        # Note: graph nodes mutate the state dict in-place — initial_state
        # is the same object that flows through every node.  We keep a
        # separate snapshot (final_state) so callers also see per-node
        # entries like final_state["specialist"] etc.
        final_state: Dict[str, Any] = dict(initial_state)
        latest_node_state: Optional[Dict[str, Any]] = None
        for update in graph.stream(initial_state):
            if isinstance(update, dict):
                final_state.update(update)
                # Extract the latest full state from the yielded update
                for node_name, node_state in update.items():
                    if isinstance(node_state, dict):
                        latest_node_state = node_state
                        latest_node_state["last_node"] = node_name
                # Keep partial_state in sync for SIGTERM handler
                if partial_state is not None:
                    partial_state.clear()
                    partial_state.update(latest_node_state or initial_state)
                # Fire progress callback for key workflow nodes
                if progress_callback and latest_node_state is not None:
                    for node_name in update:
                        progress_callback(node_name, latest_node_state)

        # The graph mutates initial_state in-place, so after streaming
        # completes initial_state contains all accumulated state from
        # every node and decision function.  Merge it into final_state
        # so callers see actual workflow results, not initial defaults.
        source = latest_node_state if latest_node_state is not None else initial_state
        for k, v in source.items():
            final_state[k] = v

        return final_state
