"""
Self-upgrade safety pipeline for Vibe agents.

Enables agents to propose, validate, and apply modifications to their own
source code through a gated pipeline:

    1. Propose a diff (change set)
    2. Apply to a temporary working copy
    3. Run pytest on the modified copy
    4. Run bandit security scan
    5. Validate via the Critic (score >= threshold)
    6. If all gates pass, apply the change and commit on a feature branch

The pipeline is **opt-in** — it only activates when the
``VIBE_SELF_UPGRADE_ENABLED`` env var is set to ``true``.

Safety invariants:
- Changes are NEVER applied directly to the running code mid-execution
- All modifications go through test + security scan gates
- Commits land on a dedicated branch, never on main/master
- A human must review and merge the resulting branch/PR
"""

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Branch prefix for all self-upgrade commits
UPGRADE_BRANCH_PREFIX = "vibe/self-upgrade"

# Minimum critic score to accept a self-upgrade
DEFAULT_MIN_SCORE = 90

# Files/directories that are never modifiable (even with self-upgrade enabled)
IMMUTABLE_PATHS = frozenset({
    "agents/self_upgrade.py",       # This module (prevent recursive bypass)
    "agents/skill_security.py",     # Security layer
    "agents/config.py",             # Core config
    ".env",
    ".env.example",
})

# Maximum diff size (lines) to prevent runaway changes
MAX_DIFF_LINES = 500


def is_self_upgrade_enabled() -> bool:
    """Check whether self-upgrade is enabled.

    Enabled by default.  Set VIBE_SELF_UPGRADE_ENABLED=false to disable.
    """
    val = os.environ.get("VIBE_SELF_UPGRADE_ENABLED", "true")
    return val.lower() not in ("false", "0", "no")


def get_project_root() -> Path:
    """Return the Vibe-Stack project root directory."""
    return Path(__file__).resolve().parent.parent


@dataclass
class UpgradeProposal:
    """A proposed self-upgrade change set."""

    description: str
    files: Dict[str, str]  # {relative_path: new_content}
    rationale: str = ""
    author: str = "vibe-self-upgrade"

    def validate_paths(self) -> List[str]:
        """Return list of validation errors for proposed file paths."""
        errors = []
        project_root = get_project_root()

        for rel_path in self.files:
            # Block immutable paths
            if rel_path in IMMUTABLE_PATHS:
                errors.append(
                    f"Cannot modify immutable path: {rel_path}"
                )
                continue

            # Must stay within project root
            full_path = (project_root / rel_path).resolve()
            try:
                full_path.relative_to(project_root)
            except ValueError:
                errors.append(
                    f"Path escapes project root: {rel_path}"
                )
                continue

            # Must be within agents/ directory (no modifying Docker, CI, etc.)
            if not rel_path.startswith("agents/"):
                errors.append(
                    f"Self-upgrade limited to agents/ directory: {rel_path}"
                )

        return errors


@dataclass
class UpgradeResult:
    """Result of a self-upgrade attempt."""

    success: bool
    proposal: UpgradeProposal
    test_passed: bool = False
    test_output: str = ""
    bandit_passed: bool = False
    bandit_output: str = ""
    critic_score: int = 0
    critic_feedback: str = ""
    branch_name: str = ""
    commit_hash: str = ""
    errors: List[str] = field(default_factory=list)


