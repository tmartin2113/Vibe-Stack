"""Canonical fixture harvester — captures high-scoring real runs as fixtures.

Called from the heartbeat after a successful workflow. Captures runs with
critic_score ≥ 90 as JSON fixtures under tests/canonical/{adapter_type}/.
Used by Tier 1b's smoke-test gate to check that proposed prompt overrides
don't regress real-world outputs.

Safety properties:
- Default-deny redaction: any content matching a secret pattern aborts
  the capture. False positives are fine; false negatives are dangerous.
- Per-adapter cap: at cap_per_adapter fixtures, the harvester stops. No
  eviction — stable fixture set means stable smoke tests.
- Failure swallowing: every failure path logs and returns None, so the
  heartbeat's task result is never affected by harvester errors.
"""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)


class RedactionRefused(Exception):
    """Raised when a candidate fixture matches a secret pattern.

    The caller should treat this as 'do not capture' and log at DEBUG.
    """


# Order matters: most specific patterns first. Each entry is (name, regex).
# False positives here are FINE — the fixture just isn't captured. False
# negatives are DANGEROUS — they would leak secrets into tests/canonical/.
_REDACTION_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("openai_api_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")),
    ("github_pat", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)),
    ("generic_api_key_var",
     re.compile(r"(?:API_KEY|SECRET_KEY|ACCESS_KEY|PRIVATE_KEY)\s*[:=]\s*\S{8,}", re.IGNORECASE)),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Catch-all for high-entropy long blobs (32+ chars of alphanumeric, no spaces).
    # Placed last so named patterns take precedence in the error message.
    ("high_entropy_blob", re.compile(r"\b[A-Za-z0-9]{32,}\b")),
]


def _redact(text: str) -> str:
    """Run the redaction table. Returns text unchanged on pass.

    Raises RedactionRefused on the first matching pattern. The exception
    message names the matched pattern so the caller's log has a useful
    reason.
    """
    if not text:
        return text
    for name, pattern in _REDACTION_PATTERNS:
        if pattern.search(text):
            raise RedactionRefused(
                f"matched redaction pattern {name!r}; refusing to capture"
            )
    return text
