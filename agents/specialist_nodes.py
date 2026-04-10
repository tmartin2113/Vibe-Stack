"""
Specialist Execution Node Implementations

All specialist execution methods extracted from the AgentNodes monolith.
These are mixed into AgentNodes via SpecialistNodesMixin.
"""

from typing import Dict, Any, List, Optional, Tuple
import logging
import re

from .state import AgentState, get_context_for_node, add_to_history
from .tools import ToolResult
from .skill_security import SkillSecurity

logger = logging.getLogger(__name__)

# Configuration: Maximum tool calling iterations per specialist execution
# This prevents infinite loops while allowing specialists to validate their work
MAX_TOOL_CALLING_ITERATIONS = 3

# Instruction appended to specialist prompts so the LLM knows how to
# signal that it needs more information from the user.
_CLARIFICATION_INSTRUCTION = """\
If you need more information from the user before you can produce a good solution, \
wrap your questions in a <clarification_needed> tag like this:

<clarification_needed>
1. Your first question
2. Your second question
</clarification_needed>

Do NOT combine a clarification request with a solution attempt — either ask questions or provide a solution."""

# Regex to detect <clarification_needed>...</clarification_needed> blocks
_CLARIFICATION_RE = re.compile(
    r"<clarification_needed>\s*(.*?)\s*</clarification_needed>",
    re.DOTALL,
)


def parse_clarification(output: str) -> Tuple[bool, List[str]]:
    """
    Check if the specialist output contains a clarification request.

    The specialist signals it needs more context by including::

        <clarification_needed>
        1. What database engine are you using?
        2. What is the expected request volume?
        </clarification_needed>

    Returns:
        (needs_clarification, questions) — True + list of question strings
        if the tag was found, (False, []) otherwise.
    """
    match = _CLARIFICATION_RE.search(output)
    if not match:
        return False, []

    raw = match.group(1).strip()
    if not raw:
        return False, []

    # Split on numbered lines (1. / 2. / -) or newlines
    questions: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        # Strip leading numbers/bullets: "1.", "2)", "-", "*"
        line = re.sub(r"^[\d]+[.)]\s*", "", line)
        line = re.sub(r"^[-*]\s*", "", line)
        line = line.strip()
        if line:
            questions.append(line)

    return bool(questions), questions


