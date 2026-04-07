"""Tier 0 builder — drafts a memory note from a signal cluster."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Protocol, Union

from ..self_upgrade_trigger import UpgradeSignal

logger = logging.getLogger(__name__)


class _LLMProtocol(Protocol):
    def generate(self, prompt: str, max_tokens: int = 200) -> str: ...


class Tier0Result:
    """Tagged union of Tier0Builder outcomes."""

    @dataclass
    class LessonDrafted:
        lesson: str
        role: str
        task_type: str
        tag: str
        signal_refs: List[str]

    @dataclass
    class Empty:
        reason: str

    #: Type alias for any Tier0Result variant. Use only for type annotations;
    #: runtime checks must use isinstance against a concrete nested class.
    AnyResult = Union["Tier0Result.LessonDrafted", "Tier0Result.Empty"]


_TIER0_PROMPT_TEMPLATE = """\
You are summarising a lesson for future agent runs to avoid a recurring mistake.

Context:
- Agent role: {role}
- Task type: {task_type}
- Recent signal(s):
{signals}

Write ONE lesson in ≤3 sentences that a future agent could apply next time to
avoid this mistake. The lesson should be concrete, actionable, and specific to
the role and task type. Return ONLY the lesson text, no preamble.
"""


class Tier0Builder:
    """Drafts a Lesson from a signal cluster using the critic adapter."""

    def __init__(self, llm: _LLMProtocol) -> None:
        self._llm = llm

    def build(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str,
        author_run_id: str,
        role: str,
    ) -> "Tier0Result.AnyResult":
        if not signals:
            return Tier0Result.Empty(reason="no signals to draft from")

        # Use the first signal's task_type; all signals in a cluster share it
        task_type = signals[0].task_type

        signals_text = "\n".join(
            f"  - {s.category}: {s.detail} (score={s.score})"
            for s in signals[:5]  # Cap to avoid overlong prompts
        )

        prompt = _TIER0_PROMPT_TEMPLATE.format(
            role=role,
            task_type=task_type,
            signals=signals_text,
        )

        lesson = self._llm.generate(prompt, max_tokens=200).strip()
        if not lesson:
            return Tier0Result.Empty(reason="llm returned empty string")

        return Tier0Result.LessonDrafted(
            lesson=lesson,
            role=role,
            task_type=task_type,
            tag="",                      # M2+ may add auto-tagging
            signal_refs=[s.id for s in signals],
        )
