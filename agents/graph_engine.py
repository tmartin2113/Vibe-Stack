"""
Lightweight State Machine Engine

Minimal DAG-based state machine that replaces LangGraph with zero external
dependencies.  Nodes are callables: (AgentState) -> AgentState; this module
handles only the execution order (edges, conditional branches, loops).

Extracted from graph.py so the engine can be tested and reused independently
of the graph-builder logic.
"""

from typing import Callable, Dict, Optional, Any, Iterator, Tuple
import logging
import concurrent.futures
import time

from .cancellation import CancellationToken, WorkflowCancelledError
from .metrics import metrics as app_metrics
from .state import AgentState

logger = logging.getLogger(__name__)


# Sentinel that means "stop execution"
END = "__end__"


class Workflow:
    """
    Minimal DAG-based state machine.

    Nodes are callables: (AgentState) -> AgentState
    Edges are either:
      - Linear:      source -> target
      - Conditional:  source -> decision_fn(state) -> {result: target}

    Usage:
        wf = Workflow()
        wf.add_node("a", fn_a)
        wf.add_node("b", fn_b)
        wf.add_edge("a", "b")
        wf.add_edge("b", END)
        wf.set_entry_point("a")
        app = wf.compile()
        final_state = app.invoke(initial_state)
    """

    def __init__(self):
        self._nodes: Dict[str, Callable[[AgentState], AgentState]] = {}
        self._edges: Dict[str, str] = {}
        self._conditional_edges: Dict[str, Tuple[Callable, Dict[str, str]]] = {}
        self._entry_point: Optional[str] = None

    def add_node(self, name: str, fn: Callable[[AgentState], AgentState]):
        self._nodes[name] = fn

    def add_edge(self, source: str, target: str):
        self._edges[source] = target

    def add_conditional_edges(
        self,
        source: str,
        decision_fn: Callable[[AgentState], str],
        route_map: Dict[str, str],
    ):
        self._conditional_edges[source] = (decision_fn, route_map)

    def set_entry_point(self, name: str):
        self._entry_point = name

    def compile(
        self,
        node_timeout: int = 0,
        workflow_timeout: int = 0,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> "CompiledWorkflow":
        """
        Compile the workflow into an executable form.

        Args:
            node_timeout: Per-node timeout in seconds (0 = no timeout)
            workflow_timeout: Total workflow timeout in seconds (0 = no timeout)
            cancellation_token: Optional token for cooperative cancellation
        """
        if self._entry_point is None:
            raise ValueError("No entry point set. Call set_entry_point() first.")
        if self._entry_point not in self._nodes:
            raise ValueError(f"Entry point '{self._entry_point}' is not a registered node.")
        return CompiledWorkflow(
            nodes=self._nodes,
            edges=self._edges,
            conditional_edges=self._conditional_edges,
            entry_point=self._entry_point,
            node_timeout=node_timeout,
            workflow_timeout=workflow_timeout,
            cancellation_token=cancellation_token,
        )


class WorkflowRecursionError(RuntimeError):
    """Raised when the workflow exceeds the maximum step limit."""


class NodeTimeoutError(RuntimeError):
    """Raised when a single node exceeds its time budget."""

    def __init__(self, node_name: str, timeout: int):
        self.node_name = node_name
        self.timeout = timeout
        super().__init__(
            f"Node '{node_name}' exceeded {timeout}s timeout"
        )


class WorkflowTimeoutError(RuntimeError):
    """Raised when total workflow execution exceeds its time budget."""

    def __init__(self, elapsed: float, timeout: int):
        self.elapsed = elapsed
        self.timeout = timeout
        super().__init__(
            f"Workflow exceeded {timeout}s timeout (elapsed: {elapsed:.1f}s)"
        )


# Default matches LangGraph's recursion_limit for parity.
DEFAULT_MAX_STEPS = 50


class CompiledWorkflow:
    """Executable workflow returned by Workflow.compile()."""

    def __init__(
        self,
        nodes: Dict[str, Callable],
        edges: Dict[str, str],
        conditional_edges: Dict[str, Tuple[Callable, Dict[str, str]]],
        entry_point: str,
        max_steps: int = DEFAULT_MAX_STEPS,
        node_timeout: int = 0,
        workflow_timeout: int = 0,
        cancellation_token: Optional[CancellationToken] = None,
    ):
        self._nodes = nodes
        self._edges = edges
        self._conditional_edges = conditional_edges
        self._entry_point = entry_point
        self._max_steps = max_steps
        self._node_timeout = node_timeout
        self._workflow_timeout = workflow_timeout
        self._cancellation_token = cancellation_token

    def _resolve_next(self, current: str, state: AgentState) -> str:
        """Determine the next node after *current* has executed."""
        # Conditional edge takes priority
        if current in self._conditional_edges:
            decision_fn, route_map = self._conditional_edges[current]
            result = decision_fn(state)
            target = route_map.get(result)
            if target is None:
                raise ValueError(
                    f"Decision function for '{current}' returned '{result}', "
                    f"which is not in route map {list(route_map.keys())}"
                )
            return target

        # Linear edge
        if current in self._edges:
            return self._edges[current]

        # No edge defined — implicit END
        return END

    # ---- internal helpers ----

    def _execute_node(self, node_name: str, node_fn: Callable, state: AgentState) -> AgentState:
        """
        Execute a single node, enforcing per-node timeout if configured.

        Uses a thread pool to run the node function so that a hung LLM call
        can be interrupted after the timeout period.  Note: the background
        thread cannot be forcibly killed in CPython, but we return control
        to the caller immediately by using shutdown(wait=False).
        """
        node_start = time.monotonic()
        try:
            if self._node_timeout <= 0:
                return node_fn(state)  # type: ignore[no-any-return]

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(node_fn, state)
            try:
                result: AgentState = future.result(timeout=self._node_timeout)
                executor.shutdown(wait=False)
                return result
            except concurrent.futures.TimeoutError:
                # Don't wait for the thread — return control immediately.
                executor.shutdown(wait=False)
                raise NodeTimeoutError(node_name, self._node_timeout)
        finally:
            duration = time.monotonic() - node_start
            app_metrics.observe(
                "vibe_node_duration_seconds", duration,
                labels={"node": node_name},
            )

    def _check_workflow_timeout(self, start_time: float) -> None:
        """Raise WorkflowTimeoutError if total elapsed time exceeds budget."""
        if self._workflow_timeout <= 0:
            return
        elapsed = time.monotonic() - start_time
        if elapsed > self._workflow_timeout:
            raise WorkflowTimeoutError(elapsed, self._workflow_timeout)

    # ---- public API (matches the subset of LangGraph we actually use) ----

    def _check_cancellation(self) -> None:
        """Raise WorkflowCancelledError if the cancellation token has fired."""
        if self._cancellation_token is not None:
            self._cancellation_token.check()

    def invoke(self, state: AgentState) -> AgentState:
        """Run the workflow to completion, returning the final state."""
        current = self._entry_point
        steps = 0
        start_time = time.monotonic()

        while current != END:
            if steps >= self._max_steps:
                raise WorkflowRecursionError(
                    f"Workflow exceeded {self._max_steps} steps "
                    f"(last node: '{current}'). This usually means a "
                    f"decision function is stuck in a loop."
                )

            self._check_workflow_timeout(start_time)
            self._check_cancellation()

            node_fn = self._nodes.get(current)
            if node_fn is None:
                raise ValueError(f"No node registered for '{current}'")

            logger.debug(f"Executing node: {current} (step {steps + 1})",
                         extra={"node": current, "step": steps + 1})
            state = self._execute_node(current, node_fn, state)
            steps += 1

            current = self._resolve_next(current, state)

        return state

    def stream(self, state: AgentState) -> Iterator[Dict[str, AgentState]]:
        """
        Yield {node_name: state} after each node completes.

        Compatible with the streaming interface used by stream_workflow
        and the daemon service.
        """
        current = self._entry_point
        steps = 0
        start_time = time.monotonic()

        while current != END:
            if steps >= self._max_steps:
                raise WorkflowRecursionError(
                    f"Workflow exceeded {self._max_steps} steps "
                    f"(last node: '{current}'). This usually means a "
                    f"decision function is stuck in a loop."
                )

            self._check_workflow_timeout(start_time)
            self._check_cancellation()

            node_fn = self._nodes.get(current)
            if node_fn is None:
                raise ValueError(f"No node registered for '{current}'")

            logger.debug(f"Executing node: {current} (step {steps + 1})")
            state = self._execute_node(current, node_fn, state)
            steps += 1

            yield {current: state}

            current = self._resolve_next(current, state)
