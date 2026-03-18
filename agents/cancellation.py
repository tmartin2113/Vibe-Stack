"""
Cooperative Cancellation for Workflow Execution

Provides a thread-safe cancellation token that the graph checks between
nodes. A background poller watches the Paperclip issue status; if the
issue transitions to 'cancelled', the token fires and the graph stops
at the next node boundary.

Usage in heartbeat:
    token = CancellationToken()
    poller = start_cancellation_poller(client, issue_id, token)
    try:
        graph.stream(state, cancellation_token=token)
    finally:
        poller.stop()
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class WorkflowCancelledError(Exception):
    """Raised when a workflow is cancelled via CancellationToken."""

    def __init__(self, reason: str = "cancelled by user"):
        self.reason = reason
        super().__init__(f"Workflow cancelled: {reason}")


class CancellationToken:
    """
    Thread-safe cancellation flag.

    Call cancel() from any thread to signal cancellation.
    Call check() from the workflow thread to raise WorkflowCancelledError
    if cancelled. Use is_cancelled for non-raising checks.
    """

    def __init__(self):
        self._event = threading.Event()
        self._reason: str = "cancelled by user"

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "cancelled by user") -> None:
        """Signal cancellation. Thread-safe, idempotent."""
        self._reason = reason
        self._event.set()

    def check(self) -> None:
        """Raise WorkflowCancelledError if cancelled."""
        if self._event.is_set():
            raise WorkflowCancelledError(self._reason)


class CancellationPoller:
    """
    Background thread that polls Paperclip issue status for cancellation.

    Polls every `interval` seconds. If the issue status is 'cancelled',
    fires the cancellation token. Stops automatically on token fire,
    explicit stop(), or if the Paperclip API is unreachable.
    """

    def __init__(
        self,
        client,  # PaperclipClient
        issue_id: str,
        token: CancellationToken,
        interval: float = 15.0,
        max_errors: int = 3,
    ):
        self._client = client
        self._issue_id = issue_id
        self._token = token
        self._interval = interval
        self._max_errors = max_errors
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the polling thread."""
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"cancel-poller-{self._issue_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the polling thread to stop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _poll_loop(self) -> None:
        """Poll issue status until cancelled, stopped, or too many errors."""
        consecutive_errors = 0

        while not self._stop_event.is_set() and not self._token.is_cancelled:
            # Wait for interval or stop signal
            if self._stop_event.wait(timeout=self._interval):
                break  # stop() was called

            try:
                issue = self._client.get_issue(self._issue_id)
                consecutive_errors = 0

                if issue.status == "cancelled":
                    logger.info(
                        "Issue %s cancelled — firing cancellation token",
                        self._issue_id,
                    )
                    self._token.cancel("issue cancelled in Paperclip")
                    return

            except Exception as e:
                consecutive_errors += 1
                logger.debug(
                    "Cancellation poll error for %s (attempt %d/%d): %s",
                    self._issue_id, consecutive_errors, self._max_errors, e,
                )
                if consecutive_errors >= self._max_errors:
                    logger.warning(
                        "Cancellation poller for %s stopping after %d consecutive errors",
                        self._issue_id, self._max_errors,
                    )
                    return


def start_cancellation_poller(
    client,
    issue_id: str,
    token: CancellationToken,
    interval: float = 15.0,
) -> CancellationPoller:
    """Create and start a cancellation poller. Returns the poller for cleanup."""
    poller = CancellationPoller(client, issue_id, token, interval=interval)
    poller.start()
    return poller
