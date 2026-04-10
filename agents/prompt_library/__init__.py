"""Prompt override loader and schema for Tier 1b self-upgrade path.

Provides:
- OverrideSchemaError: raised on validation failure
- validate_override_dict: pure function that validates a parsed dict
- OverrideEntry: frozen dataclass for loaded overrides (Task 2)
- PromptOverrideLoader: walks prompt_library/overrides/ at construction (Task 2)

Permissive at runtime: malformed files log WARN and are skipped.
Strict at gate time: validate_override_dict raises on any violation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

__all__ = [
    "OverrideEntry",
    "OverrideSchemaError",
    "PromptOverrideLoader",
    "validate_override_dict",
]

# ULID uses Crockford base32: 0-9, A-H, J, K, M, N, P-T, V-Z (no I, L, O, U)
_OVERRIDE_ID_RE = re.compile(r"^ovr_[0-9A-HJKMNP-TV-Z]{26}$")

_ALLOWED_KEYS = frozenset({
    "id",
    "task_type",
    "append",
    "signal_refs",
    "author_agent_id",
    "author_run_id",
    "created_at",
})

_APPEND_MAX_LEN = 500


class OverrideSchemaError(ValueError):
    """Raised when an override file or dict fails schema validation."""


def validate_override_dict(d: Dict[str, Any], *, filename: str) -> None:
    """Validate a parsed override dict against the Tier 1b schema.

    Raises OverrideSchemaError on any violation. Returns None on success.

    Args:
        d: The parsed YAML/dict payload.
        filename: Basename of the source file (e.g. 'ovr_01HZ...yaml'). Used
            to enforce that the file name matches the id field.
    """
    if not isinstance(d, dict):
        raise OverrideSchemaError(f"override payload must be a dict, got {type(d).__name__}")

    # Extra-key guard — strict schema. New fields require a code change.
    extra = set(d.keys()) - _ALLOWED_KEYS
    if extra:
        raise OverrideSchemaError(f"unexpected field: {sorted(extra)[0]}")

    # Required fields present
    for required in ("id", "task_type", "append", "signal_refs",
                     "author_agent_id", "author_run_id", "created_at"):
        if required not in d:
            raise OverrideSchemaError(f"missing required field: {required}")

    # id format
    ov_id = d["id"]
    if not isinstance(ov_id, str) or not _OVERRIDE_ID_RE.match(ov_id):
        raise OverrideSchemaError(
            f"id must match ^ovr_[0-9A-HJKMNP-TV-Z]{{26}}$, got {ov_id!r}"
        )

    # filename must match id
    expected_filename = f"{ov_id}.yaml"
    if filename != expected_filename:
        raise OverrideSchemaError(
            f"filename does not match id: expected {expected_filename}, got {filename}"
        )

    # task_type non-empty string (registry check is done by caller, not loader)
    if not isinstance(d["task_type"], str) or not d["task_type"].strip():
        raise OverrideSchemaError("task_type must be a non-empty string")

    # append constraints
    append = d["append"]
    if not isinstance(append, str) or not append.strip():
        raise OverrideSchemaError("append must be non-empty")
    if len(append) > _APPEND_MAX_LEN:
        raise OverrideSchemaError(
            f"append exceeds 500 characters ({len(append)})"
        )
    if "\x00" in append:
        raise OverrideSchemaError("append must not contain NUL byte")
    if "```" in append:
        raise OverrideSchemaError("append must not contain triple backtick")

    # signal_refs non-empty list of strings
    refs = d["signal_refs"]
    if not isinstance(refs, list) or len(refs) == 0:
        raise OverrideSchemaError("signal_refs must be non-empty list")
    if not all(isinstance(r, str) and r for r in refs):
        raise OverrideSchemaError("signal_refs entries must be non-empty strings")

    # author_agent_id and author_run_id non-empty strings
    for field_name in ("author_agent_id", "author_run_id"):
        val = d[field_name]
        if not isinstance(val, str) or not val.strip():
            raise OverrideSchemaError(f"{field_name} must be a non-empty string")

    # created_at parses as ISO 8601
    created_at = d["created_at"]
    if not isinstance(created_at, str):
        raise OverrideSchemaError("created_at must be an ISO 8601 string")
    try:
        # Python's fromisoformat doesn't accept trailing Z on <3.11; strip it.
        normalized = created_at.rstrip("Z").replace("Z", "")
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise OverrideSchemaError(
            f"created_at must be ISO 8601 UTC, got {created_at!r}: {exc}"
        ) from exc


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OverrideEntry:
    """Parsed, validated, active override. Immutable after construction."""

    id: str
    task_type: str
    append: str
    signal_refs: Tuple[str, ...]
    author_agent_id: str
    author_run_id: str
    created_at: str


class PromptOverrideLoader:
    """Loads active prompt overrides from disk at construction time.

    Walks ``root/{task_type}/*.yaml``, validates each file, and indexes
    active (non-decayed, non-superseded) overrides by task_type.

    Permissive: individual file failures log a WARNING and are skipped.
    A missing ``root`` directory initializes with an empty map.

    Immutable snapshot: the index is built once at ``__init__`` time.
    No hot reload. Process restart picks up new overrides.
    """

    DEFAULT_ROOT = Path("agents/prompt_library/overrides")

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = Path(root) if root is not None else self.DEFAULT_ROOT
        self._by_task_type: Dict[str, List[OverrideEntry]] = {}
        self._load()

    def _load(self) -> None:
        if not self._root.exists():
            logger.info("prompt override root %s not present; no overrides loaded", self._root)
            return
        if not self._root.is_dir():
            logger.warning("prompt override root %s is not a directory; skipping", self._root)
            return

        for task_type_dir in sorted(self._root.iterdir()):
            if not task_type_dir.is_dir():
                continue
            task_type = task_type_dir.name
            entries: List[OverrideEntry] = []
            for yaml_file in sorted(task_type_dir.glob("*.yaml")):
                if not yaml_file.name.startswith("ovr_"):
                    continue
                if self._is_inactive(yaml_file):
                    logger.debug("override %s is inactive; skipping", yaml_file.name)
                    continue
                entry = self._parse_file(yaml_file)
                if entry is not None:
                    entries.append(entry)
            if entries:
                entries.sort(key=lambda e: e.created_at)
                self._by_task_type[task_type] = entries

    def _is_inactive(self, yaml_file: Path) -> bool:
        stem = yaml_file.stem
        parent = yaml_file.parent
        return (
            (parent / f"{stem}.decayed").exists()
            or (parent / f"{stem}.superseded").exists()
        )

    def _parse_file(self, yaml_file: Path) -> Optional[OverrideEntry]:
        try:
            text = yaml_file.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text)
        except Exception as exc:
            logger.warning("skipping malformed override %s: %s", yaml_file, exc)
            return None
        # PyYAML safe_load auto-converts ISO 8601 timestamps to datetime objects.
        # Normalize back to a string so validate_override_dict sees a str.
        if isinstance(parsed, dict) and isinstance(parsed.get("created_at"), datetime):
            dt_val = parsed["created_at"]
            if dt_val.tzinfo is None or dt_val.utcoffset().total_seconds() != 0:
                logger.warning(
                    "skipping override %s: created_at is not UTC (tzinfo=%s)",
                    yaml_file, dt_val.tzinfo,
                )
                return None
            parsed = dict(parsed)
            parsed["created_at"] = dt_val.strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            validate_override_dict(parsed, filename=yaml_file.name)
        except OverrideSchemaError as exc:
            logger.warning("skipping invalid override %s: %s", yaml_file, exc)
            return None
        return OverrideEntry(
            id=parsed["id"],
            task_type=parsed["task_type"],
            append=parsed["append"].rstrip("\n"),
            signal_refs=tuple(parsed["signal_refs"]),
            author_agent_id=parsed["author_agent_id"],
            author_run_id=parsed["author_run_id"],
            created_at=parsed["created_at"],
        )

    def get_appends_for(self, task_type: str) -> List[str]:
        """Return the list of active override appends for a task_type.

        Returns an empty list if no active overrides exist for that type.
        The order is ``created_at`` ascending — the oldest active override
        is first in the returned list.
        """
        return [e.append for e in self._by_task_type.get(task_type, [])]
