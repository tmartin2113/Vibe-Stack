"""
Self-upgrade trigger — analyses workflow outcomes and proposes upgrades.

Runs as the final step of the skill_cleanup phase.  Examines the completed
workflow state for signals that the agent's own code should be improved:

Trigger signals:
  1. **Repeated low scores** — same task type scored poorly across recent runs
  2. **Tool failures**      — tools failed during specialist execution
  3. **Critic patterns**    — critic feedback mentions specific recurring issues
  4. **Refinement loops**   — specialist hit max iterations without reaching threshold

When a trigger fires, it records an *upgrade signal* in the outcome store.
Signals accumulate across runs; when enough evidence exists for a particular
issue, the trigger emits an UpgradeProposal that feeds into the
SelfUpgradePipeline.

This module is intentionally conservative — it proposes upgrades only after
seeing a pattern across multiple runs, not on a single bad outcome.
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .state import AgentState

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────

# Score below which a workflow outcome counts as "poor"
POOR_SCORE_THRESHOLD = 60

# Number of tool failures in a single run that counts as a tool problem
TOOL_FAILURE_THRESHOLD = 2

# Score at which we flag "hit max iterations without converging"
ITERATION_EXHAUSTION_SCORE = 70

# Minimum accumulated signals before proposing an upgrade
MIN_SIGNALS_TO_PROPOSE = 3


@dataclass
class UpgradeSignal:
    """A single signal that something should be upgraded."""

    category: str           # "low_score", "tool_failure", "iteration_exhaustion", "critic_pattern"
    task_type: str          # Which task type this relates to
    detail: str             # Human-readable description of what went wrong
    score: int = 0          # The score that triggered this (if applicable)
    source_node: str = ""   # Which workflow node generated the signal


@dataclass
class TriggerAnalysis:
    """Result of analysing a workflow outcome for upgrade signals."""

    signals: List[UpgradeSignal] = field(default_factory=list)
    should_propose: bool = False
    proposal_description: str = ""
    proposal_rationale: str = ""
    target_files: List[str] = field(default_factory=list)


class SelfUpgradeTrigger:
    """Analyses workflow state and decides whether to propose self-upgrades.

    This is a **heuristic analyser**, not an LLM call.  It examines the
    completed workflow state for known failure patterns and accumulates
    signals across runs via the outcome store.

    The trigger is deliberately conservative:
    - Single bad run → record signal, no proposal
    - Repeated pattern → propose targeted upgrade
    - All proposals go through the SelfUpgradePipeline safety gates
    """

    def __init__(
        self,
        poor_score_threshold: int = POOR_SCORE_THRESHOLD,
        tool_failure_threshold: int = TOOL_FAILURE_THRESHOLD,
        min_signals: int = MIN_SIGNALS_TO_PROPOSE,
        signal_store_path: Optional[str] = None,
    ):
        self.poor_score_threshold = poor_score_threshold
        self.tool_failure_threshold = tool_failure_threshold
        self.min_signals = min_signals

        # Persistent signal store (JSONL file, survives across heartbeats)
        if signal_store_path is None:
            from .config import get_skills_dir
            signal_store_path = str(
                Path(get_skills_dir()) / "upgrade_signals.jsonl"
            )
        self._signal_store_path = Path(signal_store_path)
        self._signal_store_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # Load persisted signals into memory
        self._signal_history: Dict[str, List[UpgradeSignal]] = {}
        self._load_persisted_signals()

    def analyse(self, state: AgentState) -> TriggerAnalysis:
        """Analyse a completed workflow state for upgrade signals.

        Args:
            state: The final AgentState after workflow completion.

        Returns:
            TriggerAnalysis with any detected signals and a proposal if
            enough evidence has accumulated.
        """
        analysis = TriggerAnalysis()

        task_type = state.get("routed_task_type", "general")

        # Check each signal source
        self._check_low_score(state, task_type, analysis)
        self._check_tool_failures(state, task_type, analysis)
        self._check_iteration_exhaustion(state, task_type, analysis)
        self._check_critic_patterns(state, task_type, analysis)

        # Accumulate signals (both in-memory and persisted to disk)
        if analysis.signals:
            history = self._signal_history.setdefault(task_type, [])
            history.extend(analysis.signals)
            self._persist_signals(analysis.signals, task_type)

            logger.info(
                "Self-upgrade trigger: %d new signal(s) for task_type=%s "
                "(total accumulated: %d)",
                len(analysis.signals), task_type, len(history),
            )

            # Check if we have enough accumulated signals to propose
            if len(history) >= self.min_signals:
                analysis.should_propose = True
                analysis.proposal_description, analysis.proposal_rationale = (
                    self._build_proposal(task_type, history)
                )
                analysis.target_files = self._identify_target_files(
                    task_type, history
                )
                logger.info(
                    "Self-upgrade trigger: proposing upgrade for task_type=%s "
                    "(%d signals accumulated)",
                    task_type, len(history),
                )

        return analysis

    def clear_signals(self, task_type: str) -> None:
        """Clear accumulated signals for a task type (after successful upgrade)."""
        self._signal_history.pop(task_type, None)
        self._remove_persisted_signals(task_type)

    def get_signal_count(self, task_type: str) -> int:
        """Return number of accumulated signals for a task type."""
        return len(self._signal_history.get(task_type, []))

    # ── Persistence ───────────────────────────────────────────────────

    def _persist_signals(self, signals: List[UpgradeSignal], task_type: str) -> None:
        """Append signals to the JSONL store on disk."""
        with self._lock:
            try:
                with open(self._signal_store_path, "a", encoding="utf-8") as f:
                    for s in signals:
                        entry = {
                            "category": s.category,
                            "task_type": s.task_type,
                            "detail": s.detail[:300],
                            "score": s.score,
                            "source_node": s.source_node,
                            "timestamp": datetime.utcnow().isoformat() + "Z",
                        }
                        f.write(json.dumps(entry) + "\n")
            except OSError as e:
                logger.debug("Failed to persist upgrade signals: %s", e)

    def _load_persisted_signals(self) -> None:
        """Load signals from disk into the in-memory history."""
        if not self._signal_store_path.exists():
            return
        try:
            with open(self._signal_store_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        signal = UpgradeSignal(
                            category=entry.get("category", ""),
                            task_type=entry.get("task_type", ""),
                            detail=entry.get("detail", ""),
                            score=entry.get("score", 0),
                            source_node=entry.get("source_node", ""),
                        )
                        history = self._signal_history.setdefault(
                            signal.task_type, []
                        )
                        history.append(signal)
                    except (json.JSONDecodeError, KeyError):
                        continue
            total = sum(len(v) for v in self._signal_history.values())
            if total > 0:
                logger.info(
                    "Loaded %d persisted upgrade signals from %s",
                    total, self._signal_store_path,
                )
        except OSError as e:
            logger.debug("Failed to load persisted signals: %s", e)

    def _remove_persisted_signals(self, task_type: str) -> None:
        """Remove all persisted signals for a task type from the JSONL store."""
        if not self._signal_store_path.exists():
            return
        with self._lock:
            try:
                remaining = []
                with open(self._signal_store_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("task_type") != task_type:
                                remaining.append(line)
                        except json.JSONDecodeError:
                            remaining.append(line)
                with open(self._signal_store_path, "w", encoding="utf-8") as f:
                    for line in remaining:
                        f.write(line + "\n")
            except OSError as e:
                logger.debug("Failed to clean up persisted signals: %s", e)

    # ── Signal detectors ──────────────────────────────────────────────

    def _check_low_score(
        self, state: AgentState, task_type: str, analysis: TriggerAnalysis
    ) -> None:
        """Detect workflows that scored poorly overall."""
        score = state.get("output_critic_score", 0)
        if score == 0:
            return  # Unevaluated — don't signal

        if score < self.poor_score_threshold:
            feedback = state.get("output_critic_feedback", "")
            analysis.signals.append(UpgradeSignal(
                category="low_score",
                task_type=task_type,
                detail=f"Score {score}/100 (threshold {self.poor_score_threshold}). "
                       f"Feedback: {feedback[:200]}",
                score=score,
                source_node="critic",
            ))

    def _check_tool_failures(
        self, state: AgentState, task_type: str, analysis: TriggerAnalysis
    ) -> None:
        """Detect workflows where tools failed repeatedly."""
        tool_calls = state.get("tool_calls_made", [])
        if not tool_calls:
            return

        failures = [
            tc for tc in tool_calls
            if isinstance(tc, dict) and not tc.get("result", {}).get("success", True)
        ]

        if len(failures) >= self.tool_failure_threshold:
            failed_tools = [f.get("tool", "unknown") for f in failures]
            tool_summary = ", ".join(set(failed_tools))
            analysis.signals.append(UpgradeSignal(
                category="tool_failure",
                task_type=task_type,
                detail=f"{len(failures)} tool failures: {tool_summary}",
                source_node="specialist",
            ))

    def _check_iteration_exhaustion(
        self, state: AgentState, task_type: str, analysis: TriggerAnalysis
    ) -> None:
        """Detect workflows that hit max iterations without converging."""
        iteration_count = state.get("iteration_count", 0)
        max_iterations = state.get("max_iterations", 3)
        score = state.get("output_critic_score", 0)

        if (
            iteration_count >= max_iterations
            and 0 < score < ITERATION_EXHAUSTION_SCORE
        ):
            analysis.signals.append(UpgradeSignal(
                category="iteration_exhaustion",
                task_type=task_type,
                detail=f"Hit max iterations ({max_iterations}) with score {score}/100",
                score=score,
                source_node="workflow",
            ))

    def _check_critic_patterns(
        self, state: AgentState, task_type: str, analysis: TriggerAnalysis
    ) -> None:
        """Detect recurring themes in critic feedback."""
        feedback = state.get("output_critic_feedback", "")
        if not feedback:
            return

        # Known problematic patterns in critic feedback
        patterns = [
            (r"missing.*(?:error|exception)\s+handl", "missing error handling"),
            (r"no\s+(?:test|validation)", "missing tests or validation"),
            (r"(?:incomplete|partial)\s+(?:implement|solution)", "incomplete implementation"),
            (r"security\s+(?:issue|vulnerabilit|concern)", "security concern"),
            (r"(?:wrong|incorrect)\s+(?:approach|method|algorithm)", "incorrect approach"),
        ]

        for pattern, label in patterns:
            if re.search(pattern, feedback, re.IGNORECASE):
                analysis.signals.append(UpgradeSignal(
                    category="critic_pattern",
                    task_type=task_type,
                    detail=f"Critic flagged: {label}",
                    source_node="critic",
                ))

    # ── Proposal construction ─────────────────────────────────────────

    def _build_proposal(
        self, task_type: str, signals: List[UpgradeSignal]
    ) -> Tuple[str, str]:
        """Build a proposal description and rationale from accumulated signals."""
        # Count signal categories
        categories: Dict[str, int] = {}
        for s in signals:
            categories[s.category] = categories.get(s.category, 0) + 1

        # Determine dominant issue
        dominant = max(categories, key=categories.get)  # type: ignore[arg-type]

        descriptions = {
            "low_score": f"Improve {task_type} specialist quality",
            "tool_failure": f"Fix tool invocation reliability for {task_type}",
            "iteration_exhaustion": f"Improve convergence speed for {task_type}",
            "critic_pattern": f"Address recurring quality issues in {task_type}",
        }
        description = descriptions.get(dominant, f"Upgrade {task_type} handling")

        # Build rationale from signal details
        rationale_parts = [
            f"Accumulated {len(signals)} signals across recent runs:",
        ]
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            rationale_parts.append(f"  - {cat}: {count} occurrence(s)")

        # Include most recent signal details
        recent = signals[-3:]
        rationale_parts.append("\nRecent details:")
        for s in recent:
            rationale_parts.append(f"  - [{s.category}] {s.detail[:150]}")

        return description, "\n".join(rationale_parts)

    def _identify_target_files(
        self, task_type: str, signals: List[UpgradeSignal]
    ) -> List[str]:
        """Identify which agent files are likely targets for improvement."""
        targets = set()

        for s in signals:
            if s.category == "tool_failure":
                targets.add("agents/tools/registry.py")
                targets.add("agents/specialist_nodes.py")
            elif s.category == "low_score":
                targets.add("agents/adapters.py")
                targets.add("agents/specialist_nodes.py")
            elif s.category == "iteration_exhaustion":
                targets.add("agents/graph.py")
                targets.add("agents/decision_functions.py")
            elif s.category == "critic_pattern":
                targets.add("agents/critic_nodes.py")
                targets.add("agents/adapters.py")

        return sorted(targets)


def analyse_for_upgrade(state: AgentState, trigger: Optional[SelfUpgradeTrigger] = None) -> TriggerAnalysis:
    """Convenience function for graph integration.

    Args:
        state:   Completed workflow state.
        trigger: Shared trigger instance (preserves signal history across runs).
                 If None, creates a fresh one (signals won't accumulate).

    Returns:
        TriggerAnalysis with detected signals and optional proposal.
    """
    if trigger is None:
        trigger = SelfUpgradeTrigger()
    return trigger.analyse(state)
