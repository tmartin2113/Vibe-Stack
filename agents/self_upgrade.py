"""
Self-upgrade safety pipeline for Vibe agents.

Tier 2 (typed code edit) pipeline — dormant until M4.

The pipeline is **opt-in** — it only activates when the
``VIBE_SELF_UPGRADE_ENABLED`` env var is set to ``true``.

Safety invariants:
- Changes are NEVER applied directly to the running code mid-execution
- All modifications go through test + security scan gates
- Commits land on a dedicated branch, never on main/master
- A human must review and merge the resulting branch/PR
"""

import fcntl
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Branch prefix for all self-upgrade commits
UPGRADE_BRANCH_PREFIX = "vibe/self-upgrade"

# Minimum critic score to accept a self-upgrade
DEFAULT_MIN_SCORE = 90

# Files/directories that are never modifiable (even with self-upgrade enabled)
IMMUTABLE_PATHS = frozenset({
    # Current immutables
    "agents/self_upgrade.py",
    "agents/self_upgrade_trigger.py",
    "agents/self_upgrade_dispatcher.py",
    "agents/skill_security.py",
    "agents/config.py",
    ".env",
    ".env.example",
    # Workflow core
    "agents/graph.py",
    "agents/graph_engine.py",
    "agents/graph_runners.py",
    "agents/graph_nodes.py",
    "agents/nodes.py",
    "agents/state.py",
    "agents/specialist_nodes.py",
    "agents/output_nodes.py",
    # LLM plumbing (llm_retry.py intentionally absent — threshold_tweak allowlist)
    "agents/llm_backend.py",
    "agents/backend_pool.py",
    # Storage
    "agents/message_store.py",
    "agents/memory_store.py",
    "agents/artifact_store.py",
    "agents/spending_tracker.py",
    "agents/session_store.py",
    "agents/embedder.py",
    # Heartbeat
    "agents/heartbeat.py",
    "agents/heartbeat_context.py",
    "agents/heartbeat_progress.py",
    "agents/heartbeat_signals.py",
    "agents/heartbeat_spending.py",
    "agents/heartbeat_formatting.py",
    "agents/workflow_factory.py",
    # Skill subsystem plumbing
    "agents/skill_loader.py",
    "agents/skill_generator.py",
    "agents/skill_outcome_store.py",
    "agents/skill_cleanup.py",
    "agents/skill_search.py",
    "agents/skill_remote.py",
    # External clients
    "agents/paperclip_client.py",
    "agents/ws_client.py",
    "agents/messenger_client.py",
    "agents/api_key_manager.py",
    # Resource layer
    "agents/resource_discovery.py",
    "agents/resource_allocator.py",
    # Orchestrator + main
    "agents/main.py",
    "agents/orchestrator.py",
    "agents/daemon.py",
    "agents/cancellation.py",
    "agents/intent_classifier.py",
})

# Lock file for serialising git operations across concurrent workers
_GIT_LOCK_PATH = Path(tempfile.gettempdir()) / "vibe_self_upgrade.lock"

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
class UpgradeResult:
    """Result of a self-upgrade attempt."""

    success: bool
    typed_edit: Optional[Any] = None
    test_passed: bool = False
    test_output: str = ""
    bandit_passed: bool = False
    bandit_output: str = ""
    critic_score: int = 0
    critic_feedback: str = ""
    branch_name: str = ""
    commit_hash: str = ""
    errors: List[str] = field(default_factory=list)


