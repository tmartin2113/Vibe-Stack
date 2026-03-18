"""
Abstract backend interface for LLM providers.

Defines the common interface that all backend implementations must follow.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


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
        stop: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Generate text completion from the LLM.

        Args:
            prompt: Input prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            stop: Stop sequences

        Returns:
            Dict containing:
                - 'text': Generated text
                - 'tokens_used': Number of tokens generated (if available)
                - 'finish_reason': Reason for completion (if available)
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
