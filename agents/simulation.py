"""
MiroFish-Inspired Simulation Module

Swarm intelligence prediction for the Vibe workflow pipeline.
Runs lightweight persona-based simulations to predict integration
conflicts, resolve clarification ambiguity, and vet skills.

Hardware-aware: probes GPU VRAM headroom before launching simulation
rounds. On constrained systems (<=22GB VRAM), runs sequential persona
rounds with tight token budgets. Skips simulation entirely when free
VRAM is below a configurable floor.

Integration points:
    1. Parallel sidecar  — runs alongside multi-specialist sub-tasks
    2. Clarification sim  — short-circuits human round-trip when possible
    3. Skill vetting      — offline batch evaluation (not wired into hot path)

All simulation calls reuse the already-loaded LLM backend via
PromptAdapter instances registered on the shared base_model.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .adapters import AdapterRegistry, PromptAdapter

logger = logging.getLogger(__name__)

# ── Environment knobs ──────────────────────────────────────────────

# Minimum free VRAM (MB) required to run the **sidecar** simulation
# (which competes with specialists for KV cache).  Set high because
# on constrained cards (<=24GB) the sidecar steals KV slots from
# specialists and adds latency rather than hiding behind it.
# Clarification simulation is NOT gated by this — it runs when the
# specialist is paused, so there's no KV contention.
_MIN_FREE_VRAM_MB_SIDECAR = int(os.getenv("VIBE_SIM_MIN_FREE_VRAM_MB", "6144"))

# Maximum persona rounds per simulation invocation (sequential).
_MAX_PERSONA_ROUNDS = int(os.getenv("VIBE_SIM_MAX_PERSONA_ROUNDS", "3"))

# Token budget for simulation adapter calls (keeps KV cache small).
_SIM_MAX_TOKENS = int(os.getenv("VIBE_SIM_MAX_TOKENS", "600"))

# Confidence threshold: simulated clarification answers below this
# are discarded and the questions go to the human as before.
_CLARIFICATION_CONFIDENCE_THRESHOLD = float(
    os.getenv("VIBE_SIM_CLARIFICATION_CONFIDENCE", "0.6")
)

# Master kill-switch: set VIBE_SIM_ENABLED=false to disable all simulation.
_SIM_ENABLED = os.getenv("VIBE_SIM_ENABLED", "true").lower() not in (
    "false", "0", "no",
)

# Skill vetting: set VIBE_SIM_VET_SKILLS=false to skip offline skill vetting.
_VET_SKILLS_ENABLED = os.getenv("VIBE_SIM_VET_SKILLS", "true").lower() not in (
    "false", "0", "no",
)

# Startup delay (seconds) for the parallel sidecar so specialists
# get a head-start on KV cache allocation.
_SIDECAR_DELAY_SECONDS = float(os.getenv("VIBE_SIM_SIDECAR_DELAY", "2.0"))


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


# ── Hardware gating ────────────────────────────────────────────────

@dataclass
class SimulationBudget:
    """Hardware-aware budget for a simulation run."""
    enabled: bool = True
    max_rounds: int = _MAX_PERSONA_ROUNDS
    max_tokens: int = _SIM_MAX_TOKENS
    reason: str = ""  # Why disabled (for logging)


def assess_simulation_budget(
    system_profile: Optional[Any] = None,
    mode: str = "sidecar",
) -> SimulationBudget:
    """Determine whether simulation can run and with what constraints.

    Two modes with very different VRAM requirements:

    **sidecar** — runs concurrently with specialists.  Competes for KV
    cache, so it needs substantial free VRAM (>=6GB) to avoid slowing
    down the specialists.  On 22GB cards with a 9B model this will
    typically be disabled, which is correct — the sidecar would hurt
    more than help at that VRAM level.

    **clarification** — runs when the specialist is paused waiting for
    human input.  The GPU is idle, so there is zero KV contention.
    This mode is always enabled (unless the master kill-switch is off)
    because even on 22GB the LLM has full VRAM to itself.

    Checks (in order):
        1. Master kill-switch (VIBE_SIM_ENABLED)
        2. Mode-specific VRAM gating
        3. Available headroom → rounds/token scaling
    """
    if not _SIM_ENABLED:
        return SimulationBudget(
            enabled=False, reason="Disabled via VIBE_SIM_ENABLED=false"
        )

    # ── Clarification mode: no KV contention, always run ──
    if mode == "clarification":
        # No VRAM gating — the specialist is paused, GPU is idle.
        # Scale rounds based on whether we have a GPU at all (affects
        # throughput but not correctness).
        free_vram_mb = _probe_free_vram(system_profile, mode="clarification")
        if free_vram_mb is None:
            # CPU-only: fewer rounds for throughput reasons
            return SimulationBudget(
                enabled=True,
                max_rounds=min(2, _MAX_PERSONA_ROUNDS),
                max_tokens=min(400, _SIM_MAX_TOKENS),
                reason="Clarification mode, CPU-only (no contention)",
            )
        return SimulationBudget(
            enabled=True,
            max_rounds=_MAX_PERSONA_ROUNDS,
            max_tokens=_SIM_MAX_TOKENS,
            reason=f"Clarification mode, GPU idle ({free_vram_mb}MB free)",
        )

    # ── Sidecar mode: competing with specialists for KV cache ──
    free_vram_mb = _probe_free_vram(system_profile, mode="sidecar")

    if free_vram_mb is not None:
        if free_vram_mb < _MIN_FREE_VRAM_MB_SIDECAR:
            return SimulationBudget(
                enabled=False,
                reason=(
                    f"Sidecar disabled: {free_vram_mb}MB free VRAM < "
                    f"{_MIN_FREE_VRAM_MB_SIDECAR}MB threshold "
                    f"(would compete with specialists for KV cache)"
                ),
            )

        # Scale rounds based on available headroom above the floor
        if free_vram_mb < 8192:
            # Just above threshold — minimal sidecar
            return SimulationBudget(
                enabled=True,
                max_rounds=min(2, _MAX_PERSONA_ROUNDS),
                max_tokens=min(400, _SIM_MAX_TOKENS),
                reason=f"Sidecar constrained ({free_vram_mb}MB free)",
            )
        elif free_vram_mb < 16384:
            # Comfortable headroom
            return SimulationBudget(
                enabled=True,
                max_rounds=min(3, _MAX_PERSONA_ROUNDS),
                max_tokens=min(600, _SIM_MAX_TOKENS),
                reason=f"Sidecar moderate ({free_vram_mb}MB free)",
            )
        else:
            # Plenty of headroom (multi-GPU or large card)
            return SimulationBudget(
                enabled=True,
                max_rounds=_MAX_PERSONA_ROUNDS,
                max_tokens=_SIM_MAX_TOKENS,
                reason=f"Sidecar ample ({free_vram_mb}MB free)",
            )

    # No GPU detected — CPU-only.  Sidecar still competes for inference
    # throughput, so keep it minimal.
    return SimulationBudget(
        enabled=True,
        max_rounds=min(2, _MAX_PERSONA_ROUNDS),
        max_tokens=min(400, _SIM_MAX_TOKENS),
        reason="Sidecar on CPU-only (throughput-limited)",
    )


def _probe_free_vram(
    system_profile: Optional[Any] = None,
    mode: str = "sidecar",
) -> Optional[int]:
    """Return free VRAM in MB, or None if no GPU available.

    For **sidecar** mode, tries nvidia-smi for real-time free memory
    first (since specialists are actively consuming KV cache and the
    static SystemProfile total is unreliable).  Falls back to a
    conservative 20% heuristic if nvidia-smi isn't available.

    For **clarification** mode, the GPU is idle so the static estimate
    is fine — uses 35% of total as the free estimate.
    """
    # Try real-time nvidia-smi probe first for sidecar mode
    # (clarification mode can use the cheaper static estimate)
    if mode == "sidecar":
        realtime = _nvidia_smi_free_mb()
        if realtime is not None:
            return realtime

    if system_profile is not None:
        if not getattr(system_profile, "has_gpu", False):
            return None
        total = getattr(system_profile, "total_vram_mb", 0)
        if total > 0:
            if mode == "sidecar":
                # Conservative: during active specialist work, model weights
                # + KV cache for 4 concurrent requests eat most VRAM.
                # A 9B fp16 model takes ~18GB on a 24GB card, leaving ~6GB
                # total, of which KV cache for 4 requests uses ~4GB.
                # Estimate 15% free as a worst-case-ish proxy.
                estimated_free = int(total * 0.15)
            else:
                # Clarification: GPU is idle, model weights are the only
                # consumer.  35% free is a reasonable estimate.
                estimated_free = int(total * 0.35)
            return estimated_free
        return None

    # No SystemProfile — fall back to nvidia-smi
    return _nvidia_smi_free_mb()


def _nvidia_smi_free_mb() -> Optional[int]:
    """Query nvidia-smi for real-time free GPU memory in MB.

    Returns None if nvidia-smi is unavailable or reports no free memory.
    """
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            free_mb = sum(
                int(line.strip())
                for line in result.stdout.strip().splitlines()
                if line.strip().isdigit()
            )
            return free_mb if free_mb > 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None


# ── Adapter registration ───────────────────────────────────────────

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


# ── Integration Point 1: Parallel Sidecar Simulation ──────────────

@dataclass
class SimulationReport:
    """Result of an integration simulation run."""
    report: str = ""
    conflicts: List[Dict[str, str]] = field(default_factory=list)
    risk_level: str = "unknown"  # "low", "medium", "high"
    rounds_completed: int = 0
    elapsed_seconds: float = 0.0
    skipped: bool = False
    skip_reason: str = ""


def run_integration_simulation(
    specification: str,
    sub_tasks: List[Dict[str, Any]],
    adapter_registry: AdapterRegistry,
    system_profile: Optional[Any] = None,
    delay_seconds: float = _SIDECAR_DELAY_SECONDS,
) -> SimulationReport:
    """Run a MiroFish-style integration simulation as a parallel sidecar.

    Called from a thread alongside specialist sub-task execution.
    Sequential persona rounds ensure only one LLM call is in-flight
    from the simulation at any time, minimising KV cache pressure.

    Args:
        specification: The task specification.
        sub_tasks: List of sub-task dicts (from router decomposition).
        adapter_registry: Shared adapter registry with sim adapters.
        system_profile: Optional SystemProfile for hardware gating.
        delay_seconds: Initial delay to let specialists claim KV slots.

    Returns:
        SimulationReport with conflicts, risk assessment, and the
        full synthesis report text.
    """
    budget = assess_simulation_budget(system_profile, mode="sidecar")

    if not budget.enabled:
        logger.info(f"[Simulation] Skipped: {budget.reason}")
        return SimulationReport(
            skipped=True, skip_reason=budget.reason
        )

    # Let specialists get a head start on KV allocation
    if delay_seconds > 0:
        time.sleep(delay_seconds)

    start = time.monotonic()

    # Build context: summarise sub-tasks for persona prompts
    task_summary = _build_task_summary(specification, sub_tasks)

    # Sequential persona rounds (one LLM call at a time)
    persona_adapters = [
        ("sim_maintainer", "Maintainer"),
        ("sim_consumer", "Consumer"),
        ("sim_qa", "QA Engineer"),
    ]

    perspectives: List[str] = []
    rounds_done = 0

    for adapter_name, persona_label in persona_adapters[:budget.max_rounds]:
        try:
            adapter = adapter_registry.get(adapter_name)
            if adapter is None:
                continue

            prompt = f"""Review this multi-specialist integration plan:

