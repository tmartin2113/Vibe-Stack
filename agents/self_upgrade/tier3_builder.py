"""Tier 3 builder — drafts an IssueReport and self-critiques before filing."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Protocol, Union
from uuid import uuid4

from ..self_upgrade_trigger import UpgradeSignal
from .reports import EvidenceRow, IssueReport

logger = logging.getLogger(__name__)

SELF_CRITIQUE_THRESHOLD = 70


class _LLMProtocol(Protocol):
    def generate(self, prompt: str, max_tokens: int = 500) -> str: ...


class Tier3Result:
    """Tagged union of Tier3Builder outcomes."""

    @dataclass
    class ReportDrafted:
        report: IssueReport

    @dataclass
    class Dropped:
        reason: str
        signal_refs: List[str]

    #: Type alias for any Tier3Result variant. Use only for type annotations;
    #: runtime checks must use isinstance against a concrete nested class.
    AnyResult = Union["Tier3Result.ReportDrafted", "Tier3Result.Dropped"]


_DRAFT_PROMPT = """\
You are drafting a structured bug report from accumulated signal evidence.

Signals:
{signals_block}

Produce a JSON object with EXACTLY these fields:
- title: short, PR-style title (≤ 80 chars)
- hypothesis: one paragraph explaining what you think is wrong
- suggested_change: one paragraph describing what to change (prose, not code)
- suggested_change_kind: one of "code" | "config" | "infra" | "prompt" | "data" | "external"
- confidence: float 0.0-1.0

Return ONLY the JSON object, no preamble or commentary.
"""

_CRITIQUE_PROMPT = """\
Evaluate the following draft bug report on three axes:
- evidence quality (is there concrete evidence, not just hand-waving?)
- clarity (is the hypothesis clear and specific?)
- actionability (is the suggested change something a human could act on?)

Draft:
{draft}

Return ONLY a JSON object: {{"score": <0-100>, "feedback": "<one sentence>"}}.
"""


class Tier3Builder:
    """Drafts an IssueReport from a signal cluster using an LLM, then self-critiques."""

    def __init__(self, llm: _LLMProtocol) -> None:
        self._llm = llm

    def build(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str,
        author_role: str,
    ) -> "Tier3Result.AnyResult":
        if not signals:
            return Tier3Result.Dropped(reason="no signals to draft from", signal_refs=[])

        # Draft the report
        draft = self._draft(signals)
        if draft is None:
            return Tier3Result.Dropped(
                reason="failed to parse LLM draft as JSON",
                signal_refs=[s.id for s in signals],
            )

        # Self-critique
        critique_score = self._self_critique(draft)
        if critique_score < SELF_CRITIQUE_THRESHOLD:
            return Tier3Result.Dropped(
                reason=f"self-critique scored {critique_score} < {SELF_CRITIQUE_THRESHOLD}",
                signal_refs=[s.id for s in signals],
            )

        # Build the final report
        report = IssueReport(
            report_id=f"report_{uuid4().hex[:12]}",
            title=draft["title"],
            signal_refs=[s.id for s in signals],
            evidence=[
                EvidenceRow(
                    run_id=s.source_node or "unknown",
                    task_type=s.task_type,
                    score=s.score,
                    excerpt=s.detail[:500],
                )
                for s in signals[:10]
            ],
            hypothesis=draft["hypothesis"],
            suggested_change=draft["suggested_change"],
            suggested_change_kind=draft["suggested_change_kind"],
            confidence=float(draft.get("confidence", 0.5)),
            author_agent_id=author_agent_id,
            author_role=author_role,
            created_at=datetime.utcnow().isoformat() + "Z",
        )

        return Tier3Result.ReportDrafted(report=report)

    def _draft(self, signals: List[UpgradeSignal]) -> Optional[dict]:
        signals_block = "\n".join(
            f"- [{s.category}] task={s.task_type} score={s.score}: {s.detail}"
            for s in signals[:10]
        )
        prompt = _DRAFT_PROMPT.format(signals_block=signals_block)
        raw = self._llm.generate(prompt, max_tokens=500).strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Tier3Builder: LLM draft was not valid JSON: %s", raw[:200])
            return None

    def _self_critique(self, draft: dict) -> int:
        prompt = _CRITIQUE_PROMPT.format(draft=json.dumps(draft, indent=2))
        raw = self._llm.generate(prompt, max_tokens=200).strip()

        try:
            parsed = json.loads(raw)
            return int(parsed.get("score", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            return 0
