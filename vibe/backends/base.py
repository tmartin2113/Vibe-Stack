"""
Abstract backend interface for LLM providers.

Defines the common interface that all backend implementations must follow.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, TypedDict


class GenerateResult(TypedDict):
    """Structured result from LLM generation."""
    text: str
    tokens_used: int
    prompt_tokens: int
    completion_tokens: int
    time_ms: float
    finish_reason: str


class BillingExhaustedError(RuntimeError):
    """Raised when the API provider reports billing/credit exhaustion.

    This is a fatal, non-retryable error that should permanently halt
    agent execution until the subscription is renewed.
    """

    def __init__(self, provider: str, status_code: int, detail: str = ""):
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        super().__init__(
            f"{provider} billing exhausted (HTTP {status_code}): {detail}"
        )


class BackendBase(ABC):
    """
    Abstract base class for LLM backend implementations.

    Backend implementations must inherit from this class and implement
    its methods.
    """

    def __init__(self, host: str, port: int, timeout: int = 60, model: Optional[str] = None):
        """
        Initialize the backend.

        Args:
            host: Server host
            port: Server port
            timeout: Request timeout in seconds
            model: Model name (backend-specific)
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.model = model
        self.base_url = f"http://{host}:{port}"

    @abstractmethod
    def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None
    ) -> GenerateResult:
        """
        Generate text completion from the LLM.

        Args:
            prompt: Input prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            stop: Stop sequences

        Returns:
            GenerateResult containing 'text', 'tokens_used', 'prompt_tokens',
            'completion_tokens', 'time_ms', and 'finish_reason'.
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        Check if the backend is healthy and responding.

        Returns:
            bool: True if healthy, False otherwise
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Get the backend name.

        Returns:
            str: Backend identifier (e.g., 'vllm')
        """
        pass

    @property
    def completion_url(self) -> str:
        """Get the completion endpoint URL."""
        return f"{self.base_url}/v1/completions"

    def __repr__(self) -> str:
        """String representation of the backend."""
        return f"{self.__class__.__name__}(host={self.host}, port={self.port}, model={self.model})"