{task_summary}

Identify specific integration risks from your perspective as a {persona_label}."""

            response = adapter.generate(
                prompt, max_tokens=budget.max_tokens
            )
            if response and response.strip():
                perspectives.append(
                    f"### {persona_label} Perspective\n{response.strip()}"
                )
                rounds_done += 1
        except Exception as e:
            logger.warning(f"[Simulation] {persona_label} round failed: {e}")

    if not perspectives:
        elapsed = time.monotonic() - start
        return SimulationReport(
            skipped=True,
            skip_reason="All persona rounds failed",
            elapsed_seconds=elapsed,
        )

    # Synthesis: combine perspectives into a conflict report
    try:
        synthesis_adapter = adapter_registry.get("sim_synthesis")
        if synthesis_adapter is None:
            # Fallback: concatenate perspectives
            report_text = "\n\n".join(perspectives)
        else:
            synthesis_prompt = f"""Synthesize these reviewer perspectives into an integration risk report:

{chr(10).join(perspectives)}

Specification context:
{specification[:1000]}"""

            report_text = synthesis_adapter.generate(
                synthesis_prompt, max_tokens=budget.max_tokens
            )
            if not report_text or not report_text.strip():
                report_text = "\n\n".join(perspectives)
            else:
                report_text = report_text.strip()
    except Exception as e:
        logger.warning(f"[Simulation] Synthesis failed: {e}")
        report_text = "\n\n".join(perspectives)

    elapsed = time.monotonic() - start

    # Parse risk level from report
    conflicts, risk_level = _parse_simulation_report(report_text)

    logger.info(
        f"[Simulation] Completed {rounds_done} rounds in {elapsed:.1f}s "
        f"({len(conflicts)} conflicts, risk={risk_level})"
    )

    return SimulationReport(
        report=report_text,
        conflicts=conflicts,
        risk_level=risk_level,
        rounds_completed=rounds_done,
        elapsed_seconds=elapsed,
    )


# ── Integration Point 2: Clarification Simulation ─────────────────

@dataclass
class ClarificationResult:
    """Result of a clarification simulation."""
    resolved: bool = False
    answers: Dict[str, str] = field(default_factory=dict)  # question -> answer
    unresolved: List[str] = field(default_factory=list)
    confidence: float = 0.0
    elapsed_seconds: float = 0.0
    skipped: bool = False
    skip_reason: str = ""


def simulate_clarification(
    questions: List[str],
    specification: str,
    user_request: str,
    adapter_registry: AdapterRegistry,
    system_profile: Optional[Any] = None,
) -> ClarificationResult:
    """Simulate stakeholder answers to clarification questions.

    Called when a specialist emits <clarification_needed>.  Runs 2-3
    stakeholder personas sequentially, then a mediator to synthesize
    consensus.  Only replaces the human round-trip if the mediator
    reports overall confidence above the threshold.

    Args:
        questions: List of clarification question strings.
        specification: Current task specification.
        user_request: Original user request.
        adapter_registry: Shared adapter registry with sim adapters.
        system_profile: Optional SystemProfile for hardware gating.

    Returns:
        ClarificationResult with resolved answers or unresolved questions.
    """
    budget = assess_simulation_budget(system_profile, mode="clarification")

    if not budget.enabled:
        logger.info(f"[ClarificationSim] Skipped: {budget.reason}")
        return ClarificationResult(
            skipped=True, skip_reason=budget.reason
        )

    if not questions:
        return ClarificationResult(skipped=True, skip_reason="No questions")

    start = time.monotonic()

    questions_text = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))

    context = f"""Original request: {user_request}

