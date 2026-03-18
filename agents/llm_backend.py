"""
LLM Backend Wrapper for Multi-Agent System

Provides a unified interface for the vLLM backend.
Adapts backend API to work with the multi-agent adapter system.
"""

import logging
import os
from typing import Dict, Any, List, Optional
from pathlib import Path
import sys

from .llm_retry import retry_llm_call, DEFAULT_MAX_RETRIES, DEFAULT_BASE_DELAY

# Add parent directory to path to import genesia backends
sys.path.insert(0, str(Path(__file__).parent.parent))

from genesia.backends.vllm import VLLMBackend

logger = logging.getLogger(__name__)


class LLMBackend:
    """
    Unified LLM backend wrapper for multi-agent system.

    Uses vLLM as the sole backend (OpenAI-compatible API, GPU-optimized).
    """

    def __init__(self, model: str, host: str = "localhost", port: Optional[int] = None,
                 max_retries: int = DEFAULT_MAX_RETRIES, retry_base_delay: float = DEFAULT_BASE_DELAY,
                 **kwargs):
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

    def generate(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """
        Generate completion from messages (adapter interface).

        Args:
            messages: List of message dicts with 'role' and 'content'
            **kwargs: Generation parameters (temperature, max_tokens, etc.)

        Returns:
            str: Generated text
        """
        temperature = kwargs.get("temperature", 0.7)
        max_tokens = kwargs.get("max_tokens", 2000)
        stop = kwargs.get("stop", None)
        chat_template_kwargs = kwargs.get("chat_template_kwargs", None)

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


def create_backend_from_config(config: Any) -> LLMBackend:
    """
    Create LLM backend from system config.

    Args:
        config: SystemConfig object

    Returns:
        LLMBackend instance
    """
    model = os.getenv("GENESIA_MODEL", config.model.model_name)
    host = os.getenv("GENESIA_BACKEND_HOST", "localhost")
    port_str = os.getenv("GENESIA_BACKEND_PORT")
    port = int(port_str) if port_str else None

    logger.info(f"Creating vLLM backend ({model})")

    # Read retry settings from config
    max_retries = DEFAULT_MAX_RETRIES
    retry_base_delay = DEFAULT_BASE_DELAY
    if hasattr(config, 'workflow'):
        max_retries = getattr(config.workflow, 'llm_max_retries', DEFAULT_MAX_RETRIES)
        retry_base_delay = getattr(config.workflow, 'llm_retry_base_delay', DEFAULT_BASE_DELAY)

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
