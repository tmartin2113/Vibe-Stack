"""
Complexity Triage Node

Classifies incoming requests into three complexity tiers using regex
heuristics (zero LLM calls). The tier determines which pipeline stages
are skipped:

- fast:     Skip spec building + spec critic + LLM output critic
- standard: Skip spec critic
- full:     Run everything (current behavior)
"""

import re
import logging
from .state import AgentState

logger = logging.getLogger(__name__)

# ── Fast-path signals (ALL must be true) ──

# Strong single action verbs at start of request
_ACTION_VERBS = re.compile(
    r"^\s*(write|add|fix|refactor|create|implement|generate|remove|delete|rename|"
    r"update|change|move|extract|convert|replace|make|build)\b",
    re.IGNORECASE,
)

# High-specificity keywords (function name, file path, class name, etc.)
_SPECIFICITY = re.compile(
    r"(?:"
    r"[a-z_][a-z0-9_]*\.[a-z]{1,4}\b"       # file.py, main.ts
    r"|[a-z_][a-z0-9_]*\("                    # function_name(
    r"|class\s+[A-Z]\w+"                      # class ClassName
    r"|def\s+[a-z_]\w+"                       # def func_name
    r"|`[^`]+`"                               # backtick-quoted identifiers
    r"|[A-Z][a-z]+[A-Z]\w+"                   # CamelCase identifier (case-sensitive!)
    r"|[a-z]+_[a-z]+_[a-z]+"                  # snake_case_identifier (3+ parts)
    r"|/[a-z_][a-z0-9_/]*\.[a-z]{1,4}\b"     # /path/to/file.py
    r")",
    # No IGNORECASE — CamelCase detection requires case sensitivity
)

# Ambiguity markers that prevent fast path
_AMBIGUITY = re.compile(
    r"\b(maybe|possibly|perhaps|not sure|i think|something like|"
    r"could you|would you|if possible|or maybe|might|somehow)\b",
    re.IGNORECASE,
)

# Multi-intent conjunctions
_MULTI_INTENT = re.compile(
    r"\b(and also|plus also|additionally|as well as|on top of that|"
    r"furthermore|moreover|in addition)\b",
    re.IGNORECASE,
)

# ── Full-path signals (ANY triggers) ──

_FULL_COMPLEXITY = re.compile(
    r"\b(production[- ]?ready|production|critical|comprehensive|"
    r"complete system|full system|entire|robust|well[- ]?tested|"
    r"end[- ]?to[- ]?end|scalable|enterprise|microservice)\b",
    re.IGNORECASE,
)

# Multi-specialist indicators (mirrors router.py patterns)
_MULTI_SPECIALIST = [
    re.compile(r"\b(test|tests|testing)\s+(and|with|plus|also)\s+(security|secur|document|doc|optimiz)", re.IGNORECASE),
    re.compile(r"\b(security|secur)\s+(and|with|plus|also)\s+(test|tests|testing|document|doc|optimiz)", re.IGNORECASE),
    re.compile(r"\b(document|doc)\s+(and|with|plus|also)\s+(test|tests|testing|security|secur|optimiz)", re.IGNORECASE),
    re.compile(r"\b(generate|write|create)\b.*\b(tests?|test suite)\b.*\band\b.*\b(docs?|documentation)\b", re.IGNORECASE),
    re.compile(r"\b(generate|write|create)\b.*\b(docs?|documentation)\b.*\band\b.*\b(tests?|test suite)\b", re.IGNORECASE),
]


def classify_complexity(state: AgentState) -> AgentState:
    """
    Assign a complexity tier to the request using regex heuristics.

    Writes:
        state["complexity_tier"]  = "fast" | "standard" | "full"
        state["effective_quality_threshold"] = tier-adjusted threshold

    Zero LLM calls.  If tier is already set (by orchestrator/heartbeat),
    returns immediately to avoid redundant re-triage.
    """
    # Guard: respect pre-set tier from orchestration layer
    if state.get("complexity_tier"):
        return state

    user_request = state.get("user_request", "")
    intent = state.get("intent", "code_generation")
    words = user_request.split()
    word_count = len(words)

    # Read tier thresholds from state (injected from config by graph.py)
    # Fall back to plan defaults.
    quality_threshold = state.get("quality_threshold", 85)

    # ── Check full-path triggers first (ANY match → full) ──

    if word_count > 80:
        return _set_tier(state, "full", quality_threshold)

    if _FULL_COMPLEXITY.search(user_request):
        return _set_tier(state, "full", quality_threshold)

    for pattern in _MULTI_SPECIALIST:
        if pattern.search(user_request):
            return _set_tier(state, "full", quality_threshold)

    # ── Check fast-path conditions (ALL must be true) ──

    is_fast = (
        intent == "code_generation"
        and word_count < 40
        and bool(_ACTION_VERBS.search(user_request))
        and bool(_SPECIFICITY.search(user_request))
        and not _AMBIGUITY.search(user_request)
        and not _MULTI_INTENT.search(user_request)
    )

    if is_fast:
        return _set_tier(state, "fast", 70)

    # ── Default: standard ──

    return _set_tier(state, "standard", 75)


def _set_tier(state: AgentState, tier: str, threshold: int) -> AgentState:
    """Write tier and threshold into state."""
    state["complexity_tier"] = tier
    state["effective_quality_threshold"] = threshold
    logger.info(
        f"Complexity triage: tier={tier}, threshold={threshold}, "
        f"words={len(state.get('user_request', '').split())}"
    )
    return state