Specification: {specification[:1500]}

Questions requiring answers:
{questions_text}"""

    # Collect stakeholder perspectives (sequential to minimize KV pressure)
    stakeholder_roles = ["product_owner", "end_user", "domain_expert"]
    stakeholder_responses: List[str] = []

    for role in stakeholder_roles[:budget.max_rounds]:
        adapter_name = f"sim_stakeholder_{role}"
        try:
            adapter = adapter_registry.get(adapter_name)
            if adapter is None:
                continue

            prompt = f"""{context}

Answer each question from your perspective as a {role.replace('_', ' ')}."""

            response = adapter.generate(prompt, max_tokens=budget.max_tokens)
            if response and response.strip():
                stakeholder_responses.append(
                    f"### {role.replace('_', ' ').title()}\n{response.strip()}"
                )
        except Exception as e:
            logger.warning(f"[ClarificationSim] {role} failed: {e}")

    if not stakeholder_responses:
        elapsed = time.monotonic() - start
        return ClarificationResult(
            skipped=True,
            skip_reason="All stakeholder rounds failed",
            elapsed_seconds=elapsed,
        )

    # Mediator synthesis
    try:
        mediator = adapter_registry.get("sim_mediator")
        if mediator is None:
            elapsed = time.monotonic() - start
            return ClarificationResult(
                skipped=True,
                skip_reason="Mediator adapter unavailable",
                elapsed_seconds=elapsed,
            )

        mediator_prompt = f"""Questions:
{questions_text}

