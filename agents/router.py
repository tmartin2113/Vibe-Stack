"""
Router Node for Multi-Agent System

This node classifies incoming specifications and routes them to the
appropriate specialist adapter for execution.

Classification Modes:
- regex: Fast keyword-based pattern matching (~1ms)
- llm: Semantic classification using base model (~100-500ms)
- hybrid: Regex first, LLM for low-confidence cases (~100ms avg)
"""

from typing import Dict, Any, List, Tuple, Optional, TYPE_CHECKING
import re
import logging
from .state import AgentState
from .skill_registry import SkillRegistry
from .classifier import LLMClassifier
from .router_classification import ClassificationMixin

if TYPE_CHECKING:
    from .task_type_registry import TaskTypeRegistry

__all__ = ["LLMClassifier", "RouterNode", "route_to_specialist"]

logger = logging.getLogger(__name__)


class RouterNode(ClassificationMixin):
    """
    Routes specifications to appropriate specialist adapters.

    The Router analyzes the approved specification from Vibe and
    determines which specialist adapter should handle execution.
    """

    def __init__(self, skill_registry: Optional[SkillRegistry] = None, base_model=None,
                 classification_mode: str = "hybrid", llm_confidence_threshold: float = 0.6,
                 task_type_registry: "Optional[TaskTypeRegistry]" = None):
        """
        Initialize Router with configurable classification.

        Args:
            skill_registry: Shared skill registry instance
            base_model: Base model adapter (Vibe) for LLM classification
            classification_mode: Classification strategy:
                - "regex": Fast keyword-based (original, ~1ms)
                - "llm": Semantic LLM-based (~100-500ms)
                - "hybrid": Regex first, LLM if low confidence (~100ms avg)
            llm_confidence_threshold: Threshold for hybrid mode (0.0-1.0).
                If regex confidence < threshold, use LLM. Default: 0.6
            task_type_registry: Unified registry of all task types (builtin + skill).
                If None, a default registry is created and populated from skills.
        """
        self.name = "router"

        # Use provided skill registry or create new one
        # IMPORTANT: Always pass a shared registry to avoid Bug #1
        if skill_registry is None:
            from .config import get_skills_dir
            self.skill_registry = SkillRegistry(get_skills_dir())
        else:
            self.skill_registry = skill_registry

        # Task type registry — single source of truth
        if task_type_registry is not None:
            self._task_type_registry = task_type_registry
        else:
            from .task_type_registry import create_default_registry, populate_from_skill_registry
            self._task_type_registry = create_default_registry()
            populate_from_skill_registry(self._task_type_registry, self.skill_registry)

        # Classification configuration
        self.classification_mode = classification_mode
        self.llm_confidence_threshold = llm_confidence_threshold

        # Initialize LLM classifier if needed
        if classification_mode in ["llm", "hybrid"]:
            if base_model is None:
                logger.warning(
                    f"Classification mode '{classification_mode}' requires base_model, "
                    "falling back to 'regex' mode"
                )
                self.classification_mode = "regex"
                self.llm_classifier = None
            else:
                self.llm_classifier = LLMClassifier(
                    base_model,
                    task_descriptions=self._task_type_registry.task_descriptions(),
                )
                logger.info(f"Initialized LLM classifier in '{classification_mode}' mode")
        else:
            self.llm_classifier = None

        # Load classification data from the unified task type registry
        self.task_patterns = self._task_type_registry.task_patterns()
        self.pattern_weights = self._task_type_registry.pattern_weights()
        self.hybrid_thresholds = self._task_type_registry.hybrid_thresholds()
        self.adapter_mapping = self._task_type_registry.adapter_mapping()
        self.task_type_labels = self._task_type_registry.task_labels()

        # Decomposition rules (now configurable)
        # Each rule specifies: task_types, execution_mode, aggregation_strategy
        self.decomposition_rules = [
            # Rule 1: Code generation with analysis tasks
            {
                "name": "code_with_analysis",
                "condition": lambda types: "code_generation" in types and len(types) >= 2,
                "execution": "sequential",
                "aggregation": "merge",
                "order": lambda types: (
                    ["code_generation"] +
                    (["refactoring"] if "refactoring" in types else []) +
                    [t for t in types if t not in ["code_generation", "refactoring"]]
                ),
                "priority": 1
            },
            # Rule 2: Debugging workflow
            {
                "name": "debugging_workflow",
                "condition": lambda types: "debugging" in types,
                "execution": "sequential",
                "aggregation": "sequential",
                "order": lambda types: ["debugging"] + [t for t in types if t != "debugging"],
                "priority": 2
            },
            # Rule 3: API development with tests and docs
            {
                "name": "api_full_stack",
                "condition": lambda types: "api_development" in types and
                                           ("test_generation" in types or "documentation" in types),
                "execution": "sequential",
                "aggregation": "merge",
                "order": lambda types: (
                    # Prioritize API-related tasks in order, only if they exist
                    [t for t in ["api_development", "test_generation", "documentation", "security_audit"] if t in types] +
                    # Then add any other detected tasks
                    [t for t in types if t not in ["api_development", "test_generation", "documentation", "security_audit"]]
                ),
                "priority": 3
            },
            # Rule 4: Database operations
            {
                "name": "database_workflow",
                "condition": lambda types: "database_operations" in types,
                "execution": "sequential",
                "aggregation": "sequential",
                "order": lambda types: ["database_operations"] +
                                       [t for t in types if t != "database_operations"],
                "priority": 4
            },
            # Rule 5: Parallel analysis tasks (default)
            {
                "name": "parallel_analysis",
                "condition": lambda types: len(types) >= 2 and
                                           all(t not in ["code_generation", "debugging", "api_development", "database_operations"]
                                               for t in types),
                "execution": "parallel",
                "aggregation": "report",
                "order": lambda types: types,  # Keep original order
                "priority": 10  # Lowest priority (catch-all)
            }
        ]

        # Multi-specialist detection patterns
        # These patterns detect EXPLICIT multi-concern requests where the user
        # asks for two or more distinct specialist tasks in a single specification.
        # Patterns must require clear conjunction of separate concerns — quality
        # adjectives like "comprehensive" or "production ready" are NOT indicators
        # of multiple specialists (they describe desired quality, not scope).
        self.multi_specialist_indicators = [
            # Explicit "X and Y" conjunctions between distinct task domains
            r"\b(test|tests|testing)\s+(and|with|plus|also)\s+(security|secur|document|doc|optimiz)",
            r"\b(security|secur)\s+(and|with|plus|also)\s+(test|tests|testing|document|doc|optimiz)",
            r"\b(document|documentation|docs)\s+(and|with|plus|also)\s+(test|tests|testing|security|optimiz)",
            r"\b(optimiz\w*)\s+(and|with|plus|also)\s+(test|tests|testing|security|document|doc)",

            # "X and also Y" with longer spans (verb...and...verb patterns)
            r"\bwrite\s+tests?\b.*\band\b.*\b(document|secure|optimiz)",
            r"\b(audit|review)\b.*\band\b.*\b(fix|test|document)",

            # Explicit enumeration of multiple deliverables
            r"\b(generate|write|create)\b.*\b(tests?|test suite)\b.*\band\b.*\b(docs?|documentation)\b",
            r"\b(generate|write|create)\b.*\b(docs?|documentation)\b.*\band\b.*\b(tests?|test suite)\b",
        ]

    def execute(self, state: AgentState) -> AgentState:
        """
        Route the specification to appropriate specialist(s).

        This method determines if the task requires a single specialist or
        multiple specialists. For multi-specialist tasks, it decomposes
        the specification into sub-tasks.

        Args:
            state: Current agent state with approved specification

        Returns:
            Updated state with routing decision(s)
        """
        specification = state.get("specification", "")

        # Fast path: when spec is empty (fast-tier skipped spec building),
        # classify from the raw user request instead.
        if not specification:
            specification = state.get("user_request", "")

        # Respect pre-set task type from orchestrator (e.g. Paperclip's
        # VIBE_TASK_TYPE).  This allows arbitrary agent types — the
        # orchestrator defines the type, skills provide the adapter prompt.
        pre_set_task_type = state.get("routed_task_type", "")

        if pre_set_task_type:
            # Orchestrator has already decided the task type.
            # Use the adapter mapping if known, otherwise default to "vibe"
            # (the skill's adapter_prompt will override at specialist execution).
            task_type = pre_set_task_type
            specialist = self.adapter_mapping.get(task_type, "vibe")
            confidence = 1.0  # Orchestrator decision = full confidence

            state["routed_task_type"] = task_type
            state["specialist_adapter"] = specialist
            state["routing_confidence"] = confidence
            state["specialist_iteration_count"] = 0
            state["specialist_max_iterations"] = 3
            state["requires_decomposition"] = False

            debug_info = state.get("debug_info", {})
            debug_info["router_decision"] = {
                "task_type": task_type,
                "specialist": specialist,
                "confidence": confidence,
                "decomposed": False,
                "classification_mode": "pre_set",
            }
            state["debug_info"] = debug_info

            logger.info(
                f"Using pre-set task type from orchestrator: {task_type} "
                f"→ specialist: {specialist}"
            )
        else:
            # CRITICAL FIX (Bug #1): Classify FIRST to populate secondary categories
            # This is needed for LLM-based decomposition detection to work
            complexity_tier = state.get("complexity_tier", "")
            task_type, confidence = self._classify_task(specification, force_regex=(complexity_tier == "fast"))

            # Now check if decomposition is needed (can use LLM's secondary categories)
            requires_decomposition = self._requires_decomposition(specification)

            if requires_decomposition:
                # Multi-specialist workflow: decompose into sub-tasks
                # Store the primary classification for decomposition to use
                state["routed_task_type"] = task_type
                state["routing_confidence"] = confidence
                state = self._decompose_into_subtasks(state)
                state["requires_decomposition"] = True
            else:
                # Single-specialist workflow
                specialist = self.adapter_mapping.get(task_type, "vibe")

                state["routed_task_type"] = task_type
                state["specialist_adapter"] = specialist
                state["routing_confidence"] = confidence
                state["specialist_iteration_count"] = 0
                state["specialist_max_iterations"] = 3
                state["requires_decomposition"] = False

                # Log routing decision
                debug_info = state.get("debug_info", {})
                debug_info["router_decision"] = {
                    "task_type": task_type,
                    "specialist": specialist,
                    "confidence": confidence,
                    "decomposed": False,
                    "classification_mode": self.classification_mode,
                }
                state["debug_info"] = debug_info

        # Discover relevant skills for this task (three-tier system)
        state = self._discover_skills(state)

        return state

    def _requires_decomposition(self, specification: str) -> bool:
        """
        Determine if specification requires multiple specialists.

        Uses LLM's multi-label classification if available, otherwise falls back
        to regex-based multi-specialist detection.

        Args:
            specification: The specification text to analyze

        Returns:
            True if multiple specialists are needed, False otherwise
        """
        # If LLM classification was used and returned secondary categories, use that
        if (self.classification_mode in ["llm", "hybrid"] and
            hasattr(self, '_last_secondary_categories') and
            self._last_secondary_categories):

            # LLM detected multiple categories
            secondary_count = len(self._last_secondary_categories)
            if secondary_count > 0:
                logger.info(
                    f"LLM detected {secondary_count} secondary categories, "
                    f"enabling decomposition"
                )
                return True

        # BUG FIX #2: Handle None/empty specification
        if not specification:
            return False

        # Fallback to regex-based multi-specialist detection using weighted scores.
        # A single low-weight pattern match (e.g. "error" in "error handling")
        # should not by itself trigger decomposition for that task type.
        spec_lower = specification.lower()

        # Compute weighted scores per task type (same logic as _classify_task_regex)
        DECOMPOSITION_MIN_SCORE = 0.15  # Min weighted score to count as a real match
        significant_matches: Dict[str, float] = {}
        for task_type, patterns in self.task_patterns.items():
            if task_type == "general":
                continue

            weights = self.pattern_weights.get(task_type, {})
            weighted_score = 0.0
            max_possible = 0.0

            for pattern in patterns:
                weight = weights.get(pattern, 1.0)
                max_possible += weight
                if re.search(pattern, spec_lower):
                    weighted_score += weight

            score = weighted_score / max_possible if max_possible > 0 else 0
            if score >= DECOMPOSITION_MIN_SCORE:
                significant_matches[task_type] = score

        # If 2 or more task types have significant matches
        if len(significant_matches) >= 2:
            logger.info(
                f"Regex detected {len(significant_matches)} task types "
                f"(scores: {significant_matches}), enabling decomposition"
            )
            return True

        # Check for explicit multi-specialist indicators
        for indicator in self.multi_specialist_indicators:
            if re.search(indicator, spec_lower):
                logger.info(
                    f"Multi-specialist indicator matched: {indicator[:30]}..., "
                    f"enabling decomposition"
                )
                return True

        return False

    def _decompose_into_subtasks(self, state: AgentState) -> AgentState:
        """
        Decompose a complex specification into sub-tasks for multiple specialists.

        Uses LLM's multi-label classification if available for more accurate
        decomposition. Falls back to regex-based pattern matching.

        Args:
            state: Current agent state with specification

        Returns:
            Updated state with sub_tasks list populated
        """
        specification = state.get("specification", "")
        spec_lower = specification.lower()

        # Check if LLM provided multi-label classification
        if (self.classification_mode in ["llm", "hybrid"] and
            hasattr(self, '_last_secondary_categories') and
            self._last_secondary_categories):

            # Use LLM's classification results
            primary_task = state.get("routed_task_type", "general")
            secondary_tasks = self._last_secondary_categories

            # Build task list with primary first, then secondary
            applicable_tasks = {primary_task: 100}  # High score for primary
            for i, task in enumerate(secondary_tasks):
                # Descending scores for secondary tasks
                applicable_tasks[task] = 50 - (i * 10)

            logger.info(
                f"Using LLM decomposition: primary={primary_task}, "
                f"secondary={secondary_tasks}"
            )
            sorted_tasks = sorted(applicable_tasks.items(), key=lambda x: x[1], reverse=True)

        else:
            # Fallback to regex-based task identification
            applicable_tasks = {}
            for task_type, patterns in self.task_patterns.items():
                if task_type == "general":
                    continue

                match_count = 0
                for pattern in patterns:
                    if re.search(pattern, spec_lower):
                        match_count += 1

                if match_count > 0:
                    applicable_tasks[task_type] = match_count

            logger.info(f"Using regex decomposition: {len(applicable_tasks)} tasks detected")
            # Sort by number of matches (most relevant first)
            sorted_tasks = sorted(applicable_tasks.items(), key=lambda x: x[1], reverse=True)

        # Create sub-tasks
        sub_tasks = []
        sub_task_id = 0

        # NEW: Use configurable decomposition rules
        task_types = [t[0] for t in sorted_tasks]

        # Find matching decomposition rule (highest priority first)
        matching_rule = None
        for rule in sorted(self.decomposition_rules, key=lambda r: r["priority"]):  # type: ignore[arg-type, return-value]
            if rule["condition"](task_types):  # type: ignore[operator]
                matching_rule = rule
                break

        # Apply matching rule or use defaults
        if matching_rule:
            parallel_execution = (matching_rule["execution"] == "parallel")
            aggregation_strategy = matching_rule["aggregation"]
            task_types = matching_rule["order"](task_types)  # type: ignore[operator]
            decomposition_rule_name = matching_rule["name"]
            logger.info(f"Applied decomposition rule: {decomposition_rule_name}")
        else:
            # Fallback to defaults (shouldn't happen with catch-all rule)
            parallel_execution = True
            aggregation_strategy = "merge"
            decomposition_rule_name = "default"
            logger.warning("No decomposition rule matched, using defaults")

        # Create a sub-task for each identified task type
        limited_task_types = task_types[:5]  # Limit to 5 sub-tasks max

        for i, task_type in enumerate(limited_task_types):
            specialist = self.adapter_mapping.get(task_type, "vibe")
            sibling_types = [t for t in limited_task_types if t != task_type]

            # Build seed specification scoped to this sub-task's concern
            seed_spec = self._generate_sub_task_spec(
                main_spec=specification,
                task_type=task_type,
                sibling_types=sibling_types,
                index=i,
                is_sequential=(not parallel_execution)
            )

            sub_task = {
                "id": sub_task_id,
                "task_type": task_type,
                "specialist_adapter": specialist,
                "specification": seed_spec,
                "parent_specification": specification,
                "sibling_tasks": sibling_types,
                "status": "pending",  # pending, in_progress, completed, failed
                "output": "",
                "spec_score": 0,
                "output_score": 0,
                "iteration_count": 0,
                "max_iterations": 3
            }

            sub_tasks.append(sub_task)
            sub_task_id += 1

        # Update state
        state["sub_tasks"] = sub_tasks
        state["current_sub_task_index"] = 0
        state["completed_sub_tasks"] = 0
        state["parallel_execution"] = parallel_execution
        state["aggregation_strategy"] = aggregation_strategy  # type: ignore[typeddict-item]  # type: ignore[typeddict-item]

        # Log decomposition decision
        debug_info = state.get("debug_info", {})
        debug_info["router_decision"] = {
            "decomposed": True,
            "num_subtasks": len(sub_tasks),
            "task_types": [t["task_type"] for t in sub_tasks],
            "parallel": parallel_execution,
            "strategy": aggregation_strategy,
            "decomposition_rule": decomposition_rule_name,  # Track which rule was applied
            "classification_mode": self.classification_mode  # Track which mode was used
        }
        state["debug_info"] = debug_info

        return state

    def _generate_sub_task_spec(self, main_spec: str, task_type: str,
                                sibling_types: List[str],
                                index: int, is_sequential: bool) -> str:
        """
        Generate a focused seed specification for a sub-task.

        Provides the downstream spec-builder (Vibe) with a scoped starting
        point that includes the relevant concern, sibling awareness, and
        dependency context — instead of starting from a blank slate.

        Args:
            main_spec: The original full specification from the user
            task_type: This sub-task's task type
            sibling_types: Task types of the other sub-tasks in the plan
            index: This sub-task's position in the execution order
            is_sequential: Whether sub-tasks run sequentially

        Returns:
            A seed specification string scoped to this sub-task
        """
        label = self.task_type_labels.get(task_type, task_type.replace("_", " "))

        # Sibling awareness: tell this specialist what else is being handled
        if sibling_types:
            sibling_labels = [
                self.task_type_labels.get(t, t.replace("_", " "))
                for t in sibling_types
            ]
            sibling_note = (
                f"\nOther specialists are handling: {', '.join(sibling_labels)}. "
                f"Focus only on the {label} aspects — do not duplicate their work."
            )
        else:
            sibling_note = ""

        # Dependency context for sequential execution
        dep_note = ""
        if is_sequential and index > 0:
            preceding_labels = [
                self.task_type_labels.get(t, t.replace("_", " "))
                for t in sibling_types[:index]
            ]
            dep_note = (
                f"\nThis task runs after: {', '.join(preceding_labels)}. "
                f"You may assume their outputs are available."
            )

        return (
            f"[{label.upper()}] From the requirement below, handle the "
            f"{label} concerns.\n\n"
            f"Requirement: {main_spec}"
            f"{sibling_note}"
            f"{dep_note}"
        )

    def _discover_skills(self, state: AgentState) -> AgentState:
        """
        Discover relevant skills for the current task using three-tier system.

        Checks Tier 1 (official), Tier 2 (local), and Tier 3 (generate ephemeral)
        for skills that can help with the specification.

        Args:
            state: Current agent state with routing decision

        Returns:
            Updated state with discovered_skills populated
        """
        specification = state.get("specification", "")
        task_type = state.get("routed_task_type", "general")

        # If multi-specialist, check skills for each sub-task
        if state.get("requires_decomposition", False):
            discovered_skills = []
            sub_tasks = state.get("sub_tasks", [])

            for sub_task in sub_tasks:
                sub_task_type = sub_task["task_type"]
                # Use just task_type for matching (Bug #2 fix)
                # Including full specification dilutes keyword matching
                requirement = sub_task_type

                tier, skill_name, skill_path = self.skill_registry.find_skill(requirement)

                discovered_skills.append({
                    "tier": tier,
                    "skill_name": skill_name,
                    "skill_path": str(skill_path) if skill_path else None,
                    "task_type": sub_task_type,
                    "sub_task_id": sub_task["id"]
                })
        else:
            # Single-specialist: discover multiple skills for progressive disclosure.
            # find_skills() returns up to N matches across all tiers, sorted by
            # quality-weighted score.  The SkillLoaderNode already handles multiple
            # skills (primary=70% context, secondary=summaries).
            requirement = task_type
            matches = self.skill_registry.find_skills(requirement)

            if matches:
                discovered_skills = [{
                    "tier": tier,
                    "skill_name": skill_name,
                    "skill_path": str(skill_path) if skill_path else None,
                    "task_type": task_type
                } for tier, skill_name, skill_path in matches]
            else:
                # Fallback: ephemeral generation
                discovered_skills = [{
                    "tier": "ephemeral",
                    "skill_name": None,
                    "skill_path": None,
                    "task_type": task_type
                }]

        # Update state with discovered skills
        state["discovered_skills"] = discovered_skills

        # Track which skills will be actively used (non-ephemeral)
        active_skills = [
            s["skill_name"] for s in discovered_skills
            if s["skill_name"] is not None
        ]
        state["skills_in_use"] = active_skills

        # Initialize quality scores dict
        state["skill_quality_scores"] = {name: 0 for name in active_skills}

        # Log skill discovery
        debug_info = state.get("debug_info", {})
        debug_info["discovered_skills"] = {
            "count": len(discovered_skills),
            "by_tier": {
                "official": len([s for s in discovered_skills if s["tier"] == "official"]),
                "local": len([s for s in discovered_skills if s["tier"] == "local"]),
                "temp": len([s for s in discovered_skills if s["tier"] == "temp"]),
                "ephemeral": len([s for s in discovered_skills if s["tier"] == "ephemeral"])
            },
            "skills": [
                {
                    "tier": s["tier"],
                    "name": s["skill_name"],
                    "task_type": s["task_type"]
                }
                for s in discovered_skills
            ]
        }
        state["debug_info"] = debug_info

        return state

    def add_decomposition_rule(self, rule: Dict[str, Any]) -> None:
        """
        NEW: Add a custom decomposition rule.

        Allows dynamic addition of decomposition rules for new workflows.

        Args:
            rule: Dictionary with keys: name, condition (callable), execution,
                  aggregation, order (callable), priority

        Example:
            >>> router.add_decomposition_rule({
            ...     "name": "ml_workflow",
            ...     "condition": lambda types: "machine_learning" in types,
            ...     "execution": "sequential",
            ...     "aggregation": "report",
            ...     "order": lambda types: ["data_processing", "machine_learning", "test_generation"],
            ...     "priority": 3
            ... })
        """
        required_fields = {"name", "condition", "execution", "aggregation", "order", "priority"}
        if not required_fields.issubset(rule.keys()):
            raise ValueError(f"Rule must contain fields: {required_fields}")

        # Validate execution mode
        if rule["execution"] not in {"sequential", "parallel"}:
            raise ValueError("execution must be 'sequential' or 'parallel'")

        # Validate aggregation strategy
        if rule["aggregation"] not in {"merge", "sequential", "report"}:
            raise ValueError("aggregation must be 'merge', 'sequential', or 'report'")

        # Check if rule name already exists
        existing_names = {r["name"] for r in self.decomposition_rules}
        if rule["name"] in existing_names:
            logger.warning(f"Rule '{rule['name']}' already exists, will be replaced")
            self.decomposition_rules = [r for r in self.decomposition_rules if r["name"] != rule["name"]]

        self.decomposition_rules.append(rule)
        logger.info(f"Added decomposition rule: {rule['name']} (priority {rule['priority']})")

    def remove_decomposition_rule(self, rule_name: str) -> None:
        """
        NEW: Remove a decomposition rule by name.

        Args:
            rule_name: Name of rule to remove
        """
        original_count = len(self.decomposition_rules)
        self.decomposition_rules = [r for r in self.decomposition_rules if r["name"] != rule_name]

        if len(self.decomposition_rules) < original_count:
            logger.info(f"Removed decomposition rule: {rule_name}")
        else:
            logger.warning(f"Rule '{rule_name}' not found")

    def get_decomposition_rules(self) -> List[Dict[str, Any]]:
        """
        NEW: Get all decomposition rules sorted by priority.

        Returns:
            List of decomposition rules
        """
        return sorted(self.decomposition_rules, key=lambda r: r["priority"])

    def get_available_specialists(self) -> List[str]:
        """
        Get list of available specialist adapters.

        Returns:
            List of specialist adapter names
        """
        return list(set(self.adapter_mapping.values()))

    def get_task_types(self) -> List[str]:
        """
        Get list of recognized task types.

        Returns:
            List of task type names
        """
        return list(self.task_patterns.keys())


# Convenience function for graph integration
def route_to_specialist(state: AgentState, skill_registry: SkillRegistry,
                       base_model=None, classification_mode: str = "hybrid",
                       task_type_registry: "Optional[TaskTypeRegistry]" = None) -> AgentState:
    """
    Route specification to specialist adapter.

    This is a convenience function that can be used directly in the graph.

    Args:
        state: Current agent state
        skill_registry: Shared SkillRegistry instance (required to avoid Bug #1)
        base_model: Optional base model for LLM classification
        classification_mode: Classification strategy ("regex", "llm", or "hybrid")
        task_type_registry: Optional unified task type registry.

    Returns:
        Updated state with routing decision
    """
    router = RouterNode(
        skill_registry=skill_registry,
        base_model=base_model,
        classification_mode=classification_mode,
        task_type_registry=task_type_registry,
    )
    return router.execute(state)
