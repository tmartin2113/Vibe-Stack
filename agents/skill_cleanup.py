"""
Skill Cleanup Node - TTL-based skill cache management at session end.

This node should be called at the end of the workflow to:
- Track quality scores for used skills (enabling promotion)
- Record outcomes in the SkillOutcomeStore (closing the reinforcement loop)
- Trigger self-refinement for low-scoring skills
- Evict stale temp skills (unused > 7 days) while retaining recent ones
- Evict stale official skills (unused > 30 days) since they can be re-fetched

Ephemeral skills are preserved across sessions so they can be reused
without regeneration.  Official skills use a lighter cache since the
GitHub repository is always available for re-downloading.
"""

import logging
from typing import Any, Optional
from .state import AgentState
from .skill_registry import SkillRegistry
from .skill_outcome_store import SkillOutcomeStore
from .skill_generator import SkillGeneratorNode, REFINEMENT_THRESHOLD

logger = logging.getLogger(__name__)


class SkillCleanupNode:
    """
    Handles TTL-based skill cache eviction, final usage tracking, outcome
    recording, and self-refinement of low-scoring skills.

    This node should be called at the end of a session to:
    1. Track final quality scores for all used skills
    2. Record skill outcomes in the outcome store (reinforcement memory)
    3. Trigger self-refinement for skills below REFINEMENT_THRESHOLD
    4. Evict stale temp skills (> 7 days unused) while retaining recent ones
    5. Evict stale official skills (> 30 days unused) — re-fetchable from GitHub
    6. Update usage statistics in the index
    """

    def __init__(
        self,
        skill_registry: SkillRegistry,
        outcome_store: Optional[SkillOutcomeStore] = None,
        base_model: Any = None,
    ):
        """
        Initialize skill cleanup node.

        Args:
            skill_registry: Shared SkillRegistry instance
            outcome_store:  Shared SkillOutcomeStore for recording outcomes.
                           If None, outcome recording and refinement are skipped.
            base_model:     LLM backend, passed to SkillGeneratorNode for
                           LLM-driven refinement.
        """
        self.name = "skill_cleanup"
        self.skill_registry = skill_registry
        self.outcome_store = outcome_store
        self.base_model = base_model

    def execute(self, state: AgentState) -> AgentState:
        """
        Clean up skills and track final usage statistics.

        Args:
            state: Current agent state with skill usage information

        Returns:
            Updated state with cleanup complete
        """
        skills_in_use = state.get("skills_in_use", [])

        if skills_in_use:
            # Build shared lookups once (Bug #5 fix: avoid redundant computation)
            subtask_scores = self._build_subtask_score_map(state)
            subtask_feedback = self._build_subtask_feedback_map(state)
            fallback_score = (
                state.get("output_critic_score")
                or state.get("critic_score")
                or 0
            )
            fallback_feedback = (
                state.get("output_critic_feedback", "")
                or state.get("critic_feedback", "")
            )

            # Track usage for all skills that were used
            self._track_skill_usage(
                state, skills_in_use, subtask_scores, fallback_score
            )

            # Record outcomes and trigger refinement (reinforcement loop)
            self._record_outcomes_and_refine(
                state, skills_in_use, subtask_scores, subtask_feedback,
                fallback_score, fallback_feedback,
            )
        else:
            logger.info("No skills were used in this session")

        # Evict stale skills (TTL-based — retains recent temp and official)
        self._evict_stale_skills()

        # Log cleanup statistics
        stats = self.skill_registry.get_stats()
        logger.info(
            f"🧹 Skill cleanup complete - "
            f"Total skills: {stats['total_skills']}, "
            f"Local: {stats['by_tier']['local']['count']}, "
            f"Temp (retained): {stats['by_tier']['temp']['count']}"
        )

        # Mark cleanup as complete
        state["skills_cleaned_up"] = True

        return state

    def _track_skill_usage(
        self,
        state: AgentState,
        skills_in_use: list,
        subtask_scores: dict,
        fallback_score: int,
    ):
        """
        Track usage statistics for all skills used in this session.

        Derives quality scores from actual critic evaluations rather than
        relying on skill_quality_scores (which is never populated by
        upstream nodes).  Falls back through:
          1. Per-skill scores from sub-task critics (multi-specialist path)
          2. The top-level output_critic_score (single-specialist path)
          3. The legacy critic_score alias

        Args:
            state:           Current agent state
            skills_in_use:   List of skill names used this session
            subtask_scores:  Pre-built skill_name -> score map
            fallback_score:  Single-specialist fallback score
        """
        for skill_name in skills_in_use:
            quality_score = subtask_scores.get(skill_name, fallback_score)

            # Track in registry (this also handles auto-promotion)
            self.skill_registry.track_usage(skill_name, quality_score)

            logger.debug(
                f"Tracked usage for {skill_name}: score={quality_score}"
            )

    def _record_outcomes_and_refine(
        self,
        state: AgentState,
        skills_in_use: list,
        subtask_scores: dict,
        subtask_feedback: dict,
        fallback_score: int,
        fallback_feedback: str,
    ):
        """
        Record skill outcomes in the outcome store and trigger refinement
        for low-scoring skills.

        This is the write side of the reinforcement loop:
          Critic scores -> Outcome Store -> (future) Skill Generator reads them

        For skills scoring below REFINEMENT_THRESHOLD, triggers immediate
        self-refinement using the critic feedback.
        """
        if self.outcome_store is None:
            return

        specification = state.get("specification", "")
        skill_to_task_type = self._build_skill_to_task_type_map(state)
        loaded_skills = {
            s["name"]: s for s in state.get("loaded_skills", [])
        }

        refined_count = 0

        for skill_name in skills_in_use:
            score = subtask_scores.get(skill_name, fallback_score)
            task_type = skill_to_task_type.get(skill_name, "general")
            loaded = loaded_skills.get(skill_name, {})
            skill_content = loaded.get("content", "")

            # Bug #2 fix: use per-sub-task feedback when available,
            # fall back to top-level feedback for single-specialist path
            feedback = subtask_feedback.get(skill_name, fallback_feedback)

            # Bug #1 fix: score=0 means "never evaluated by critic", not
            # "evaluated and scored terribly". Skip recording to avoid
            # poisoning the negative examples pool with unevaluated skills.
            # Bug #3 fix: empty skill_content produces useless RAG examples.
            if score == 0:
                logger.debug(
                    f"Skipping outcome for {skill_name}: score=0 (unevaluated)"
                )
                continue

            if not skill_content:
                logger.debug(
                    f"Skipping outcome for {skill_name}: empty skill content"
                )
                continue

            # Record the outcome
            self.outcome_store.record(
                skill_name=skill_name,
                task_type=task_type,
                specification=specification,
                skill_content=skill_content,
                score=score,
                feedback=feedback,
            )

            # Self-refinement: if score is below threshold and we have
            # feedback, regenerate with critic directives
            if score < REFINEMENT_THRESHOLD and feedback:
                generator = SkillGeneratorNode(
                    self.skill_registry, self.outcome_store,
                    base_model=self.base_model,
                )
                refined = generator.refine_skill(
                    skill_name=skill_name,
                    task_type=task_type,
                    original_content=skill_content,
                    score=score,
                    feedback=feedback,
                    specification=specification,
                )
                if refined:
                    refined_count += 1

        if refined_count > 0:
            logger.info(f"🔄 Refined {refined_count} low-scoring skill(s)")

    @staticmethod
    def _build_subtask_score_map(state: AgentState) -> dict:
        """
        Map skill names to quality scores using sub-task evaluation results.

        In the multi-specialist path each sub-task carries an output_score
        set by the sub-task critic.  We correlate these back to skill names
        via discovered_skills (which links task_type -> skill_name).
        """
        score_map: dict = {}

        sub_tasks = state.get("sub_tasks", [])
        if not sub_tasks:
            return score_map

        # Build task_type -> skill_name lookup from discovered_skills
        task_to_skill: dict = {}
        for entry in state.get("discovered_skills", []):
            name = entry.get("skill_name")
            task_type = entry.get("task_type")
            if name and task_type:
                task_to_skill[task_type] = name

        for sub_task in sub_tasks:
            task_type = sub_task.get("task_type")
            score = sub_task.get("output_score", 0)
            skill_name = task_to_skill.get(task_type)
            if skill_name and score > 0:
                score_map[skill_name] = score

        return score_map

    @staticmethod
    def _build_subtask_feedback_map(state: AgentState) -> dict:
        """
        Map skill names to per-sub-task critic feedback.

        In the multi-specialist path each sub-task carries output_feedback
        set by the sub-task critic (nodes.py:1554).  We correlate these back
        to skill names the same way _build_subtask_score_map does for scores.

        Returns:
            Dict mapping skill_name -> feedback string.
            Empty dict if no sub-tasks or no correlatable feedback.
        """
        feedback_map: dict = {}

        sub_tasks = state.get("sub_tasks", [])
        if not sub_tasks:
            return feedback_map

        # Build task_type -> skill_name lookup from discovered_skills
        task_to_skill: dict = {}
        for entry in state.get("discovered_skills", []):
            name = entry.get("skill_name")
            task_type = entry.get("task_type")
            if name and task_type:
                task_to_skill[task_type] = name

        for sub_task in sub_tasks:
            task_type = sub_task.get("task_type")
            feedback = sub_task.get("output_feedback", "")
            skill_name = task_to_skill.get(task_type)
            if skill_name and feedback:
                feedback_map[skill_name] = feedback

        return feedback_map

    @staticmethod
    def _build_skill_to_task_type_map(state: AgentState) -> dict:
        """Map skill names to their task types from discovered_skills."""
        mapping: dict = {}
        for entry in state.get("discovered_skills", []):
            name = entry.get("skill_name")
            task_type = entry.get("task_type")
            if name and task_type:
                mapping[name] = task_type
        return mapping

    def _evict_stale_skills(self):
        """
        Evict stale skills using TTL-based retention, then clear workspace.

        Temp skills are retained for 7 days after last use — this
        preserves novel generated skills across sessions so they can
        be reused without regeneration.

        Official skills are evicted after 30 days unused — they can
        always be re-fetched from the GitHub repository on demand.

        Skills promoted to local tier are permanent and unaffected.

        Workspace skills are cleared unconditionally — they are
        project-scoped and reloaded fresh on each task.
        """
        self.skill_registry.cleanup_temp()
        self.skill_registry.clear_workspace()


def cleanup_skills(
    state: AgentState,
    skill_registry: SkillRegistry,
    outcome_store: Optional[SkillOutcomeStore] = None,
    base_model: Any = None,
) -> AgentState:
    """
    Convenience function for graph integration.

    Args:
        state: Current agent state
        skill_registry: Shared SkillRegistry instance
        outcome_store:  Optional SkillOutcomeStore for outcome recording
        base_model:     Optional LLM backend for refinement

    Returns:
        Updated state with cleanup complete
    """
    cleanup_node = SkillCleanupNode(
        skill_registry, outcome_store=outcome_store, base_model=base_model
    )
    return cleanup_node.execute(state)