Stakeholder perspectives:
{chr(10).join(stakeholder_responses)}

Synthesize consensus answers. Mark unresolvable questions as UNRESOLVED."""

        mediator_response = mediator.generate(
            mediator_prompt, max_tokens=budget.max_tokens
        )
    except Exception as e:
        logger.warning(f"[ClarificationSim] Mediator failed: {e}")
        elapsed = time.monotonic() - start
        return ClarificationResult(
            skipped=True,
            skip_reason=f"Mediator failed: {e}",
            elapsed_seconds=elapsed,
        )

    elapsed = time.monotonic() - start

    # Parse mediator output
    answers, unresolved, confidence = _parse_mediator_response(
        mediator_response or "", questions
    )

    resolved = confidence >= _CLARIFICATION_CONFIDENCE_THRESHOLD and len(unresolved) == 0

    logger.info(
        f"[ClarificationSim] {len(answers)} answered, {len(unresolved)} unresolved, "
        f"confidence={confidence:.2f}, resolved={resolved} ({elapsed:.1f}s)"
    )

    return ClarificationResult(
        resolved=resolved,
        answers=answers,
        unresolved=unresolved,
        confidence=confidence,
        elapsed_seconds=elapsed,
    )


# ── Helpers ────────────────────────────────────────────────────────

def _build_task_summary(
    specification: str,
    sub_tasks: List[Dict[str, Any]],
) -> str:
    """Build a compact summary of the specification and sub-tasks."""
    parts = [f"## Specification\n{specification[:1500]}"]

    if sub_tasks:
        parts.append("\n## Sub-Tasks")
        for i, st in enumerate(sub_tasks):
            task_type = st.get("task_type", "unknown")
            spec = st.get("specification", "")[:300]
            adapter = st.get("specialist_adapter", "unknown")
            parts.append(
                f"\n### Sub-Task {i+1}: {task_type} (specialist: {adapter})\n{spec}"
            )

    return "\n".join(parts)


def _parse_simulation_report(
    report: str,
) -> Tuple[List[Dict[str, str]], str]:
    """Extract structured conflicts and overall risk level from report text."""
    import re

    conflicts: List[Dict[str, str]] = []
    risk_level = "low"

    # Match lines like: - [RISK_LEVEL: HIGH] description
    risk_pattern = re.compile(
        r"-\s*\[(?:RISK_LEVEL:\s*)?(HIGH|MEDIUM|LOW)\]\s*(.+)",
        re.IGNORECASE,
    )

    for match in risk_pattern.finditer(report):
        level = match.group(1).lower()
        description = match.group(2).strip()
        conflicts.append({"level": level, "description": description})

    # Determine overall risk: highest individual risk
    if any(c["level"] == "high" for c in conflicts):
        risk_level = "high"
    elif any(c["level"] == "medium" for c in conflicts):
        risk_level = "medium"
    elif conflicts:
        risk_level = "low"
    else:
        # No structured risks found — check for keywords
        lower = report.lower()
        if "critical" in lower or "breaking" in lower:
            risk_level = "high"
        elif "warning" in lower or "inconsisten" in lower:
            risk_level = "medium"
        else:
            risk_level = "low"

    return conflicts, risk_level


def _parse_mediator_response(
    response: str,
    original_questions: List[str],
) -> Tuple[Dict[str, str], List[str], float]:
    """Parse mediator output into answers, unresolved questions, and confidence.

    Returns:
        (answers_dict, unresolved_list, confidence_float)
    """
    import re

    answers: Dict[str, str] = {}
    unresolved: List[str] = []

    # Try to extract confidence score
    confidence = 0.0
    conf_match = re.search(
        r"[Oo]verall\s+confidence:\s*([\d.]+)", response
    )
    if conf_match:
        try:
            confidence = float(conf_match.group(1))
            confidence = max(0.0, min(1.0, confidence))
        except ValueError:
            confidence = 0.0

    # Parse resolved answers: look for Q:/A: pairs
    qa_pattern = re.compile(
        r"Q:\s*(.+?)\s*\n\s*A:\s*(.+?)(?=\n\s*(?:Q:|Confidence:|##|\Z))",
        re.DOTALL,
    )

    for match in qa_pattern.finditer(response):
        question = match.group(1).strip()
        answer = match.group(2).strip()

        # Match back to original questions (fuzzy: first 30 chars)
        matched_q = _match_question(question, original_questions)
        if matched_q and "unresolved" not in answer.lower() and "uncertain" not in answer.lower():
            answers[matched_q] = answer
        elif matched_q:
            unresolved.append(matched_q)

    # Any original questions not accounted for are unresolved
    for q in original_questions:
        if q not in answers and q not in unresolved:
            unresolved.append(q)

    # If no confidence was extracted, estimate from resolution ratio
    if confidence == 0.0 and original_questions:
        confidence = len(answers) / len(original_questions)

    return answers, unresolved, confidence


def _match_question(
    parsed_q: str,
    original_questions: List[str],
) -> Optional[str]:
    """Fuzzy-match a parsed question back to the original list."""
    parsed_lower = parsed_q.lower().strip()

    # Exact match first
    for oq in original_questions:
        if oq.lower().strip() == parsed_lower:
            return oq

    # Prefix match (first 30 chars)
    for oq in original_questions:
        if (oq.lower().strip()[:30] == parsed_lower[:30]
                and len(parsed_lower) > 5):
            return oq

    # Substring containment
    for oq in original_questions:
        if parsed_lower[:20] in oq.lower() or oq.lower()[:20] in parsed_lower:
            return oq

    return None


def format_simulation_for_aggregator(
    sim_report: SimulationReport,
) -> str:
    """Format a SimulationReport as context for the aggregator prompt.

    Returns an empty string if simulation was skipped or produced no output.
    """
    if sim_report.skipped or not sim_report.report:
        return ""

    conflict_summary = ""
    if sim_report.conflicts:
        lines = []
        for c in sim_report.conflicts:
            lines.append(f"  - [{c['level'].upper()}] {c['description']}")
        conflict_summary = "\n".join(lines)

    return f"""
