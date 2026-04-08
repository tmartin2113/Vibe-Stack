"""Tests for IssueReport + EvidenceRow + render_report."""
from agents.self_upgrade.reports import (
    EvidenceRow, IssueReport, render_report,
)


def test_render_report_produces_markdown_with_yaml_frontmatter():
    report = IssueReport(
        report_id="report_01",
        title="Critic can't score empty feedback",
        signal_refs=["sig_1", "sig_2"],
        evidence=[
            EvidenceRow(
                run_id="run_abc", task_type="code_generation",
                score=40, excerpt="Score 40/100",
            ),
        ],
        hypothesis="heuristic_critic returns 40 when feedback is empty",
        suggested_change="Return None + skip persistence when no actionable feedback",
        suggested_change_kind="code",
        confidence=0.75,
        author_agent_id="agent_1",
        author_role="backend_engineer",
        created_at="2026-04-06T00:00:00Z",
    )

    rendered = render_report(report)

    # YAML frontmatter block
    assert rendered.startswith("---\n")
    assert "report_id: report_01" in rendered
    assert "tier: 3" in rendered
    assert "kind: code" in rendered
    assert "confidence: 0.75" in rendered
    # Body sections
    assert "## Hypothesis" in rendered
    assert "## Evidence" in rendered
    assert "## Suggested change" in rendered
    assert "run_abc" in rendered


def test_render_report_includes_multiple_evidence_rows():
    report = IssueReport(
        report_id="report_02",
        title="Test",
        signal_refs=["sig_1"],
        evidence=[
            EvidenceRow(run_id="r1", task_type="t", score=40, excerpt="e1"),
            EvidenceRow(run_id="r2", task_type="t", score=45, excerpt="e2"),
            EvidenceRow(run_id="r3", task_type="t", score=50, excerpt="e3"),
        ],
        hypothesis="h",
        suggested_change="c",
        suggested_change_kind="config",
        confidence=0.6,
        author_agent_id="",
        author_role="",
        created_at="",
    )
    rendered = render_report(report)
    for rid in ("r1", "r2", "r3"):
        assert rid in rendered
    for excerpt in ("e1", "e2", "e3"):
        assert excerpt in rendered


def test_render_report_escapes_signal_refs_as_yaml_list():
    report = IssueReport(
        report_id="r",
        title="t",
        signal_refs=["sig_a", "sig_b", "sig_c"],
        evidence=[],
        hypothesis="h",
        suggested_change="c",
        suggested_change_kind="code",
        confidence=0.5,
        author_agent_id="",
        author_role="",
        created_at="",
    )
    rendered = render_report(report)
    # YAML list format expected
    assert "signal_refs:" in rendered
    assert "  - sig_a" in rendered
    assert "  - sig_b" in rendered
    assert "  - sig_c" in rendered
