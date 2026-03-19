"""Inter-Agent Bulletin Board — shared message board across agent sessions and containers.

Posts timestamped, attributed messages to a shared BULLETIN.md file mounted
via Docker volume.  Agents can post notes, read recent entries, and search
by keyword.  Thread-safe via fcntl file locking.

Requires ``BULLETIN_PATH`` environment variable pointing to the bulletin
file (e.g. ``/shared/bulletin/BULLETIN.md``).
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)

_BULLETIN_HEADER = "# Inter-Agent Bulletin Board\n\nShared notes between agents.\n\n---\n"

# Regex to split the file into individual entries.
# Each entry starts with ### [timestamp] agent-name
_ENTRY_RE = re.compile(
    r"^### \[(?P<timestamp>[^\]]+)\] (?P<agent>.+?)$",
    re.MULTILINE,
)


def _get_bulletin_path() -> Optional[str]:
    """Return the configured bulletin file path, or None."""
    return os.environ.get("BULLETIN_PATH")


def _get_agent_name() -> str:
    """Determine the current agent's display name."""
    return (
        os.environ.get("VIBE_AGENT_NAME")
        or os.environ.get("PAPERCLIP_AGENT_ID")
        or "unknown-agent"
    )


def _ensure_file(path: Path) -> None:
    """Create the bulletin file with header if it doesn't exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_BULLETIN_HEADER, encoding="utf-8")


def _read_entries(path: Path) -> List[Dict[str, str]]:
    """Parse all entries from the bulletin file.

    Returns a list of dicts with keys: timestamp, agent, topic, body.
    """
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8")
    entries: List[Dict[str, str]] = []
    matches = list(_ENTRY_RE.finditer(text))

    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()

        # Extract optional topic line
        topic = ""
        body_lines = []
        for line in block.split("\n"):
            stripped = line.strip()
            if stripped.startswith("> Topic:"):
                topic = stripped[len("> Topic:"):].strip()
            elif stripped == "---":
                continue
            else:
                body_lines.append(line)

        entries.append({
            "timestamp": match.group("timestamp"),
            "agent": match.group("agent"),
            "topic": topic,
            "body": "\n".join(body_lines).strip(),
        })

    return entries


def read_recent_entries(limit: int = 10) -> str:
    """Read the last N bulletin entries, formatted for context injection.

    Returns empty string if bulletin is not configured or has no entries.
    """
    bulletin_path = _get_bulletin_path()
    if not bulletin_path:
        return ""

    path = Path(bulletin_path)
    if not path.exists():
        return ""

    try:
        entries = _read_entries(path)
        if not entries:
            return ""

        recent = entries[-limit:]
        lines = []
        for e in recent:
            header = f"[{e['timestamp']}] {e['agent']}"
            if e["topic"]:
                header += f" (topic: {e['topic']})"
            lines.append(f"- **{header}**: {e['body']}")

        return (
            "\n\n## Bulletin Board (Recent Posts)\n\n"
            + "\n".join(lines)
        )
    except Exception as exc:
        logger.debug(f"Bulletin read for injection skipped: {exc}")
        return ""


class BulletinBoardTool(Tool):
    """Shared inter-agent bulletin board for posting and reading messages.

    All agents share a single ``BULLETIN.md`` file mounted via Docker volume.
    Use this to leave notes, share decisions, flag blockers, or coordinate
    work across agent sessions and containers.

    Requires ``BULLETIN_PATH`` environment variable.
    """

    def __init__(self):
        super().__init__(
            name="bulletin_board",
            description=(
                "Post and read messages on a shared inter-agent bulletin board. "
                "Use to leave notes, share decisions, flag blockers, or coordinate "
                "work across agent sessions. Actions: post, read, search."
            ),
            category=ToolCategory.SPECIALIZED,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "Bulletin action: 'post' (add a message), "
                        "'read' (get recent entries), 'search' (find by keyword)"
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "The message to post (required for 'post' action)",
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
            },
            "required": ["action"],
        }

    def execute(self, **kwargs) -> ToolResult:
        action = kwargs.get("action", "").strip().lower()
        if not action:
            return ToolResult(success=False, output="", error="No action specified. Use: post, read, search")

        bulletin_path = _get_bulletin_path()
        if not bulletin_path:
            return ToolResult(
                success=False, output="",
                error="BULLETIN_PATH not configured. Bulletin board is disabled.",
            )

        path = Path(bulletin_path)

        if action == "post":
            return self._post(path, kwargs)
        elif action == "read":
            return self._read(path, kwargs)
        elif action == "search":
            return self._search(path, kwargs)
        else:
            return ToolResult(
                success=False, output="",
                error=f"Unknown action: {action!r}. Use: post, read, search",
            )

    def _post(self, path: Path, kwargs: Dict) -> ToolResult:
        message = kwargs.get("message", "").strip()
        if not message:
            return ToolResult(success=False, output="", error="No message provided for post action")

        topic = kwargs.get("topic", "").strip()
        agent = _get_agent_name()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Build entry
        entry_lines = [f"\n### [{timestamp}] {agent}"]
        if topic:
            entry_lines.append(f"> Topic: {topic}")
        entry_lines.append("")
        entry_lines.append(message)
        entry_lines.append("")
        entry_lines.append("---")
        entry_lines.append("")
        entry_text = "\n".join(entry_lines)

        try:
            _ensure_file(path)

            # Append with file locking for thread safety
            with open(path, "a", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(entry_text)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            logger.info(f"Bulletin post by {agent}: {message[:80]}...")
            return ToolResult(
                success=True,
                output=f"Posted to bulletin board at {timestamp} as {agent}",
                metadata={"timestamp": timestamp, "agent": agent, "topic": topic},
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"Failed to post: {exc}")

    def _read(self, path: Path, kwargs: Dict) -> ToolResult:
        limit = int(kwargs.get("limit", 20))
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100

        try:
            entries = _read_entries(path)
            if not entries:
                return ToolResult(success=True, output="No bulletin entries found.")

            recent = entries[-limit:]
            lines = []
            for e in recent:
                header = f"[{e['timestamp']}] {e['agent']}"
                if e["topic"]:
                    header += f" | Topic: {e['topic']}"
                lines.append(f"### {header}\n{e['body']}\n")

            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata={"count": len(recent), "total": len(entries)},
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"Failed to read bulletin: {exc}")

    def _search(self, path: Path, kwargs: Dict) -> ToolResult:
        query = kwargs.get("query", "").strip()
        if not query:
            return ToolResult(success=False, output="", error="No search query provided")

        try:
            entries = _read_entries(path)
            if not entries:
                return ToolResult(success=True, output="No bulletin entries found.")

            query_lower = query.lower()
            matches = [
                e for e in entries
                if query_lower in e["body"].lower()
                or query_lower in e["topic"].lower()
                or query_lower in e["agent"].lower()
            ]

            if not matches:
                return ToolResult(
                    success=True,
                    output=f"No entries matching '{query}'.",
                    metadata={"count": 0, "total": len(entries)},
                )

            lines = []
            for e in matches:
                header = f"[{e['timestamp']}] {e['agent']}"
                if e["topic"]:
                    header += f" | Topic: {e['topic']}"
                lines.append(f"### {header}\n{e['body']}\n")

            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata={"count": len(matches), "total": len(entries)},
            )
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"Failed to search bulletin: {exc}")
