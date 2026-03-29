"""
Spending Tracker & Circuit Breaker — pluggable-backend cost ledger.

Tracks per-heartbeat cost events and implements a circuit breaker that
trips when spending velocity, heartbeat frequency, or consecutive
non-idle heartbeats exceed configurable thresholds.

Circuit breaker states:
    CLOSED  — normal operation, all heartbeats proceed
    OPEN    — tripped, heartbeats return immediately with retry_after
    HALF_OPEN — after cooldown, one probe heartbeat allowed through

Storage:
    - Default: SQLite + WAL mode at ~/.vibe/spending_ledger.db
    - Optional: any StorageBackend (e.g. PostgresBackend for multi-node)
"""

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .storage.base import StorageBackend

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Return timezone-aware UTC now (avoids deprecated datetime.utcnow())."""
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    """Return UTC now as a naive-looking ISO string for SQLite storage."""
    # Strip tzinfo so stored strings stay comparable with existing data
    return _utcnow().replace(tzinfo=None).isoformat()


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerStatus:
    """Current state of the circuit breaker."""
    state: BreakerState
    reason: str = ""
    opened_at: str = ""
    cooldown_until: str = ""
    trip_count: int = 0
    retry_after_seconds: int = 0


@dataclass
class SpendingSummary:
    """Rolling window spending summary for status display."""
    window_seconds: int
    total_cost_cents: int
    total_heartbeats: int
    non_idle_heartbeats: int
    consecutive_non_idle: int
    breaker: BreakerStatus


class SpendingTracker:
    """
    Pluggable-backend spending ledger with circuit breaker.

    Thread-safe via per-call connections and self._lock for all
    read-modify-write operations. Uses WAL mode for concurrent
    read/write performance when backed by SQLite.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        window_seconds: int = 3600,
        max_cents_per_window: int = 500,
        max_heartbeats_per_window: int = 30,
        max_consecutive_non_idle: int = 10,
        cooldown_seconds: int = 300,
        max_cooldown_seconds: int = 7200,
        retention_days: int = 30,
        agent_id: str = "",
        storage_backend: "Optional[StorageBackend]" = None,
    ):
        self.storage_backend = storage_backend

        if db_path is None:
            db_path = str(Path.home() / ".vibe" / "spending_ledger.db")

        self.db_path = db_path
        self.scope = agent_id or "global"
        self.window_seconds = window_seconds
        self.max_cents_per_window = max_cents_per_window
        self.max_heartbeats_per_window = max_heartbeats_per_window
        self.max_consecutive_non_idle = max_consecutive_non_idle
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self.retention_days = retention_days
        self._lock = threading.Lock()

        if self.storage_backend is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    _SCHEMA_DDL = """
        CREATE TABLE IF NOT EXISTS cost_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            agent_id    TEXT NOT NULL DEFAULT '',
            agent_name  TEXT NOT NULL DEFAULT '',
            run_id      TEXT NOT NULL DEFAULT '',
            issue_id    TEXT NOT NULL DEFAULT '',
            provider    TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_cents  INTEGER NOT NULL DEFAULT 0,
            status      TEXT NOT NULL DEFAULT '',
            tokens_per_second REAL NOT NULL DEFAULT 0,
            generation_duration_ms INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_cost_events_timestamp
            ON cost_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_cost_events_status
            ON cost_events(status);

        CREATE TABLE IF NOT EXISTS circuit_breaker (
            scope         TEXT PRIMARY KEY,
            state         TEXT NOT NULL DEFAULT 'closed',
            opened_at     TEXT NOT NULL DEFAULT '',
            reason        TEXT NOT NULL DEFAULT '',
            cooldown_until TEXT NOT NULL DEFAULT '',
            trip_count    INTEGER NOT NULL DEFAULT 0
        );
    """

    def _init_db(self) -> None:
        if self.storage_backend is not None:
            self.storage_backend.execute_script(self._SCHEMA_DDL)
            return

        conn = self._connect()
        try:
            conn.executescript(self._SCHEMA_DDL)
            # Migrate existing DBs: add columns if missing
            try:
                conn.execute("SELECT tokens_per_second FROM cost_events LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE cost_events ADD COLUMN tokens_per_second REAL NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE cost_events ADD COLUMN generation_duration_ms INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def _ph(self) -> str:
        """Parameter placeholder — '?' for SQLite, '%s' for PostgreSQL."""
        if self.storage_backend is not None:
            return self.storage_backend.placeholder
        return "?"

    # ------------------------------------------------------------------
    # Data-access helpers (route through storage_backend or SQLite)
    # ------------------------------------------------------------------

    def _exec(self, sql: str, params: tuple = ()) -> None:
        """Execute a write statement through the appropriate backend."""
        if self.storage_backend is not None:
            self.storage_backend.execute(sql, params)
            return
        conn = self._connect()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple = ()):
        """Fetch one row as a dict-like object, or None."""
        if self.storage_backend is not None:
            return self.storage_backend.fetchone_dict(sql, params)
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _query_all(self, sql: str, params: tuple = ()) -> list:
        """Fetch all rows as dict-like objects."""
        if self.storage_backend is not None:
            return self.storage_backend.fetchall_dict(sql, params)
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Event Recording
    # ------------------------------------------------------------------

    def record_event(
        self,
        status: str,
        cost_cents: int = 0,
        agent_id: str = "",
        agent_name: str = "",
        run_id: str = "",
        issue_id: str = "",
        provider: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        tokens_per_second: float = 0.0,
        generation_duration_ms: int = 0,
    ) -> None:
        """Record a cost event and evaluate circuit breaker thresholds."""
        now = _utcnow_iso()
        ph = self._ph
        with self._lock:
            try:
                self._exec(
                    f"""
                    INSERT INTO cost_events (
                        timestamp, agent_id, agent_name, run_id, issue_id,
                        provider, model, input_tokens, output_tokens,
                        cost_cents, status, tokens_per_second, generation_duration_ms
                    ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                    """,
                    (now, agent_id, agent_name, run_id, issue_id,
                     provider, model, input_tokens, output_tokens,
                     cost_cents, status, tokens_per_second, generation_duration_ms),
                )

                # Evaluate thresholds (may trip breaker)
                self._evaluate_thresholds()
            except Exception as e:
                logger.warning("Failed to record spending event: %s", e)

    # ------------------------------------------------------------------
    # Circuit Breaker Check
    # ------------------------------------------------------------------

    def check_circuit_breaker(self) -> Optional[BreakerStatus]:
        """
        Check the circuit breaker state.

        Returns None if CLOSED (proceed normally).
        Returns BreakerStatus if OPEN (with retry_after info).
        For HALF_OPEN, allows the probe through (returns None) and
        transitions to CLOSED if successful.

        Thread-safe: holds self._lock for the entire read-modify-write.
        """
        ph = self._ph
        with self._lock:
            try:
                row = self._query_one(
                    f"SELECT * FROM circuit_breaker WHERE scope = {ph}",
                    (self.scope,),
                )

                if row is None or row["state"] == BreakerState.CLOSED.value:
                    return None

                state = BreakerState(row["state"])
                cooldown_until = row["cooldown_until"]
                now = _utcnow().replace(tzinfo=None)

                if state == BreakerState.OPEN:
                    if cooldown_until:
                        cooldown_dt = datetime.fromisoformat(cooldown_until)
                        if now >= cooldown_dt:
                            # Transition to HALF_OPEN — allow probe
                            self._exec(
                                f"UPDATE circuit_breaker SET state = {ph} WHERE scope = {ph}",
                                (BreakerState.HALF_OPEN.value, self.scope),
                            )
                            logger.info("Circuit breaker -> HALF_OPEN (cooldown expired)")
                            return None  # Allow probe through

                        retry_after = max(1, int((cooldown_dt - now).total_seconds()))
                    else:
                        retry_after = self.cooldown_seconds

                    return BreakerStatus(
                        state=BreakerState.OPEN,
                        reason=row["reason"],
                        opened_at=row["opened_at"],
                        cooldown_until=cooldown_until,
                        trip_count=row["trip_count"],
                        retry_after_seconds=retry_after,
                    )

                if state == BreakerState.HALF_OPEN:
                    # Allow the probe heartbeat through
                    return None

            except Exception as e:
                logger.warning("Circuit breaker check failed: %s", e)
                return None

        return None

    def close_breaker_after_probe(self) -> None:
        """Called after a successful HALF_OPEN probe to transition to CLOSED."""
        ph = self._ph
        with self._lock:
            try:
                row = self._query_one(
                    f"SELECT state FROM circuit_breaker WHERE scope = {ph}",
                    (self.scope,),
                )
                if row and row["state"] == BreakerState.HALF_OPEN.value:
                    self._exec(
                        f"UPDATE circuit_breaker SET state = {ph}, reason = '', "
                        f"opened_at = '', cooldown_until = '' WHERE scope = {ph}",
                        (BreakerState.CLOSED.value, self.scope),
                    )
                    logger.info("Circuit breaker -> CLOSED (probe succeeded)")
            except Exception as e:
                logger.warning("Failed to close breaker after probe: %s", e)

    # ------------------------------------------------------------------
    # Threshold Evaluation
    # ------------------------------------------------------------------

    def _evaluate_thresholds(self) -> None:
        """Evaluate all thresholds and trip breaker if any are violated."""
        ph = self._ph
        now = _utcnow().replace(tzinfo=None)
        window_start = (now - timedelta(seconds=self.window_seconds)).isoformat()

        # 1. Spend velocity: SUM(cost_cents) in window
        row = self._query_one(
            f"SELECT COALESCE(SUM(cost_cents), 0) as total FROM cost_events "
            f"WHERE timestamp >= {ph}",
            (window_start,),
        )
        total_cents = row["total"] if row else 0

        if total_cents > self.max_cents_per_window:
            self._trip_breaker(
                f"Spend velocity {total_cents}c exceeds {self.max_cents_per_window}c "
                f"in {self.window_seconds}s window",
            )
            return

        # 2. Heartbeat frequency: COUNT of non-idle in window
        row = self._query_one(
            f"SELECT COUNT(*) as cnt FROM cost_events "
            f"WHERE timestamp >= {ph} AND status != 'idle'",
            (window_start,),
        )
        non_idle_count = row["cnt"] if row else 0

        if non_idle_count > self.max_heartbeats_per_window:
            self._trip_breaker(
                f"Heartbeat frequency {non_idle_count} non-idle exceeds "
                f"{self.max_heartbeats_per_window} in {self.window_seconds}s window",
            )
            return

        # 3. Consecutive non-idle streak
        rows = self._query_all(
            f"SELECT status FROM cost_events ORDER BY id DESC LIMIT {ph}",
            (self.max_consecutive_non_idle,),
        )

        if (
            len(rows) >= self.max_consecutive_non_idle
            and all(r["status"] != "idle" for r in rows)
        ):
            self._trip_breaker(
                f"Consecutive non-idle streak: {len(rows)} heartbeats "
                f"(threshold: {self.max_consecutive_non_idle})",
            )

    def _trip_breaker(self, reason: str) -> None:
        """Trip the circuit breaker to OPEN state."""
        ph = self._ph
        now = _utcnow().replace(tzinfo=None)

        # Get current trip count for exponential backoff
        row = self._query_one(
            f"SELECT trip_count, state FROM circuit_breaker WHERE scope = {ph}",
            (self.scope,),
        )

        if row and row["state"] == BreakerState.OPEN.value:
            # Already open -- don't re-trip
            return

        old_trip_count = row["trip_count"] if row else 0
        new_trip_count = old_trip_count + 1

        # Exponential backoff: cooldown * 2^(trip_count-1), capped
        cooldown = min(
            self.cooldown_seconds * (2 ** (new_trip_count - 1)),
            self.max_cooldown_seconds,
        )
        cooldown_until = (now + timedelta(seconds=cooldown)).isoformat()

        self._exec(
            f"""
            INSERT INTO circuit_breaker (scope, state, opened_at, reason, cooldown_until, trip_count)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            ON CONFLICT(scope) DO UPDATE SET
                state = excluded.state,
                opened_at = excluded.opened_at,
                reason = excluded.reason,
                cooldown_until = excluded.cooldown_until,
                trip_count = excluded.trip_count
            """,
            (self.scope, BreakerState.OPEN.value, now.isoformat(), reason, cooldown_until, new_trip_count),
        )
        logger.warning(
            "Circuit breaker TRIPPED (trip #%d, cooldown %ds): %s",
            new_trip_count, cooldown, reason,
        )

    # ------------------------------------------------------------------
    # Status & Management
    # ------------------------------------------------------------------

    def get_status(self) -> SpendingSummary:
        """Get current spending status and circuit breaker state."""
        ph = self._ph
        with self._lock:
            now = _utcnow().replace(tzinfo=None)
            window_start = (now - timedelta(seconds=self.window_seconds)).isoformat()

            # Total cost in window
            row = self._query_one(
                f"SELECT COALESCE(SUM(cost_cents), 0) as total FROM cost_events "
                f"WHERE timestamp >= {ph}",
                (window_start,),
            )
            total_cents = row["total"] if row else 0

            # Total heartbeats in window
            row = self._query_one(
                f"SELECT COUNT(*) as cnt FROM cost_events WHERE timestamp >= {ph}",
                (window_start,),
            )
            total_heartbeats = row["cnt"] if row else 0

            # Non-idle heartbeats in window
            row = self._query_one(
                f"SELECT COUNT(*) as cnt FROM cost_events "
                f"WHERE timestamp >= {ph} AND status != 'idle'",
                (window_start,),
            )
            non_idle = row["cnt"] if row else 0

            # Consecutive non-idle streak (use same LIMIT as breaker evaluation)
            rows = self._query_all(
                f"SELECT status FROM cost_events ORDER BY id DESC LIMIT {ph}",
                (self.max_consecutive_non_idle,),
            )
            consecutive = 0
            for r in rows:
                if r["status"] != "idle":
                    consecutive += 1
                else:
                    break

            # Breaker state
            breaker_row = self._query_one(
                f"SELECT * FROM circuit_breaker WHERE scope = {ph}",
                (self.scope,),
            )

            if breaker_row:
                state = BreakerState(breaker_row["state"])
                retry_after = 0
                if state == BreakerState.OPEN and breaker_row["cooldown_until"]:
                    cooldown_dt = datetime.fromisoformat(breaker_row["cooldown_until"])
                    retry_after = max(0, int((cooldown_dt - now).total_seconds()))

                breaker = BreakerStatus(
                    state=state,
                    reason=breaker_row["reason"],
                    opened_at=breaker_row["opened_at"],
                    cooldown_until=breaker_row["cooldown_until"],
                    trip_count=breaker_row["trip_count"],
                    retry_after_seconds=retry_after,
                )
            else:
                breaker = BreakerStatus(state=BreakerState.CLOSED)

            return SpendingSummary(
                window_seconds=self.window_seconds,
                total_cost_cents=total_cents,
                total_heartbeats=total_heartbeats,
                non_idle_heartbeats=non_idle,
                consecutive_non_idle=consecutive,
                breaker=breaker,
            )

    def reset(self) -> None:
        """Reset the circuit breaker to CLOSED and clear trip count."""
        ph = self._ph
        with self._lock:
            self._exec(
                f"""
                INSERT INTO circuit_breaker (scope, state, opened_at, reason, cooldown_until, trip_count)
                VALUES ({ph}, 'closed', '', '', '', 0)
                ON CONFLICT(scope) DO UPDATE SET
                    state = 'closed',
                    opened_at = '',
                    reason = '',
                    cooldown_until = '',
                    trip_count = 0
                """,
                (self.scope,),
            )
            logger.info("Circuit breaker reset to CLOSED")

    def prune(self) -> int:
        """Remove cost events older than retention_days."""
        ph = self._ph
        cutoff = (_utcnow().replace(tzinfo=None) - timedelta(days=self.retention_days)).isoformat()
        with self._lock:
            if self.storage_backend is not None:
                count = self.storage_backend.fetchval(
                    f"SELECT COUNT(*) FROM cost_events WHERE timestamp < {ph}",
                    (cutoff,),
                )
                count = count or 0
                if count > 0:
                    self.storage_backend.execute(
                        f"DELETE FROM cost_events WHERE timestamp < {ph}",
                        (cutoff,),
                    )
                    logger.info("Pruned %d old cost events", count)
                return count
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM cost_events WHERE timestamp < ?",
                    (cutoff,),
                )
                conn.commit()
                removed = cursor.rowcount
                if removed > 0:
                    logger.info("Pruned %d old cost events", removed)
                return removed
            finally:
                conn.close()

    def get_recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get most recent cost events."""
        ph = self._ph
        rows = self._query_all(
            f"SELECT * FROM cost_events ORDER BY id DESC LIMIT {ph}",
            (limit,),
        )
        return [dict(r) for r in rows]