## Integration Simulation Report (risk: {sim_report.risk_level})

{sim_report.report}

{f"### Flagged Conflicts{chr(10)}{conflict_summary}" if conflict_summary else ""}
""".strip()


def format_clarification_for_spec(
    clar_result: ClarificationResult,
) -> str:
    """Format resolved clarification answers for injection into the spec.

    Returns an empty string if nothing was resolved.
    """
    if not clar_result.resolved or not clar_result.answers:
        return ""

    lines = [
        "\n## Simulated Clarification Answers "
        f"(confidence: {clar_result.confidence:.0%})\n"
    ]
    for q, a in clar_result.answers.items():
        lines.append(f"**Q:** {q}")
        lines.append(f"**A:** {a}\n")

    return "\n".join(lines)


# ── Integration Point 3: Offline Skill Vetting ──────────────────

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

_SKILL_VET_ADAPTER_DEFS = [
    ("sim_vet_specialist", SKILL_VET_SPECIALIST_PROMPT, {"temperature": 0.4, "max_tokens": _SIM_MAX_TOKENS}),
    ("sim_vet_critic", SKILL_VET_CRITIC_PROMPT, {"temperature": 0.1, "max_tokens": 200}),
    ("sim_vet_task_gen", SKILL_VET_TASK_GEN_PROMPT, {"temperature": 0.7, "max_tokens": _SIM_MAX_TOKENS}),
]


@dataclass
class VettingResult:
    """Result of an offline skill vetting simulation."""
    avg_score: float = 0.0
    feedback_summary: str = ""
    per_task_scores: List[float] = field(default_factory=list)
    tasks_evaluated: int = 0
    elapsed_seconds: float = 0.0
    skipped: bool = False
    skip_reason: str = ""


def vet_skill_with_simulation(
    skill_content: str,
    task_type: str,
    adapter_registry: AdapterRegistry,
    system_profile: Optional[Any] = None,
) -> VettingResult:
    """Offline vetting of a skill via synthetic task simulation.

    Generates 3-5 synthetic task descriptions for the given task_type,
    then for each task simulates a specialist using the skill and a
    critic evaluating the output.  Returns the average score and a
    feedback summary.

    Hardware-aware: uses ``assess_simulation_budget(mode="clarification")``
    since vetting runs offline with no KV contention.

    Args:
        skill_content: The SKILL.md content to vet.
        task_type:     The task type the skill targets.
        adapter_registry: Shared adapter registry (must have sim_vet_* adapters).
        system_profile: Optional SystemProfile for hardware gating.

    Returns:
        VettingResult with average score and feedback summary.
    """
    if not _VET_SKILLS_ENABLED:
        return VettingResult(skipped=True, skip_reason="Disabled via VIBE_SIM_VET_SKILLS=false")

    budget = assess_simulation_budget(system_profile, mode="clarification")
    if not budget.enabled:
        return VettingResult(skipped=True, skip_reason=budget.reason)

    start = time.monotonic()

    # ── Step 1: Generate synthetic tasks ──
    task_gen = adapter_registry.get("sim_vet_task_gen")
    if task_gen is None:
        return VettingResult(
            skipped=True,
            skip_reason="sim_vet_task_gen adapter not registered",
        )

    try:
        gen_prompt = (
            f"Task type: {task_type.replace('_', ' ')}\n\n"
            f"Generate 3-5 realistic task descriptions for this task type."
        )
        gen_response = task_gen.generate(gen_prompt, max_tokens=budget.max_tokens)
    except Exception as e:
        logger.warning(f"[SkillVet] Task generation failed: {e}")
        return VettingResult(
            skipped=True,
            skip_reason=f"Task generation failed: {e}",
            elapsed_seconds=time.monotonic() - start,
        )

    # Parse "TASK: ..." lines
    synthetic_tasks = _parse_synthetic_tasks(gen_response or "")
    if not synthetic_tasks:
        return VettingResult(
            skipped=True,
            skip_reason="No synthetic tasks generated",
            elapsed_seconds=time.monotonic() - start,
        )

    # Cap to budget rounds
    synthetic_tasks = synthetic_tasks[:budget.max_rounds]

    # ── Step 2: For each task, simulate specialist + critic ──
    specialist_adapter = adapter_registry.get("sim_vet_specialist")
    critic_adapter = adapter_registry.get("sim_vet_critic")

    if specialist_adapter is None or critic_adapter is None:
        return VettingResult(
            skipped=True,
            skip_reason="sim_vet_specialist or sim_vet_critic adapter not registered",
            elapsed_seconds=time.monotonic() - start,
        )

    scores: List[float] = []
    feedbacks: List[str] = []

    for task_desc in synthetic_tasks:
        try:
            # Specialist generates output using the skill
            spec_prompt = (
                f"## Skill Instructions\n{skill_content[:1500]}\n\n"
                f"## Task\n{task_desc}\n\n"
                f"Follow the skill's workflow to complete this task."
            )
            specialist_output = specialist_adapter.generate(
                spec_prompt, max_tokens=budget.max_tokens
            )
            if not specialist_output or not specialist_output.strip():
                continue

            # Critic evaluates the output
            critic_prompt = (
                f"Task: {task_desc}\n\n"
                f"Specialist output:\n{specialist_output.strip()[:1000]}\n\n"
                f"Score this output 0-100 and provide feedback."
            )
            critic_response = critic_adapter.generate(
                critic_prompt, max_tokens=200
            )
            score, feedback = _parse_vet_critic_response(critic_response or "")
            if score is not None:
                scores.append(score)
                if feedback:
                    feedbacks.append(feedback)
        except Exception as e:
            logger.warning(f"[SkillVet] Evaluation round failed: {e}")

    elapsed = time.monotonic() - start

    if not scores:
        return VettingResult(
            skipped=True,
            skip_reason="All evaluation rounds failed",
            elapsed_seconds=elapsed,
        )

    avg_score = sum(scores) / len(scores)
    feedback_summary = "; ".join(feedbacks) if feedbacks else "No feedback collected"

    logger.info(
        f"[SkillVet] {task_type}: avg_score={avg_score:.1f} "
        f"({len(scores)} tasks evaluated) in {elapsed:.1f}s"
    )

    return VettingResult(
        avg_score=avg_score,
        feedback_summary=feedback_summary,
        per_task_scores=scores,
        tasks_evaluated=len(scores),
        elapsed_seconds=elapsed,
    )


def _parse_synthetic_tasks(response: str) -> List[str]:
    """Extract 'TASK: ...' lines from the task generator response."""
    tasks = []
    for line in response.splitlines():
        line = line.strip()
        if line.upper().startswith("TASK:"):
            task = line[5:].strip()
            if task:
                tasks.append(task)
    return tasks


def _parse_vet_critic_response(response: str) -> Tuple[Optional[float], str]:
    """Parse 'SCORE: N' and 'FEEDBACK: ...' from the critic response."""
    import re
    score = None
    feedback = ""

    score_match = re.search(r"SCORE:\s*(\d+)", response, re.IGNORECASE)
    if score_match:
        try:
            score = float(score_match.group(1))
            score = max(0.0, min(100.0, score))
        except ValueError:
            pass

    feedback_match = re.search(r"FEEDBACK:\s*(.+)", response, re.IGNORECASE)
    if feedback_match:
        feedback = feedback_match.group(1).strip()

    return score, feedback
