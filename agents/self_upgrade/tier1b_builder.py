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
    re.compile(r"\bdisregard\s+(?:previous|prior|the\b)", re.IGNORECASE),
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

        # Gate 1: cluster validation
        cluster_error = self._validate_cluster(signals)
        if cluster_error is not None:
            return Tier1bResult.LowConfidence(
                reason=cluster_error, signal_refs=sig_refs,
            )

        # All signals share task_type at this point
        task_type = signals[0].task_type

        # Gate 2: adapter resolution
        adapter = self._resolve_adapter(task_type)
        if adapter is None:
            return Tier1bResult.LowConfidence(
                reason=f"unknown task_type: {task_type}",
                signal_refs=sig_refs,
            )

        # Gate 3: fixture availability (per-adapter ramp-up gate)
        if not self._check_fixture_availability(adapter):
            return Tier1bResult.LowConfidence(
                reason=f"no fixtures yet for adapter: {adapter}",
                signal_refs=sig_refs,
            )

        # Gate 4: draft the override (deterministic) and validate
        detail = signals[0].detail
        append_text = self._draft_append(task_type, detail)

        override_id = self._new_override_id()
        from datetime import datetime, timezone
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Gate 5: schema
        schema_err = self._validate_schema_for_draft(
            override_id=override_id,
            task_type=task_type,
            append=append_text,
            signal_refs=sig_refs,
            author_agent_id=author_agent_id,
            author_run_id=author_run_id,
            created_at=created_at,
        )
        if schema_err is not None:
            return Tier1bResult.GateFailed(
                gate="schema",
                detail=schema_err,
                signal_refs=sig_refs,
            )

        # Gate 6: safety-clause regex blocklist
        matched = _matches_safety_blocklist(append_text)
        if matched is not None:
            return Tier1bResult.GateFailed(
                gate="safety_regex",
                detail=f"matched blocklist pattern: {matched}",
                signal_refs=sig_refs,
            )

        # Gate 7: canonical smoke test
        smoke_err = self._smoke_test(adapter=adapter, append_text=append_text)
        if smoke_err is not None:
            return Tier1bResult.GateFailed(
                gate="smoke_test",
                detail=smoke_err,
                signal_refs=sig_refs,
            )

        # Publish path (only when allow_publish=True)
        if not self._allow_publish:
            return Tier1bResult.LowConfidence(
                reason=f"gates passed; publish disabled by allow_publish=False",
                signal_refs=sig_refs,
            )

        # Compute pre-merge baseline snapshot (rolling-avg floor) —
        # for now, use the arithmetic mean of current baseline.json scores
        # for this adapter. Task 17 will refine.
        baseline_snapshot = self._compute_baseline_snapshot(adapter)

        try:
            branch = self._publish_branch_create(override_id)
        except Exception as exc:
            return Tier1bResult.GateFailed(
                gate="publish",
                detail=f"branch creation failed: {exc}",
                signal_refs=sig_refs,
            )

        try:
            self._publish_write_files(
                task_type=task_type,
                override_id=override_id,
                append=append_text,
                signal_refs=sig_refs,
                author_agent_id=author_agent_id,
                author_run_id=author_run_id,
                created_at=created_at,
                baseline_snapshot=baseline_snapshot,
            )
        except Exception as exc:
            return Tier1bResult.GateFailed(
                gate="publish",
                detail=f"file write failed: {exc}",
                signal_refs=sig_refs,
            )

        # Gate 8: append-only diff check
        diff_err = self._publish_diff_check()
        if diff_err is not None:
            return Tier1bResult.GateFailed(
                gate="diff_check",
                detail=diff_err,
                signal_refs=sig_refs,
            )

        try:
            commit_sha = self._publish_commit(override_id, task_type)
        except Exception as exc:
            return Tier1bResult.GateFailed(
                gate="publish",
                detail=f"commit failed: {exc}",
                signal_refs=sig_refs,
            )

        # Push
        push_err = self._publish_push(branch)
        if push_err is not None:
            return Tier1bResult.GateFailed(
                gate="publish",
                detail=push_err,
                signal_refs=sig_refs,
            )

        # PR create
        gate_outputs = (
            "- schema: ok\n"
            "- safety_regex: ok\n"
            "- smoke_test: ok\n"
            "- diff_check: ok\n"
        )
        pr_url, pr_err = self._publish_pr(
            branch=branch,
            override_id=override_id,
            task_type=task_type,
            append=append_text,
            signal_refs=sig_refs,
            gate_outputs=gate_outputs,
        )
        if pr_err is not None:
            return Tier1bResult.GateFailed(
                gate="publish",
                detail=f"{pr_err} (branch={branch})",
                signal_refs=sig_refs,
            )

        # Companion issue (failure is non-fatal)
        issue_id = self._file_companion_issue(
            override_id=override_id,
            task_type=task_type,
            adapter=adapter,
            branch=branch,
            commit=commit_sha,
            pr_url=pr_url or "",
            append=append_text,
            signal_refs=sig_refs,
        )

        return Tier1bResult.OverrideCommitted(
            override_id=override_id,
            task_type=task_type,
            branch=branch,
            commit=commit_sha,
            pr_url=pr_url or "",
            issue_id=issue_id,
            signal_refs=sig_refs,
        )

    def _validate_cluster(self, signals: List[UpgradeSignal]) -> Optional[str]:
        """Defensive re-check of the dispatcher's Tier 1b classification.

        Returns an error string on failure, None on success.
        """
        if not signals:
            return "empty cluster"
        task_types = {s.task_type for s in signals}
        if len(task_types) != 1:
            return f"cluster task_type mismatch: {sorted(task_types)}"
        details = {s.detail for s in signals}
        if len(details) != 1:
            return f"cluster detail mismatch ({len(details)} distinct details)"
        return None

    def _resolve_adapter(self, task_type: str) -> Optional[str]:
        """Map task_type → adapter name via the registry.

        Returns None if the registry has no mapping for the task_type.
        """
        try:
            mapping = self._task_type_registry.adapter_mapping()
        except Exception as exc:
            logger.warning("tier1b: adapter_mapping failed: %s", exc)
            return None
        return mapping.get(task_type)

    def _check_fixture_availability(self, adapter: str) -> bool:
        """Return True if fixtures_root/adapter has >= MIN_FIXTURES_PER_ADAPTER."""
        adapter_dir = self._fixtures_root / adapter
        if not adapter_dir.exists() or not adapter_dir.is_dir():
            return False
        count = 0
        for f in adapter_dir.iterdir():
            if f.is_file() and f.suffix == ".json" and f.name != "baseline.json":
                count += 1
        return count >= MIN_FIXTURES_PER_ADAPTER

    _TASK_ANCHOR_PREFIX = "When handling {task_type} tasks"

    def _draft_append(self, task_type: str, detail: str) -> str:
        """Produce a deterministic override append from cluster detail.

        Format: "When handling {task_type} tasks: {detail}."

        The detail is trimmed; a trailing period is added if absent.
        The result is hard-capped at APPEND_MAX_LEN characters (truncated
        at a word boundary where possible).
        """
        detail_clean = (detail or "").strip().rstrip(".")
        if not detail_clean:
            return ""
        anchor = self._TASK_ANCHOR_PREFIX.format(task_type=task_type)
        draft = f"{anchor}: {detail_clean}."
        if len(draft) <= APPEND_MAX_LEN:
            return draft
        # Truncate at a word boundary, leaving room for trailing ellipsis + period
        cutoff = APPEND_MAX_LEN - 4
        truncated = draft[:cutoff]
        last_space = truncated.rfind(" ")
        if last_space > 0:
            truncated = truncated[:last_space]
        return truncated + "...."

    def _validate_schema_for_draft(
        self,
        *,
        override_id: str,
        task_type: str,
        append: str,
        signal_refs: List[str],
        author_agent_id: str,
        author_run_id: str,
        created_at: str,
    ) -> Optional[str]:
        """Run validate_override_dict on an in-memory candidate.

        Returns None on success or the violation detail on failure.
        """
        candidate = {
            "id": override_id,
            "task_type": task_type,
            "append": append,
            "signal_refs": signal_refs,
            "author_agent_id": author_agent_id,
            "author_run_id": author_run_id,
            "created_at": created_at,
        }
        try:
            from agents.prompt_library import validate_override_dict
            validate_override_dict(candidate, filename=f"{override_id}.yaml")
        except Exception as exc:
            return str(exc)
        return None

    def _new_override_id(self) -> str:
        """Return a unique override id suitable for the schema regex."""
        import secrets
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        body = "".join(alphabet[secrets.randbelow(32)] for _ in range(26))
        return f"ovr_{body}"

    def _smoke_test(
        self,
        *,
        adapter: str,
        append_text: str,
    ) -> Optional[str]:
        """Run the smoke test against fixtures.

        Returns None on pass, or an error string on failure.
        """
        import json as _json
        adapter_dir = self._fixtures_root / adapter
        baseline_path = adapter_dir / "baseline.json"
        if not baseline_path.exists():
            return f"baseline.json missing for adapter {adapter}"
        try:
            baseline = _json.loads(baseline_path.read_text())
        except (OSError, _json.JSONDecodeError) as exc:
            return f"baseline.json unreadable: {exc}"

        # The augmented prompt is the append text; the scorer is
        # responsible for composing the actual system prompt.
        augmented = append_text

        for fixture_path in sorted(adapter_dir.glob("*.json")):
            if fixture_path.name == "baseline.json":
                continue
            fixture_id = fixture_path.stem
            if fixture_id not in baseline:
                # Fixture exists but baseline doesn't know it — skip silently
                continue
            baseline_score = float(baseline[fixture_id])
            try:
                new_score = float(self._smoke_scorer.score_fixture(
                    fixture_id=fixture_id,
                    augmented_prompt=augmented,
                ))
            except Exception as exc:
                return f"scorer raised on {fixture_id}: {exc}"
            drop = baseline_score - new_score
            if drop > SMOKE_MAX_DROP_PCT:
                return (
                    f"fixture {fixture_id} dropped from "
                    f"{baseline_score:.1f} to {new_score:.1f} "
                    f"(-{drop:.1f}, exceeds {SMOKE_MAX_DROP_PCT} tolerance)"
                )
        return None

    _BRANCH_PREFIX = "vibe/self-upgrade/tier1b"

    def _publish_branch_create(self, override_id: str) -> str:
        """Create and check out a new branch for the override. Returns branch name."""
        branch = f"{self._BRANCH_PREFIX}-{override_id}"
        self._git.run(["checkout", "-b", branch], check=True)
        return branch

    def _publish_write_files(
        self,
        *,
        task_type: str,
        override_id: str,
        append: str,
        signal_refs: List[str],
        author_agent_id: str,
        author_run_id: str,
        created_at: str,
        baseline_snapshot: float,
    ) -> tuple[Path, Path]:
        """Write the override YAML and its .baseline sidecar.

        Returns (override_path, baseline_path).
        """
        target_dir = self._overrides_root / task_type
        target_dir.mkdir(parents=True, exist_ok=True)
        override_path = target_dir / f"{override_id}.yaml"
        baseline_path = target_dir / f"{override_id}.baseline"

        # Render YAML by hand to keep the multiline append readable
        yaml_text = (
            f"id: {override_id}\n"
            f"task_type: {task_type}\n"
            f"append: |\n"
            f"  {append}\n"
            f"signal_refs:\n"
            + "".join(f"  - {ref}\n" for ref in signal_refs)
            + f"author_agent_id: {author_agent_id}\n"
            f"author_run_id: {author_run_id}\n"
            f"created_at: {created_at}\n"
        )
        override_path.write_text(yaml_text)

        baseline_path.write_text(
            f"{created_at} {baseline_snapshot:.1f}\n"
        )
        return override_path, baseline_path

    def _publish_diff_check(self) -> Optional[str]:
        """Run git diff --name-status and reject any modifications under overrides/.

        Returns None on pass, or a violation string on failure.
        """
        result = self._git.run(["diff", "--name-status", "HEAD"], check=False)
        if result.returncode != 0:
            return f"git diff failed: {result.stderr}"
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            status, path = parts
            if path.startswith("agents/prompt_library/overrides/"):
                if status != "A":
                    return (
                        f"append-only violation: {status} on {path} "
                        f"(only 'A' allowed under overrides/)"
                    )
        return None

    def _publish_commit(self, override_id: str, task_type: str) -> str:
        """Stage the new files and create a commit. Returns commit SHA."""
        # Stage specific paths only — do not 'git add .'
        self._git.run([
            "add",
            f"agents/prompt_library/overrides/{task_type}/{override_id}.yaml",
            f"agents/prompt_library/overrides/{task_type}/{override_id}.baseline",
        ], check=True)
        self._git.run([
            "commit",
            "-m",
            f"feat(prompt_overrides): tier1b {override_id} for {task_type}",
        ], check=True)
        # Capture the commit SHA
        result = self._git.run(["rev-parse", "HEAD"], check=True)
        return result.stdout.strip() or "unknown"

    def _compute_baseline_snapshot(self, adapter: str) -> float:
        """Compute the pre-merge baseline floor for the adapter.

        For M0 of Tier 1b, this is the arithmetic mean of the adapter's
        current baseline.json scores. Returns 0.0 if the baseline is
        missing or empty (the baseline sidecar is still written, but
        the regression monitor will skip comparison for missing values).
        """
        import json as _json
        baseline_path = self._fixtures_root / adapter / "baseline.json"
        if not baseline_path.exists():
            return 0.0
        try:
            scores = _json.loads(baseline_path.read_text())
        except (OSError, _json.JSONDecodeError):
            return 0.0
        if not scores:
            return 0.0
        values = [float(v) for v in scores.values()]
        return sum(values) / len(values)

    def _publish_push(self, branch: str) -> Optional[str]:
        """Push the branch to origin. Returns None on success, error string on failure."""
        result = self._git.run(["push", "-u", "origin", branch], check=False)
        if result.returncode != 0:
            return f"push failed: {result.stderr[:200]}"
        return None

    def _publish_pr(
        self,
        *,
        branch: str,
        override_id: str,
        task_type: str,
        append: str,
        signal_refs: List[str],
        gate_outputs: str,
    ) -> tuple[Optional[str], Optional[str]]:
        """Open a PR via gh. Returns (pr_url, error) tuple."""
        title = f"feat(prompt_overrides): tier1b {override_id} for {task_type}"
        body = self._render_pr_body(
            override_id=override_id,
            task_type=task_type,
            append=append,
            signal_refs=signal_refs,
            gate_outputs=gate_outputs,
            branch=branch,
        )
        result = self._git.run(
            ["gh", "pr", "create", "--title", title, "--body", body],
            check=False,
        )
        if result.returncode != 0:
            return None, f"PR create failed: {result.stderr[:200]}"
        pr_url = result.stdout.strip().splitlines()[-1].strip() if result.stdout else ""
        return pr_url, None

    def _render_pr_body(
        self,
        *,
        override_id: str,
        task_type: str,
        append: str,
        signal_refs: List[str],
        gate_outputs: str,
        branch: str,
    ) -> str:
        refs = "\n".join(f"- {r}" for r in signal_refs)
        return (
            f"## Tier 1b prompt override\n\n"
            f"- **Override id:** `{override_id}`\n"
            f"- **Task type:** `{task_type}`\n"
            f"- **Branch:** `{branch}`\n\n"
            f"### Append preview\n\n"
            f"```\n{append[:200]}\n```\n\n"
            f"### Gate outputs\n\n"
            f"{gate_outputs}\n\n"
            f"### Signal refs\n\n"
            f"{refs}\n"
        )

    def _file_companion_issue(
        self,
        *,
        override_id: str,
        task_type: str,
        adapter: str,
        branch: str,
        commit: str,
        pr_url: str,
        append: str,
        signal_refs: List[str],
    ) -> str:
        """File a Paperclip issue for human triage. Returns issue_id on success, empty string on failure."""
        try:
            description = (
                f"```yaml\n"
                f"override_id: {override_id}\n"
                f"task_type: {task_type}\n"
                f"adapter: {adapter}\n"
                f"branch: {branch}\n"
                f"commit: {commit}\n"
                f"pr_url: {pr_url}\n"
                f"signal_refs:\n"
                + "".join(f"  - {r}\n" for r in signal_refs)
                + f"gate_outputs:\n"
                f"  schema: ok\n"
                f"  safety_regex: ok\n"
                f"  smoke_test: ok\n"
                f"  diff_check: ok\n"
                f"append_preview: {append[:200]!r}\n"
                f"```\n\n"
                f"## What changed\n\n"
                f"Added prompt override `{override_id}` scoped to "
                f"task_type `{task_type}`.\n"
            )
            issue = self._paperclip.create_issue(
                title=f"[self-upgrade] tier 1b prompt override for {task_type}",
                description=description,
                labels=[
                    "self-upgrade",
                    "auto-generated",
                    "tier-1b",
                    f"task:{task_type}",
                ],
                assignee_user_id=self._human_triage_user_id or None,
            )
            return issue.id
        except Exception as exc:
            logger.warning(
                "tier1b: companion issue filing failed (orphaned PR %s): %s",
                pr_url, exc,
            )
            return ""
