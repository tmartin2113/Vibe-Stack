"""A/B versioning for skill refinements.

Pure A/B logic — no LLM, no Paperclip, no network. All functions are
deterministic given their inputs. This module is the single source of
truth for the ``__v{N}`` naming convention.

Used by Tier1aBuilder (to write v2 candidates), skill_loader (to pick
active versions), and skill_cleanup (to promote winners and archive losers).
"""

from __future__ import annotations

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
