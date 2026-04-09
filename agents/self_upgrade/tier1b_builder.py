"""Tier 1b builder — drafts a deterministic prompt override from a signal cluster.

Called by SelfUpgradeDispatcher when classify_signals() returns Tier.ONE_B
(same detail, same task_type, >=3 signals). Validates the cluster,
resolves the adapter, checks fixture availability, drafts an append,
runs four deterministic gates (schema + safety regex + smoke test +
append-only diff), and on pass commits the override to a new branch,
pushes, opens a PR, and files a companion Paperclip issue.

Already pre-registered in _ADDITIONAL_IMMUTABLES from M0 — this module
cannot be modified by the self-upgrade pipeline.

Mirrors agents/self_upgrade/tier1a_builder.py's tagged-union + private-method
layout.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Protocol, Tuple, Union

from ..self_upgrade_trigger import UpgradeSignal

logger = logging.getLogger(__name__)


# Maximum characters in an override append field (per spec).
APPEND_MAX_LEN = 500

# Minimum canonical fixtures required for an adapter before Tier 1b
# will attempt to build an override for any task_type mapping to it.
MIN_FIXTURES_PER_ADAPTER = 3

# Maximum allowed absolute score drop per canonical fixture in the smoke
# test (5 points). Any fixture dropping more than this → GateFailed.
SMOKE_MAX_DROP_PCT = 5

# Conservative regex blocklist for prompt-injection patterns in the
# proposed append text. False positives are fine (cluster falls through
# to Tier 3). False negatives are dangerous — the human PR review is
# the catch-all. New patterns grow this list; do not weaken or remove
# existing ones without an explicit invariant-test update.
SAFETY_CLAUSE_BLOCKLIST: Tuple[re.Pattern, ...] = (
    re.compile(r"\bignore\s+(?:previous|prior|all|the\s+above)", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:previous|prior|the\s+system)", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\breveal\s+(?:your\s+)?(?:system\s+)?prompt", re.IGNORECASE),
    re.compile(r"\boverride\s+(?:safety|security)\b", re.IGNORECASE),
    re.compile(r"\bjailbreak\b", re.IGNORECASE),
    re.compile(r"<\s*/?\s*system\s*>", re.IGNORECASE),
)


class SmokeScorer(Protocol):
    """Protocol for scoring a candidate override against canonical fixtures."""

    def score_fixture(self, fixture_id: str, augmented_prompt: str) -> int:
        """Score a fixture using the augmented prompt as system prompt.

        Returns the critic's overall score (0-100).
        """
        ...


@dataclass
class GitRunResult:
    returncode: int
    stdout: str
    stderr: str


class GitRunner(Protocol):
    """Protocol for running git commands from the builder.

    Abstracted so tests can pass a fake. Production implementation
    shells out via subprocess.
    """

    def run(
        self,
        args: List[str],
        *,
        cwd: Optional[Path] = None,
        check: bool = True,
    ) -> GitRunResult:
        ...


class Tier1bResult:
    """Tagged union of Tier1bBuilder.build() outcomes."""

    @dataclass
    class OverrideCommitted:
        override_id: str
        task_type: str
        branch: str
        commit: str
        pr_url: str
        issue_id: str
        signal_refs: List[str]

    @dataclass
    class LowConfidence:
        reason: str
        signal_refs: List[str]

    @dataclass
    class GateFailed:
        gate: str
        detail: str
        signal_refs: List[str]

    AnyResult = Union[
        "Tier1bResult.OverrideCommitted",
        "Tier1bResult.LowConfidence",
        "Tier1bResult.GateFailed",
    ]


def _matches_safety_blocklist(text: str) -> Optional[str]:
    """Return the name of the first matched blocklist pattern, or None."""
    for pattern in SAFETY_CLAUSE_BLOCKLIST:
        if pattern.search(text):
            return pattern.pattern
    return None


class Tier1bBuilder:
    """Drafts a deterministic prompt override from a same-detail signal cluster."""

    def __init__(
        self,
        *,
        task_type_registry: Any,
        smoke_scorer: SmokeScorer,
        git_runner: Any,
        paperclip_client: Any,
        fixtures_root: Path,
        overrides_root: Path = Path("agents/prompt_library/overrides"),
        human_triage_user_id: str = "",
        allow_publish: bool = True,
    ) -> None:
        self._task_type_registry = task_type_registry
        self._smoke_scorer = smoke_scorer
        self._git = git_runner
        self._paperclip = paperclip_client
        self._fixtures_root = Path(fixtures_root)
        self._overrides_root = Path(overrides_root)
        self._human_triage_user_id = human_triage_user_id
        self._allow_publish = allow_publish

    def build(
        self,
        signals: List[UpgradeSignal],
        *,
        author_agent_id: str,
        author_run_id: str,
    ) -> "Tier1bResult.AnyResult":
        """Classify and build a prompt override for the signal cluster.

        Returns:
            OverrideCommitted on full success (gates + publish).
            LowConfidence for pre-conditions that shouldn't fire an issue
                about the gates themselves (missing fixtures, unknown
                task_type, cluster mismatch).
            GateFailed for hard gate rejections (schema, safety, smoke,
                diff, publish failure).
        """
        sig_refs = [s.id for s in signals]
        # Stub until later tasks wire gates.
        return Tier1bResult.LowConfidence(
            reason="stub (gates not yet wired)",
            signal_refs=sig_refs,
        )
