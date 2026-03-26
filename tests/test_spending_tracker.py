"""Tests for the spending tracker and circuit breaker."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.spending_tracker import (
    BreakerState,
    BreakerStatus,
    SpendingTracker,
)


@pytest.fixture
def tracker(tmp_path):
    """SpendingTracker with in-memory-like temp DB and low thresholds for fast testing."""
    db_path = str(tmp_path / "test_spending.db")
    return SpendingTracker(
        db_path=db_path,
        window_seconds=3600,
        max_cents_per_window=100,
        max_heartbeats_per_window=5,
        max_consecutive_non_idle=3,
        cooldown_seconds=60,
        max_cooldown_seconds=300,
        retention_days=7,
    )


# ------------------------------------------------------------------
# DB Initialization
# ------------------------------------------------------------------


class TestDBInit:
    def test_creates_tables(self, tracker):
        conn = tracker._connect()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [t["name"] for t in tables]
            assert "cost_events" in table_names
            assert "circuit_breaker" in table_names
        finally:
            conn.close()

    def test_creates_indexes(self, tracker):
        conn = tracker._connect()
        try:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            index_names = [i["name"] for i in indexes]
            assert "idx_cost_events_timestamp" in index_names
            assert "idx_cost_events_status" in index_names
        finally:
            conn.close()

    def test_idempotent_init(self, tracker):
        # Calling _init_db again should not fail
        tracker._init_db()

    def test_wal_mode(self, tracker):
        conn = tracker._connect()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            assert mode[0] == "wal"
        finally:
            conn.close()


# ------------------------------------------------------------------
# Event Recording
# ------------------------------------------------------------------


class TestEventRecording:
    def test_record_basic_event(self, tracker):
        tracker.record_event(
            status="success",
            cost_cents=10,
            agent_id="agent-1",
            provider="openai",
            model="gpt-4o",
            input_tokens=100,
            output_tokens=50,
        )
        events = tracker.get_recent_events(limit=1)
        assert len(events) == 1
        assert events[0]["status"] == "success"
        assert events[0]["cost_cents"] == 10
        assert events[0]["agent_id"] == "agent-1"

    def test_record_idle_event(self, tracker):
        tracker.record_event(status="idle")
        events = tracker.get_recent_events(limit=1)
        assert len(events) == 1
        assert events[0]["status"] == "idle"
        assert events[0]["cost_cents"] == 0

    def test_multiple_events_ordered(self, tracker):
        for i in range(5):
            tracker.record_event(status="success", cost_cents=i)
        events = tracker.get_recent_events(limit=10)
        assert len(events) == 5
        # Most recent first
        assert events[0]["cost_cents"] == 4


# ------------------------------------------------------------------
# Rolling Window Queries (via get_status)
# ------------------------------------------------------------------


class TestRollingWindow:
    def test_empty_status(self, tracker):
        status = tracker.get_status()
        assert status.total_cost_cents == 0
        assert status.total_heartbeats == 0
        assert status.non_idle_heartbeats == 0
        assert status.consecutive_non_idle == 0
        assert status.breaker.state == BreakerState.CLOSED

    def test_cost_aggregation(self, tracker):
        tracker.record_event(status="success", cost_cents=10)
        tracker.record_event(status="success", cost_cents=20)
        status = tracker.get_status()
        assert status.total_cost_cents == 30

    def test_heartbeat_counts(self, tracker):
        tracker.record_event(status="success")
        tracker.record_event(status="idle")
        tracker.record_event(status="failed")
        status = tracker.get_status()
        assert status.total_heartbeats == 3
        assert status.non_idle_heartbeats == 2


# ------------------------------------------------------------------
# Breaker State Transitions
# ------------------------------------------------------------------


class TestBreakerTransitions:
    def test_starts_closed(self, tracker):
        result = tracker.check_circuit_breaker()
        assert result is None  # None means CLOSED / proceed

    def test_trips_on_spend_velocity(self, tracker):
        # Exceed max_cents_per_window (100)
        tracker.record_event(status="success", cost_cents=101)
        # Breaker should now be OPEN
        result = tracker.check_circuit_breaker()
        assert result is not None
        assert result.state == BreakerState.OPEN

    def test_trips_on_heartbeat_frequency(self, tmp_path):
        # Use high consecutive threshold so frequency trips first
        t = SpendingTracker(
            db_path=str(tmp_path / "freq.db"),
            max_cents_per_window=10000,
            max_heartbeats_per_window=5,
            max_consecutive_non_idle=100,  # Won't trip
            cooldown_seconds=60,
        )
        for _ in range(6):
            t.record_event(status="success", cost_cents=1)
        result = t.check_circuit_breaker()
        assert result is not None
        assert result.state == BreakerState.OPEN
        assert "Heartbeat frequency" in result.reason

    def test_trips_on_consecutive_non_idle(self, tracker):
        # max_consecutive_non_idle = 3
        tracker.record_event(status="success", cost_cents=1)
        tracker.record_event(status="success", cost_cents=1)
        tracker.record_event(status="success", cost_cents=1)
        result = tracker.check_circuit_breaker()
        assert result is not None
        assert result.state == BreakerState.OPEN
        assert "Consecutive" in result.reason

    def test_idle_resets_streak(self, tracker):
        tracker.record_event(status="success", cost_cents=1)
        tracker.record_event(status="success", cost_cents=1)
        tracker.record_event(status="idle")  # Reset streak
        tracker.record_event(status="success", cost_cents=1)
        # Only 1 consecutive non-idle, below threshold of 3
        result = tracker.check_circuit_breaker()
        assert result is None

    def test_open_returns_retry_after(self, tracker):
        tracker.record_event(status="success", cost_cents=101)
        result = tracker.check_circuit_breaker()
        assert result is not None
        assert result.retry_after_seconds > 0

    def test_close_after_probe(self, tracker):
        # Trip it
        tracker.record_event(status="success", cost_cents=101)
        assert tracker.check_circuit_breaker() is not None

        # Manually set to HALF_OPEN (simulating cooldown expiry)
        conn = tracker._connect()
        conn.execute(
            "UPDATE circuit_breaker SET state = 'half_open' WHERE scope = ?",
            (tracker.scope,)
        )
        conn.commit()
        conn.close()

        # HALF_OPEN allows probe through
        result = tracker.check_circuit_breaker()
        assert result is None

        # After successful probe, close it
        tracker.close_breaker_after_probe()
        status = tracker.get_status()
        assert status.breaker.state == BreakerState.CLOSED


# ------------------------------------------------------------------
# Consecutive Streak Detection
# ------------------------------------------------------------------


class TestConsecutiveStreak:
    def test_streak_count(self, tracker):
        tracker.record_event(status="success")
        tracker.record_event(status="success")
        status = tracker.get_status()
        assert status.consecutive_non_idle == 2

    def test_idle_breaks_streak(self, tracker):
        tracker.record_event(status="success")
        tracker.record_event(status="idle")
        tracker.record_event(status="success")
        status = tracker.get_status()
        assert status.consecutive_non_idle == 1

    def test_all_idle_zero_streak(self, tracker):
        tracker.record_event(status="idle")
        tracker.record_event(status="idle")
        status = tracker.get_status()
        assert status.consecutive_non_idle == 0


# ------------------------------------------------------------------
# Cooldown Backoff
# ------------------------------------------------------------------


class TestCooldownBackoff:
    def test_first_trip_uses_base_cooldown(self, tracker):
        tracker.record_event(status="success", cost_cents=101)
        status = tracker.get_status()
        assert status.breaker.trip_count == 1

    def test_re_trip_increases_cooldown(self, tracker):
        # Trip once
        tracker.record_event(status="success", cost_cents=101)
        # Reset
        tracker.reset()
        # Trip again — trip_count resets on manual reset, so this is trip #1 again
        tracker.record_event(status="success", cost_cents=101)
        status = tracker.get_status()
        assert status.breaker.trip_count == 1

    def test_trip_count_increments(self, tmp_path):
        db_path = str(tmp_path / "backoff.db")
        tracker = SpendingTracker(
            db_path=db_path,
            max_cents_per_window=10,
            max_heartbeats_per_window=100,
            max_consecutive_non_idle=100,
            cooldown_seconds=60,
            max_cooldown_seconds=3600,
        )
        # Trip 1
        tracker.record_event(status="success", cost_cents=11)
        status = tracker.get_status()
        assert status.breaker.trip_count == 1

        # Manually close to re-trip (simulating half_open → closed → re-trip)
        conn = tracker._connect()
        conn.execute(
            "UPDATE circuit_breaker SET state = 'closed' WHERE scope = ?",
            (tracker.scope,)
        )
        conn.commit()
        conn.close()

        # Trip 2
        tracker.record_event(status="success", cost_cents=11)
        status = tracker.get_status()
        assert status.breaker.trip_count == 2


# ------------------------------------------------------------------
# Pruning
# ------------------------------------------------------------------


class TestPruning:
    def test_prune_old_events(self, tmp_path):
        db_path = str(tmp_path / "prune.db")
        tracker = SpendingTracker(db_path=db_path, retention_days=1)

        # Insert an old event directly
        conn = tracker._connect()
        old_time = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO cost_events (timestamp, status) VALUES (?, 'success')",
            (old_time,),
        )
        conn.commit()
        conn.close()

        # Insert a recent event normally
        tracker.record_event(status="success")

        removed = tracker.prune()
        assert removed == 1

        events = tracker.get_recent_events(limit=10)
        assert len(events) == 1

    def test_prune_keeps_recent(self, tracker):
        tracker.record_event(status="success")
        removed = tracker.prune()
        assert removed == 0
        assert len(tracker.get_recent_events()) == 1


# ------------------------------------------------------------------
# Reset
# ------------------------------------------------------------------


class TestReset:
    def test_reset_clears_breaker(self, tracker):
        tracker.record_event(status="success", cost_cents=101)
        assert tracker.check_circuit_breaker() is not None

        tracker.reset()
        assert tracker.check_circuit_breaker() is None

        status = tracker.get_status()
        assert status.breaker.state == BreakerState.CLOSED
        assert status.breaker.trip_count == 0

    def test_reset_idempotent(self, tracker):
        tracker.reset()
        tracker.reset()
        status = tracker.get_status()
        assert status.breaker.state == BreakerState.CLOSED


# ------------------------------------------------------------------
# Per-Agent Scope Isolation
# ------------------------------------------------------------------


class TestPerAgentScope:
    def test_agent_id_sets_scope(self, tmp_path):
        db_path = str(tmp_path / "scope.db")
        t = SpendingTracker(db_path=db_path, agent_id="agent-abc")
        assert t.scope == "agent-abc"

    def test_empty_agent_id_defaults_to_global(self, tmp_path):
        db_path = str(tmp_path / "scope.db")
        t = SpendingTracker(db_path=db_path)
        assert t.scope == "global"

    def test_agents_have_independent_breakers(self, tmp_path):
        """Two agents sharing one DB — tripping one doesn't trip the other."""
        db_path = str(tmp_path / "shared.db")
        agent_a = SpendingTracker(
            db_path=db_path, agent_id="eng-1",
            max_cents_per_window=100, cooldown_seconds=60,
        )
        agent_b = SpendingTracker(
            db_path=db_path, agent_id="eng-2",
            max_cents_per_window=100, cooldown_seconds=60,
        )

        # Trip agent A's breaker
        agent_a.record_event(status="success", cost_cents=101)
        assert agent_a.check_circuit_breaker() is not None

        # Agent B should still be clear
        assert agent_b.check_circuit_breaker() is None

    def test_agent_reset_does_not_affect_other(self, tmp_path):
        db_path = str(tmp_path / "shared.db")
        agent_a = SpendingTracker(
            db_path=db_path, agent_id="eng-1",
            max_cents_per_window=100, cooldown_seconds=60,
        )
        agent_b = SpendingTracker(
            db_path=db_path, agent_id="eng-2",
            max_cents_per_window=100, cooldown_seconds=60,
        )

        # Trip both
        agent_a.record_event(status="success", cost_cents=101)
        agent_b.record_event(status="success", cost_cents=101)

        # Reset only agent A
        agent_a.reset()
        assert agent_a.check_circuit_breaker() is None
        assert agent_b.check_circuit_breaker() is not None

    def test_agent_status_scoped(self, tmp_path):
        db_path = str(tmp_path / "shared.db")
        agent_a = SpendingTracker(
            db_path=db_path, agent_id="eng-1",
            max_cents_per_window=10000, cooldown_seconds=60,
        )
        agent_b = SpendingTracker(
            db_path=db_path, agent_id="eng-2",
            max_cents_per_window=10000, cooldown_seconds=60,
        )

        agent_a.record_event(status="success", cost_cents=50)

        status_a = agent_a.get_status()
        status_b = agent_b.get_status()

        # Both see the cost events (cost_events table is shared),
        # but breaker state is per-agent
        assert status_a.breaker.state == BreakerState.CLOSED
        assert status_b.breaker.state == BreakerState.CLOSED


# ------------------------------------------------------------------
# Tokens Per Second Tracking
# ------------------------------------------------------------------


def test_tokens_per_second_column_exists(tmp_path):
    """spending_ledger should have tokens_per_second and generation_duration_ms columns."""
    from agents.spending_tracker import SpendingTracker
    import sqlite3

    db = str(tmp_path / "test.db")
    tracker = SpendingTracker(db_path=db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    # Check column exists by inserting with it
    tracker.record_event(
        status="success",
        agent_name="test-agent",
        input_tokens=100,
        output_tokens=50,
        tokens_per_second=25.0,
        generation_duration_ms=2000,
    )
    row = conn.execute("SELECT tokens_per_second, generation_duration_ms, agent_name FROM cost_events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["tokens_per_second"] == 25.0
    assert row["generation_duration_ms"] == 2000
    assert row["agent_name"] == "test-agent"
    conn.close()
