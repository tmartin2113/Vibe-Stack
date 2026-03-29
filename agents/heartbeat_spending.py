"""
Heartbeat spending helpers.

Pricing tables, cost estimation, spending tracker initialization,
config validation, and artifact cache maintenance.
"""

import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import SystemConfig
    from .spending_tracker import SpendingTracker

logger = logging.getLogger(__name__)


# Per-million-token pricing in cents. Conservative estimates; actual pricing
# varies by tier and changes over time.  Local backends (vllm, ollama,
# llama.cpp) are free — they run on the agent's own hardware.
_PRICING_PER_MILLION: Dict[str, Dict[str, tuple]] = {
    # backend -> model_prefix -> (input_cents, output_cents) per 1M tokens
    "openai": {
        "gpt-4o": (250, 1000),
        "gpt-4o-mini": (15, 60),
        "gpt-4-turbo": (1000, 3000),
        "gpt-4": (3000, 6000),
        "gpt-3.5": (50, 150),
        "_default": (250, 1000),
    },
    "anthropic": {
        "claude-3-opus": (1500, 7500),
        "claude-3.5-sonnet": (300, 1500),
        "claude-3-sonnet": (300, 1500),
        "claude-3-haiku": (25, 125),
        "claude-3.5-haiku": (80, 400),
        "_default": (300, 1500),
    },
    "google": {
        "gemini-1.5-pro": (125, 500),
        "gemini-1.5-flash": (8, 30),
        "gemini-pro": (50, 150),
        "_default": (125, 500),
    },
}

# Local backends — always free
_FREE_BACKENDS = {"vllm", "ollama", "llama.cpp", "llamacpp"}


def _estimate_cost_cents(
    backend: str,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> int:
    """
    Estimate cost in cents from backend, model, and token counts.

    Returns 0 for local backends (vLLM, Ollama, llama.cpp).
    For cloud backends, uses conservative per-million-token pricing.
    """
    backend_lower = backend.lower()
    if backend_lower in _FREE_BACKENDS or not input_tokens and not output_tokens:
        return 0

    pricing = _PRICING_PER_MILLION.get(backend_lower)
    if pricing is None:
        return 0

    # Find best matching model prefix (longest match wins to avoid
    # "gpt-4o" matching before "gpt-4o-mini")
    model_lower = model_name.lower()
    input_rate, output_rate = pricing["_default"]
    best_prefix_len = 0
    for prefix, rates in pricing.items():
        if prefix != "_default" and model_lower.startswith(prefix) and len(prefix) > best_prefix_len:
            input_rate, output_rate = rates
            best_prefix_len = len(prefix)

    cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    return max(1, round(cost))  # At least 1 cent if any cloud tokens used


def _get_spending_tracker(config: "SystemConfig") -> "Optional[SpendingTracker]":
    """Create a SpendingTracker if spending tracking is enabled."""
    if not config.spending.enabled:
        return None
    try:
        from .spending_tracker import SpendingTracker
        return SpendingTracker(
            db_path=config.spending.db_path,
            window_seconds=config.spending.window_seconds,
            max_cents_per_window=config.spending.max_cents_per_window,
            max_heartbeats_per_window=config.spending.max_heartbeats_per_window,
            max_consecutive_non_idle=config.spending.max_consecutive_non_idle,
            cooldown_seconds=config.spending.cooldown_seconds,
            max_cooldown_seconds=config.spending.max_cooldown_seconds,
            retention_days=config.spending.retention_days,
            agent_id=os.environ.get("PAPERCLIP_AGENT_ID", ""),
        )
    except (ImportError, OSError) as e:
        logger.warning("Failed to initialize spending tracker (non-fatal): %s", e)
        return None


def _validate_heartbeat_config(config: "SystemConfig") -> List[str]:
    """
    Validate config for heartbeat mode. Returns list of issues (empty = valid).

    Runs the base config.validate() plus heartbeat-specific checks.
    """
    issues: List[str] = []

    if not config.model.model_name:
        issues.append("Model name not specified")

    if config.mattermost.enabled and not config.mattermost.webhook_url:
        issues.append("Mattermost enabled but webhook URL not configured")

    # Heartbeat-specific: Paperclip connectivity requirements
    if not os.environ.get("PAPERCLIP_API_URL") and not config.paperclip.api_url:
        issues.append("PAPERCLIP_API_URL not set (required for heartbeat mode)")

    if not os.environ.get("PAPERCLIP_AGENT_ID"):
        issues.append("PAPERCLIP_AGENT_ID not set (required for self-comment filtering)")

    return issues


def _artifact_cache_maintenance() -> None:
    """Best-effort artifact cache cleanup: evict expired + LRU overflow."""
    try:
        from .artifact_store import ArtifactStore
        store = ArtifactStore()
        expired = store.cleanup_expired()
        if expired:
            logger.info("Heartbeat cache cleanup: removed %d expired artifacts", expired)
        # Also enforce LRU cap (separate from TTL expiry)
        evicted = store._evict_if_needed()
        if evicted:
            logger.info("Heartbeat cache eviction: removed %d over-limit artifacts", evicted)
    except (ImportError, OSError, sqlite3.DatabaseError) as e:
        logger.debug("Artifact cache maintenance skipped: %s", e)