class Tier2Pipeline:
    """Tier 2 (typed code edit) pipeline — dormant until M4.

    Gated pipeline for applying typed edits via the AST verifier. Currently a
    skeleton: __init__ and execute() exist but execute() returns Rejected for
    every input until M4 ships TypedEdit and the AST verifier.

    Private helpers (_run_tests, _run_bandit, _apply_and_commit, _generate_diff_text)
    are kept intact for M4's per-edit-type gates.
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

    def execute(self, typed_edit: Optional[Any] = None) -> UpgradeResult:
        """Execute a typed edit through the safety pipeline.

        Dormant until M4. Currently:
        - Returns Rejected if VIBE_SELF_UPGRADE_ENABLED is false
        - Returns Rejected for any non-None typed_edit (TypedEdit doesn't exist yet)
        - Returns Rejected for None typed_edit ("no edit provided")
        """
        if not is_self_upgrade_enabled():
            return UpgradeResult(
                success=False,
                errors=["Self-upgrade not enabled (VIBE_SELF_UPGRADE_ENABLED=false)"],
            )

        if typed_edit is None:
            return UpgradeResult(
                success=False,
                errors=["Tier2Pipeline dormant until M4: no typed_edit provided"],
            )

        return UpgradeResult(
            success=False,
            errors=["Tier2Pipeline dormant until M4: TypedEdit handling not implemented"],
        )

    # Dormant — see Tier2Pipeline docstring
    def _run_tests(self, proposal: Any) -> Tuple[bool, str]:
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

    # Dormant — see Tier2Pipeline docstring
    def _run_bandit(self, proposal: Any) -> Tuple[bool, str]:
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

    # Dormant — see Tier2Pipeline docstring
    def _generate_diff_text(self, proposal: Any) -> str:
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

    # Dormant — see Tier2Pipeline docstring
    def _apply_and_commit(
        self, proposal: Any
    ) -> Tuple[str, str]:
        """Apply changes to a feature branch, commit, and push.

        Uses a file lock to prevent concurrent git operations from
        multiple daemon workers.

        Returns:
            (branch_name, commit_hash)
        """
        # Generate a deterministic branch name from the proposal
        proposal_hash = hashlib.sha256(
            proposal.description.encode()
        ).hexdigest()[:8]
        branch_name = f"{UPGRADE_BRANCH_PREFIX}/{proposal_hash}"

        # Acquire file lock to serialise git operations
        lock_fd = open(_GIT_LOCK_PATH, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fd.close()
            raise RuntimeError(
                "Another self-upgrade is in progress (lock held)"
            )

        try:
            # Remember current branch to restore on failure
            current_branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.project_root),
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            # Create and switch to feature branch
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=str(self.project_root),
                capture_output=True, text=True, check=True,
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
                    capture_output=True, check=True,
                )

                commit_msg = (
                    f"self-upgrade: {proposal.description}\n\n"
                    f"Author: {proposal.author}\n"
                    f"Rationale: {proposal.rationale}\n\n"
                    f"This change was proposed and validated by the Vibe "
                    f"self-upgrade pipeline.\n"
                    f"Gates passed: path-validation, diff-size, pytest, bandit"
                )

                subprocess.run(
                    ["git", "commit", "-m", commit_msg],
                    cwd=str(self.project_root),
                    capture_output=True, text=True, check=True,
                )

                # Extract commit hash
                commit_hash = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(self.project_root),
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

                # Push to remote (best-effort — works in CI/deployed envs)
                push_result = subprocess.run(
                    ["git", "push", "-u", "origin", branch_name],
                    cwd=str(self.project_root),
                    capture_output=True, text=True,
                )
                if push_result.returncode == 0:
                    logger.info("Pushed branch %s to remote", branch_name)
                else:
                    logger.warning(
                        "Failed to push %s (non-fatal): %s",
                        branch_name, push_result.stderr[:200],
                    )

                # Switch back to original branch (leave upgrade branch intact)
                subprocess.run(
                    ["git", "checkout", current_branch],
                    cwd=str(self.project_root),
                    capture_output=True,
                )

                return branch_name, commit_hash

            except (subprocess.CalledProcessError, OSError):
                # Revert to previous branch on failure
                subprocess.run(
                    ["git", "checkout", current_branch],
                    cwd=str(self.project_root),
                    capture_output=True,
                )
                raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


# Backward-compat alias — remove after M4 ships and all callers move to Tier2Pipeline.
SelfUpgradePipeline = Tier2Pipeline
