"""
SIGTERM graceful shutdown for Paperclip heartbeat workflows.

When Paperclip kills the container mid-workflow, this module catches the
signal, posts partial results to the Paperclip issue, and raises a clean
exception so the heartbeat can exit gracefully.
"""

import logging
import signal
from typing import Any, Dict

from .paperclip_client import PaperclipAPIError, PaperclipClient

logger = logging.getLogger(__name__)


class SigtermReceived(BaseException):
    """Raised in the main thread when SIGTERM is received during workflow."""


_previous_sigterm_handler: Any = None


def install_sigterm_handler(
    client: PaperclipClient,
    issue_id: str,
    partial_state: Dict[str, Any],
) -> None:
    """Install a SIGTERM handler that raises SigtermReceived in the main thread."""
    global _previous_sigterm_handler

    def _handler(signum: int, frame: Any) -> None:
        raise SigtermReceived()

    _previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _handler)


def restore_sigterm_handler() -> None:
    """Restore the previous SIGTERM handler."""
    global _previous_sigterm_handler
    if _previous_sigterm_handler is not None:
        signal.signal(signal.SIGTERM, _previous_sigterm_handler)
        _previous_sigterm_handler = None


def post_sigterm_partial(
    client: PaperclipClient,
    issue_id: str,
    partial_state: Dict[str, Any],
) -> None:
    """Post partial results to Paperclip when SIGTERM interrupts the workflow."""
    output = partial_state.get("current_output", "") or partial_state.get(
        "specialist_output", ""
    )
    score = partial_state.get("critic_score", 0) or partial_state.get(
        "heuristic_critic_score", 0
    )
    last_node = partial_state.get("last_node", "unknown")

    truncated = output[:2000] if output else "No output yet"
    comment = (
        f"## Interrupted (SIGTERM)\n\n"
        f"The agent was shut down before completing the workflow.\n\n"
        f"**Last step:** {last_node}\n"
        f"**Score so far:** {score}/100\n\n"
        f"### Partial output\n\n{truncated}\n\n"
        f"**This issue has been set to blocked for retry.**"
    )
    try:
        client.update_issue(issue_id, status="blocked", comment=comment)
    except PaperclipAPIError as e:
        logger.warning("Failed to post SIGTERM partial result (non-fatal): %s", e)
