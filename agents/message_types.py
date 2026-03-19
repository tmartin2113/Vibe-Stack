"""Typed message schemas for the V2 inter-agent communication system.

Defines message types, the core Message dataclass, and typed metadata
payloads for structured inter-agent communication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid


class MessageType(str, Enum):
    """Categories of inter-agent messages."""

    DECISION = "decision"  # Architectural/design choice
    BLOCKER = "blocker"  # Something blocking progress
    HANDOFF = "handoff"  # Work transfer between agents
    STATUS = "status"  # Progress update
    INFO = "info"  # General note (default, backward compat with v1)
    QUESTION = "question"  # Question for other agents/humans
    COMPLETION = "completion"  # Task/subtask done notification


# Message types that are considered high-priority for context injection
HIGH_PRIORITY_TYPES = frozenset({
    MessageType.DECISION,
    MessageType.BLOCKER,
    MessageType.HANDOFF,
})

# Default TTL in seconds (7 days)
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# Broadcast recipient sentinel
BROADCAST = "*"


@dataclass
class Message:
    """A single inter-agent message.

    Attributes:
        id: Unique message identifier (UUID).
        sender: Agent name/ID that sent the message.
        recipient: Target agent name, or '*' for broadcast.
        msg_type: Category of message.
        topic: Optional topic tag (e.g. 'architecture', 'auth').
        content: The message body text.
        metadata: Typed payload dict (varies by msg_type).
        parent_id: ID of parent message for threading (None = root).
        issue_id: Paperclip issue ID this relates to (optional).
        paperclip_comment_id: Comment ID from Paperclip dual-write.
        ttl_seconds: Time-to-live in seconds (0 = never expire).
        created_at: UTC ISO timestamp.
        expires_at: UTC ISO timestamp when message expires (None = never).
        read_by: List of agent names that have read this message.
        score: Search relevance score (populated by search methods).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sender: str = ""
    recipient: str = BROADCAST
    msg_type: MessageType = MessageType.INFO
    topic: str = ""
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    issue_id: Optional[str] = None
    paperclip_comment_id: Optional[str] = None
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    expires_at: Optional[str] = None
    read_by: List[str] = field(default_factory=list)
    score: float = 0.0

    def __post_init__(self):
        # Coerce msg_type from string if needed
        if isinstance(self.msg_type, str):
            self.msg_type = MessageType(self.msg_type)

        # Compute expires_at from ttl_seconds if not set
        if self.expires_at is None and self.ttl_seconds > 0:
            created = datetime.fromisoformat(self.created_at)
            from datetime import timedelta

            self.expires_at = (
                created + timedelta(seconds=self.ttl_seconds)
            ).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "msg_type": self.msg_type.value,
            "topic": self.topic,
            "content": self.content,
            "metadata": self.metadata,
            "parent_id": self.parent_id,
            "issue_id": self.issue_id,
            "paperclip_comment_id": self.paperclip_comment_id,
            "ttl_seconds": self.ttl_seconds,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "read_by": self.read_by,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Message:
        """Deserialize from a plain dict."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            sender=data.get("sender", ""),
            recipient=data.get("recipient", BROADCAST),
            msg_type=data.get("msg_type", "info"),
            topic=data.get("topic", ""),
            content=data.get("content", ""),
            metadata=data.get("metadata", {}),
            parent_id=data.get("parent_id"),
            issue_id=data.get("issue_id"),
            paperclip_comment_id=data.get("paperclip_comment_id"),
            ttl_seconds=data.get("ttl_seconds", DEFAULT_TTL_SECONDS),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            expires_at=data.get("expires_at"),
            read_by=data.get("read_by", []),
            score=data.get("score", 0.0),
        )

    def is_expired(self) -> bool:
        """Check if this message has expired."""
        if self.ttl_seconds == 0 or self.expires_at is None:
            return False
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(self.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now > expires

    def format_for_context(self) -> str:
        """Format this message for injection into specialist context."""
        prefix = f"[{self.msg_type.value.upper()}]"
        header = f"{prefix} from {self.sender}"
        if self.topic:
            header += f" (topic: {self.topic})"
        return f"- **{header}**: {self.content}"


# ── Typed metadata payloads ──────────────────────────────────────


@dataclass
class DecisionPayload:
    """Metadata for DECISION messages."""

    decision: str = ""
    rationale: str = ""
    alternatives_considered: List[str] = field(default_factory=list)
    reversible: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "rationale": self.rationale,
            "alternatives_considered": self.alternatives_considered,
            "reversible": self.reversible,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DecisionPayload:
        return cls(
            decision=data.get("decision", ""),
            rationale=data.get("rationale", ""),
            alternatives_considered=data.get("alternatives_considered", []),
            reversible=data.get("reversible", True),
        )


@dataclass
class BlockerPayload:
    """Metadata for BLOCKER messages."""

    blocker_description: str = ""
    blocking_task_id: Optional[str] = None
    severity: str = "medium"  # low, medium, high, critical
    needs_human: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blocker_description": self.blocker_description,
            "blocking_task_id": self.blocking_task_id,
            "severity": self.severity,
            "needs_human": self.needs_human,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BlockerPayload:
        return cls(
            blocker_description=data.get("blocker_description", ""),
            blocking_task_id=data.get("blocking_task_id"),
            severity=data.get("severity", "medium"),
            needs_human=data.get("needs_human", False),
        )


@dataclass
class HandoffPayload:
    """Metadata for HANDOFF messages."""

    from_agent: str = ""
    to_agent: str = ""
    task_summary: str = ""
    context: str = ""
    artifacts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "task_summary": self.task_summary,
            "context": self.context,
            "artifacts": self.artifacts,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HandoffPayload:
        return cls(
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            task_summary=data.get("task_summary", ""),
            context=data.get("context", ""),
            artifacts=data.get("artifacts", []),
        )


@dataclass
class StatusPayload:
    """Metadata for STATUS messages."""

    task_id: Optional[str] = None
    progress_pct: float = 0.0
    current_step: str = ""
    eta_seconds: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "progress_pct": self.progress_pct,
            "current_step": self.current_step,
            "eta_seconds": self.eta_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StatusPayload:
        return cls(
            task_id=data.get("task_id"),
            progress_pct=data.get("progress_pct", 0.0),
            current_step=data.get("current_step", ""),
            eta_seconds=data.get("eta_seconds"),
        )
