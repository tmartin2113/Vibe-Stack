"""Tier 1a builder — drafts a v2 refinement candidate for an underperforming skill.

Called by SelfUpgradeDispatcher when classify_signals() returns Tier.ONE_A.
Resolves the matching skill, checks eligibility, drafts refined content via
SkillGeneratorNode.draft_refined_content (pure function), and writes the
result to a new __v2 sibling directory via skill_ab.write_candidate.

Never modifies the existing v1 content. Never touches the archive directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union, TYPE_CHECKING

from ..self_upgrade_trigger import UpgradeSignal

if TYPE_CHECKING:
    from ..skill_generator import SkillGeneratorNode
    from ..skill_outcome_store import SkillOutcomeStore
    from ..skill_registry import SkillRegistry

logger = logging.getLogger(__name__)

# Hard cap on aggregated feedback text passed to the LLM (per spec).
_FEEDBACK_CHAR_CAP = 3000


class Tier1aResult:
    """Tagged union of Tier1aBuilder.build() outcomes."""

    @dataclass
    class CandidateWritten:
        skill_name: str        # base name, e.g. "myCodeSkill"
        v2_path: Path          # absolute path to the new __v2 directory
        signal_refs: List[str]

    @dataclass
    class LowConfidence:
        reason: str            # specific reason string used in dispatcher logs
        signal_refs: List[str]

    AnyResult = Union["Tier1aResult.CandidateWritten", "Tier1aResult.LowConfidence"]


class Tier1aBuilder:
    """Drafts a v2 refinement candidate for an underperforming skill."""

    def __init__(
        self,
        *,
        skill_generator: "SkillGeneratorNode",
        skill_registry: "SkillRegistry",
        outcome_store: "SkillOutcomeStore",
        skills_root: Path,
    ) -> None:
        self._skill_generator = skill_generator
        self._skill_registry = skill_registry
        self._outcome_store = outcome_store
        self._skills_root = skills_root

    def build(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str = "",
        author_run_id: str = "",
    ) -> "Tier1aResult.AnyResult":
        """Draft a v2 refinement candidate from a signal cluster.

        Steps:
        1. Determine target task_type (all signals share one by classifier rule)
        2. Resolve matching skill via skill_registry.find_skill
        3. Check outcome_store has ≥1 recorded outcome for the base skill
        4. Check no __v2 already exists (only one A/B at a time)
        5. Aggregate feedback and call draft_refined_content
        6. (Task 12) Write the v2 sibling via skill_ab.write_candidate
        """
        from .. import skill_ab

        signal_refs = [s.id for s in signals]

        # Step 1: task_type from the first signal (classifier guarantees
        # all cluster members share a task_type).
        task_type = signals[0].task_type

        # Step 2: resolve the matching skill
        tier, skill_name, skill_path = self._skill_registry.find_skill(task_type)
        if not skill_name or not skill_path:
            return Tier1aResult.LowConfidence(
                reason="no matching skill",
                signal_refs=signal_refs,
            )

        # Step 3: ≥1 recorded outcome proves the skill has been exercised
        all_outcomes = self._outcome_store._read_all()
        base_outcomes = [
            o for o in all_outcomes if o.get("skill_name") == skill_name
        ]
        if len(base_outcomes) < 1:
            return Tier1aResult.LowConfidence(
                reason="no recorded outcomes",
                signal_refs=signal_refs,
            )

        # Step 4: no A/B already in progress
        skill_path = Path(skill_path)
        parent_dir = skill_path.parent
        existing_versions = skill_ab.list_versions_for(
            skill_name, skills_root=parent_dir
        )
        if len(existing_versions) > 1:
            return Tier1aResult.LowConfidence(
                reason="A/B in progress",
                signal_refs=signal_refs,
            )

        # Step 5: draft the refined content
        original_content = (skill_path / "SKILL.md").read_text(
            encoding="utf-8", errors="replace"
        )
        aggregated_feedback = self._aggregate_feedback(signals, base_outcomes)
        worst_score = min((s.score for s in signals if s.score is not None),
                          default=50)

        refined = self._skill_generator.draft_refined_content(
            task_type=task_type,
            specification="",  # spec not available at dispatch time
            original_content=original_content,
            feedback=aggregated_feedback,
            score=worst_score,
        )

        if not refined or not refined.strip():
            return Tier1aResult.LowConfidence(
                reason="draft_refined_content returned empty",
                signal_refs=signal_refs,
            )

        # Step 6 happens in Task 12 — for now, placeholder
        return Tier1aResult.LowConfidence(
            reason="write path not yet implemented",
            signal_refs=signal_refs,
        )

    @staticmethod
    def _aggregate_feedback(
        signals: List[UpgradeSignal],
        outcomes: List[dict],
    ) -> str:
        """Concatenate distinct critic feedback strings, dedupe, cap at 3000 chars.

        Pulls feedback from both the signal cluster (current-run critic) and
        prior recorded outcomes (historical critic feedback for the same
        skill). Deduplication uses exact string match.
        """
        pieces: List[str] = []
        seen: set = set()

        for s in signals:
            if s.detail and s.detail not in seen:
                seen.add(s.detail)
                pieces.append(s.detail)

        for o in outcomes:
            fb = o.get("feedback", "")
            if fb and fb not in seen:
                seen.add(fb)
                pieces.append(fb)

        combined = "\n\n".join(pieces)
        if len(combined) > _FEEDBACK_CHAR_CAP:
            combined = combined[:_FEEDBACK_CHAR_CAP]
        return combined
