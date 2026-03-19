"""Tests for inter-agent message type schemas.

Covers:
- MessageType enum values and string coercion
- Message dataclass: construction, serialization, expiry, formatting
- Typed payload dataclasses: to_dict / from_dict round-trips
"""

from datetime import datetime, timedelta, timezone

import pytest


class TestMessageType:
    """MessageType enum behavior."""

    def test_all_types_are_strings(self):
        from agents.message_types import MessageType

        for mt in MessageType:
            assert isinstance(mt.value, str)
            assert mt.value == mt  # str(Enum) works

    def test_expected_values(self):
        from agents.message_types import MessageType

        expected = {
            "decision", "blocker", "handoff", "status",
            "info", "question", "completion",
        }
        actual = {mt.value for mt in MessageType}
        assert actual == expected

    def test_construct_from_string(self):
        from agents.message_types import MessageType

        assert MessageType("info") == MessageType.INFO
        assert MessageType("blocker") == MessageType.BLOCKER

    def test_invalid_type_raises(self):
        from agents.message_types import MessageType

        with pytest.raises(ValueError):
            MessageType("invalid_type")


class TestMessage:
    """Message dataclass construction and behavior."""

    def _make(self, **kwargs):
        from agents.message_types import Message

        defaults = {
            "sender": "agent-a",
            "content": "hello world",
        }
        defaults.update(kwargs)
        return Message(**defaults)

    def test_defaults(self):
        from agents.message_types import MessageType, BROADCAST

        msg = self._make()
        assert msg.sender == "agent-a"
        assert msg.content == "hello world"
        assert msg.recipient == BROADCAST
        assert msg.msg_type == MessageType.INFO
        assert msg.topic == ""
        assert msg.metadata == {}
        assert msg.parent_id is None
        assert msg.read_by == []
        assert msg.score == 0.0
        assert msg.id  # UUID generated

    def test_id_unique(self):
        a = self._make()
        b = self._make()
        assert a.id != b.id

    def test_msg_type_coercion_from_string(self):
        from agents.message_types import MessageType

        msg = self._make(msg_type="decision")
        assert msg.msg_type == MessageType.DECISION

    def test_expires_at_computed(self):
        msg = self._make(ttl_seconds=3600)
        assert msg.expires_at is not None
        created = datetime.fromisoformat(msg.created_at)
        expires = datetime.fromisoformat(msg.expires_at)
        diff = (expires - created).total_seconds()
        assert abs(diff - 3600) < 2

    def test_ttl_zero_no_expiry(self):
        msg = self._make(ttl_seconds=0)
        assert msg.expires_at is None

    def test_explicit_expires_at_not_overwritten(self):
        exp = "2099-01-01T00:00:00+00:00"
        msg = self._make(expires_at=exp, ttl_seconds=3600)
        assert msg.expires_at == exp

    def test_to_dict(self):
        msg = self._make(topic="arch", metadata={"key": "val"})
        d = msg.to_dict()
        assert d["sender"] == "agent-a"
        assert d["content"] == "hello world"
        assert d["topic"] == "arch"
        assert d["msg_type"] == "info"
        assert d["metadata"] == {"key": "val"}
        assert "id" in d
        assert "created_at" in d

    def test_from_dict_round_trip(self):
        from agents.message_types import Message

        msg = self._make(
            topic="test",
            metadata={"foo": "bar"},
            ttl_seconds=7200,
        )
        d = msg.to_dict()
        restored = Message.from_dict(d)
        assert restored.id == msg.id
        assert restored.sender == msg.sender
        assert restored.content == msg.content
        assert restored.topic == msg.topic
        assert restored.metadata == msg.metadata
        assert restored.msg_type == msg.msg_type

    def test_from_dict_minimal(self):
        from agents.message_types import Message, MessageType

        msg = Message.from_dict({"content": "bare"})
        assert msg.content == "bare"
        assert msg.msg_type == MessageType.INFO

    def test_is_expired_false_when_future(self):
        msg = self._make(ttl_seconds=99999)
        assert not msg.is_expired()

    def test_is_expired_true_when_past(self):
        from agents.message_types import Message

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        msg = Message(
            sender="a",
            content="x",
            ttl_seconds=0,
            expires_at=past,
        )
        # Force ttl_seconds > 0 so is_expired actually checks
        msg.ttl_seconds = 1
        assert msg.is_expired()

    def test_is_expired_false_when_no_ttl(self):
        msg = self._make(ttl_seconds=0)
        assert not msg.is_expired()

    def test_format_for_context(self):
        from agents.message_types import MessageType

        msg = self._make(
            msg_type=MessageType.BLOCKER,
            topic="auth",
        )
        text = msg.format_for_context()
        assert "[BLOCKER]" in text
        assert "agent-a" in text
        assert "auth" in text
        assert "hello world" in text

    def test_format_for_context_no_topic(self):
        msg = self._make(topic="")
        text = msg.format_for_context()
        assert "topic:" not in text


