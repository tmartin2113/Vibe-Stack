"""
Simulation Adapter Definitions

Prompt constants and PromptAdapter registrations for the MiroFish-inspired
simulation module.  Extracted from simulation.py to keep prompt content
separate from orchestration logic.
"""

import logging
from typing import Any

from .adapters import AdapterRegistry, PromptAdapter
from .simulation_budget import _SIM_MAX_TOKENS

logger = logging.getLogger(__name__)

# ── Simulation system prompts ─────────────────────────────────────

PERSONA_MAINTAINER_PROMPT = """\
You are a senior software maintainer reviewing a multi-specialist \
integration plan. Focus on:
- Cross-component interface mismatches (function signatures, data shapes)
- Naming inconsistencies across specialist outputs
- Missing error handling at integration boundaries
- Dependency conflicts between sub-task outputs

Be specific and concise. List concrete conflicts, not general advice."""

PERSONA_CONSUMER_PROMPT = """\
You are an end-user/developer who will consume the outputs of this \
multi-specialist workflow. Focus on:
- API usability and consistency across generated components
- Missing documentation or unclear usage patterns
- Gaps between what was requested and what is being produced
- Whether the combined outputs form a coherent, usable deliverable

Be specific and concise. List concrete gaps, not general advice."""

PERSONA_QA_PROMPT = """\
You are a QA engineer reviewing a multi-specialist plan for testability \
and correctness. Focus on:
- Untested integration paths between specialist outputs
- Edge cases that fall between specialist boundaries
- Assumptions one specialist makes about another's output
- Data flow inconsistencies across the pipeline

Be specific and concise. List concrete risks, not general advice."""

SYNTHESIS_PROMPT = """\
You are an integration analyst. Given the following reviewer perspectives \
on a multi-specialist plan, produce a brief conflict/risk report.

Format your output as:
## Integration Risks
- [RISK_LEVEL: HIGH|MEDIUM|LOW] <description>

## Recommended Mitigations
- <actionable suggestion>

Keep it under 400 words. Only include genuinely actionable items."""

STAKEHOLDER_PROMPTS = {
    "product_owner": """\
You are a product owner. Answer the following clarification questions \
from the perspective of someone who wrote the original request. Infer \
reasonable answers from the specification context. If you genuinely \
cannot answer a question, say "UNCERTAIN" for that question.

After each answer, rate your confidence: HIGH, MEDIUM, or LOW.""",

    "end_user": """\
You are a typical end-user of the system being built. Answer the \
following clarification questions from the perspective of someone who \
will use the final product. Prefer simple, practical answers. If you \
genuinely cannot answer, say "UNCERTAIN".

After each answer, rate your confidence: HIGH, MEDIUM, or LOW.""",

    "domain_expert": """\
You are a domain expert. Answer the following clarification questions \
using technical best practices and common patterns. If the question \
is too context-dependent to answer generically, say "UNCERTAIN".

After each answer, rate your confidence: HIGH, MEDIUM, or LOW.""",
}

MEDIATOR_PROMPT = """\
You are a mediator synthesizing answers from multiple stakeholder \
perspectives. For each question:

1. If stakeholders agree, adopt the consensus answer.
2. If they disagree but one has HIGH confidence, prefer that answer.
3. If all are UNCERTAIN or conflict with LOW confidence, mark as UNRESOLVED.

Output format:
## Resolved
- Q: <question>
  A: <synthesized answer>
  Confidence: <HIGH|MEDIUM|LOW>

## Unresolved
- Q: <question>
  Reason: <why it could not be resolved>

Overall confidence: <float 0.0-1.0>"""

SKILL_VET_SPECIALIST_PROMPT = """\
You are an AI specialist executing a task using a skill definition. \
Given the skill instructions and a task description, produce a short \
code/text output that follows the skill's workflow steps. \
Keep your output concise (under 400 words)."""

SKILL_VET_CRITIC_PROMPT = """\
You are a quality critic. Given a specialist's output for a task, \
score it 0-100 and provide brief feedback. Output exactly:
SCORE: <number>
FEEDBACK: <one-line summary>"""

SKILL_VET_TASK_GEN_PROMPT = """\
You are a task generator. Given a task type, produce 3-5 realistic \
one-paragraph task descriptions that a user might submit. \
Output each task on its own line prefixed with "TASK: "."""


# ── Adapter definition lists ──────────────────────────────────────

# Simulation adapter definitions: (name, system_prompt, generation_config)
_SIM_ADAPTER_DEFS = [
    ("sim_maintainer", PERSONA_MAINTAINER_PROMPT, {"temperature": 0.5, "max_tokens": _SIM_MAX_TOKENS}),
    ("sim_consumer", PERSONA_CONSUMER_PROMPT, {"temperature": 0.6, "max_tokens": _SIM_MAX_TOKENS}),
    ("sim_qa", PERSONA_QA_PROMPT, {"temperature": 0.4, "max_tokens": _SIM_MAX_TOKENS}),
    ("sim_synthesis", SYNTHESIS_PROMPT, {"temperature": 0.2, "max_tokens": _SIM_MAX_TOKENS}),
    ("sim_mediator", MEDIATOR_PROMPT, {"temperature": 0.2, "max_tokens": _SIM_MAX_TOKENS}),
]

# Stakeholder adapters for clarification simulation
_STAKEHOLDER_ADAPTER_DEFS = [
    (f"sim_stakeholder_{role}", prompt, {"temperature": 0.5, "max_tokens": 400})
    for role, prompt in STAKEHOLDER_PROMPTS.items()
]

_SKILL_VET_ADAPTER_DEFS = [
    ("sim_vet_specialist", SKILL_VET_SPECIALIST_PROMPT, {"temperature": 0.4, "max_tokens": _SIM_MAX_TOKENS}),
    ("sim_vet_critic", SKILL_VET_CRITIC_PROMPT, {"temperature": 0.1, "max_tokens": 200}),
    ("sim_vet_task_gen", SKILL_VET_TASK_GEN_PROMPT, {"temperature": 0.7, "max_tokens": _SIM_MAX_TOKENS}),
]


# ── Adapter registration ───────────────────────────────────────────

def register_simulation_adapters(
    registry: AdapterRegistry,
    base_model: Any,
) -> None:
    """Register simulation PromptAdapters on the shared base_model.

    Called once during WorkflowFactory._ensure_initialised().
    Each adapter is a lightweight wrapper — no extra model loading.
    """
    all_defs = _SIM_ADAPTER_DEFS + _STAKEHOLDER_ADAPTER_DEFS + _SKILL_VET_ADAPTER_DEFS
    for name, prompt, config in all_defs:
        adapter = PromptAdapter(name, prompt, base_model, config=config)
        registry.register(adapter)

    logger.info(
        "Registered %d simulation adapters",
        len(all_defs),
    )
