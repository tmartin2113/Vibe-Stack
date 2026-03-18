"""
Heuristic Output Critic

Evaluates specialist output using cheap regex/length heuristics (zero LLM
calls).  When the heuristic score meets the effective quality threshold the
output is approved immediately, skipping the LLM critic.  When it falls
short the pipeline falls through to the LLM critic as a safety net.
"""

import re
import logging
from .state import AgentState

logger = logging.getLogger(__name__)

# Task types that must contain code blocks
_CODE_TASK_TYPES = {
    "test_generation", "security_audit", "code_generation",
    "debugging", "refactoring", "data_processing",
    "api_development", "database_operations",
}

# Error / traceback patterns
_ERROR_PATTERNS = re.compile(
    r"(?:Traceback \(most recent call last\)"
    r"|^\s*raise\s+\w+Error"
    r"|Error:|Exception:|FAILED|FATAL"
    r"|SyntaxError:|IndentationError:|NameError:|TypeError:|ValueError:"
    r"|ImportError:|AttributeError:|KeyError:|IndexError:)",
    re.MULTILINE,
)

# Truncation markers
_TRUNCATION = re.compile(
    r"(?:\.\.\.\s*$"
    r"|\[truncated\]"
    r"|\[\.\.\.content truncated\.\.\.\]"
    r"|# \.\.\. more"
    r"|// \.\.\. rest)",
    re.MULTILINE | re.IGNORECASE,
)

# Structural signals (positive)
_STRUCTURAL = re.compile(
    r"(?:^\s*(?:def |class |async def |function )\w+"
    r"|^\s*return\b"
    r"|^\s*(?:export |module\.exports)"
    r"|^\s*(?:CREATE TABLE|ALTER TABLE|INSERT INTO|SELECT )"
    r"|^\s*(?:import |from \S+ import ))",
    re.MULTILINE,
)

# Code block fence
_CODE_BLOCK = re.compile(r"```[\s\S]*?```")

# Minimum lengths per task family
_MIN_LENGTHS = {
    "code": 50,
    "text": 100,
}


def heuristic_evaluate_output(state: AgentState) -> AgentState:
    """
    Score specialist output with cheap heuristics.

    Writes:
        state["heuristic_critic_score"]   int 0-100
        state["heuristic_critic_passed"]  bool
    """
    output = state.get("specialist_output", "")
    task_type = state.get("routed_task_type", "general")
    threshold = state.get("effective_quality_threshold", state.get("quality_threshold", 85))

    score = _compute_score(output, task_type)

    passed = score >= threshold
    state["heuristic_critic_score"] = score
    state["heuristic_critic_passed"] = passed

    logger.info(
        f"Heuristic critic: score={score}, threshold={threshold}, "
        f"passed={passed}, task_type={task_type}"
    )
    return state


def _compute_score(output: str, task_type: str) -> int:
    """Compute a heuristic quality score for the specialist output."""
    if not output or not output.strip():
        return 0

    score = 85  # Baseline — "looks fine until proven otherwise"

    # ── Length checks ──
    is_code_task = task_type in _CODE_TASK_TYPES
    min_len = _MIN_LENGTHS["code"] if is_code_task else _MIN_LENGTHS["text"]
    if len(output.strip()) < min_len:
        score -= 20

    # ── Code block presence for code tasks ──
    if is_code_task and not _CODE_BLOCK.search(output):
        score -= 30

    # ── Error / traceback patterns ──
    if _ERROR_PATTERNS.search(output):
        score -= 40

    # ── Truncation markers ──
    if _TRUNCATION.search(output):
        score -= 15

    # ── Positive structural signals ──
    if _STRUCTURAL.search(output):
        score += 5

    # Clamp
    return max(0, min(100, score))
