"""Inter-Agent Bulletin Board V2 — SQLite-backed message board with structured types.

Backward-compatible upgrade from v1 (file-based BULLETIN.md).  Now uses
MessageStore for structured storage, FTS5 search, threading, and read tracking.

Activates when ``MESSAGE_STORE_PATH`` or ``BULLETIN_PATH`` env var is set.

Actions: post, read, search, reply, thread, stats
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


def _get_agent_name() -> str:
    """Determine the current agent's display name."""
    return (
        os.environ.get("VIBE_AGENT_NAME")
        or os.environ.get("PAPERCLIP_AGENT_ID")
        or "unknown-agent"
    )


def _is_configured() -> bool:
    """Check if the bulletin board / message store is configured."""
    return bool(
        os.environ.get("MESSAGE_STORE_PATH")
        or os.environ.get("BULLETIN_PATH")
    )


def _get_store():
    """Get the shared MessageStore instance."""
    from ..message_store import get_shared_message_store

    return get_shared_message_store()


def read_recent_entries(limit: int = 10) -> str:
    """Read recent messages, formatted for context injection.

    Backward-compatible with the v1 function signature used by
    ``inject_memory`` in ``graph.py``.

    Returns empty string if not configured or no entries.
    """
    if not _is_configured():
        return ""

    try:
        store = _get_store()
        agent = _get_agent_name()
        messages = store.relevant_messages(
            query="",
            agent_name=agent,
            max_results=limit,
        )
        if not messages:
            return ""

        lines = []
        for msg in messages:
            lines.append(msg.format_for_context())

        return (
            "\n\n## Bulletin Board (Recent Messages)\n\n"
            + "\n".join(lines)
        )
    except Exception as exc:
        logger.debug(f"Bulletin read for injection skipped: {exc}")
        return ""


