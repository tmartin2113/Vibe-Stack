"""Tier 3 issue report data model and markdown renderer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal


@dataclass
class EvidenceRow:
    run_id: str
    task_type: str
    score: int
    excerpt: str  # <= 500 chars


@dataclass
class IssueReport:
    report_id: str
    title: str
    signal_refs: List[str]
    evidence: List[EvidenceRow]
    hypothesis: str
    suggested_change: str
    suggested_change_kind: Literal["code", "config", "infra", "prompt", "data", "external"]
    confidence: float
    author_agent_id: str
    author_role: str
    created_at: str


def render_report(report: IssueReport) -> str:
    """Render an IssueReport as markdown with a YAML frontmatter block.

    Format:
        ---
        report_id: ...
        tier: 3
        kind: ...
        confidence: ...
        author_agent_id: ...
        author_role: ...
        created_at: ...
        signal_refs:
          - sig_a
          - sig_b
        ---

        ## Hypothesis
        ...

        ## Suggested change
        ...

        ## Evidence
        - **run <id>** (task: <type>, score: <score>)
          > <excerpt>
    """
    frontmatter_lines = [
        "---",
        f"report_id: {report.report_id}",
        "tier: 3",
        f"kind: {report.suggested_change_kind}",
        f"confidence: {report.confidence}",
        f"author_agent_id: {report.author_agent_id}",
        f"author_role: {report.author_role}",
        f"created_at: {report.created_at}",
        "signal_refs:",
    ]
    for sid in report.signal_refs:
        frontmatter_lines.append(f"  - {sid}")
    frontmatter_lines.append("---")

    body_lines = [
        "",
        "## Hypothesis",
        "",
        report.hypothesis,
        "",
        "## Suggested change",
        "",
        report.suggested_change,
        "",
        "## Evidence",
        "",
    ]
    for ev in report.evidence:
        body_lines.append(
            f"- **run {ev.run_id}** (task: {ev.task_type}, score: {ev.score})"
        )
        body_lines.append(f"  > {ev.excerpt}")

    return "\n".join(frontmatter_lines + body_lines)
