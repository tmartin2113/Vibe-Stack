"""
Critic Node Implementations

All evaluation/critic methods extracted from the AgentNodes monolith.
These are mixed into AgentNodes via CriticNodesMixin.
"""

from typing import Dict, Any
import re
import logging

from .state import AgentState

logger = logging.getLogger(__name__)


def _get_skill_criteria(state: AgentState, task_type: str) -> str:
    """
    Build a criteria section from skill-declared quality_criteria.

    Checks loaded_skills for any skill whose quality_criteria is a
    non-empty list.  Returns a formatted string ready for prompt
    insertion, or empty string if no skill declares criteria.
    """
    loaded_skills = state.get("loaded_skills", [])
    if not loaded_skills:
        return ""

    for skill in loaded_skills:
        criteria = skill.get("quality_criteria")
        if criteria:  # non-None and non-empty list
            lines = "\n".join(f"- {c}" for c in criteria)
            skill_name = skill.get("name", "loaded skill")
            return (
                f"\n**Domain-Specific Criteria (from {skill_name})**:\n"
                f"{lines}\n"
            )
    return ""


class CriticNodesMixin:
    """
    Mixin containing all critic/evaluation node methods.

    Depends on self.adapters (AdapterRegistry) and self.config (SystemConfig).
    """

    # Task-type-specific evaluation criteria for the critic (hardcoded fallback)
    TASK_EVALUATION_CRITERIA = {
        "code_generation": (
            "- Correctness: Does the code run without errors and produce expected results?\n"
            "- Edge Cases: Are boundary conditions and error paths handled?\n"
            "- Security: Are there injection, XSS, or other OWASP vulnerabilities?\n"
            "- Maintainability: Is the code readable, modular, and well-named?\n"
            "- Best Practices: Does it follow language idioms and conventions?"
        ),
        "test_generation": (
            "- Coverage: Are all important code paths and edge cases tested?\n"
            "- Isolation: Are tests independent and not relying on external state?\n"
            "- Assertions: Are assertions specific and meaningful (not just 'assert True')?\n"
            "- Readability: Are test names descriptive of what they verify?\n"
            "- Reliability: Will tests pass consistently (no flakiness)?"
        ),
        "security_audit": (
            "- Thoroughness: Are all OWASP Top 10 categories considered?\n"
            "- Specificity: Are vulnerabilities pinpointed with file/line references?\n"
            "- Severity: Are findings properly classified (critical/high/medium/low)?\n"
            "- Remediation: Are actionable fixes provided for each finding?\n"
            "- False Positives: Are findings genuine, not theoretical?"
        ),
        "documentation": (
            "- Accuracy: Does the documentation match the actual code behavior?\n"
            "- Completeness: Are all public APIs, parameters, and return types documented?\n"
            "- Examples: Are usage examples provided for non-trivial functions?\n"
            "- Clarity: Can a new developer understand the code from the docs alone?\n"
            "- Format: Does it follow the project's docstring convention?"
        ),
        "performance_optimization": (
            "- Measurability: Are performance claims backed by benchmarks or analysis?\n"
            "- Impact: Do optimizations target actual bottlenecks (not premature)?\n"
            "- Correctness: Do optimizations preserve original behavior?\n"
            "- Trade-offs: Are memory/readability trade-offs acknowledged?\n"
            "- Profiling: Is profiling data or complexity analysis included?"
        ),
        "debugging": (
            "- Root Cause: Is the actual root cause identified (not just symptoms)?\n"
            "- Fix Correctness: Does the fix resolve the issue without regressions?\n"
            "- Explanation: Is the bug mechanism clearly explained?\n"
            "- Verification: Is there a way to verify the fix works?\n"
            "- Prevention: Are suggestions given to prevent similar bugs?"
        ),
        "code_review": (
            "- Thoroughness: Are all code quality dimensions covered?\n"
            "- Constructiveness: Is feedback actionable and specific?\n"
            "- Prioritization: Are critical issues distinguished from nice-to-haves?\n"
            "- Accuracy: Are identified issues genuine problems?\n"
            "- Balance: Are strengths acknowledged alongside weaknesses?"
        ),
    }

    def evaluate_specification(self, state: AgentState) -> AgentState:
        """
        CRITIC STAGE 1: Evaluate specification completeness.

        Reviews the specification to ensure it has enough detail for
        a specialist to produce high-quality output.
        """
        specification = state.get("specification", "")
        user_request = state["user_request"]

        prompt = f"""You are evaluating a specification for completeness and clarity.

Original User Request: {user_request}

Specification to Review: {specification}

Evaluate whether this specification has enough detail for a specialist to create a high-quality result.

Consider:
1. Are all requirements clearly stated?
2. Are there ambiguities that need clarification?
3. Is any critical information missing?
4. Are constraints and preferences specified?

Provide scores (0-100) and detailed feedback.

Output format:
SCORES:
Completeness: [0-100]
Clarity: [0-100]
Specificity: [0-100]
Feasibility: [0-100]
Overall: [0-100]

REASONING:
[Detailed feedback on what's good and what needs improvement]"""

        # Use Critic adapter
        critic = self.adapters.switch_to("critic")

        evaluation = critic.generate(
            prompt,
            temperature=0.1,  # Low temperature for consistent scoring
            max_tokens=600
        )

        logger.info("Critic Stage 1: Specification evaluation complete")
        logger.debug(f"Evaluation: {evaluation[:100]}...")

        # Parse the critic output
        scores, feedback = self._parse_critic_output(evaluation)

        state["spec_critic_scores"] = scores
        state["spec_critic_score"] = scores.get("overall", 0)
        state["spec_critic_feedback"] = feedback
        state["adapters_used"] = state.get("adapters_used", []) + ["critic"]

        logger.info(f"Spec Critic scores: Overall={scores.get('overall', 0)}/100")

        return state

    def evaluate_output(self, state: AgentState) -> AgentState:
        """
        CRITIC STAGE 2: Evaluate specialist output quality.

        Reviews the actual output from the specialist to ensure it meets
        quality standards and fulfills the specification. Uses task-type-specific
        evaluation criteria for more targeted feedback.
        """
        specification = state.get("specification", "")
        output = state.get("specialist_output", "")
        user_request = state.get("user_request", "")
        routed_task_type = state.get("routed_task_type", "general")
        specialist_name = state.get("specialist_adapter", "unknown")

        # Prefer skill-declared criteria; fall back to hardcoded per-task-type
        criteria_section = _get_skill_criteria(state, routed_task_type)
        if not criteria_section:
            task_criteria = self.TASK_EVALUATION_CRITERIA.get(routed_task_type, "")
            if task_criteria:
                criteria_section = f"""
**Domain-Specific Criteria for {routed_task_type.replace('_', ' ')}**:
{task_criteria}
"""

        prompt = f"""Evaluate the quality of this {routed_task_type.replace('_', ' ')} output.

**Original User Request**: {user_request}

**Specification**: {specification}

**Specialist**: {specialist_name}

**Output to Evaluate**: {output}
{criteria_section}
Does the output successfully fulfill the specification and address the user's request?

Provide scores (0-100) and detailed, actionable feedback.

Output format:
SCORES:
Completeness: [0-100]
Accuracy: [0-100]
Quality: [0-100]
Clarity: [0-100]
Helpfulness: [0-100]
Overall: [0-100]

REASONING:
[Detailed feedback on strengths and specific areas for improvement]"""

        # Use Critic adapter
        critic = self.adapters.switch_to("critic")

        evaluation = critic.generate(
            prompt,
            temperature=0.1,  # Low temperature for consistent scoring
            max_tokens=600
        )

        logger.info("Critic Stage 2: Output evaluation complete")
        logger.debug(f"Evaluation: {evaluation[:100]}...")

        # Parse the critic output
        scores, feedback = self._parse_critic_output(evaluation)

        state["output_critic_scores"] = scores
        state["output_critic_score"] = scores.get("overall", 0)
        state["output_critic_feedback"] = feedback
        state["adapters_used"] = state.get("adapters_used", []) + ["critic"]

        logger.info(f"Output Critic scores: Overall={scores.get('overall', 0)}/100")

        return state

    def _parse_critic_output(self, evaluation: str) -> tuple[Dict[str, int], str]:
        """
        Parse critic output to extract scores and reasoning.

        Uses multiple parsing strategies for resilience against format deviations
        from smaller models. Logs a warning when falling back to defaults so
        score degradation is visible rather than silent.
        """
        default_score = 50
        scores = {
            "completeness": default_score,
            "accuracy": default_score,
            "quality": default_score,
            "clarity": default_score,
            "coherence": default_score,
            "helpfulness": default_score,
            "overall": default_score
        }
        parsed_count = 0

        # Extract REASONING section (safe)
        feedback = self._safe_split_after(evaluation, "REASONING:", evaluation)

        # Extract SCORES section (safe)
        scores_section = self._safe_split_before(evaluation, "REASONING:", "")

        # Flexible dimension matching: accepts "Dim: 72", "Dim - 72",
        # "Dim = 72", "Dim Score: 72", "DIM 72", "Dim: 72/100"
        dim_pattern = re.compile(
            r'(?:^|[\n]).*?'  # line start
            r'({dims})'  # dimension name
            r'(?:\s+score)?'  # optional " Score"
            r'\s*[:=\-]\s*'  # separator
            r'(\d+)'  # the score
            r'[^\S\n]*(?:/\s*100|%)?'  # optional "/100" or "%" (no newline consumption)
            .format(dims='|'.join(re.escape(d) for d in scores.keys())),
            re.IGNORECASE,
        )

        # Also match bare "DIMENSION 72" (no separator)
        bare_pattern = re.compile(
            r'(?:^|[\n])\s*'
            r'({dims})'
            r'\s+'
            r'(\d+)'
            r'[^\S\n]*(?:/\s*100|%)?'  # optional "/100" or "%" (no newline consumption)
            .format(dims='|'.join(re.escape(d) for d in scores.keys())),
            re.IGNORECASE,
        )

        def _extract_from(text: str) -> int:
            nonlocal parsed_count
            count = 0
            for match in dim_pattern.finditer(text):
                dim = match.group(1).lower()
                value = max(0, min(100, int(match.group(2))))
                if dim in scores:
                    scores[dim] = value
                    count += 1
            if count == 0:
                for match in bare_pattern.finditer(text):
                    dim = match.group(1).lower()
                    value = max(0, min(100, int(match.group(2))))
                    if dim in scores:
                        scores[dim] = value
                        count += 1
            parsed_count += count
            return count

        # Strategy 1: parse from SCORES section
        if scores_section:
            _extract_from(scores_section)

        # Strategy 2: if nothing from SCORES section, scan entire output
        if parsed_count == 0:
            _extract_from(evaluation)

        # Strategy 3: find any line with "overall" or "score" and a number
        if parsed_count == 0:
            for line in evaluation.split('\n'):
                line_lower = line.lower()
                if 'overall' in line_lower or 'score' in line_lower:
                    match = re.search(r'(\d+)\s*(?:/\s*100|%)?', line)
                    if match:
                        value = int(match.group(1))
                        if 0 <= value <= 100:
                            scores["overall"] = value
                            parsed_count = 1
                            break

        # Strategy 4: last resort — find any "N/100" pattern
        if parsed_count == 0:
            all_scores = re.findall(r'(\d+)\s*/\s*100', evaluation)
            if all_scores:
                scores["overall"] = max(0, min(100, int(all_scores[-1])))
                parsed_count = 1
                logger.warning(
                    "Critic output didn't follow expected format. "
                    "Extracted fallback overall score: %d/100",
                    scores["overall"],
                )

        if parsed_count == 0:
            logger.warning(
                "Failed to parse any scores from critic output. "
                "Defaulting all dimensions to %d/100. "
                "Raw output: %s...",
                default_score, evaluation[:200],
            )
        elif parsed_count < len(scores):
            defaulted = [d for d, v in scores.items() if v == default_score]
            if defaulted:
                logger.warning(
                    "Partial critic parse: %d/%d dimensions extracted. "
                    "Dimensions defaulting to %d: %s",
                    parsed_count, len(scores), default_score, defaulted,
                )

        return scores, feedback

    # ===== MULTI-TURN HISTORY BUILDER =====

    @staticmethod
    def _build_refinement_history(state: Dict[str, Any]) -> list:
        """
        Build multi-turn message history from conversation_history.

        Converts the stored iteration history into assistant/user turn pairs
        so the specialist can see its prior attempts and feedback as a natural
        conversation arc rather than a single flat prompt.
        """
        history = state.get("conversation_history", [])
        if not history:
            return []

        turns = []
        for entry in history:
            # Prior specialist output as an assistant turn
            if entry.get("output"):
                turns.append({
                    "role": "assistant",
                    "content": entry["output"]
                })
            # Critic feedback as a user turn (the critic "talks back")
            if entry.get("feedback_summary"):
                turns.append({
                    "role": "user",
                    "content": f"[Critic - iteration {entry.get('iteration', '?')}, "
                               f"score {entry.get('score', '?')}/100]: {entry['feedback_summary']}"
                })
        return turns

    def _format_scores(self, scores: Dict[str, int]) -> str:
        """Format scores dict for display"""
        return "\n".join([f"{k.title()}: {v}/100" for k, v in scores.items()])

    # ===== SUB-TASK CRITIC METHODS =====

    def evaluate_sub_specification(self, state: AgentState) -> AgentState:
        """
        Critic Stage 1 for sub-task specification.

        Evaluates whether the sub-specification has enough detail for
        the specialist to produce quality output.
        """
        sub_tasks = state.get("sub_tasks", [])
        current_index = state.get("current_sub_task_index", 0)

        if current_index >= len(sub_tasks):
            return state

        current_subtask = sub_tasks[current_index]
        sub_spec = current_subtask.get("specification", "")
        task_type = current_subtask["task_type"]

        prompt = f"""Evaluate this specification for a {task_type} task.

Sub-Specification: {sub_spec}

Does this provide enough detail for a specialist to produce quality output?

Output format:
SCORES:
Completeness: [0-100]
Clarity: [0-100]
Specificity: [0-100]
Overall: [0-100]

REASONING:
[Feedback]"""

        critic = self.adapters.switch_to("critic")
        evaluation = critic.generate(prompt, temperature=0.1, max_tokens=400)

        scores, feedback = self._parse_critic_output(evaluation)

        # Update sub-task
        current_subtask["spec_score"] = scores.get("overall", 0)
        current_subtask["spec_feedback"] = feedback
        current_subtask["status"] = "spec_evaluated"
        sub_tasks[current_index] = current_subtask
        state["sub_tasks"] = sub_tasks

        logger.info(f"Evaluated sub-specification: {task_type} scored {scores.get('overall', 0)}/100")

        return state

    def evaluate_sub_output(self, state: AgentState) -> AgentState:
        """
        Critic Stage 2 for sub-task output.

        Evaluates the quality of the specialist's output for this sub-task.
        """
        sub_tasks = state.get("sub_tasks", [])
        current_index = state.get("current_sub_task_index", 0)

        if current_index >= len(sub_tasks):
            return state

        current_subtask = sub_tasks[current_index]
        sub_spec = current_subtask.get("specification", "")
        output = current_subtask.get("output", "")
        task_type = current_subtask["task_type"]
        user_request = state.get("user_request", "")

        # Prefer skill-declared criteria; fall back to hardcoded per-task-type
        criteria_section = _get_skill_criteria(state, task_type)
        if not criteria_section:
            task_criteria = self.TASK_EVALUATION_CRITERIA.get(task_type, "")
            if task_criteria:
                criteria_section = f"""
**Domain-Specific Criteria for {task_type.replace('_', ' ')}**:
{task_criteria}
"""

        prompt = f"""Evaluate this {task_type.replace('_', ' ')} output (sub-task {current_index + 1} of a multi-specialist plan).

**Original User Request**: {user_request}

**Sub-Task Specification**: {sub_spec}

**Output to Evaluate**: {output}
{criteria_section}
Does this output successfully fulfill the specification?

Output format:
SCORES:
Completeness: [0-100]
Accuracy: [0-100]
Quality: [0-100]
Overall: [0-100]

REASONING:
[Specific, actionable feedback]"""

        critic = self.adapters.switch_to("critic")
        evaluation = critic.generate(prompt, temperature=0.1, max_tokens=400)

        scores, feedback = self._parse_critic_output(evaluation)

        # Update sub-task
        current_subtask["output_score"] = scores.get("overall", 0)
        current_subtask["output_feedback"] = feedback
        current_subtask["status"] = "evaluated"
        sub_tasks[current_index] = current_subtask
        state["sub_tasks"] = sub_tasks

        logger.info(f"Evaluated sub-output: {task_type} scored {scores.get('overall', 0)}/100")

        return state

    def evaluate_aggregated_output(self, state: AgentState) -> AgentState:
        """
        Final Critic: Evaluate the aggregated output from multiple specialists.

        This is the final quality check before delivering to the user.
        """
        specification = state.get("specification", "")
        aggregated = state.get("aggregated_output", "")
        user_request = state.get("user_request", "")
        sub_tasks = state.get("sub_tasks", [])

        # Build sub-task summary for the critic
        sub_task_summary = ""
        if sub_tasks:
            lines = []
            for i, st in enumerate(sub_tasks):
                status_icon = "completed" if st.get("status") == "completed" else "failed"
                lines.append(
                    f"  {i+1}. {st.get('task_type', 'unknown')} → {st.get('specialist_adapter', 'unknown')} "
                    f"({status_icon}, score: {st.get('output_score', 0)}/100)"
                )
            sub_task_summary = f"\n**Sub-Task Breakdown**:\n" + "\n".join(lines) + "\n"

        # Inject skill-declared criteria if available
        routed_task_type = state.get("routed_task_type", "general")
        criteria_section = _get_skill_criteria(state, routed_task_type)

        prompt = f"""Evaluate the final aggregated output that combines work from multiple specialists.

**Original User Request**: {user_request}

**Overall Specification**: {specification}
{sub_task_summary}
**Aggregated Output**: {aggregated}
{criteria_section}
Evaluate whether the aggregated output:
1. Fully addresses the user's original request
2. Maintains coherence across specialist contributions (no contradictions or gaps)
3. Properly integrates outputs (not just concatenated sections)

Output format:
SCORES:
Completeness: [0-100]
Accuracy: [0-100]
Quality: [0-100]
Coherence: [0-100]
Helpfulness: [0-100]
Overall: [0-100]

REASONING:
[Detailed feedback on integration quality and any gaps between specialist outputs]"""

        critic = self.adapters.switch_to("critic")
        evaluation = critic.generate(prompt, temperature=0.1, max_tokens=600)

        scores, feedback = self._parse_critic_output(evaluation)

        state["output_critic_scores"] = scores
        state["output_critic_score"] = scores.get("overall", 0)
        state["output_critic_feedback"] = feedback

        # Copy aggregated output to specialist_output for compatibility
        state["specialist_output"] = aggregated

        logger.info(f"Final Aggregated Critic score: {scores.get('overall', 0)}/100")

        return state
