"""
Progress reporting for Paperclip heartbeat workflows.

Posts periodic status comments to Paperclip at key workflow nodes so that
users can track execution progress instead of seeing silence between
in_progress and completion.
"""

import logging
import os
from typing import Any, Callable, Dict

from .paperclip_client import PaperclipAPIError, PaperclipClient

logger = logging.getLogger(__name__)

# Nodes that trigger a Paperclip progress comment.
# Mapping: node_name -> human-readable label.
PROGRESS_NODES: Dict[str, str] = {
    "specialist": "Running specialist",
    "heuristic_critic": "Evaluating output (heuristic)",
    "critic_output": "Evaluating output (LLM critic)",
    "vibe": "Building specification",
    "router": "Routing task",
}


def make_progress_callback(
    client: PaperclipClient,
    issue_id: str,
) -> Callable[[str, Dict[str, Any]], None]:
    """
    Return a callback that posts progress comments to Paperclip.

    Only fires for key nodes (specialist, critic) to avoid spamming.
    Progress updates are best-effort — API failures are silently ignored.
    """

    def _on_node_complete(node_name: str, state: Dict[str, Any]) -> None:
        label = PROGRESS_NODES.get(node_name)
        if label is None:
            return

        iteration = state.get("iteration_count", 0)
        max_iter = state.get("max_iterations", 3)
        score = state.get("output_critic_score") or state.get("heuristic_critic_score") or 0

        parts = [f"**{label}**"]
        if node_name == "specialist":
            parts.append(f"(iteration {iteration + 1}/{max_iter})")
        if score and node_name in ("heuristic_critic", "critic_output"):
            parts.append(f"— score: {score}/100")

        comment = f"_Progress:_ {' '.join(parts)}"

        try:
            client.add_comment(issue_id, comment)
        except PaperclipAPIError:
            # Progress updates are best-effort — don't fail the workflow
            pass

        # Best-effort dual-write to MessageStore
        try:
            from .message_store import get_shared_message_store
            from .message_types import MessageType

            store = get_shared_message_store()
            store.send(
                content=comment,
                sender=os.environ.get("VIBE_AGENT_NAME", "vibe"),
                msg_type=MessageType.STATUS,
                topic=f"progress:{issue_id}",
                issue_id=issue_id,
                metadata={"node": node_name, "iteration": iteration, "score": score},
                ttl_seconds=3600,  # 1 hour — progress is ephemeral
            )
        except Exception:
            pass  # Best-effort, same as Paperclip write

    return _on_node_complete