class BulletinBoardTool(Tool):
    """Shared inter-agent bulletin board for posting and reading messages.

    Uses MessageStore (SQLite + FTS5) for structured message storage with
    typed messages, threading, search, and read tracking.

    Actions: post, read, search, reply, thread, stats

    Requires ``MESSAGE_STORE_PATH`` or ``BULLETIN_PATH`` environment variable.
    """

    def __init__(self):
        super().__init__(
            name="bulletin_board",
            description=(
                "Post and read messages on a shared inter-agent bulletin board. "
                "Use to leave notes, share decisions, flag blockers, or coordinate "
                "work across agent sessions. "
                "Actions: post, read, search, reply, thread, stats."
            ),
            category=ToolCategory.SPECIALIZED,
        )
        self._bridge = None

    def _get_bridge(self):
        if self._bridge is None:
            from ..message_store import PaperclipBridge

            self._bridge = PaperclipBridge()
        return self._bridge

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Bulletin action: 'post' (add a message), "
                        "'read' (get recent entries), 'search' (find by keyword), "
                        "'reply' (reply to a message), 'thread' (get message thread), "
                        "'stats' (message store statistics)"
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "The message to post (required for 'post' and 'reply')",
                },
                "topic": {
                    "type": "string",
                    "description": "Optional topic tag for the post (e.g. 'architecture', 'blocker')",
                    "default": "",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of recent entries to return (for 'read' action)",
                    "default": 20,
                },
                "query": {
                    "type": "string",
                    "description": "Search keyword (for 'search' action)",
                },
                "msg_type": {
                    "type": "string",
                    "description": (
                        "Message type: 'info' (default), 'decision', 'blocker', "
                        "'handoff', 'status', 'question', 'completion'"
                    ),
                    "default": "info",
                },
                "recipient": {
                    "type": "string",
                    "description": "Target agent name, or '*' for broadcast (default)",
                    "default": "*",
                },
                "reply_to": {
                    "type": "string",
                    "description": "Message ID to reply to (for 'reply' action)",
                },
                "message_id": {
                    "type": "string",
                    "description": "Message ID (for 'thread' action)",
                },
                "ttl_seconds": {
                    "type": "integer",
                    "description": "Time-to-live in seconds (0 = never expire)",
                },
                "metadata": {
                    "type": "object",
                    "description": "Additional structured metadata for the message",
                },
                "type_filter": {
                    "type": "string",
                    "description": "Comma-separated message types to filter by (for 'read')",
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Only return unread messages (for 'read')",
                    "default": False,
                },
            },
            "required": ["action"],
        }

    def execute(self, **kwargs) -> ToolResult:
        action = kwargs.get("action", "").strip().lower()
        if not action:
            return ToolResult(
                success=False, output="",
                error="No action specified. Use: post, read, search, reply, thread, stats",
            )

        if not _is_configured():
            return ToolResult(
                success=False, output="",
                error="MESSAGE_STORE_PATH or BULLETIN_PATH not configured. Bulletin board is disabled.",
            )

        try:
            store = _get_store()
        except Exception as exc:
            return ToolResult(
                success=False, output="",
                error=f"Failed to initialize message store: {exc}",
            )

        dispatch = {
            "post": self._post,
            "read": self._read,
            "search": self._search,
            "reply": self._reply,
            "thread": self._thread,
            "stats": self._stats,
        }
        handler = dispatch.get(action)
        if not handler:
            return ToolResult(
                success=False, output="",
                error=f"Unknown action: {action!r}. Use: post, read, search, reply, thread, stats",
            )

        return handler(store, kwargs)

    def _post(self, store, kwargs: Dict) -> ToolResult:
        from ..message_types import MessageType, DEFAULT_TTL_SECONDS

        message = kwargs.get("message", "").strip()
        if not message:
            return ToolResult(
                success=False, output="", error="No message provided for post action"
            )

        topic = kwargs.get("topic", "").strip()
        recipient = kwargs.get("recipient", "*").strip()
        metadata = kwargs.get("metadata") or {}
        ttl = kwargs.get("ttl_seconds", DEFAULT_TTL_SECONDS)

        # Parse msg_type
        type_str = kwargs.get("msg_type", "info").strip().lower()
        try:
            msg_type = MessageType(type_str)
        except ValueError:
            msg_type = MessageType.INFO

        try:
            msg = store.send(
                content=message,
                recipient=recipient,
                msg_type=msg_type,
                topic=topic,
                metadata=metadata,
                issue_id=kwargs.get("issue_id"),
                ttl_seconds=ttl,
            )

            # Paperclip dual-write (best-effort)
            bridge = self._get_bridge()
            comment_id = bridge.post_comment(msg)
            if comment_id:
                store.update_paperclip_comment_id(msg.id, comment_id)

            return ToolResult(
                success=True,
                output=(
                    f"Posted {msg.msg_type.value} message to bulletin board "
                    f"at {msg.created_at} as {msg.sender}"
                ),
                metadata={
                    "message_id": msg.id,
                    "timestamp": msg.created_at,
                    "sender": msg.sender,
                    "topic": topic,
                    "msg_type": msg.msg_type.value,
                },
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="", error=f"Failed to post: {exc}"
            )

    def _read(self, store, kwargs: Dict) -> ToolResult:
        from ..message_types import MessageType

        limit = int(kwargs.get("limit", 20))
        limit = max(1, min(limit, 100))
        unread_only = kwargs.get("unread_only", False)
        agent = _get_agent_name()

        try:
            if unread_only:
                messages = store.get_unread(agent_name=agent, limit=limit)
            else:
                # Parse type filter
                type_filter = None
                filter_str = kwargs.get("type_filter", "").strip()
                if filter_str:
                    types = []
                    for t in filter_str.split(","):
                        t = t.strip().lower()
                        try:
                            types.append(MessageType(t))
                        except ValueError:
                            pass
                    if types:
                        type_filter = types

                messages = store.read_recent(
                    limit=limit,
                    recipient=agent,
                    type_filter=type_filter,
                    topic=kwargs.get("topic", "").strip() or None,
                )

            if not messages:
                return ToolResult(success=True, output="No messages found.")

            lines = []
            for msg in messages:
                header = f"[{msg.created_at}] {msg.sender}"
                if msg.topic:
                    header += f" | Topic: {msg.topic}"
                header += f" | Type: {msg.msg_type.value}"
                read_status = (
                    " (read)" if agent in msg.read_by else " (unread)"
                )
                lines.append(
                    f"### {header}{read_status}\n"
                    f"ID: {msg.id}\n"
                    f"{msg.content}\n"
                )

            # Mark as read
            store.mark_many_read(
                [m.id for m in messages], agent_name=agent
            )

            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata={"count": len(messages)},
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="", error=f"Failed to read: {exc}"
            )

    def _search(self, store, kwargs: Dict) -> ToolResult:
        query = kwargs.get("query", "").strip()
        if not query:
            return ToolResult(
                success=False, output="", error="No search query provided"
            )

        try:
            results = store.hybrid_search(query, max_results=20)
            if not results:
                return ToolResult(
                    success=True,
                    output=f"No messages matching '{query}'.",
                    metadata={"count": 0},
                )

            lines = []
            for msg in results:
                header = f"[{msg.created_at}] {msg.sender}"
                if msg.topic:
                    header += f" | Topic: {msg.topic}"
                header += f" | Type: {msg.msg_type.value}"
                lines.append(
                    f"### {header} (score: {msg.score:.3f})\n"
                    f"ID: {msg.id}\n"
                    f"{msg.content}\n"
                )

            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata={"count": len(results)},
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="", error=f"Failed to search: {exc}"
            )

    def _reply(self, store, kwargs: Dict) -> ToolResult:
        parent_id = kwargs.get("reply_to", "").strip()
        if not parent_id:
            return ToolResult(
                success=False, output="",
                error="No reply_to message ID provided",
            )

        message = kwargs.get("message", "").strip()
        if not message:
            return ToolResult(
                success=False, output="",
                error="No message provided for reply action",
            )

        type_str = kwargs.get("msg_type", "").strip().lower()
        msg_type = None
        if type_str:
            from ..message_types import MessageType

            try:
                msg_type = MessageType(type_str)
            except ValueError:
                pass

        try:
            msg = store.reply(
                parent_id=parent_id,
                content=message,
                msg_type=msg_type,
                metadata=kwargs.get("metadata") or {},
            )

            # Paperclip dual-write
            bridge = self._get_bridge()
            comment_id = bridge.post_comment(msg)
            if comment_id:
                store.update_paperclip_comment_id(msg.id, comment_id)

            return ToolResult(
                success=True,
                output=f"Replied to message {parent_id}",
                metadata={
                    "message_id": msg.id,
                    "parent_id": parent_id,
                    "sender": msg.sender,
                },
            )
        except ValueError as exc:
            return ToolResult(
                success=False, output="", error=str(exc)
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="", error=f"Failed to reply: {exc}"
            )

    def _thread(self, store, kwargs: Dict) -> ToolResult:
        message_id = kwargs.get("message_id", "").strip()
        if not message_id:
            return ToolResult(
                success=False, output="",
                error="No message_id provided for thread action",
            )

        try:
            thread = store.get_thread(message_id)
            if not thread:
                return ToolResult(
                    success=True,
                    output=f"No thread found for message {message_id}.",
                )

            lines = []
            for i, msg in enumerate(thread):
                indent = "  " if msg.parent_id else ""
                header = f"[{msg.created_at}] {msg.sender}"
                if msg.topic:
                    header += f" | {msg.topic}"
                lines.append(
                    f"{indent}### {header}\n"
                    f"{indent}ID: {msg.id}\n"
                    f"{indent}{msg.content}\n"
                )

            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata={"thread_length": len(thread)},
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="", error=f"Failed to get thread: {exc}"
            )

    def _stats(self, store, kwargs: Dict) -> ToolResult:
        try:
            stats = store.get_stats()
            lines = [
                f"Total messages: {stats['total_messages']}",
                f"Thread count: {stats['thread_count']}",
                f"Embeddings: {stats['embedding_count']}",
                f"Max capacity: {stats['max_messages']}",
                "Messages by type:",
            ]
            for t, count in sorted(stats["by_type"].items()):
                lines.append(f"  {t}: {count}")

            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata=stats,
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="", error=f"Failed to get stats: {exc}"
            )
