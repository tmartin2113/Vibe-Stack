"""
Anthropic backend implementation.

Provides integration with Anthropic's Messages API.
Uses /v1/messages for both chat and text completions (Anthropic only
exposes a messages endpoint, not a separate completions endpoint).
"""

import os
import time
import json
import logging
import requests
from typing import Dict, Any, List, Optional
from vibe.backends.base import BackendBase, BillingExhaustedError, GenerateResult


def estimate_tokens(text: str) -> int:
    """Estimate token count (~4 chars per token)."""
    return len(text) // 4


logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_ANTHROPIC_VERSION = "2023-06-01"
_MAX_RETRIES = 3


class AnthropicBackend(BackendBase):
    """Backend implementation for Anthropic Messages API."""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        # Accept host/port to satisfy BackendBase.__init__ signature
        host: str = "api.anthropic.com",
        port: int = 443,
    ):
        """
        Initialize the Anthropic backend.

        Args:
            model: Model name (default: claude-sonnet-4-20250514)
            api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            base_url: Custom base URL. Default: https://api.anthropic.com/v1
            timeout: Request timeout in seconds
            host: Passed to BackendBase (not used directly)
            port: Passed to BackendBase (not used directly)
        """
        super().__init__(host=host, port=port, timeout=timeout, model=model or _DEFAULT_MODEL)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = base_url.rstrip("/") if base_url else "https://api.anthropic.com/v1"

    @property
    def name(self) -> str:
        """Backend name."""
        return "anthropic"

    @property
    def messages_url(self) -> str:
        """Get the messages endpoint URL."""
        return f"{self.base_url}/messages"

    def _headers(self) -> Dict[str, str]:
        """Build request headers with Anthropic-specific auth."""
        return {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _handle_rate_limit(self, response: requests.Response, attempt: int) -> None:
        """
        Handle 429 rate limit responses with retry-after support.

        Args:
            response: The HTTP response
            attempt: Current retry attempt (0-indexed)

        Raises:
            TimeoutError: If all retries are exhausted
        """
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                wait_seconds = float(retry_after)
            except (ValueError, TypeError):
                wait_seconds = 2 ** attempt
        else:
            wait_seconds = 2 ** attempt

        if attempt >= _MAX_RETRIES - 1:
            raise TimeoutError(
                f"Anthropic rate limit exceeded after {_MAX_RETRIES} retries. "
                f"Last retry-after: {retry_after}"
            )

        logger.warning(
            "Anthropic rate limited (429), retrying in %.1fs (attempt %d/%d)",
            wait_seconds, attempt + 1, _MAX_RETRIES,
        )
        time.sleep(wait_seconds)

    def _extract_system_and_messages(
        self, messages: List[Dict[str, str]]
    ) -> tuple:
        """
        Extract system message and user/assistant messages from the message list.

        Anthropic requires the system prompt as a top-level parameter, not as a
        message in the list. Only user and assistant roles are allowed in messages.

        Args:
            messages: List of {role, content} dicts

        Returns:
            Tuple of (system_text or None, filtered_messages)
        """
        system_text = None
        filtered = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                # Concatenate multiple system messages if present
                if system_text is None:
                    system_text = content
                else:
                    system_text = f"{system_text}\n\n{content}"
            elif role in ("user", "assistant"):
                filtered.append({"role": role, "content": content})

        return system_text, filtered

    def _parse_response(self, result: Dict[str, Any], elapsed_ms: float, input_estimate: int) -> GenerateResult:
        """
        Parse Anthropic Messages API response into standard format.

        Args:
            result: Parsed JSON response
            elapsed_ms: Request elapsed time in milliseconds
            input_estimate: Estimated input tokens (fallback)

        Returns:
            Standardized result dict
        """
        content_blocks = result.get("content", [])
        if not content_blocks:
            raise ValueError("Empty response from Anthropic")

        # Extract text from content blocks
        text_parts = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))

        content = "\n".join(text_parts).strip()
        if not content:
            raise ValueError("Empty text in response")

        usage = result.get("usage", {})
        prompt_tokens = usage.get("input_tokens", input_estimate)
        completion_tokens = usage.get("output_tokens", estimate_tokens(content))

        return {
            "text": content,
            "tokens_used": completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "time_ms": elapsed_ms,
            "finish_reason": result.get("stop_reason", "end_turn"),
        }

    def health_check(self) -> bool:
        """
        Check if the Anthropic API is reachable.

        Verifies that an API key is configured and the API responds.
        Uses a minimal messages request with max_tokens=1 to verify auth.

        Returns:
            bool: True if healthy, False otherwise
        """
        if not self.api_key:
            logger.warning("Anthropic health check failed: no API key configured")
            return False
        try:
            response = requests.post(
                self.messages_url,
                headers=self._headers(),
                json={
                    "model": self.model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=10,
            )
            # 200 = success, 401 = bad key, anything else = server issue
            return bool(response.status_code == 200)
        except Exception as e:
            logger.debug("Anthropic health check failed: %s", e)
            return False

    def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[list] = None,
    ) -> GenerateResult:
        """
        Generate text completion by converting prompt to messages format.

        Anthropic only has a messages API, so the raw prompt is wrapped as a
        single user message.

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
        messages = [{"role": "user", "content": prompt}]
        return self._call_messages(
            messages=messages,
            system_text=None,
            max_tokens=max_tokens if max_tokens is not None else 2000,
            temperature=temperature,
            stop=stop,
            input_estimate=estimate_tokens(prompt),
        )

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

        Uses Anthropic's /v1/messages endpoint. System messages are extracted
        and passed as the top-level 'system' parameter.

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
        system_text, filtered_messages = self._extract_system_and_messages(messages)

        return self._call_messages(
            messages=filtered_messages,
            system_text=system_text,
            max_tokens=max_tokens if max_tokens is not None else 2000,
            temperature=temperature,
            stop=stop,
            input_estimate=estimate_tokens(str(messages)),
        )

    def _call_messages(
        self,
        messages: List[Dict[str, str]],
        system_text: Optional[str],
        max_tokens: int,
        temperature: Optional[float],
        stop: Optional[list],
        input_estimate: int,
    ) -> GenerateResult:
        """
        Internal method to call the Anthropic Messages API with retry logic.

        Args:
            messages: User/assistant messages (no system role)
            system_text: System prompt (passed as top-level param) or None
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            stop: Stop sequences
            input_estimate: Estimated input tokens for fallback

        Returns:
            Standardized result dict

        Raises:
            TimeoutError: If request times out or rate limit retries exhausted
            RuntimeError: If request fails
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if system_text is not None:
            payload["system"] = system_text

        if temperature is not None:
            payload["temperature"] = temperature

        if stop is not None:
            payload["stop_sequences"] = stop

        for attempt in range(_MAX_RETRIES):
            try:
                start_time = time.time()

                response = requests.post(
                    self.messages_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )

                if response.status_code == 402:
                    detail = response.text[:500]
                    raise BillingExhaustedError("anthropic", 402, detail)

                if response.status_code == 429:
                    # Distinguish credit exhaustion from temporary rate limit.
                    # Anthropic signals credit exhaustion via error type in the
                    # JSON body even on 429 responses.
                    try:
                        body = response.json()
                        err_type = body.get("error", {}).get("type", "")
                        err_msg = body.get("error", {}).get("message", "")
                    except (json.JSONDecodeError, ValueError):
                        err_type, err_msg = "", ""

                    if err_type in ("billing_error", "insufficient_credits") or \
                       "credit" in err_msg.lower() or "billing" in err_msg.lower() or \
                       "quota" in err_msg.lower():
                        raise BillingExhaustedError("anthropic", 429, err_msg or err_type)

                    self._handle_rate_limit(response, attempt)
                    continue

                response.raise_for_status()

                elapsed_ms = (time.time() - start_time) * 1000
                result = response.json()

                return self._parse_response(result, elapsed_ms, input_estimate)

            except requests.exceptions.Timeout:
                raise TimeoutError(f"Request timed out after {self.timeout} seconds")
            except TimeoutError:
                raise
            except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as e:
                if attempt < _MAX_RETRIES - 1 and isinstance(e, requests.exceptions.RequestException):
                    logger.warning(
                        "Anthropic request failed (attempt %d/%d): %s",
                        attempt + 1, _MAX_RETRIES, e,
                    )
                    continue
                raise RuntimeError(f"Error calling Anthropic: {e}")

        raise RuntimeError("Anthropic request failed after all retries")

    def __repr__(self) -> str:
        """String representation of the backend."""
        return f"AnthropicBackend(model={self.model}, base_url={self.base_url})"
