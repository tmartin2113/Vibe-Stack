"""
Skill Cleanup Node - TTL-based skill cache management at session end.

This node should be called at the end of the workflow to:
- Track quality scores for used skills (enabling promotion)
- Record outcomes in the SkillOutcomeStore (closing the reinforcement loop)
- Evict stale temp skills (unused > 7 days) while retaining recent ones
- Evict stale official skills (unused > 30 days) since they can be re-fetched

Ephemeral skills are preserved across sessions so they can be reused
without regeneration.  Official skills use a lighter cache since the
GitHub repository is always available for re-downloading.
"""

import logging
from pathlib import Path
from typing import Any, Optional
from .state import AgentState
from .skill_registry import SkillRegistry
from .skill_outcome_store import SkillOutcomeStore
from . import skill_ab

logger = logging.getLogger(__name__)


class SkillCleanupNode:
    """
    Handles TTL-based skill cache eviction, final usage tracking, and
    outcome recording.

    This node should be called at the end of a session to:
    1. Track final quality scores for all used skills
    2. Record skill outcomes in the outcome store (reinforcement memory)
    3. Evict stale temp skills (> 7 days unused) while retaining recent ones
    4. Evict stale official skills (> 30 days unused) — re-fetchable from GitHub
    5. Update usage statistics in the index

    Note: Skill refinement is no longer triggered here. Refinements now run
    through the self-upgrade dispatcher (Tier 1a), which writes versioned
    candidates; promotion of A/B winners is added in a separate task.
    """

    # Tier 1b regression monitor constants
    _REGRESSION_THRESHOLD = 8  # points absolute drop triggers alert
    _REGRESSION_DEDUP_DAYS = 30
    _REGRESSION_ROLLING_K = 20  # must match the baseline window used at commit

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
                           If None, outcome recording is skipped.
            base_model:     Reserved for future use (currently unused).
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
            fallback_score = state.get("output_critic_score") or 0
            fallback_feedback = state.get("output_critic_feedback", "")

            # Track usage for all skills that were used
            self._track_skill_usage(
                state, skills_in_use, subtask_scores, fallback_score
            )

            # Record outcomes (reinforcement loop). Refinement runs via
            # the self-upgrade dispatcher (Tier 1a); this node records
            # outcomes and then promotes A/B winners below.
            self._record_outcomes_and_refine(
                state, skills_in_use, subtask_scores, subtask_feedback,
                fallback_score, fallback_feedback,
            )

            # Tier 1a promotion: check whether any A/B'd skill in this
            # run has hit K per-version outcomes; promote winners and
            # archive losers inline. Iterates all three tier dirs since
            # a skill can live in any of them; list_versions_for returns
            # empty for tiers that don't contain the skill, so the extra
            # calls are cheap no-ops.
            if self.outcome_store is not None:
                self._promote_ab_winners(skills_in_use)

            # Tier 1b regression monitor
            try:
                self._check_override_regressions()
            except Exception as exc:
                logger.warning("override regression check failed: %s", exc)
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

        Derives quality scores from actual critic evaluations, falling
        back through:
          1. Per-skill scores from sub-task critics (multi-specialist path)
          2. The top-level output_critic_score (single-specialist path)
          3. Zero (no score available)

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
        Record skill outcomes in the outcome store.

        This is the write side of the reinforcement loop:
          Critic scores -> Outcome Store -> Skill Generator reads them

        Refinement is no longer triggered here; it runs through the
        self-upgrade dispatcher (Tier 1a).
        """
        if self.outcome_store is None:
            return

        specification = state.get("specification", "")
        skill_to_task_type = self._build_skill_to_task_type_map(state)
        loaded_skills = {
            s["name"]: s for s in state.get("loaded_skills", [])
        }

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

    def _promote_ab_winners(self, skills_in_use: list) -> None:
        """Promote Tier 1a A/B winners for any skills that hit K per-version.

        Iterates the three tier directories (temp/local/official) because
        the skill could live in any of them. maybe_promote_winners returns
        an empty list for tiers that don't contain the skill, so the extra
        calls are cheap no-ops.

        Defensive: a promotion failure never crashes cleanup.
        """
        tier_dirs = (
            self.skill_registry.temp_dir,
            self.skill_registry.local_dir,
            self.skill_registry.official_dir,
        )
        for tier_dir in tier_dirs:
            try:
                promotions = skill_ab.maybe_promote_winners(
                    skill_names_in_run=list(skills_in_use),
                    outcome_store=self.outcome_store,
                    skills_root=tier_dir,
                    skill_registry=self.skill_registry,
                    K_per_version=10,
                )
                for p in promotions:
                    logger.info(
                        "🎯 Tier 1a promoted %s: v%d beat v%d "
                        "(avg %.1f vs %.1f)",
                        p.base_name, p.winner_version, p.loser_version,
                        p.winner_avg, p.loser_avg,
                    )
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(
                    "Tier 1a promotion check failed for %s: %s",
                    tier_dir, e,
                )

    def _check_override_regressions(self) -> None:
        """Compare active Tier 1b overrides against their pre-merge baselines.

        For each active override (no .decayed or .superseded sibling),
        read the .baseline sidecar and compute the current task_type
        rolling avg. If the drop exceeds _REGRESSION_THRESHOLD, file a
        Paperclip issue for human triage. Dedup via .regression_alerts.jsonl.
        """
        import datetime as _dt
        import json as _json

        overrides_root = getattr(
            self, "_overrides_root",
            Path("agents/prompt_library/overrides"),
        )
        if not overrides_root.exists():
            return

        alerts_log = getattr(
            self, "_alerts_log",
            overrides_root / ".regression_alerts.jsonl",
        )

        for task_type_dir in overrides_root.iterdir():
            if not task_type_dir.is_dir():
                continue
            task_type = task_type_dir.name
            for yaml_file in task_type_dir.glob("ovr_*.yaml"):
                stem = yaml_file.stem
                if (task_type_dir / f"{stem}.decayed").exists():
                    continue
                if (task_type_dir / f"{stem}.superseded").exists():
                    continue
                baseline_file = task_type_dir / f"{stem}.baseline"
                if not baseline_file.exists():
                    continue

                if SkillCleanupNode._already_alerted_recently(self, alerts_log, stem):
                    continue

                try:
                    baseline_text = baseline_file.read_text().strip()
                    _, score_str = baseline_text.split(" ", 1)
                    baseline_score = float(score_str)
                except (OSError, ValueError) as exc:
                    logger.debug(
                        "override %s baseline unreadable: %s", stem, exc
                    )
                    continue

                _rolling_k = SkillCleanupNode._REGRESSION_ROLLING_K
                try:
                    current_avg = self._rolling_avg_for(
                        task_type, k=_rolling_k
                    )
                except Exception as exc:
                    logger.debug(
                        "override %s rolling avg failed: %s", stem, exc
                    )
                    continue
                if current_avg is None:
                    continue

                drop = baseline_score - float(current_avg)
                if drop <= SkillCleanupNode._REGRESSION_THRESHOLD:
                    continue

                # File the alert
                try:
                    issue = self._paperclip.create_issue(
                        title=f"[tier-1b-regression] override {stem} regressing {task_type}",
                        description=(
                            f"Override `{stem}` for `{task_type}` appears to be "
                            f"regressing.\n\n"
                            f"- **Pre-merge baseline:** {baseline_score:.1f}\n"
                            f"- **Current rolling avg (K={_rolling_k}):** "
                            f"{float(current_avg):.1f}\n"
                            f"- **Drop:** {drop:.1f} points\n\n"
                            f"Human action: write a decay PR by adding "
                            f"`{stem}.decayed` sibling marker, or close this "
                            f"issue if the regression is unrelated.\n"
                        ),
                        labels=[
                            "self-upgrade",
                            "auto-generated",
                            "tier-1b",
                            "tier-1b-regression",
                            f"task:{task_type}",
                        ],
                        assignee_user_id=getattr(self, "_human_triage_user_id", "") or None,
                    )
                    SkillCleanupNode._record_alert(self, alerts_log, stem, issue.id)
                except Exception as exc:
                    logger.warning(
                        "tier1b regression alert filing failed for %s: %s",
                        stem, exc,
                    )

    def _already_alerted_recently(self, alerts_log: Path, override_id: str) -> bool:
        """Return True if this override was alerted within _REGRESSION_DEDUP_DAYS."""
        import datetime as _dt
        import json as _json
        if not alerts_log.exists():
            return False
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            days=SkillCleanupNode._REGRESSION_DEDUP_DAYS
        )
        try:
            for line in alerts_log.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if entry.get("override_id") != override_id:
                    continue
                try:
                    filed = _dt.datetime.fromisoformat(
                        entry["filed_at"].rstrip("Z")
                    ).replace(tzinfo=_dt.timezone.utc)
                except (KeyError, ValueError):
                    continue
                if filed > cutoff:
                    return True
        except OSError:
            return False
        return False

    def _record_alert(self, alerts_log: Path, override_id: str, issue_id: str) -> None:
        """Append an entry to the dedup log."""
        import datetime as _dt
        import json as _json
        try:
            alerts_log.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "override_id": override_id,
                "filed_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "issue_id": issue_id,
            }
            with alerts_log.open("a") as f:
                f.write(_json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("tier1b alerts log append failed: %s", exc)

    def _rolling_avg_for(self, task_type: str, *, k: int) -> Optional[float]:
        """Return the rolling avg critic score for a task_type over last k runs.

        Delegates to the outcome store if available. Returns None if no
        data or the store is unavailable.
        """
        try:
            if self.outcome_store is None:
                return None
            return self.outcome_store.rolling_avg_for_task_type(task_type, k=k)
        except Exception as exc:
            logger.debug("rolling_avg_for task_type %s failed: %s", task_type, exc)
            return None

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
        base_model:     Reserved for future use (currently unused)

    Returns:
        Updated state with cleanup complete
    """
    cleanup_node = SkillCleanupNode(
        skill_registry, outcome_store=outcome_store, base_model=base_model
    )
    return cleanup_node.execute(state)


# Alias so tests and future callers can import SkillCleanup directly.
SkillCleanup = SkillCleanupNode
