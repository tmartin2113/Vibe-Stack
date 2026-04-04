"""
LLM Retry with Exponential Backoff

Provides retry logic for LLM backend calls that handles:
- Transient HTTP errors (429 rate limit, 500/502/503/504 server errors)
- Timeout errors from any backend
- Retry-After header respect for rate-limited responses
- Exponential backoff with jitter to avoid thundering herd
- Non-retryable errors pass through immediately (auth, bad request, parse)
"""

import logging
import random
import time
from typing import Callable, Optional, TypeVar, Set

import requests

from vibe.backends.base import BillingExhaustedError
from .metrics import metrics as app_metrics

logger = logging.getLogger(__name__)

T = TypeVar("T")

# HTTP status codes that are safe to retry
RETRYABLE_STATUS_CODES: Set[int] = {429, 500, 502, 503, 504}

# Default retry parameters
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds cap


class LLMRetryExhausted(RuntimeError):
    """Raised when all retry attempts are exhausted."""

    def __init__(self, attempts: int, last_error: Exception):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"LLM call failed after {attempts} attempts. Last error: {last_error}"
        )


def _extract_retry_after(error: Exception) -> Optional[float]:
    """
    Extract Retry-After header value from a requests exception.

    Returns:
        Seconds to wait, or None if header not present.
    """
    response = getattr(error, "response", None)
    if response is None:
        return None

    retry_after = None
    if hasattr(response, "headers"):
        retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")

    if retry_after is None:
        return None

    try:
        return float(retry_after)
    except (ValueError, TypeError):
        return None


def _is_retryable(error: Exception) -> bool:
    """
    Determine if an error is transient and worth retrying.

    Retryable:
        - TimeoutError (any backend)
        - requests.exceptions.ConnectionError (server down/unreachable)
        - requests.exceptions.Timeout
        - RuntimeError wrapping a retryable HTTP status (429, 5xx)

    Not retryable:
        - 400 Bad Request (malformed payload)
        - 401/403 Auth errors (wrong API key)
        - 404 Not Found (wrong model/endpoint)
        - ValueError / KeyError / JSONDecodeError (response parse failures)
    """
    # Billing exhaustion — never retry
    if isinstance(error, BillingExhaustedError):
        return False

    # Direct timeout — always retry
    if isinstance(error, TimeoutError):
        return True

    # requests connection/timeout errors — always retry
    if isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True

    # RuntimeError from backends — check if it wraps a retryable HTTP error
    if isinstance(error, RuntimeError):
        # Check if there's an embedded HTTP response with a retryable status
        cause = error.__cause__ or error.__context__
        if isinstance(cause, requests.exceptions.HTTPError):
            response = getattr(cause, "response", None)
            if response is not None and response.status_code in RETRYABLE_STATUS_CODES:
                return True

        # Heuristic: check error message for retryable status codes
        msg = str(error).lower()
        for code in RETRYABLE_STATUS_CODES:
            if str(code) in msg:
                return True

        # Connection-related messages
        if any(phrase in msg for phrase in ["connection", "timed out", "timeout", "unavailable"]):
            return True

    return False


def _compute_delay(attempt: int, base_delay: float, max_delay: float, retry_after: Optional[float]) -> float:
    """
    Compute delay for the next retry attempt.

    Uses exponential backoff with full jitter, capped at max_delay.
    If Retry-After header is present, uses the larger of computed delay
    and the server-requested delay.
    """
    # Exponential backoff: base_delay * 2^attempt
    exp_delay = base_delay * (2 ** attempt)
    # Full jitter: random between 0 and exp_delay
    jittered = random.uniform(0, exp_delay)
    # Cap at max_delay
    delay = min(jittered, max_delay)

    # Respect Retry-After if present (use the larger value)
    if retry_after is not None:
        delay = max(delay, min(retry_after, max_delay))

    return delay


def retry_llm_call(
    fn: Callable[..., T],
    *args,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    **kwargs,
) -> T:
    """
    Execute an LLM backend call with retry on transient failures.

    Args:
        fn: The callable to execute (e.g., backend.generate)
        *args: Positional arguments passed to fn
        max_retries: Maximum number of retry attempts (0 = no retries)
        base_delay: Base delay in seconds for exponential backoff
        max_delay: Maximum delay cap in seconds
        **kwargs: Keyword arguments passed to fn

    Returns:
        The return value of fn

    Raises:
        LLMRetryExhausted: If all retry attempts are exhausted
        Exception: Non-retryable errors are raised immediately
    """
    last_error: Optional[Exception] = None
    call_start = time.monotonic()

    for attempt in range(max_retries + 1):
        try:
            result = fn(*args, **kwargs)
            duration = time.monotonic() - call_start
            app_metrics.increment("vibe_llm_calls_total", labels={"status": "success"})
            app_metrics.observe("vibe_llm_call_duration_seconds", duration)
            return result
        except Exception as e:
            last_error = e

            # Non-retryable error — raise immediately
            if not _is_retryable(e):
                duration = time.monotonic() - call_start
                app_metrics.increment("vibe_llm_calls_total", labels={"status": "error"})
                app_metrics.observe("vibe_llm_call_duration_seconds", duration)
                raise

            # Record retry
            if attempt < max_retries:
                app_metrics.increment("vibe_llm_retries_total", labels={"error": type(e).__name__})

            # Last attempt — don't sleep, just raise
            if attempt >= max_retries:
                break

            # Compute delay
            retry_after = _extract_retry_after(e)
            delay = _compute_delay(attempt, base_delay, max_delay, retry_after)

            logger.warning(
                "LLM call failed (attempt %d/%d): %s. Retrying in %.1fs...",
                attempt + 1,
                max_retries + 1,
                e,
                delay,
            )

            time.sleep(delay)

    duration = time.monotonic() - call_start
    app_metrics.increment("vibe_llm_calls_total", labels={"status": "exhausted"})
    app_metrics.observe("vibe_llm_call_duration_seconds", duration)
    assert last_error is not None  # loop always sets last_error before break
    raise LLMRetryExhausted(attempts=max_retries + 1, last_error=last_error)
