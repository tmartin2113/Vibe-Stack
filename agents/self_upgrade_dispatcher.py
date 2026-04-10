"""Self-upgrade dispatcher — routes classified signals to tier-specific builders.

M0: all tiers are stubs. Classifier runs for real but every dispatch returns
DispatchResult.Rejected with a "stub" reason. Real tier routing is added in M1+.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, TYPE_CHECKING, Union

from .self_upgrade_trigger import UpgradeSignal

if TYPE_CHECKING:
    from .lesson_store import LessonStore
    from .paperclip_client import PaperclipClient
    from .self_upgrade.tier0_builder import Tier0Builder
    from .self_upgrade.tier1a_builder import Tier1aBuilder
    from .self_upgrade.tier1b_builder import Tier1bBuilder
    from .self_upgrade.tier3_builder import Tier3Builder

logger = logging.getLogger(__name__)


class Tier(str, Enum):
    ZERO = "0"
    ONE_A = "1a"
    ONE_B = "1b"
    TWO = "2"
    THREE = "3"


class DispatchResult:
    """Tagged union of dispatcher outcomes."""

    @dataclass
    class Tier0Written:
        lesson_id: str
        signal_refs: List[str]

    @dataclass
    class Tier1aQueued:
        refinement_id: str
        signal_refs: List[str]

    @dataclass
    class Tier1bCommitted:
        branch: str
        commit: str
        pr_url: str
        issue_id: str
        signal_refs: List[str]

    @dataclass
    class Tier2Committed:
        branch: str
        commit: str
        pr_url: str
        issue_id: str
        edit_type: str
        signal_refs: List[str]

    @dataclass
    class Tier3Filed:
        issue_id: str
        signal_refs: List[str]

    @dataclass
    class Rejected:
        reason: str
        signal_refs: List[str]

    #: Type alias for any DispatchResult variant. Use only for type annotations;
    #: runtime checks must use isinstance against a concrete nested class
    #: (e.g. `isinstance(result, DispatchResult.Tier0Written)`).
    AnyResult = Union[
        "DispatchResult.Tier0Written",
        "DispatchResult.Tier1aQueued",
        "DispatchResult.Tier1bCommitted",
        "DispatchResult.Tier2Committed",
        "DispatchResult.Tier3Filed",
        "DispatchResult.Rejected",
    ]


class SelfUpgradeDispatcher:
    """Routes accumulated signals to tier-specific builders.

    All dependencies are optional keyword arguments. When a tier's dependencies
    are None, dispatching to that tier returns Rejected("dependencies not wired")
    — useful for unit testing the classifier in isolation and for the M0
    transition state where no tier builders existed yet.
    """

    def __init__(
        self,
        *,
        lesson_store: "Optional[LessonStore]" = None,
        tier0_builder: "Optional[Tier0Builder]" = None,
        tier1a_builder: "Optional[Tier1aBuilder]" = None,
        tier1b_builder: "Optional[Tier1bBuilder]" = None,
        tier3_builder: "Optional[Tier3Builder]" = None,
        paperclip_client: "Optional[PaperclipClient]" = None,
        human_triage_user_id: str = "",
    ) -> None:
        self._lesson_store = lesson_store
        self._tier0 = tier0_builder
        self._tier1a = tier1a_builder
        self._tier1b = tier1b_builder
        self._tier3 = tier3_builder
        self._paperclip = paperclip_client
        self._human_triage_user_id = human_triage_user_id

    def classify_signals(self, signals: List[UpgradeSignal]) -> Tier:
        """Heuristic classifier. No LLM call.

        Rules are evaluated in order, first match wins. See spec §"Dispatch logic".
        """
        if not signals:
            return Tier.THREE

        non_empty = [s for s in signals if s.detail and s.detail.strip()]

        # Rule: all-empty-feedback signal cluster → Tier 3 (can't draft a lesson
        # or override from empty feedback)
        if not non_empty:
            return Tier.THREE

        # Rule: single actionable signal, no repeats → Tier 0 memory note
        if len(non_empty) == 1:
            return Tier.ZERO

        # Rule: repeated same-pattern feedback on same task type → Tier 1b
        # (same-detail clusters suggest a missing prompt instruction)
        details = {s.detail for s in non_empty}
        task_types = {s.task_type for s in non_empty}
        if len(details) == 1 and len(task_types) == 1 and len(non_empty) >= 3:
            return Tier.ONE_B

        # Rule: varied-detail cluster on same task_type with ≥3 signals
        # → Tier 1a refinement of the matching skill. Evaluated AFTER the
        # Tier 1b same-detail rule so that same-detail clusters still route
        # to the cheaper prompt-append fix (Tier 1b).
        if len(task_types) == 1 and len(non_empty) >= 3:
            return Tier.ONE_A

        # TODO (M4): threshold / registry / tool-failure rules for Tier 2

        # Fallback: Tier 3 report
        return Tier.THREE

    def dispatch(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str = "",
        author_run_id: str = "",
        role: str = "*",
    ) -> "DispatchResult.AnyResult":
        """Classify the signals and route to the appropriate tier builder.

        When a tier's dependencies aren't wired (e.g. lesson_store is None for
        Tier 0), returns Rejected with a clear reason rather than crashing.
        """
        tier = self.classify_signals(signals)
        sig_refs = [s.id for s in signals]

        logger.info(
            "Self-upgrade dispatcher classified %d signal(s) to tier %s",
            len(signals), tier.value,
        )

        if tier == Tier.ZERO:
            return self._handle_tier0(signals, author_agent_id, author_run_id, role)
        if tier == Tier.ONE_A:
            return self._handle_tier1a(signals, author_agent_id, author_run_id, role)
        if tier == Tier.ONE_B:
            return self._handle_tier1b(signals, author_agent_id, author_run_id, role)
        if tier == Tier.THREE:
            return self._handle_tier3(signals, author_agent_id, role)

        # Tier 2 still a stub
        return DispatchResult.Rejected(
            reason=f"tier {tier.value} not implemented yet",
            signal_refs=sig_refs,
        )

    def _handle_tier0(
        self,
        signals: List[UpgradeSignal],
        author_agent_id: str,
        author_run_id: str,
        role: str,
    ) -> "DispatchResult.AnyResult":
        """Build a lesson via Tier0Builder and persist it via LessonStore."""
        if self._tier0 is None or self._lesson_store is None:
            return DispatchResult.Rejected(
                reason="tier0 dependencies not wired (lesson_store or tier0_builder missing)",
                signal_refs=[s.id for s in signals],
            )

        # Lazy import to avoid circular imports at module load
        from .self_upgrade.tier0_builder import Tier0Result

        result = self._tier0.build(
            signals,
            author_agent_id=author_agent_id,
            author_run_id=author_run_id,
            role=role,
        )

        if isinstance(result, Tier0Result.Empty):
            return DispatchResult.Rejected(
                reason=f"tier0 builder empty: {result.reason}",
                signal_refs=[s.id for s in signals],
            )

        # Tier0Result.LessonDrafted — persist and return
        lesson_id = self._lesson_store.add(
            role=result.role,
            task_type=result.task_type,
            tag=result.tag,
            lesson=result.lesson,
            author_agent_id=author_agent_id,
            author_run_id=author_run_id,
        )
        return DispatchResult.Tier0Written(
            lesson_id=lesson_id,
            signal_refs=result.signal_refs,
        )

    def _handle_tier1a(
        self,
        signals: List[UpgradeSignal],
        author_agent_id: str,
        author_run_id: str,
        role: str,
    ) -> "DispatchResult.AnyResult":
        """Build a v2 refinement candidate via Tier1aBuilder.

        Falls through to Tier 3 on LowConfidence so signals still produce a
        human-visible artifact instead of silently disappearing.
        """
        if self._tier1a is None:
            return DispatchResult.Rejected(
                reason="tier1a dependencies not wired",
                signal_refs=[s.id for s in signals],
            )

        # Lazy import to avoid circular imports at module load
        from .self_upgrade.tier1a_builder import Tier1aResult

        result = self._tier1a.build(
            signals,
            author_agent_id=author_agent_id,
            author_run_id=author_run_id,
        )

        if isinstance(result, Tier1aResult.LowConfidence):
            # Fall through to Tier 3 — let the signals surface as a human issue
            logger.info(
                "Tier 1a returned low confidence (%s); falling through to Tier 3",
                result.reason,
            )
            return self._handle_tier3(signals, author_agent_id, role)

        return DispatchResult.Tier1aQueued(
            refinement_id=result.skill_name + "__v2",
            signal_refs=result.signal_refs,
        )

    def _handle_tier1b(
        self,
        signals: List[UpgradeSignal],
        author_agent_id: str,
        author_run_id: str,
        role: str,
    ) -> "DispatchResult.AnyResult":
        """Build a prompt override via Tier1bBuilder.

        On LowConfidence or GateFailed, falls through to Tier 3 so the
        signals still surface as a human-visible issue with the builder's
        refusal reason in the body.
        """
        if self._tier1b is None:
            return DispatchResult.Rejected(
                reason="tier1b dependencies not wired",
                signal_refs=[s.id for s in signals],
            )

        # Lazy import to avoid circular imports at module load
        from .self_upgrade.tier1b_builder import Tier1bResult

        result = self._tier1b.build(
            signals,
            author_agent_id=author_agent_id,
            author_run_id=author_run_id,
        )

        if isinstance(result, Tier1bResult.LowConfidence):
            logger.info(
                "Tier 1b returned low confidence (%s); falling through to Tier 3",
                result.reason,
            )
            return self._handle_tier3(signals, author_agent_id, role)

        if isinstance(result, Tier1bResult.GateFailed):
            logger.info(
                "Tier 1b gate %s failed (%s); falling through to Tier 3",
                result.gate, result.detail,
            )
            return self._handle_tier3(signals, author_agent_id, role)

        # Tier1bResult.OverrideCommitted — wrap into DispatchResult
        return DispatchResult.Tier1bCommitted(
            branch=result.branch,
            commit=result.commit,
            pr_url=result.pr_url,
            issue_id=result.issue_id,
            signal_refs=result.signal_refs,
        )

    def _handle_tier3(
        self,
        signals: List[UpgradeSignal],
        author_agent_id: str,
        role: str,
    ) -> "DispatchResult.AnyResult":
        """Build an IssueReport via Tier3Builder and file it via PaperclipClient."""
        if self._tier3 is None or self._paperclip is None:
            return DispatchResult.Rejected(
                reason="tier3 dependencies not wired (tier3_builder or paperclip_client missing)",
                signal_refs=[s.id for s in signals],
            )

        # Lazy import to avoid circular imports at module load
        from .self_upgrade.tier3_builder import Tier3Result
        from .self_upgrade.reports import render_report

        result = self._tier3.build(
            signals,
            author_agent_id=author_agent_id,
            author_role=role,
        )

        if isinstance(result, Tier3Result.Dropped):
            return DispatchResult.Rejected(
                reason=f"tier3 dropped: {result.reason}",
                signal_refs=[s.id for s in signals],
            )

        # Tier3Result.ReportDrafted — file the issue
        report = result.report
        issue = self._paperclip.create_issue(
            title=f"[self-report] {report.title}",
            description=render_report(report),
            labels=[
                "self-upgrade",
                "auto-generated",
                "tier-3",
                f"kind:{report.suggested_change_kind}",
            ],
            assignee_user_id=self._human_triage_user_id or None,
        )

        return DispatchResult.Tier3Filed(
            issue_id=issue.id,
            signal_refs=report.signal_refs,
        )
