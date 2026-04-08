"""A/B versioning for skill refinements.

Pure A/B logic — no LLM, no Paperclip, no network. All functions are
deterministic given their inputs. This module is the single source of
truth for the ``__v{N}`` naming convention.

Used by Tier1aBuilder (to write v2 candidates), skill_loader (to pick
active versions), and skill_cleanup (to promote winners and archive losers).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .skill_outcome_store import SkillOutcomeStore
    from .skill_registry import SkillRegistry


VERSION_SUFFIX_RE = re.compile(r"^(?P<base>.+?)__v(?P<version>\d+)$")


def is_versioned_name(name: str) -> bool:
    """Return True if ``name`` matches the ``<base>__v<N>`` convention."""
    return VERSION_SUFFIX_RE.match(name) is not None


def base_name(name: str) -> str:
    """Return the base name for a possibly-versioned skill name.

    ``base_name("x__v2") == "x"``; ``base_name("x") == "x"``.
    """
    match = VERSION_SUFFIX_RE.match(name)
    if match:
        return match.group("base")
    return name


def versioned_name(base: str, version: int) -> str:
    """Construct a versioned name from a base and an integer version.

    ``versioned_name("x", 2) == "x__v2"``.
    """
    return f"{base}__v{version}"


def list_versions_for(base: str, *, skills_root: Path) -> List[Path]:
    """Return all version directories for a base name, sorted by version.

    The base directory (no suffix) is treated as version 1 and always
    appears first if it exists. Subsequent versions appear in ascending
    version order.

    Directories under ``skills_root/archive/`` are never included.
    Directories without a readable ``SKILL.md`` are never included.
    Returns an empty list if no matching directories exist.
    """
    if not skills_root.is_dir():
        return []

    matches: List[tuple[int, Path]] = []
    for entry in skills_root.iterdir():
        if entry.name == "archive":
            continue
        if not entry.is_dir():
            continue
        if not (entry / "SKILL.md").is_file():
            continue

        if entry.name == base:
            matches.append((1, entry))
            continue

        m = VERSION_SUFFIX_RE.match(entry.name)
        if m and m.group("base") == base:
            v = int(m.group("version"))
            # Reject non-canonical names like "foo__v02" — they parse to
            # version 2 but are not produced by versioned_name(), so
            # including them makes ordering non-deterministic when both
            # forms exist. Only canonical names contribute to versioning.
            if entry.name == versioned_name(base, v):
                matches.append((v, entry))

    matches.sort(key=lambda pair: pair[0])
    return [path for _version, path in matches]


def bucket_for_run(run_input: str, num_buckets: int = 2) -> int:
    """Return a deterministic bucket index for a run input.

    Formula: ``sha256(run_input).digest()[0] % num_buckets``.

    Deliberately does NOT use Python's built-in ``hash()`` — PEP 456
    process-level randomization would break cross-process determinism.
    Empty ``run_input`` returns 0 (stable fallback).
    """
    digest = hashlib.sha256(run_input.encode("utf-8")).digest()
    return digest[0] % num_buckets


def pick_active_version(
    candidates: List[Path],
    *,
    run_input: str,
) -> Path:
    """Pick one version directory from a candidate list, deterministically.

    Invariant: same ``run_input`` + same candidate list → same result, always.
    Called by the skill loader during workflow execution. Never mutates the
    filesystem.

    Args:
        candidates: Version directories from ``list_versions_for``, in the
            order returned by that function (ascending by version).
        run_input: The bucketing input — typically ``state["session_id"]``.
            If empty, returns ``candidates[0]`` as a stable fallback.

    Raises:
        ValueError: If ``candidates`` is empty.
    """
    if not candidates:
        raise ValueError("pick_active_version requires at least one candidate")
    if len(candidates) == 1 or not run_input:
        return candidates[0]
    bucket = bucket_for_run(run_input, num_buckets=len(candidates))
    return candidates[bucket]


def write_candidate(
    base: str,
    *,
    version: int,
    content: str,
    description: str,
    task_types: List[str],
    tier: str,
    parent_dir: Path,
    skill_registry: "SkillRegistry",
) -> Path:
    """Write a new ``__v{N}`` sibling directory and register it.

    Writes ``SKILL.md`` to ``parent_dir/<base>__v<version>/`` and then calls
    ``skill_registry.register_skill`` which handles validation, integrity
    hash storage, and index updates.

    Args:
        base: Base skill name (no version suffix).
        version: Integer version number (typically 2).
        content: Full SKILL.md content for the candidate.
        description: Skill description (passed through to register_skill).
        task_types: Task types this skill handles (passed through).
        tier: Skill tier — one of "temp", "local", "official".
        parent_dir: Directory that will contain the new sibling.
        skill_registry: SkillRegistry instance to register with.

    Returns:
        Absolute path to the new version directory.

    Raises:
        FileExistsError: If anything (file or directory) already exists at
            the target path. The caller is responsible for checking via
            ``list_versions_for`` before calling this function.
    """
    target_dir = parent_dir / versioned_name(base, version)
    if target_dir.exists():
        raise FileExistsError(
            f"Cannot write candidate: {target_dir} already exists"
        )

    target_dir.mkdir()
    (target_dir / "SKILL.md").write_text(content, encoding="utf-8")

    skill_registry.register_skill(
        name=versioned_name(base, version),
        description=description,
        tier=tier,
        task_types=task_types,
        skill_path=target_dir,
    )

    return target_dir