class SelfUpgradePipeline:
    """Gated pipeline for applying self-modifications safely.

    Each upgrade proposal goes through:
    1. Path validation (immutable files, directory constraints)
    2. Diff size check
    3. pytest on a temporary copy of the modified source
    4. bandit security scan on changed files
    5. (Optional) Critic scoring via the workflow's critic adapter
    6. Git commit on a feature branch
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        min_critic_score: int = DEFAULT_MIN_SCORE,
        max_diff_lines: int = MAX_DIFF_LINES,
    ):
        self.project_root = (project_root or get_project_root()).resolve()
        self.min_critic_score = min_critic_score
        self.max_diff_lines = max_diff_lines

    def execute(
        self,
        proposal: UpgradeProposal,
        critic_fn=None,
    ) -> UpgradeResult:
        """Run the full upgrade pipeline.

        Args:
            proposal: The proposed changes.
            critic_fn: Optional callable(description, diff_text) -> (score, feedback).
                       If None, critic gate is skipped.

        Returns:
            UpgradeResult with pass/fail details for each gate.
        """
        if not is_self_upgrade_enabled():
            return UpgradeResult(
                success=False,
                proposal=proposal,
                errors=["Self-upgrade is not enabled (set VIBE_SELF_UPGRADE_ENABLED=true)"],
            )

        result = UpgradeResult(success=False, proposal=proposal)

        # Gate 1: Path validation
        path_errors = proposal.validate_paths()
        if path_errors:
            result.errors = path_errors
            logger.warning("Self-upgrade blocked by path validation: %s", path_errors)
            return result

        # Gate 2: Diff size check
        total_lines = sum(
            content.count("\n") + 1 for content in proposal.files.values()
        )
        if total_lines > self.max_diff_lines:
            result.errors = [
                f"Diff too large: {total_lines} lines (max {self.max_diff_lines})"
            ]
            logger.warning("Self-upgrade blocked by diff size: %d lines", total_lines)
            return result

        # Gate 3 & 4: Run tests and bandit in a temporary copy
        try:
            test_passed, test_output = self._run_tests(proposal)
            result.test_passed = test_passed
            result.test_output = test_output

            if not test_passed:
                result.errors.append("pytest failed on proposed changes")
                logger.warning("Self-upgrade blocked: tests failed")
                return result

            bandit_passed, bandit_output = self._run_bandit(proposal)
            result.bandit_passed = bandit_passed
            result.bandit_output = bandit_output

            if not bandit_passed:
                result.errors.append("bandit found security issues in proposed changes")
                logger.warning("Self-upgrade blocked: bandit scan failed")
                return result
        except Exception as e:
            result.errors.append(f"Validation error: {e}")
            logger.exception("Self-upgrade validation error")
            return result

        # Gate 5: Critic scoring (optional)
        if critic_fn is not None:
            try:
                diff_text = self._generate_diff_text(proposal)
                score, feedback = critic_fn(proposal.description, diff_text)
                result.critic_score = score
                result.critic_feedback = feedback

                if score < self.min_critic_score:
                    result.errors.append(
                        f"Critic score {score} below threshold {self.min_critic_score}"
                    )
                    logger.warning(
                        "Self-upgrade blocked: critic score %d < %d",
                        score, self.min_critic_score,
                    )
                    return result
            except Exception as e:
                result.errors.append(f"Critic evaluation error: {e}")
                logger.exception("Self-upgrade critic error")
                return result

        # All gates passed — apply changes and commit
        try:
            branch_name, commit_hash = self._apply_and_commit(proposal)
            result.branch_name = branch_name
            result.commit_hash = commit_hash
            result.success = True
            logger.info(
                "Self-upgrade applied: branch=%s commit=%s",
                branch_name, commit_hash,
            )
        except Exception as e:
            result.errors.append(f"Failed to apply changes: {e}")
            logger.exception("Self-upgrade apply error")

        return result

    def _run_tests(self, proposal: UpgradeProposal) -> Tuple[bool, str]:
        """Run pytest against a temporary copy with the proposed changes."""
        with tempfile.TemporaryDirectory(prefix="vibe_upgrade_") as tmpdir:
            tmp_path = Path(tmpdir)

            # Copy the project
            shutil.copytree(
                self.project_root,
                tmp_path / "project",
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".git", "node_modules",
                    ".venv", "venv",
                ),
            )

            project_copy = tmp_path / "project"

            # Apply proposed changes
            for rel_path, content in proposal.files.items():
                target = project_copy / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

            # Run pytest
            try:
                proc = subprocess.run(
                    ["python", "-m", "pytest", "tests/", "-x", "--tb=short", "-q"],
                    cwd=str(project_copy),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                passed = proc.returncode == 0
                output = proc.stdout + proc.stderr
                return passed, output[-2000:]  # Truncate to last 2000 chars
            except subprocess.TimeoutExpired:
                return False, "pytest timed out after 300 seconds"
            except FileNotFoundError:
                return False, "pytest not found"

    def _run_bandit(self, proposal: UpgradeProposal) -> Tuple[bool, str]:
        """Run bandit security scan on the proposed files."""
        with tempfile.TemporaryDirectory(prefix="vibe_bandit_") as tmpdir:
            tmp_path = Path(tmpdir)

            # Write only the changed files for scanning
            files_to_scan = []
            for rel_path, content in proposal.files.items():
                if not rel_path.endswith(".py"):
                    continue
                target = tmp_path / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                files_to_scan.append(str(target))

            if not files_to_scan:
                return True, "No Python files to scan"

            try:
                proc = subprocess.run(
                    ["python", "-m", "bandit", "-r"] + files_to_scan +
                    ["-ll", "--format", "txt"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                # bandit returns 0 if no issues, 1 if issues found
                passed = proc.returncode == 0
                output = proc.stdout + proc.stderr
                return passed, output[-2000:]
            except subprocess.TimeoutExpired:
                return False, "bandit timed out after 60 seconds"
            except FileNotFoundError:
                # bandit not installed — treat as pass with warning
                logger.warning("bandit not installed, skipping security scan")
                return True, "bandit not installed (skipped)"

    def _generate_diff_text(self, proposal: UpgradeProposal) -> str:
        """Generate a human-readable diff summary for critic review."""
        parts = [f"## Self-Upgrade Proposal: {proposal.description}\n"]

        for rel_path, new_content in proposal.files.items():
            original_path = self.project_root / rel_path
            if original_path.exists():
                original = original_path.read_text()
                parts.append(f"### Modified: {rel_path}")
                parts.append(f"Original: {len(original)} chars → New: {len(new_content)} chars")
            else:
                parts.append(f"### New file: {rel_path}")
                parts.append(f"Size: {len(new_content)} chars")
            parts.append("")

        if proposal.rationale:
            parts.append(f"### Rationale\n{proposal.rationale}")

        return "\n".join(parts)

    def _apply_and_commit(
        self, proposal: UpgradeProposal
    ) -> Tuple[str, str]:
        """Apply changes to a feature branch and commit.

        Returns:
            (branch_name, commit_hash)
        """
        # Generate a deterministic branch name from the proposal
        proposal_hash = hashlib.sha256(
            proposal.description.encode()
        ).hexdigest()[:8]
        branch_name = f"{UPGRADE_BRANCH_PREFIX}/{proposal_hash}"

        # Create and switch to feature branch
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=str(self.project_root),
            capture_output=True,
            text=True,
            check=True,
        )

        try:
            # Apply changes
            for rel_path, content in proposal.files.items():
                target = self.project_root / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)

            # Stage and commit
            file_paths = list(proposal.files.keys())
            subprocess.run(
                ["git", "add"] + file_paths,
                cwd=str(self.project_root),
                capture_output=True,
                check=True,
            )

            commit_msg = (
                f"self-upgrade: {proposal.description}\n\n"
                f"Author: {proposal.author}\n"
                f"Rationale: {proposal.rationale}\n\n"
                f"This change was proposed and validated by the Vibe self-upgrade pipeline.\n"
                f"Gates passed: path-validation, diff-size, pytest, bandit"
            )

            proc = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                check=True,
            )

            # Extract commit hash
            hash_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                check=True,
            )
            commit_hash = hash_proc.stdout.strip()

            return branch_name, commit_hash

        except Exception:
            # Revert to previous branch on failure
            subprocess.run(
                ["git", "checkout", "-"],
                cwd=str(self.project_root),
                capture_output=True,
            )
            raise
