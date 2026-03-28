"""
LLM Backend Wrapper for Multi-Agent System

Provides a unified interface for the vLLM backend.
Adapts backend API to work with the multi-agent adapter system.
"""

import logging
import os
from typing import Dict, Any, List, Optional
from typing_extensions import TypedDict
from pathlib import Path
import sys


class LLMGenerateKwargs(TypedDict, total=False):
    """Type-safe keyword arguments for LLMBackend.generate() calls."""

    temperature: float
    max_tokens: int
    stop: Optional[List[str]]
    chat_template_kwargs: Optional[Dict[str, Any]]

from .llm_retry import retry_llm_call, DEFAULT_MAX_RETRIES, DEFAULT_BASE_DELAY

# Add parent directory to path to import vibe backends
sys.path.insert(0, str(Path(__file__).parent.parent))

from vibe.backends.vllm import VLLMBackend

logger = logging.getLogger(__name__)


class LLMBackend:
    """
    Unified LLM backend wrapper for multi-agent system.

    Uses vLLM as the sole backend (OpenAI-compatible API, GPU-optimized).
    """

    def __init__(self, model: str, host: str = "localhost", port: Optional[int] = None,
                 max_retries: int = DEFAULT_MAX_RETRIES, retry_base_delay: float = DEFAULT_BASE_DELAY):
        """
        Initialize LLM backend.

        Args:
            model: Model name
            host: vLLM server host
            port: vLLM server port (default: 8000)
            max_retries: Max retry attempts on transient failures (0 = no retries)
            retry_base_delay: Base delay in seconds for exponential backoff
        """
        self.backend_type = "vllm"
        self.model_name = model
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay

        if port is None:
            port = 8000
        self.backend = VLLMBackend(host=host, port=port, model=model)
        logger.info(f"Initialized vLLM backend: {model} @ {host}:{port}")

    def health_check(self) -> bool:
        """
        Check if backend is healthy and responding.

        Returns:
            bool: True if healthy
        """
        return self.backend.health_check()

    def generate(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 2000,
        stop: Optional[List[str]] = None,
        chat_template_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> str:
        """
        Generate completion from messages (adapter interface).

        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            stop: Stop sequences
            chat_template_kwargs: Extra kwargs for chat template
            **kwargs: Additional backend-specific parameters

        Returns:
            str: Generated text
        """
        # Use chat completions to preserve system prompts
        result = retry_llm_call(
            self.backend.generate_chat,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            chat_template_kwargs=chat_template_kwargs,
            max_retries=self.max_retries,
            base_delay=self.retry_base_delay,
        )

        return result["text"]  # type: ignore[no-any-return]


def _create_backend_for_url(url: str, model: str, backend_type: str = "vllm") -> VLLMBackend:
    """Create a raw VLLMBackend from a host:port string."""
    if ":" in url:
        h, p = url.rsplit(":", 1)
        return VLLMBackend(host=h, port=int(p), model=model)
    return VLLMBackend(host=url, port=8000, model=model)


def create_backend_from_config(config: Any):
    """
    Create LLM backend from system config.

    If fallback URLs are configured (via config or VIBE_FALLBACK_URLS env var),
    returns a BackendPool wrapping the primary + fallback backends.
    Otherwise returns a single LLMBackend (backwards compatible).

    Args:
        config: SystemConfig object

    Returns:
        LLMBackend or BackendPool instance (both expose generate() and health_check())
    """
    model = os.getenv("VIBE_MODEL", config.model.model_name)
    host = os.getenv("VIBE_BACKEND_HOST", "localhost")
    port_str = os.getenv("VIBE_BACKEND_PORT")
    port = int(port_str) if port_str else None

    # Read retry settings from config
    max_retries = DEFAULT_MAX_RETRIES
    retry_base_delay = DEFAULT_BASE_DELAY
    if hasattr(config, 'workflow'):
        max_retries = getattr(config.workflow, 'llm_max_retries', DEFAULT_MAX_RETRIES)
        retry_base_delay = getattr(config.workflow, 'llm_retry_base_delay', DEFAULT_BASE_DELAY)

    # Determine fallback URLs
    fallback_urls: List[str] = []
    pool_config = getattr(config, "backend_pool", None)
    if pool_config is not None:
        raw = getattr(pool_config, "fallback_urls", None)
        if isinstance(raw, list) and raw:
            fallback_urls = raw
    env_urls = os.getenv("VIBE_FALLBACK_URLS")
    if env_urls and not fallback_urls:
        fallback_urls = [u.strip() for u in env_urls.split(",") if u.strip()]

    if fallback_urls:
        from .backend_pool import BackendPool

        # Build primary + fallback raw backends
        primary_port = port if port is not None else 8000
        primary = VLLMBackend(host=host, port=primary_port, model=model)
        backends = [primary] + [
            _create_backend_for_url(u, model) for u in fallback_urls
        ]

        strategy = pool_config.strategy if pool_config else "failover"
        max_failures = pool_config.max_consecutive_failures if pool_config else 3
        recovery = pool_config.recovery_timeout if pool_config else 60

        pool = BackendPool(
            backends=backends,
            strategy=strategy,
            max_consecutive_failures=max_failures,
            recovery_timeout=recovery,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
        )

        logger.info(
            "Created BackendPool (%s) with %d backends for model %s",
            strategy,
            len(backends),
            model,
        )

        if not pool.health_check():
            logger.warning("BackendPool health check: no healthy backends found")

        return pool

    # Single backend (original path)
    logger.info(f"Creating vLLM backend ({model})")

    backend = LLMBackend(
        model=model,
        host=host,
        port=port,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
    )

    if not backend.health_check():
        logger.warning(f"vLLM backend health check failed at {host}:{port_str or 8000}")
        logger.warning("Make sure the vLLM server is running!")

    return backend
