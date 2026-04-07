"""Self-upgrade dispatcher — routes classified signals to tier-specific builders.

M0: all tiers are stubs. Classifier runs for real but every dispatch returns
DispatchResult.Rejected with a "stub" reason. Real tier routing is added in M1+.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Union

from .self_upgrade_trigger import UpgradeSignal

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

    Union = Union[
        "DispatchResult.Tier0Written",
        "DispatchResult.Tier1aQueued",
        "DispatchResult.Tier1bCommitted",
        "DispatchResult.Tier2Committed",
        "DispatchResult.Tier3Filed",
        "DispatchResult.Rejected",
    ]


class SelfUpgradeDispatcher:
    """Routes accumulated signals to tier-specific builders."""

    def __init__(self) -> None:
        pass

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

        # TODO (M2+): skill-cluster rule for Tier 1a
        # TODO (M4): threshold / registry / tool-failure rules for Tier 2

        # Fallback: Tier 3 report
        return Tier.THREE

    def dispatch(self, signals: List[UpgradeSignal]) -> "DispatchResult.Union":
        """Classify the signals and route to the appropriate builder.

        M0: every tier is a stub and returns Rejected("stub not implemented").
        """
        tier = self.classify_signals(signals)
        sig_refs = [s.id for s in signals]

        logger.info(
            "Self-upgrade dispatcher classified %d signal(s) to tier %s",
            len(signals), tier.value,
        )

        # M0: every tier is a stub
        return DispatchResult.Rejected(
            reason=f"stub: tier {tier.value} builder not implemented in M0",
            signal_refs=sig_refs,
        )