class SpecialistNodesMixin:
    """
    Mixin containing all specialist execution node methods.

    Depends on self.adapters, self.tool_registry, and self.config.
    """

    @staticmethod
    def _resolve_skill_adapter_prompt(loaded_skills: list) -> str:
        """
        Extract adapter_prompt from the primary loaded skill.

        The first skill with a non-empty adapter_prompt wins.  This allows
        skills to define the specialist's system prompt, making the agent
        type determined by the spec + skills rather than hardcoded mappings.
        """
        for skill in loaded_skills:
            prompt = skill.get("adapter_prompt")
            if prompt:
                return prompt
        return ""

    @staticmethod
    def _resolve_skill_generation_config(loaded_skills: list) -> Dict[str, Any]:
        """
        Extract generation_config from the primary loaded skill.

        Merges configs from all loaded skills (first skill wins on conflicts).
        """
        merged: Dict[str, Any] = {}
        for skill in reversed(loaded_skills):  # reversed so first skill wins
            config = skill.get("generation_config")
            if config:
                merged.update(config)
        return merged

    @staticmethod
    def _resolve_skill_tools_enabled(loaded_skills: list):
        """
        Extract tools_enabled from loaded skills.

        Returns True/False if any skill declares it, None if no skill has
        an opinion (fall back to hardcoded set).
        """
        for skill in loaded_skills:
            val = skill.get("tools_enabled")
            if val is not None:
                return val
        return None

    def _get_specialist_config(self, specialist_name: str) -> Dict[str, Any]:
        """
        Get generation parameters for a specialist.

        Pulls from SystemConfig.generation if available, otherwise falls back
        to sensible per-specialist defaults.
        """
        # Try to use centralized GenerationConfig
        gen_config = getattr(self.config, 'generation', None) if self.config else None
        if gen_config:
            config = gen_config.get_config(specialist_name)
            if config != gen_config.get_config("general") or specialist_name == "general":
                return config  # type: ignore[no-any-return]

        # Fallback: per-specialist defaults (used when no config or no override)
        defaults = {
            # Legacy task types (used by execute_task)
            "code": {"temperature": 0.3, "max_tokens": 1500},
            "creative": {"temperature": 0.8, "max_tokens": 2000},
            "research": {"temperature": 0.4, "max_tokens": 1500},
            "general": {"temperature": 0.5, "max_tokens": 1000},
            # Specialist adapter names (used by execute_with_specialist / execute_sub_task)
            "test_generator": {"temperature": 0.3, "max_tokens": 1500},
            "security_auditor": {"temperature": 0.2, "max_tokens": 1500},
            "doc_generator": {"temperature": 0.4, "max_tokens": 1200},
            "performance_optimizer": {"temperature": 0.3, "max_tokens": 1200},
            "debugging_assistant": {"temperature": 0.3, "max_tokens": 1500},
            "data_specialist": {"temperature": 0.3, "max_tokens": 1500},
            "api_generator": {"temperature": 0.3, "max_tokens": 1500},
            "database_specialist": {"temperature": 0.2, "max_tokens": 1200},
            "code_reviewer": {"temperature": 0.3, "max_tokens": 1800},
            "vibe": {"temperature": 0.5, "max_tokens": 1500}
        }
        return defaults.get(specialist_name, defaults["vibe"])

    def execute_with_specialist(self, state: AgentState) -> AgentState:
        """
        Execute the task using the routed specialist adapter.

        Uses the user request and any previous feedback to generate output.
        Supports iterative improvement based on Critic Stage 2 feedback.
        Passes multi-turn history for refinement loops so the model can see
        its prior attempts alongside critic feedback.
        """
        specialist_name = state.get("specialist_adapter", "vibe")
        specialist_iteration = state.get("specialist_iteration_count", 0)

        # Gather workflow context for the specialist
        user_request = state.get("user_request", "")
        routed_task_type = state.get("routed_task_type", "general")
        routing_confidence = state.get("routing_confidence", 0)
        loaded_skills = state.get("loaded_skills", [])

        # Build skill context if skills were discovered — inject full SKILL.md
        # content so the specialist has domain-specific instructions, not just names.
        skill_context = ""
        if loaded_skills:
            skill_sections = []
            for skill in loaded_skills:
                content = skill.get("content", "")
                name = skill.get("name", "unknown")
                if content:
                    # Truncate very long skills to keep prompt reasonable
                    truncated = content[:3000]
                    if len(content) > 3000:
                        truncated += "\n[...skill content truncated...]"
                    skill_sections.append(
                        f"### Skill: {name}\n\n{truncated}"
                    )
                else:
                    skill_sections.append(f"### Skill: {name} (content unavailable)")

            if skill_sections:
                skill_context = (
                    "\n\n## Relevant Skills\n\n"
                    "Follow the instructions in these skills when applicable:\n\n"
                    + "\n\n---\n\n".join(skill_sections)
                )

        # Build multi-turn history for refinement iterations
        multi_turn_history = self._build_refinement_history(state) if specialist_iteration > 0 else []

        # Build memory context if available (auto-injected by inject_memory node)
        memory_context = state.get("memory_context", "")

        # Build prompt for specialist
        if specialist_iteration == 0:
            base_prompt = f"""Complete the following task.

**Task**: {user_request}

**Task Type**: {routed_task_type} (confidence: {routing_confidence:.0%})
{skill_context}
{memory_context}

{_CLARIFICATION_INSTRUCTION}

Otherwise, provide a complete, high-quality solution that directly addresses the task."""

        else:
            previous_output = state.get("specialist_output", "")
            feedback = state.get("output_critic_feedback", "")
            score = state.get("output_critic_score", 0)

            base_prompt = f"""Your previous attempt scored {score}/100. Improve it based on the feedback.

**Original Task**: {user_request}
{skill_context}
{memory_context}

**Your Previous Output**:
{previous_output}

**Critic Feedback**:
{feedback}

{_CLARIFICATION_INSTRUCTION}

Otherwise, focus on the specific issues identified in the feedback. Provide an improved solution."""

        # Determine tool access: skill-declared tools_enabled overrides the
        # hardcoded set, allowing arbitrary agent types to opt in/out of tools.
        tool_enabled_specialists = {
            "test_generator", "security_auditor", "data_specialist",
            "database_specialist", "code_reviewer",
            "vibe", "code", "api_generator", "performance_optimizer",
            "debugging_assistant", "doc_generator", "general",
        }
        skill_tools_enabled = self._resolve_skill_tools_enabled(loaded_skills)
        has_tool_access = (
            skill_tools_enabled
            if skill_tools_enabled is not None
            else specialist_name in tool_enabled_specialists
        )

        if has_tool_access:
            tool_schemas = self.tool_registry.get_all_schemas()
            tools_text = "\n".join([
                f"- {t['name']}: {t['description']}"
                for t in tool_schemas
            ])

            prompt = f"""{base_prompt}

**Available Tools**:
You can use the following tools to validate your work:
{tools_text}

To use a tool, include in your output:
<tool_call name="tool_name">{{"param1": "value1"}}</tool_call>

You can make multiple tool calls. After seeing tool results, you can refine your output."""
        else:
            prompt = base_prompt

        # Resolve adapter: skill-provided adapter_prompt overrides the
        # hardcoded specialist prompt, enabling spec-driven agent types.
        skill_adapter_prompt = self._resolve_skill_adapter_prompt(loaded_skills)
        specialist = self.adapters.get_or_create(
            specialist_name, skill_adapter_prompt
        )

        logger.info(f"Executing with specialist: {specialist_name} (iteration {specialist_iteration})"
                     + (" [skill-provided prompt]" if skill_adapter_prompt else ""))

        # Get generation config: skill-provided overrides take priority
        config = self._get_specialist_config(specialist_name)
        skill_gen_config = self._resolve_skill_generation_config(loaded_skills)
        if skill_gen_config:
            config = {**config, **skill_gen_config}

        # Tool calling loop - allow up to MAX_TOOL_CALLING_ITERATIONS tool calls
        max_tool_iterations = MAX_TOOL_CALLING_ITERATIONS
        tool_iteration = 0
        tool_results_history = []

        # Compute effective tool permissions from loaded skills
        effective_allowed_tools = SkillSecurity.compute_effective_allowed_tools(
            loaded_skills
        )

        # Pass multi-turn history for refinement iterations
        gen_kwargs = {**config}
        if multi_turn_history:
            gen_kwargs["history"] = multi_turn_history

        output = specialist.generate(
            prompt, task_type=state.get("routed_task_type"), **gen_kwargs
        )

        # If the specialist is asking for clarification, escalate to human.
        # (MiroFish handles simulation externally via the MiroFishSimulation tool.)
        needs_clarification, questions = parse_clarification(output)
        if needs_clarification:
            state["clarification_needed"] = True
            state["clarification_questions"] = questions
            state["specialist_output"] = output
            state["adapters_used"] = state.get("adapters_used", []) + [specialist_name]
            state["tool_calls_made"] = state.get("tool_calls_made", []) + tool_results_history
            logger.info(
                f"Specialist requested clarification ({len(questions)} questions) "
                f"— escalating to human"
            )
            return state

        while tool_iteration < max_tool_iterations:
            try:
                # Check if output contains tool calls
                tool_call = self.tool_registry.parse_tool_call(output)

                if not tool_call:
                    # No more tool calls, we're done
                    break

                tool_name = tool_call["name"]
                tool_params = tool_call["params"]

                logger.info(f"Specialist requested tool: {tool_name}")

                # Security: enforce skill tool permissions
                if (effective_allowed_tools is not None
                        and tool_name not in effective_allowed_tools):
                    logger.warning(
                        f"BLOCKED: Specialist {specialist_name} requested "
                        f"tool {tool_name!r} which is not in the allowed "
                        f"set {sorted(effective_allowed_tools)} for loaded skills"
                    )
                    tool_result = ToolResult(
                        success=False,
                        output="",
                        error=(
                            f"Tool '{tool_name}' is not permitted by the "
                            f"loaded skill(s). Allowed tools: "
                            f"{sorted(effective_allowed_tools)}"
                        ),
                    )
                else:
                    # Execute tool
                    tool_result = self.tool_registry.execute_tool(tool_name, **tool_params)

                # Format tool result for specialist
                if tool_result.success:
                    result_text = f"""Tool: {tool_name}
Status: SUCCESS
Output:
{tool_result.output}"""
                else:
                    result_text = f"""Tool: {tool_name}
Status: FAILED
Error: {tool_result.error}"""

                tool_results_history.append({
                    "tool": tool_name,
                    "params": tool_params,
                    "result": tool_result.to_dict()
                })

                # Provide tool result back to specialist with full context
                continuation_prompt = f"""Original Task:
{user_request}

Your Previous Response:
{output}

Tool Execution Result:
{result_text}

Based on this tool result, continue with your solution. You can:
1. Make additional tool calls if needed to validate further
2. Refine your solution based on the tool output
3. Provide your final solution

If you're satisfied with the results, provide your final output without more tool calls."""

                # Generate next iteration with reduced max_tokens to
                # discourage verbose narration between tool calls.
                # The model should either make the next tool call or
                # provide final output — not narrate what it's doing.
                continuation_config = {**config}
                continuation_max = min(config.get("max_tokens", 1500), 500)
                continuation_config["max_tokens"] = continuation_max
                output = specialist.generate(
                    continuation_prompt,
                    task_type=state.get("routed_task_type"),
                    **continuation_config,
                )
                tool_iteration += 1

            except Exception as e:
                # If tool calling fails, log error and continue with current output
                logger.error(f"Tool calling error in iteration {tool_iteration}: {e}", exc_info=True)
                tool_results_history.append({
                    "tool": "error",
                    "params": {},
                    "result": {
                        "success": False,
                        "output": "",
                        "error": f"Tool calling loop error: {str(e)}",
                        "metadata": {}
                    }
                })
                # Break loop on error to avoid cascading failures
                break

        # Check if we hit max iterations with unparsed tool calls
        if tool_iteration >= max_tool_iterations:
            remaining_tool_call = self.tool_registry.parse_tool_call(output)
            if remaining_tool_call:
                logger.warning(f"Tool iteration limit reached ({max_tool_iterations}). "
                             f"Unparsed tool call for '{remaining_tool_call['name']}' ignored.")
                # Add note to output for visibility
                output = output + f"\n\n[Note: Tool iteration limit reached. Tool call for '{remaining_tool_call['name']}' was not executed.]"

        logger.info(f"Specialist {specialist_name} generated output (with {len(tool_results_history)} tool calls)")
        logger.debug(f"Output: {output[:100]}...")

        state["specialist_output"] = output
        state["adapters_used"] = state.get("adapters_used", []) + [specialist_name]
        state["tool_calls_made"] = state.get("tool_calls_made", []) + tool_results_history

        return state

    def plan_refinement(self, state: AgentState) -> AgentState:
        """
        Analyze critique and create concrete improvement plan.
        """
        context = get_context_for_node(state, "refinement")
        specialist_name = state.get("specialist_adapter", "general")
        routed_task_type = state.get("routed_task_type", "general")
        user_request = state.get("user_request", "")

        prompt = f"""The {routed_task_type.replace('_', ' ')} output scored {context.get('output_critic_score', 0)}/100. Plan refinements.

**Original User Request**: {user_request}

**Specialist**: {specialist_name} ({routed_task_type.replace('_', ' ')})

**Critic Scores**:
{self._format_scores(context.get('output_critic_scores', {}))}

**Critic Feedback**: {context.get('output_critic_feedback', 'N/A')}

**Iteration**: {context['iteration']}/{context['iteration'] + context.get('iterations_remaining', 0)}

Given that this is a {routed_task_type.replace('_', ' ')} task executed by the {specialist_name} specialist, what specific changes would most improve the score?
Focus on the top 2-3 highest-impact improvements that are actionable by this specialist."""

        # Use Refinement adapter
        refinement = self.adapters.switch_to("refinement")

        plan = refinement.generate(
            prompt,
            temperature=0.4,
            max_tokens=300
        )

        logger.info("Refinement plan created")
        logger.debug(f"Plan: {plan[:100]}...")

        state["adapters_used"] = state.get("adapters_used", []) + ["refinement"]

        # Add current iteration to history before next loop
        state = add_to_history(state)

        return state

    def execute_sub_task(self, state: AgentState) -> AgentState:
        """
        Execute a specific sub-task with its designated specialist.

        Uses the sub-task's specification and specialist adapter to
        generate output. Supports iterative refinement.
        """
        sub_tasks = state.get("sub_tasks", [])
        current_index = state.get("current_sub_task_index", 0)

        if current_index >= len(sub_tasks):
            return state

        current_subtask = sub_tasks[current_index]
        sub_spec = current_subtask.get("specification", "")
        specialist_name = current_subtask["specialist_adapter"]
        iteration = current_subtask.get("iteration_count", 0)
        task_type = current_subtask.get("task_type", "general")
        user_request = state.get("user_request", "")

        # Build sibling output context for sequential workflows
        sibling_output_context = ""
        if not state.get("parallel_execution", True):
            # Sequential: earlier siblings' outputs are available
            completed_siblings = []
            for i, st in enumerate(sub_tasks):
                if i >= current_index:
                    break
                if st.get("status") in ("completed", "evaluated", "executed") and st.get("output"):
                    sib_type = st.get("task_type", "unknown")
                    sib_output = st["output"]
                    # Truncate long outputs to avoid token explosion
                    if len(sib_output) > 800:
                        sib_output = sib_output[:800] + "\n... [truncated]"
                    completed_siblings.append(
                        f"### {sib_type.replace('_', ' ').title()} (Score: {st.get('output_score', 0)}/100)\n{sib_output}"
                    )
            if completed_siblings:
                sibling_output_context = (
                    "\n\n**Outputs from earlier specialists** (build on these, don't duplicate):\n"
                    + "\n\n".join(completed_siblings)
                )

        # Build skill context for this sub-task's domain
        sub_skill_context = ""
        loaded_skills = state.get("loaded_skills", [])
        # Filter to skills relevant to this sub-task's type (used for both
        # prompt injection AND tool permission enforcement below).
        relevant_skills = [s for s in loaded_skills if s.get("task_type") == task_type]
        if not relevant_skills and loaded_skills:
            logger.debug(
                f"No skills match sub-task task_type={task_type!r}; "
                f"falling back to all {len(loaded_skills)} loaded skills"
            )
            relevant_skills = loaded_skills  # Fall back to all loaded skills
        if relevant_skills:
            for skill in relevant_skills[:2]:  # Limit to 2 skills per sub-task
                content = skill.get("content", "")
                name = skill.get("name", "unknown")
                if content:
                    truncated = content[:2000]
                    if len(content) > 2000:
                        truncated += "\n[...skill content truncated...]"
                    sub_skill_context += f"\n\n### Skill: {name}\n\n{truncated}"
            if sub_skill_context:
                sub_skill_context = (
                    "\n\n## Relevant Skills\n\n"
                    "Follow these skill instructions when applicable:"
                    + sub_skill_context
                )

        # Memory context (auto-injected by inject_memory node)
        memory_context = state.get("memory_context", "")

        # Build prompt
        if iteration == 0:
            prompt = f"""Complete the following sub-task as part of a multi-specialist plan.

**Original User Request**: {user_request}

**Your Sub-Task** ({task_type.replace('_', ' ')}):
{sub_spec}
{sibling_output_context}
{sub_skill_context}
{memory_context}

{_CLARIFICATION_INSTRUCTION}

Otherwise, provide a high-quality, complete solution focused on your area of expertise."""
        else:
            # Refinement
            prev_output = current_subtask.get("output", "")
            feedback = current_subtask.get("output_feedback", "")
            score = current_subtask.get("output_score", 0)

            prompt = f"""Your previous attempt scored {score}/100. Improve based on feedback.

**Original User Request**: {user_request}

**Specification**: {sub_spec}
{sibling_output_context}
{sub_skill_context}
{memory_context}

**Your Previous Output**: {prev_output}

**Critic Feedback**: {feedback}

{_CLARIFICATION_INSTRUCTION}

Otherwise, focus on the specific issues identified. Provide an improved solution."""

        # Load specialist — skill-provided adapter_prompt overrides hardcoded
        skill_adapter_prompt = self._resolve_skill_adapter_prompt(relevant_skills)
        specialist = self.adapters.get_or_create(specialist_name, skill_adapter_prompt)

        # Get generation config: skill-provided overrides take priority
        config = self._get_specialist_config(specialist_name)
        skill_gen_config = self._resolve_skill_generation_config(relevant_skills)
        if skill_gen_config:
            config = {**config, **skill_gen_config}

        # Determine tool access: skill-declared tools_enabled overrides hardcoded set
        tool_enabled_specialists = {
            "test_generator", "security_auditor", "data_specialist",
            "database_specialist", "code_reviewer",
            "vibe", "code", "api_generator", "performance_optimizer",
            "debugging_assistant", "doc_generator", "general",
        }
        skill_tools_enabled = self._resolve_skill_tools_enabled(relevant_skills)
        has_tool_access = (
            skill_tools_enabled
            if skill_tools_enabled is not None
            else specialist_name in tool_enabled_specialists
        )

        # Generate initial output
        output = specialist.generate(
            prompt, task_type=state.get("routed_task_type"), **config
        )

        # If the sub-task specialist asks for clarification, escalate to human.
        needs_clarification, questions = parse_clarification(output)
        if needs_clarification:
            state["clarification_needed"] = True
            state["clarification_questions"] = state.get("clarification_questions", []) + questions
            current_subtask["output"] = output
            current_subtask["status"] = "clarification_needed"
            sub_tasks[current_index] = current_subtask
            state["sub_tasks"] = sub_tasks
            logger.info(
                f"Sub-task {current_index} specialist requested clarification "
                f"({len(questions)} questions) — escalating to human"
            )
            return state

        # Tool calling loop if specialist supports it
        if has_tool_access:
            max_tool_iterations = MAX_TOOL_CALLING_ITERATIONS
            tool_iteration = 0
            tool_results_history = []

            # Compute effective tool permissions from relevant skills only
            # (matches the subset whose content was injected into the prompt)
            effective_allowed_tools = SkillSecurity.compute_effective_allowed_tools(
                relevant_skills
            )

            while tool_iteration < max_tool_iterations:
                try:
                    # Check if output contains tool calls
                    tool_call = self.tool_registry.parse_tool_call(output)

                    if not tool_call:
                        # No more tool calls, we're done
                        break

                    tool_name = tool_call["name"]
                    tool_params = tool_call["params"]

                    logger.info(f"Sub-task {current_index} specialist requested tool: {tool_name}")

                    # Security: enforce skill tool permissions
                    if (effective_allowed_tools is not None
                            and tool_name not in effective_allowed_tools):
                        logger.warning(
                            f"BLOCKED: Sub-task {current_index} specialist "
                            f"{specialist_name} requested tool {tool_name!r} "
                            f"which is not in the allowed set "
                            f"{sorted(effective_allowed_tools)} for loaded skills"
                        )
                        tool_result = ToolResult(
                            success=False,
                            output="",
                            error=(
                                f"Tool '{tool_name}' is not permitted by the "
                                f"loaded skill(s). Allowed tools: "
                                f"{sorted(effective_allowed_tools)}"
                            ),
                        )
                    else:
                        # Execute tool
                        tool_result = self.tool_registry.execute_tool(tool_name, **tool_params)

                    # Format tool result
                    if tool_result.success:
                        result_text = f"""Tool: {tool_name}
Status: SUCCESS
Output:
{tool_result.output}"""
                    else:
                        result_text = f"""Tool: {tool_name}
Status: FAILED
Error: {tool_result.error}"""

                    tool_results_history.append({
                        "tool": tool_name,
                        "params": tool_params,
                        "result": tool_result.to_dict()
                    })

                    # Provide tool result back with context
                    continuation_prompt = f"""Original Specification:
{sub_spec}

Your Previous Response:
{output}

Tool Execution Result:
{result_text}

Based on this tool result, continue with your solution. You can:
1. Make additional tool calls if needed
2. Refine your solution based on the tool output
3. Provide your final solution

If satisfied, provide final output without more tool calls."""

                    # Generate next iteration with reduced max_tokens
                    continuation_config = {**config}
                    continuation_max = min(config.get("max_tokens", 1500), 500)
                    continuation_config["max_tokens"] = continuation_max
                    output = specialist.generate(continuation_prompt, **continuation_config)
                    tool_iteration += 1

                except Exception as e:
                    logger.error(f"Tool calling error in sub-task {current_index}, iteration {tool_iteration}: {e}", exc_info=True)
                    tool_results_history.append({
                        "tool": "error",
                        "params": {},
                        "result": {
                            "success": False,
                            "output": "",
                            "error": f"Tool calling error: {str(e)}",
                            "metadata": {}
                        }
                    })
                    break

            # Check if max iterations reached with unparsed tool calls
            if tool_iteration >= max_tool_iterations:
                remaining_tool_call = self.tool_registry.parse_tool_call(output)
                if remaining_tool_call:
                    logger.warning(f"Sub-task {current_index} tool iteration limit reached. "
                                 f"Unparsed tool call for '{remaining_tool_call['name']}' ignored.")
                    output = output + f"\n\n[Note: Tool iteration limit reached. Tool call for '{remaining_tool_call['name']}' was not executed.]"

            # Store tool history in sub-task
            current_subtask["tool_calls"] = tool_results_history
            logger.info(f"Sub-task {current_index} made {len(tool_results_history)} tool calls")

        # Update sub-task
        current_subtask["output"] = output
        current_subtask["status"] = "executed"
        sub_tasks[current_index] = current_subtask
        state["sub_tasks"] = sub_tasks

        logger.info(f"Executed sub-task {current_index} with {specialist_name}")

        return state
