"""
Aggregator Node for Multi-Specialist Workflows

This node combines outputs from multiple specialist adapters
into a coherent final deliverable using LLM-driven synthesis.

The LLM resolves cross-references between specialist outputs (e.g.,
tests referencing functions from code generation), deduplicates
overlapping content, and produces a unified result. Falls back to
structured string concatenation if no adapter is available.
"""

import logging
from typing import Dict, Any, List, Optional

from .adapters import AdapterRegistry
from .state import AgentState

logger = logging.getLogger(__name__)

AGGREGATOR_SYSTEM_PROMPT = """You are an expert technical writer and integration specialist. \
Your role is to synthesize outputs from multiple specialist agents into a single, coherent deliverable. \
You excel at resolving cross-references between code, tests, and documentation, \
deduplicating overlapping content, and harmonizing structure across diverse technical outputs."""


class AggregatorNode:
    """
    Aggregates outputs from multiple specialists into a final result.

    When an adapter_registry is provided, the aggregator uses the vibe
    (or other available) LLM adapter to intelligently merge outputs —
    resolving cross-references, deduplicating content, and harmonizing
    style. Without a registry, it falls back to structured concatenation.

    Strategies:
    - "merge": Unified response integrating code + analysis outputs
    - "sequential": Narrative showing specialist progression
    - "report": Structured multi-section analysis report
    """

    def __init__(self, adapter_registry: Optional[AdapterRegistry] = None):
        self.name = "aggregator"
        self.adapter_registry = adapter_registry

    def execute(self, state: AgentState) -> AgentState:
        """
        Aggregate specialist outputs into final deliverable.

        Args:
            state: Current agent state with completed sub-tasks

        Returns:
            Updated state with aggregated_output
        """
        sub_tasks = state.get("sub_tasks", [])
        aggregation_strategy = state.get("aggregation_strategy", "merge")
        user_request = state.get("user_request", "")
        specification = state.get("specification", "")

        # Filter to completed sub-tasks only
        completed = [st for st in sub_tasks if st.get("status") == "completed"]

        if not completed:
            state["aggregated_output"] = "Error: No sub-tasks completed successfully."
            state["final_aggregation_score"] = 0
            return state

        # Build the raw material: structured representation of all outputs
        raw_sections = self._build_raw_sections(completed)

        # Try LLM-driven aggregation first, fall back to string concatenation
        adapter = self._get_aggregation_adapter()

        if adapter is not None:
            logger.info(f"Using LLM-driven aggregation (strategy: {aggregation_strategy})")
            aggregated = self._llm_aggregate(
                adapter, raw_sections, user_request, specification,
                aggregation_strategy
            )
        else:
            logger.info(f"No adapter available, using fallback concatenation (strategy: {aggregation_strategy})")
            aggregated = self._fallback_aggregate(
                raw_sections, user_request, aggregation_strategy
            )

        state["aggregated_output"] = aggregated

        # Track which outputs went into the aggregation
        completed_count = len(completed)
        state["completed_sub_tasks"] = completed_count

        # Calculate average output score
        avg_score = sum(st.get("output_score") or 0 for st in completed) / completed_count
        state["final_aggregation_score"] = int(avg_score)

        state["adapters_used"] = state.get("adapters_used", []) + ["aggregator"]

        return state

    # ===== LLM-DRIVEN AGGREGATION =====

    def _get_aggregation_adapter(self):
        """
        Get an LLM adapter for aggregation.

        Prefers 'vibe' (the spec-builder, good at synthesis) but
        will use any available adapter. Returns None if no registry
        or no adapter is available.
        """
        if self.adapter_registry is None:
            return None

        # Prefer vibe for synthesis tasks
        try:
            adapter = self.adapter_registry.get("vibe")
            if adapter is not None:
                return adapter
        except (KeyError, Exception):
            pass

        # Fall back to any available adapter
        adapter_names = self.adapter_registry.list_adapters()
        for name in adapter_names:
            try:
                adapter = self.adapter_registry.get(name)
                if adapter is not None:
                    return adapter
            except (KeyError, Exception):
                continue

        return None

    def _build_raw_sections(self, completed: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """
        Build structured sections from completed sub-tasks.

        Each section includes metadata that the LLM can use to
        understand relationships between outputs.
        """
        sections = []
        for subtask in completed:
            sections.append({
                "task_type": subtask.get("task_type", "general"),
                "title": self._get_section_title(subtask.get("task_type", "general")),
                "specialist": subtask.get("specialist_adapter", "unknown"),
                "output": subtask.get("output", ""),
                "score": subtask.get("output_score") or 0,
                "specification": subtask.get("specification", ""),
            })
        return sections

    def _llm_aggregate(
        self,
        adapter,
        raw_sections: List[Dict[str, str]],
        user_request: str,
        specification: str,
        strategy: str,
    ) -> str:
        """
        Use the LLM to intelligently aggregate specialist outputs.

        The prompt instructs the model to:
        1. Resolve cross-references (e.g., test imports matching code exports)
        2. Deduplicate overlapping explanations or code
        3. Harmonize naming, style, and structure
        4. Produce a unified deliverable appropriate to the strategy
        """
        # Format specialist outputs for the LLM prompt
        sections_text = self._format_sections_for_prompt(raw_sections)

        if strategy == "merge":
            prompt = self._build_merge_prompt(sections_text, user_request, specification)
        elif strategy == "sequential":
            prompt = self._build_sequential_prompt(sections_text, user_request, specification)
        elif strategy == "report":
            prompt = self._build_report_prompt(sections_text, user_request, specification)
        else:
            prompt = self._build_merge_prompt(sections_text, user_request, specification)

        try:
            result = adapter.generate(
                prompt, system_prompt=AGGREGATOR_SYSTEM_PROMPT,
                temperature=0.3, max_tokens=4000
            )

            if result and result.strip():
                stripped = result.strip()
                # Reject only truly empty/trivial responses (e.g., "OK", "Done").
                # A short but substantive response (e.g., a concise summary) is valid.
                # Use a low floor (10 chars) to catch LLM non-answers while
                # accepting legitimately brief aggregations.
                if len(stripped) > 10:
                    return stripped  # type: ignore[no-any-return]

            # LLM returned empty/trivial output, fall back
            logger.warning("LLM aggregation returned insufficient output, falling back")
            return self._fallback_aggregate(raw_sections, user_request, strategy)

        except Exception as e:
            logger.error(f"LLM aggregation failed: {e}, falling back to concatenation")
            return self._fallback_aggregate(raw_sections, user_request, strategy)

    def _format_sections_for_prompt(self, raw_sections: List[Dict[str, str]]) -> str:
        """Format raw sections into a text block for the LLM prompt."""
        parts = []
        for i, section in enumerate(raw_sections, 1):
            # Truncate very long outputs to stay within token limits
            output = section["output"]
            if len(output) > 3000:
                output = output[:3000] + "\n... [truncated for aggregation]"

            parts.append(
                f"### SPECIALIST {i}: {section['title']} "
                f"(by {section['specialist']}, score: {section['score']}/100)\n"
                f"{output}"
            )
        return "\n\n---\n\n".join(parts)

    def _build_merge_prompt(self, sections_text: str, user_request: str, specification: str) -> str:
        """Build the LLM prompt for merge-strategy aggregation."""
        return f"""You are integrating outputs from multiple specialist agents into a single, unified deliverable.

**Original User Request**: {user_request}

**Specification**: {specification}

**Specialist Outputs to Integrate**:

{sections_text}

**Your Task**: Produce a single, coherent response that a developer can use directly. Follow these rules:

1. **Resolve cross-references**: If tests reference functions/classes from the code output, ensure names match exactly. If documentation references API endpoints from the code, ensure paths and signatures are consistent.
2. **Deduplicate**: If multiple specialists explain the same concept or include overlapping code, keep the best version and remove redundancy. Do not repeat setup instructions, import blocks, or boilerplate that appears in multiple outputs.
3. **Harmonize structure**: Use consistent naming conventions, code style, and markdown formatting across all sections. Code blocks should use the same language tags.
4. **Order logically**: Put implementation code first, then tests, then documentation, then analysis (security, performance, etc.).
5. **Preserve all substance**: Do not drop any meaningful code, test case, finding, or recommendation. Dedup structure, not content.

Produce the final integrated output now. Do not include meta-commentary about the integration process."""

    def _build_sequential_prompt(self, sections_text: str, user_request: str, specification: str) -> str:
        """Build the LLM prompt for sequential-strategy aggregation."""
        return f"""You are combining outputs from multiple specialist agents that worked in sequence, where each built on the previous one's work.

**Original User Request**: {user_request}

**Specification**: {specification}

**Specialist Outputs (in execution order)**:

{sections_text}

**Your Task**: Produce a coherent narrative that shows how each specialist's work builds on the previous. Follow these rules:

1. **Resolve forward references**: Later specialists may reference earlier outputs. Ensure all names, paths, and interfaces are consistent across the chain.
2. **Remove redundant context**: Each specialist may have restated the problem or earlier findings. Keep one clear problem statement at the top, then show the progression.
3. **Show the build-up**: Use clear section headers (## Step 1, ## Step 2, etc.) to show the sequential flow, but make sure later sections reference earlier results naturally rather than repeating them.
4. **Deduplicate code**: If a later specialist refined or replaced code from an earlier step, show only the final version in context (with a note about what changed).
5. **Preserve the specialist attribution**: Note which specialist produced each section.

Produce the final sequential output now. Do not include meta-commentary about the integration process."""

    def _build_report_prompt(self, sections_text: str, user_request: str, specification: str) -> str:
        """Build the LLM prompt for report-strategy aggregation."""
        return f"""You are producing a structured analysis report from multiple specialist agents that worked independently on different aspects of the same request.

**Original User Request**: {user_request}

**Specification**: {specification}

**Specialist Outputs**:

{sections_text}

**Your Task**: Produce a professional report with the following structure:

1. **Executive Summary** (3-5 bullet points): Synthesize the key findings across ALL specialists into a unified summary. Do not just list each specialist's conclusion — identify themes, agreements, and conflicts between them.
2. **Detailed Findings**: One section per specialist area, but:
   - Cross-reference related findings (e.g., "The security audit identified the same input validation gap noted in the code review")
   - Deduplicate overlapping recommendations — if two specialists suggest the same fix, consolidate into one actionable item
   - Resolve any contradictions (e.g., one specialist says X is fine, another flags it)
3. **Consolidated Recommendations**: A single prioritized list of action items drawn from ALL specialist outputs, deduplicated and ordered by impact.

Produce the final report now. Do not include meta-commentary about the report generation process."""

    # ===== FALLBACK (String Concatenation) =====

    def _fallback_aggregate(
        self,
        raw_sections: List[Dict[str, str]],
        user_request: str,
        strategy: str,
    ) -> str:
        """
        Fallback aggregation using structured string concatenation.

        Used when no LLM adapter is available.
        """
        if strategy == "merge":
            return self._fallback_merge(raw_sections, user_request)
        elif strategy == "sequential":
            return self._fallback_sequential(raw_sections, user_request)
        elif strategy == "report":
            return self._fallback_report(raw_sections, user_request)
        else:
            return self._fallback_merge(raw_sections, user_request)

    def _fallback_merge(self, sections: List[Dict[str, str]], user_request: str) -> str:
        """Fallback merge: code first, then analysis, grouped by type."""
        result = f"# Result for: {user_request}\n\n"

        code_types = {"code_generation", "refactoring", "debugging"}
        code_sections = [s for s in sections if s["task_type"] in code_types]
        analysis_sections = [s for s in sections if s["task_type"] not in code_types]

        if code_sections:
            result += "## Implementation\n\n"
            for section in code_sections:
                result += f"{section['output']}\n\n"

        if analysis_sections:
            task_groups: Dict[str, List[Dict[str, str]]] = {}
            for section in analysis_sections:
                task_groups.setdefault(section["task_type"], []).append(section)

            for task_type, group in task_groups.items():
                result += f"## {self._get_section_title(task_type)}\n\n"
                for section in group:
                    result += f"{section['output']}\n\n"

        return result.strip()

    def _fallback_sequential(self, sections: List[Dict[str, str]], user_request: str) -> str:
        """Fallback sequential: numbered steps with specialist attribution."""
        result = f"# Result for: {user_request}\n\n"
        result += "*The following specialists worked on this task in sequence:*\n\n"

        for i, section in enumerate(sections, 1):
            result += f"## Step {i}: {section['title']}\n"
            result += f"*Specialist: {section['specialist']} (Quality Score: {section['score']}/100)*\n\n"
            result += f"{section['output']}\n\n"
            result += "---\n\n"

        return result.strip()

    def _fallback_report(self, sections: List[Dict[str, str]], user_request: str) -> str:
        """Fallback report: structured report with executive summary."""
        result = "# Comprehensive Analysis Report\n\n"
        result += f"**Request:** {user_request}\n\n"
        result += f"**Specialists Consulted:** {len(sections)}\n\n"

        # Executive summary
        result += "## Executive Summary\n\n"
        result += "This report presents findings from multiple specialist analyses:\n\n"
        for section in sections:
            result += f"- **{section['title']}**: Quality Score {section['score']}/100\n"
        result += "\n---\n\n"

        # Detailed sections
        result += "## Detailed Findings\n\n"
        for i, section in enumerate(sections, 1):
            result += f"### {i}. {section['title']}\n"
            result += f"*By: {section['specialist']} | Quality: {section['score']}/100*\n\n"
            result += f"{section['output']}\n\n"
            result += "---\n\n"

        # Overall assessment
        avg_score = sum(int(s["score"]) for s in sections) / len(sections) if sections else 0
        result += "## Overall Assessment\n\n"
        result += f"**Average Quality Score:** {avg_score:.1f}/100\n\n"

        if avg_score >= 85:
            result += "All analyses meet quality standards.\n"
        elif avg_score >= 70:
            result += "Generally good quality, some areas may need attention.\n"
        else:
            result += "Quality concerns identified, review recommended.\n"

        return result.strip()

    # ===== SHARED HELPERS =====

    def _get_section_title(self, task_type: str) -> str:
        """Get human-readable section title for task type."""
        titles = {
            "test_generation": "Test Suite",
            "security_audit": "Security Audit",
            "documentation": "Documentation",
            "performance_optimization": "Performance Optimization",
            "debugging": "Debug Report",
            "refactoring": "Refactored Code",
            "code_generation": "Generated Code",
            "data_processing": "Data Processing",
            "api_development": "API Development",
            "database_operations": "Database Operations",
            "code_review": "Code Review",
            "general": "General Analysis",
        }
        return titles.get(task_type, task_type.replace("_", " ").title())


# Convenience function for graph integration
def aggregate_outputs(state: AgentState, adapter_registry: Optional[AdapterRegistry] = None) -> AgentState:
    """
    Aggregate specialist outputs into final result.

    This is a convenience function that can be used directly in the graph.

    Args:
        state: Current agent state
        adapter_registry: Optional adapter registry for LLM-driven aggregation
    """
    aggregator = AggregatorNode(adapter_registry=adapter_registry)
    return aggregator.execute(state)
