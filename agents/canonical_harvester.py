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


import json
import secrets
from datetime import datetime, timezone
from pathlib import Path


# Crockford base32 alphabet used by ULIDs (no I, L, O, U)
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Simple stopword set for keyword extraction (intentionally small —
# the keyword check is a weak signal in the smoke test; the primary
# score comes from the critic).
_STOPWORDS = frozenset({
    "the", "and", "of", "a", "to", "in", "that", "it", "is", "was",
    "for", "on", "with", "as", "at", "by", "this", "be", "are", "or",
    "an", "but", "not", "from", "if", "then", "so", "do", "you", "your",
    "has", "have", "had", "will", "can", "may", "use", "using",
})

# Exponential moving average smoothing factor for baseline.json updates.
# alpha=0.3 weights new scores moderately — stable enough to resist
# single-run noise, responsive enough to track gradual drift.
_BASELINE_EMA_ALPHA = 0.3


def _new_ulid() -> str:
    """Return a canonical-fixture ID: 'can_' + 26 random Crockford base32 chars.

    Not a true ULID (no timestamp prefix) — just a stable-format unique id.
    """
    body = "".join(
        _CROCKFORD_ALPHABET[secrets.randbelow(32)] for _ in range(26)
    )
    return f"can_{body}"


def _count_fixtures(directory: Path) -> int:
    """Count *.json files in a directory, excluding baseline.json.

    Returns 0 if the directory does not exist.
    """
    if not directory.exists() or not directory.is_dir():
        return 0
    count = 0
    for f in directory.iterdir():
        if f.is_file() and f.suffix == ".json" and f.name != "baseline.json":
            count += 1
    return count


def _extract_keywords(text: str, *, top_n: int = 20) -> List[str]:
    """Return up to top_n content-bearing lowercase tokens from text.

    Dumb on purpose: filter stopwords, lowercase, dedupe while preserving
    order of first occurrence. Used as a weak recall signal in the smoke
    test — the critic's score is the primary metric.
    """
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text)
    seen: List[str] = []
    seen_set: set = set()
    for tok in tokens:
        low = tok.lower()
        if low in _STOPWORDS or len(low) < 3:
            continue
        if low in seen_set:
            continue
        seen.append(tok)
        seen_set.add(low)
        if len(seen) >= top_n:
            break
    return seen


def _update_baseline(directory: Path, *, fixture_id: str, score: float) -> None:
    """Update baseline.json for the adapter's fixture directory.

    Creates the file if it doesn't exist. Applies exponential moving
    average (alpha=0.3) for existing fixture ids, preserves others
    verbatim, adds new ids at the observed score.
    """
    baseline_path = directory / "baseline.json"
    if baseline_path.exists():
        try:
            current = json.loads(baseline_path.read_text())
        except (OSError, json.JSONDecodeError):
            current = {}
    else:
        current = {}
    if fixture_id in current:
        prev = float(current[fixture_id])
        current[fixture_id] = (
            _BASELINE_EMA_ALPHA * float(score) + (1.0 - _BASELINE_EMA_ALPHA) * prev
        )
    else:
        current[fixture_id] = float(score)
    baseline_path.write_text(json.dumps(current, indent=2, sort_keys=True))


def _utcnow_iso() -> str:
    """Return current UTC time as ISO 8601 with trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
