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
