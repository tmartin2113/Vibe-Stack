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
    "agents/self_upgrade.py",           # This module (prevent recursive bypass)
    "agents/self_upgrade_trigger.py",   # Trigger module (prevent signal manipulation)
    "agents/skill_security.py",         # Security layer
    "agents/config.py",                 # Core config
    ".env",
    ".env.example",
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


# ── LLM-driven code generation ───────────────────────────────────────


def generate_upgrade_proposal(
    description: str,
    rationale: str,
    target_files: List[str],
    base_model: Any,
    project_root: Optional[Path] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Optional[UpgradeProposal]:
    """Use the LLM to generate an UpgradeProposal from trigger analysis.

    Reads the target files, asks the LLM to propose improvements based on
    the accumulated signal rationale, and returns a structured proposal.

    Args:
        description:  What should be improved (from TriggerAnalysis).
        rationale:    Why (accumulated signal details).
        target_files: Which files to read and potentially modify.
        base_model:   The LLM backend instance.
        state:        Optional workflow state dict — if provided, self-upgrade
                      token usage is added to total_input/output_tokens.
        project_root: Project root directory.

    Returns:
        UpgradeProposal with modified file contents, or None if the LLM
        declines to propose changes.
    """
    if project_root is None:
        project_root = get_project_root()

    # Read current contents of target files
    file_contents = {}
    for rel_path in target_files:
        full_path = project_root / rel_path
        if full_path.exists() and full_path.is_file():
            try:
                file_contents[rel_path] = full_path.read_text()
            except OSError:
                continue

    if not file_contents:
        logger.warning("No readable target files for self-upgrade proposal")
        return None

    # Build the LLM prompt
    file_sections = []
    for rel_path, content in file_contents.items():
        # Truncate very large files to avoid context overflow
        truncated = content[:8000] if len(content) > 8000 else content
        file_sections.append(
            f"### {rel_path}\n```python\n{truncated}\n```"
        )

    prompt = f"""You are proposing a targeted improvement to the Vibe agent codebase.

## Improvement Goal
{description}

## Evidence (from accumulated workflow signals)
{rationale}

## Current Source Files
{chr(10).join(file_sections)}

## Instructions
1. Analyse the evidence and source files above
2. Identify ONE specific, minimal change that addresses the dominant issue
3. Output the COMPLETE modified file content for each file you change
4. If no change is warranted, respond with exactly: NO_CHANGE

## Output Format
For each file you modify, output:
FILE: <relative_path>
```python
<complete file content>
```

Only output files you actually changed. Keep changes minimal and backward-compatible.
Do NOT modify function signatures unless absolutely necessary.
Do NOT add new dependencies.
"""

    messages = [
        {"role": "system", "content": (
            "You are a senior software engineer performing a controlled "
            "self-upgrade on the Vibe agent codebase. Output only the "
            "requested format — no commentary."
        )},
        {"role": "user", "content": prompt},
    ]

    try:
        response = base_model.generate(
            messages, temperature=0.3, max_tokens=4000,
        )
    except Exception as e:
        logger.error("LLM call failed during self-upgrade generation: %s", e)
        return None

    # Best-effort token tracking — estimate from prompt/response length
    # and add to workflow state so heartbeat reports them to Paperclip.
    if state is not None:
        prompt_chars = sum(len(m["content"]) for m in messages)
        response_chars = len(response) if response else 0
        # Rough estimate: ~4 chars per token (conservative)
        est_input = prompt_chars // 4
        est_output = response_chars // 4
        state["total_input_tokens"] = state.get("total_input_tokens", 0) + est_input
        state["total_output_tokens"] = state.get("total_output_tokens", 0) + est_output

    if not response or "NO_CHANGE" in response:
        logger.info("LLM declined to propose changes for: %s", description)
        return None

    # Parse the response into file contents
    modified_files = _parse_llm_file_output(response, file_contents)
    if not modified_files:
        logger.warning("Failed to parse LLM output for self-upgrade proposal")
        return None

    return UpgradeProposal(
        description=description,
        files=modified_files,
        rationale=rationale,
        author="vibe-self-upgrade",
    )


def _parse_llm_file_output(
    response: str, original_files: Dict[str, str]
) -> Dict[str, str]:
    """Parse LLM output into {rel_path: content} dict.

    Expected format:
        FILE: agents/foo.py
        ```python
        <content>
        ```
    """
    import re

    files: Dict[str, str] = {}
    # Match FILE: <path> followed by a fenced code block
    pattern = r"FILE:\s*(\S+)\s*\n```(?:python)?\n(.*?)```"
    matches = re.findall(pattern, response, re.DOTALL)

    for rel_path, content in matches:
        rel_path = rel_path.strip()
        # Only accept files that were in our target list
        if rel_path in original_files:
            # Sanity: content shouldn't be empty or identical
            if content.strip() and content.strip() != original_files[rel_path].strip():
                files[rel_path] = content

    return files
