"""
BackendPool — Multi-backend load balancing and failover.

Wraps multiple BackendBase instances and exposes the same generate()/health_check()
interface as LLMBackend, making it a drop-in replacement.

Features:
- Strategies: failover, round_robin, least_loaded
- Circuit breaker per backend (closed -> open -> half-open -> closed)
- Thread-safe for concurrent specialist threads
- Transparent failover across healthy backends
"""

import logging
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from vibe.backends.base import BackendBase
from .llm_retry import retry_llm_call, DEFAULT_MAX_RETRIES, DEFAULT_BASE_DELAY

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"        # Normal operation
    OPEN = "open"            # Backend marked unhealthy, no requests sent
    HALF_OPEN = "half_open"  # Recovery probe: one request allowed through


class BackendEntry:
    """Tracks per-backend state for the pool."""

    def __init__(self, backend: BackendBase, recovery_timeout: float,
                 max_consecutive_failures: int):
        self.backend = backend
        self.recovery_timeout = recovery_timeout
        self.max_consecutive_failures = max_consecutive_failures

        self.circuit_state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.last_failure_time: float = 0.0
        self.total_inflight = 0  # for least_loaded strategy

        self._lock = threading.Lock()

    @property
    def is_available(self) -> bool:
        """Check if backend can accept requests."""
        with self._lock:
            if self.circuit_state == CircuitState.CLOSED:
                return True
            if self.circuit_state == CircuitState.OPEN:
                # Check if recovery timeout has elapsed
                if time.monotonic() - self.last_failure_time >= self.recovery_timeout:
                    self.circuit_state = CircuitState.HALF_OPEN
                    logger.info(
                        "Circuit breaker half-open for %s — allowing probe request",
                        self.backend,
                    )
                    return True
                return False
            # HALF_OPEN: allow one probe
            return True

    def record_success(self) -> None:
        with self._lock:
            if self.circuit_state == CircuitState.HALF_OPEN:
                logger.info(
                    "Circuit breaker closed for %s — probe succeeded", self.backend
                )
            self.circuit_state = CircuitState.CLOSED
            self.consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            self.last_failure_time = time.monotonic()
            if self.circuit_state == CircuitState.HALF_OPEN:
                # Probe failed — back to open
                self.circuit_state = CircuitState.OPEN
                logger.warning(
                    "Circuit breaker re-opened for %s — probe failed", self.backend
                )
            elif self.consecutive_failures >= self.max_consecutive_failures:
                if self.circuit_state != CircuitState.OPEN:
                    self.circuit_state = CircuitState.OPEN
                    logger.warning(
                        "Circuit breaker opened for %s after %d consecutive failures",
                        self.backend,
                        self.consecutive_failures,
                    )

    def status_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "backend": repr(self.backend),
                "circuit_state": self.circuit_state.value,
                "consecutive_failures": self.consecutive_failures,
                "inflight": self.total_inflight,
            }


class BackendPool:
    """
    Multi-backend pool with load balancing and failover.

    Drop-in replacement for LLMBackend — exposes the same
    generate(messages, **kwargs) -> str and health_check() -> bool interface.
    """

    def __init__(
        self,
        backends: List[BackendBase],
        strategy: str = "failover",
        max_consecutive_failures: int = 3,
        recovery_timeout: float = 60,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay: float = DEFAULT_BASE_DELAY,
    ):
        if not backends:
            raise ValueError("BackendPool requires at least one backend")

        self.strategy = strategy
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay

        self._entries = [
            BackendEntry(b, recovery_timeout, max_consecutive_failures)
            for b in backends
        ]
        self._rr_index = 0
        self._rr_lock = threading.Lock()

        # Expose attributes expected by callers of LLMBackend
        primary = backends[0]
        self.model_name = getattr(primary, "model", None) or ""
        self.backend_type = getattr(primary, "name", "pool")

        logger.info(
            "BackendPool initialised: strategy=%s, backends=%d, "
            "max_consecutive_failures=%d, recovery_timeout=%.0fs",
            strategy,
            len(backends),
            max_consecutive_failures,
            recovery_timeout,
        )

    # ------------------------------------------------------------------
    # Strategy: select which entries to try, in order
    # ------------------------------------------------------------------

    def _select_order(self) -> List[BackendEntry]:
        """Return entries in the order dictated by the current strategy."""
        available = [e for e in self._entries if e.is_available]
        if not available:
            # All circuits open — return all so caller can report proper error
            return list(self._entries)

        if self.strategy == "round_robin":
            with self._rr_lock:
                idx = self._rr_index % len(available)
                self._rr_index += 1
            return available[idx:] + available[:idx]

        if self.strategy == "least_loaded":
            return sorted(available, key=lambda e: e.total_inflight)

        # Default: failover — prefer order as given (primary first)
        return available

    # ------------------------------------------------------------------
    # Public interface (mirrors LLMBackend)
    # ------------------------------------------------------------------

    def generate(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """
        Generate a completion, trying backends according to the pool strategy.

        On failure the pool transparently retries on the next healthy backend.
        Each individual backend call uses the per-call retry logic from llm_retry.
        """
        temperature = kwargs.get("temperature", 0.7)
        max_tokens = kwargs.get("max_tokens", 2000)
        stop = kwargs.get("stop", None)
        chat_template_kwargs = kwargs.get("chat_template_kwargs", None)

        order = self._select_order()
        last_error: Optional[Exception] = None

        for entry in order:
            if not entry.is_available:
                continue
            backend = entry.backend
            logger.debug("BackendPool selecting %s", backend)

            entry.total_inflight += 1
            try:
                result = retry_llm_call(
                    backend.generate_chat,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stop=stop,
                    chat_template_kwargs=chat_template_kwargs,
                    max_retries=self.max_retries,
                    base_delay=self.retry_base_delay,
                )
                entry.record_success()
                return result["text"]  # type: ignore[index]
            except Exception as exc:
                last_error = exc
                entry.record_failure()
                logger.warning(
                    "BackendPool: %s failed (%s), trying next backend",
                    backend,
                    exc,
                )
            finally:
                entry.total_inflight -= 1

        # All backends exhausted
        raise RuntimeError(
            f"BackendPool: all {len(self._entries)} backends failed. "
            f"Last error: {last_error}"
        )

    def health_check(self) -> bool:
        """Return True if at least one backend is healthy."""
        for entry in self._entries:
            try:
                if entry.backend.health_check():
                    return True
            except Exception:
                pass
        return False

    def pool_status(self) -> List[Dict[str, Any]]:
        """Return health/state snapshot of every backend in the pool."""
        return [entry.status_dict() for entry in self._entries]