class TestDecisionPayload:
    def test_round_trip(self):
        from agents.message_types import DecisionPayload

        p = DecisionPayload(
            decision="Use PostgreSQL",
            rationale="Better for our scale",
            alternatives_considered=["MySQL", "SQLite"],
            reversible=False,
        )
        d = p.to_dict()
        restored = DecisionPayload.from_dict(d)
        assert restored.decision == p.decision
        assert restored.rationale == p.rationale
        assert restored.alternatives_considered == p.alternatives_considered
        assert restored.reversible == p.reversible

    def test_defaults(self):
        from agents.message_types import DecisionPayload

        p = DecisionPayload()
        assert p.decision == ""
        assert p.reversible is True
        assert p.alternatives_considered == []


class TestBlockerPayload:
    def test_round_trip(self):
        from agents.message_types import BlockerPayload

        p = BlockerPayload(
            blocker_description="API is down",
            blocking_task_id="task-123",
            severity="critical",
            needs_human=True,
        )
        d = p.to_dict()
        restored = BlockerPayload.from_dict(d)
        assert restored.blocker_description == p.blocker_description
        assert restored.blocking_task_id == p.blocking_task_id
        assert restored.severity == p.severity
        assert restored.needs_human == p.needs_human

    def test_defaults(self):
        from agents.message_types import BlockerPayload

        p = BlockerPayload()
        assert p.severity == "medium"
        assert p.needs_human is False


class TestHandoffPayload:
    def test_round_trip(self):
        from agents.message_types import HandoffPayload

        p = HandoffPayload(
            from_agent="agent-a",
            to_agent="agent-b",
            task_summary="Complete the auth module",
            context="Started OAuth flow",
            artifacts=["auth.py", "tests/test_auth.py"],
        )
        d = p.to_dict()
        restored = HandoffPayload.from_dict(d)
        assert restored.from_agent == p.from_agent
        assert restored.to_agent == p.to_agent
        assert restored.artifacts == p.artifacts


class TestStatusPayload:
    def test_round_trip(self):
        from agents.message_types import StatusPayload

        p = StatusPayload(
            task_id="task-42",
            progress_pct=75.5,
            current_step="Running tests",
            eta_seconds=120,
        )
        d = p.to_dict()
        restored = StatusPayload.from_dict(d)
        assert restored.task_id == p.task_id
        assert restored.progress_pct == p.progress_pct
        assert restored.current_step == p.current_step
        assert restored.eta_seconds == p.eta_seconds

    def test_defaults(self):
        from agents.message_types import StatusPayload

        p = StatusPayload()
        assert p.progress_pct == 0.0
        assert p.eta_seconds is None


class TestHighPriorityTypes:
    def test_contains_expected(self):
        from agents.message_types import HIGH_PRIORITY_TYPES, MessageType

        assert MessageType.DECISION in HIGH_PRIORITY_TYPES
        assert MessageType.BLOCKER in HIGH_PRIORITY_TYPES
        assert MessageType.HANDOFF in HIGH_PRIORITY_TYPES
        assert MessageType.INFO not in HIGH_PRIORITY_TYPES
        assert MessageType.STATUS not in HIGH_PRIORITY_TYPES
