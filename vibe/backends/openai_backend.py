"""
OpenAI backend implementation.

Provides integration with OpenAI's API (and Azure OpenAI via custom base_url).
Uses /v1/chat/completions for structured messages and /v1/completions for
raw text prompts.
"""

import os
import time
import json
import logging
import requests
from typing import Dict, Any, List, Optional
from vibe.backends.base import BackendBase, GenerateResult


def estimate_tokens(text: str) -> int:
    """Estimate token count (~4 chars per token)."""
    return len(text) // 4


logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o"
_MAX_RETRIES = 3


class OpenAIBackend(BackendBase):
    """Backend implementation for OpenAI API."""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        # Accept host/port to satisfy BackendBase.__init__ signature
        host: str = "api.openai.com",
        port: int = 443,
    ):
        """
        Initialize the OpenAI backend.

        Args:
            model: Model name (default: gpt-4o)
            api_key: OpenAI API key. Falls back to OPENAI_API_KEY env var.
            base_url: Custom base URL (e.g. for Azure OpenAI). Default: https://api.openai.com/v1
            timeout: Request timeout in seconds
            host: Passed to BackendBase (not used directly)
            port: Passed to BackendBase (not used directly)
        """
        super().__init__(host=host, port=port, timeout=timeout, model=model or _DEFAULT_MODEL)
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url.rstrip("/") if base_url else "https://api.openai.com/v1"

    @property
    def name(self) -> str:
        """Backend name."""
        return "openai"

    @property
    def completion_url(self) -> str:
        """Get the text completion endpoint URL."""
        return f"{self.base_url}/completions"

    @property
    def chat_completion_url(self) -> str:
        """Get the chat completion endpoint URL."""
        return f"{self.base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        """Get the models endpoint URL."""
        return f"{self.base_url}/models"

    def _headers(self) -> Dict[str, str]:
        """Build request headers with authorization."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _handle_rate_limit(self, response: requests.Response, attempt: int) -> None:
        """
        Handle 429 rate limit responses with Retry-After support.

        Args:
            response: The HTTP response
            attempt: Current retry attempt (0-indexed)

        Raises:
            TimeoutError: If all retries are exhausted
        """
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                wait_seconds = float(retry_after)
            except (ValueError, TypeError):
                wait_seconds = 2 ** attempt
        else:
            wait_seconds = 2 ** attempt

        if attempt >= _MAX_RETRIES - 1:
            raise TimeoutError(
                f"OpenAI rate limit exceeded after {_MAX_RETRIES} retries. "
                f"Last Retry-After: {retry_after}"
            )

        logger.warning(
            "OpenAI rate limited (429), retrying in %.1fs (attempt %d/%d)",
            wait_seconds, attempt + 1, _MAX_RETRIES,
        )
        time.sleep(wait_seconds)

    def health_check(self) -> bool:
        """
        Check if the OpenAI API is reachable by listing models.

        Returns:
            bool: True if healthy, False otherwise
        """
        if not self.api_key:
            logger.warning("OpenAI health check failed: no API key configured")
            return False
        try:
            response = requests.get(
                self.models_url,
                headers=self._headers(),
                timeout=10,
            )
            return bool(response.status_code == 200)
        except Exception as e:
            logger.debug("OpenAI health check failed: %s", e)
            return False

    def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[list] = None,
    ) -> GenerateResult:
        """
        Generate text completion using OpenAI /v1/completions endpoint.

        Args:
            prompt: Input prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            stop: Stop sequences

        Returns:
            Dict containing 'text', 'tokens_used', 'prompt_tokens',
            'completion_tokens', 'time_ms', 'finish_reason'

        Raises:
            TimeoutError: If request times out or rate limit retries exhausted
            RuntimeError: If request fails
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "temperature": temperature if temperature is not None else 0.7,
            "max_tokens": max_tokens if max_tokens is not None else 2000,
        }

        if stop is not None:
            payload["stop"] = stop

        for attempt in range(_MAX_RETRIES):
            try:
                start_time = time.time()

                response = requests.post(
                    self.completion_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )

                if response.status_code == 429:
                    self._handle_rate_limit(response, attempt)
                    continue

                response.raise_for_status()

                elapsed_ms = (time.time() - start_time) * 1000
                result = response.json()

                choices = result.get("choices", [])
                if not choices:
                    raise ValueError("Empty response from OpenAI")

                content = choices[0].get("text", "").strip()
                if not content:
                    raise ValueError("Empty text in response")

                usage = result.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", estimate_tokens(prompt))
                completion_tokens = usage.get("completion_tokens", estimate_tokens(content))

                return {
                    "text": content,
                    "tokens_used": completion_tokens,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "time_ms": elapsed_ms,
                    "finish_reason": choices[0].get("finish_reason", "stop"),
                }

            except requests.exceptions.Timeout:
                raise TimeoutError(f"Request timed out after {self.timeout} seconds")
            except TimeoutError:
                raise
            except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as e:
                if attempt < _MAX_RETRIES - 1 and isinstance(e, requests.exceptions.RequestException):
                    logger.warning("OpenAI request failed (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, e)
                    continue
                raise RuntimeError(f"Error calling OpenAI: {e}")

        raise RuntimeError("OpenAI request failed after all retries")

    def generate_chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[list] = None,
        chat_template_kwargs: Optional[Dict[str, Any]] = None,
    ) -> GenerateResult:
        """
        Generate chat completion using structured messages.

        Uses OpenAI's /v1/chat/completions endpoint.

        Args:
            messages: List of {role, content} dicts with system/user/assistant roles
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            stop: Stop sequences
            chat_template_kwargs: Ignored (kept for interface compatibility)

        Returns:
            Dict containing 'text', 'tokens_used', 'prompt_tokens',
            'completion_tokens', 'time_ms', 'finish_reason'

        Raises:
            TimeoutError: If request times out or rate limit retries exhausted
            RuntimeError: If request fails
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else 0.7,
        }

        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        if stop is not None:
            payload["stop"] = stop

        for attempt in range(_MAX_RETRIES):
            try:
                start_time = time.time()

                response = requests.post(
                    self.chat_completion_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )

                if response.status_code == 429:
                    self._handle_rate_limit(response, attempt)
                    continue

                response.raise_for_status()

                elapsed_ms = (time.time() - start_time) * 1000
                result = response.json()

                choices = result.get("choices", [])
                if not choices:
                    raise ValueError("Empty response from OpenAI")

                content = choices[0].get("message", {}).get("content", "").strip()
                if not content:
                    raise ValueError("Empty content in response")

                usage = result.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", estimate_tokens(str(messages)))
                completion_tokens = usage.get("completion_tokens", estimate_tokens(content))

                return {
                    "text": content,
                    "tokens_used": completion_tokens,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "time_ms": elapsed_ms,
                    "finish_reason": choices[0].get("finish_reason", "stop"),
                }

            except requests.exceptions.Timeout:
                raise TimeoutError(f"Request timed out after {self.timeout} seconds")
            except TimeoutError:
                raise
            except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as e:
                if attempt < _MAX_RETRIES - 1 and isinstance(e, requests.exceptions.RequestException):
                    logger.warning("OpenAI chat request failed (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, e)
                    continue
                raise RuntimeError(f"Error calling OpenAI: {e}")

        raise RuntimeError("OpenAI chat request failed after all retries")

    def __repr__(self) -> str:
        """String representation of the backend."""
        return f"OpenAIBackend(model={self.model}, base_url={self.base_url})"
