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

import re
from datetime import datetime
from typing import Any, Dict


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
