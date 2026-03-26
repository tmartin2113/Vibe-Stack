"""
vLLM backend implementation.

Provides integration with vLLM OpenAI-compatible API.
Uses /v1/chat/completions for structured messages (preserving system prompts)
and /v1/completions as a fallback for raw text prompts.
"""

import time
import json
import logging
import requests
from typing import Dict, Any, List, Optional
from vibe.backends.base import BackendBase


def estimate_tokens(text: str) -> int:
    """Estimate token count (~4 chars per token)."""
    return len(text) // 4

logger = logging.getLogger(__name__)


class VLLMBackend(BackendBase):
    """Backend implementation for vLLM server (OpenAI-compatible API)."""

    @property
    def name(self) -> str:
        """Backend name."""
        return "vllm"

    @property
    def completion_url(self) -> str:
        """Get the text completion endpoint URL."""
        return f"{self.base_url}/v1/completions"

    @property
    def chat_completion_url(self) -> str:
        """Get the chat completion endpoint URL."""
        return f"{self.base_url}/v1/chat/completions"

    @property
    def health_url(self) -> str:
        """Get the health check endpoint URL."""
        return f"{self.base_url}/health"

    def health_check(self) -> bool:
        """
        Check if vLLM server is healthy.

        Returns:
            bool: True if healthy, False otherwise
        """
        try:
            response = requests.get(self.health_url, timeout=5)
            return bool(response.status_code == 200)
        except Exception:
            # Try models endpoint as fallback
            try:
                response = requests.get(f"{self.base_url}/v1/models", timeout=5)
                return bool(response.status_code == 200)
            except Exception:
                return False

    def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[list] = None
    ) -> Dict[str, Any]:
        """
        Generate text completion using vLLM /v1/completions endpoint.

        This is the raw text completion path. Prefer generate_chat() for
        structured messages to preserve system prompts and use the model's
        native chat template.

        Args:
            prompt: Input prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            stop: Stop sequences

        Returns:
            Dict containing 'text', 'tokens_used', 'time_ms', 'finish_reason'

        Raises:
            TimeoutError: If request times out
            RuntimeError: If request fails
        """
        try:
            start_time = time.time()

            # Default stop sequences (standardized across all backends)
            if stop is None:
                stop = [
                    "REQUEST:", "ANALYSIS:", "Using this", "INPUT:", "OUTPUT:", "Input:", "Output:",
                    "\n\nINPUT:", "\n\nOUTPUT:", "\n\nRequest:", "\n\nAnalysis:",
                    "---", "###", "EXAMPLE:", "Example:",
                    "\n\n\n\n", "\n\n\n\n\n", "\n\n\n\n\n\n",  # Multiple newlines indicate stuck generation
                    "...\n\n...", "---\n\n---"  # Repetitive patterns
                ]

            payload = {
                "prompt": prompt,
                "temperature": temperature if temperature is not None else 0.7,
                "max_tokens": max_tokens if max_tokens is not None else 2000,
                "stop": stop,
                # Use only OpenAI-compatible additive penalties (not stacked with repetition_penalty)
                "presence_penalty": 0.3,
                "frequency_penalty": 0.3
            }

            # Add model if specified
            if self.model:
                payload["model"] = self.model

            response = requests.post(
                self.completion_url,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()

            elapsed_ms = (time.time() - start_time) * 1000
            result = response.json()

            # vLLM uses OpenAI-compatible format
            choices = result.get("choices", [])
            if not choices:
                raise ValueError("Empty response from vLLM")

            content = choices[0].get("text", "").strip()
            if not content:
                raise ValueError("Empty text in response")

            # Get token usage
            usage = result.get("usage", {})
            tokens = usage.get("completion_tokens", estimate_tokens(content))
            prompt_tokens = usage.get("prompt_tokens", estimate_tokens(prompt))

            return {
                "text": content,
                "tokens_used": tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": tokens,
                "time_ms": elapsed_ms,
                "finish_reason": choices[0].get("finish_reason", "stop")
            }

        except requests.exceptions.Timeout:
            raise TimeoutError(f"Request timed out after {self.timeout} seconds")
        except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Error calling vLLM: {e}")

    def generate_chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[list] = None,
        chat_template_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Generate chat completion using structured messages.

        Uses vLLM's /v1/chat/completions endpoint which applies the model's
        native chat template, preserving system prompts properly.

        Args:
            messages: List of {role, content} dicts with system/user/assistant roles
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            stop: Stop sequences

        Returns:
            Dict containing 'text', 'tokens_used', 'time_ms', 'finish_reason'

        Raises:
            TimeoutError: If request times out
            RuntimeError: If request fails
        """
        try:
            start_time = time.time()

            payload: Dict[str, Any] = {
                "messages": messages,
                "temperature": temperature if temperature is not None else 0.7,
                "presence_penalty": 0.3,
                "frequency_penalty": 0.3
            }

            if max_tokens is not None:
                payload["max_tokens"] = max_tokens

            if stop is not None:
                payload["stop"] = stop

            if self.model:
                payload["model"] = self.model

            if chat_template_kwargs:
                payload["chat_template_kwargs"] = chat_template_kwargs

            response = requests.post(
                self.chat_completion_url,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()

            elapsed_ms = (time.time() - start_time) * 1000
            result = response.json()

            choices = result.get("choices", [])
            if not choices:
                raise ValueError("Empty response from vLLM")

            content = choices[0].get("message", {}).get("content", "").strip()
            if not content:
                raise ValueError("Empty content in response")

            usage = result.get("usage", {})
            tokens = usage.get("completion_tokens", estimate_tokens(content))
            prompt_tokens = usage.get("prompt_tokens", estimate_tokens(str(messages)))

            return {
                "text": content,
                "tokens_used": tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": tokens,
                "time_ms": elapsed_ms,
                "finish_reason": choices[0].get("finish_reason", "stop")
            }

        except requests.exceptions.Timeout:
            raise TimeoutError(f"Request timed out after {self.timeout} seconds")
        except (requests.exceptions.RequestException, ValueError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Error calling vLLM: {e}")
