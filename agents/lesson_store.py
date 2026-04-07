"""Dedicated store for Tier 0 "lessons learned" memory notes.

Scoped to (role, task_type, tag) so the read path can cheaply filter by exact
match. Distinct from memory_store because lessons have different metadata
requirements (outcome_delta, uses, status, decay) that don't make sense for
general-purpose memories.

Thread-safe with per-call SQLite connections (matching session_store.py pattern).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_DEFAULT_DB_DIR = Path.home() / ".vibe"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "lessons.db"


@dataclass
class Lesson:
    lesson_id: str
    role: str                   # "*" for role-agnostic
    task_type: str              # "*" for type-agnostic
    tag: str
    lesson: str
    author_agent_id: str
    author_run_id: str
    created_at: str
    uses: int = 0
    outcome_delta: Optional[float] = None
    last_used_at: Optional[str] = None
    status: str = "active"      # active | decayed | superseded


class LessonStore:
    """SQLite-backed lesson store."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS lessons (
            lesson_id       TEXT PRIMARY KEY,
            role            TEXT NOT NULL,
            task_type       TEXT NOT NULL,
            tag             TEXT NOT NULL DEFAULT '',
            lesson          TEXT NOT NULL,
            author_agent_id TEXT NOT NULL DEFAULT '',
            author_run_id   TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL,
            uses            INTEGER NOT NULL DEFAULT 0,
            outcome_delta   REAL,
            last_used_at    TEXT,
            status          TEXT NOT NULL DEFAULT 'active'
        );
        CREATE INDEX IF NOT EXISTS idx_lessons_scope
            ON lessons(role, task_type, status);
        CREATE INDEX IF NOT EXISTS idx_lessons_outcome
            ON lessons(outcome_delta DESC);

        CREATE TABLE IF NOT EXISTS lesson_uses (
            lesson_id    TEXT NOT NULL,
            run_id       TEXT NOT NULL,
            run_score    INTEGER NOT NULL,
            used_at      TEXT NOT NULL,
            PRIMARY KEY (lesson_id, run_id)
        );
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(self._SCHEMA)

    def add(
        self,
        *,
        role: str,
        task_type: str,
        tag: str,
        lesson: str,
        author_agent_id: str,
        author_run_id: str,
    ) -> str:
        """Insert a new lesson. Returns its lesson_id."""
        lesson_id = f"lesson_{uuid4().hex[:12]}"
        now = datetime.utcnow().isoformat() + "Z"

        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO lessons (lesson_id, role, task_type, tag, lesson, "
                "author_agent_id, author_run_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (lesson_id, role, task_type, tag, lesson,
                 author_agent_id, author_run_id, now),
            )

        return lesson_id

    def list_by_scope(
        self,
        *,
        role: str,
        task_type: str,
        status: str = "active",
        limit: int = 5,
    ) -> List[Lesson]:
        """List lessons matching (role, task_type) with status filter.

        Wildcard matching: lessons with role="*" match any role, and lessons
        with task_type="*" match any task_type. Results are ordered by
        outcome_delta DESC (best-performing first).
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM lessons "
                "WHERE (role = ? OR role = '*') "
                "AND (task_type = ? OR task_type = '*') "
                "AND status = ? "
                "ORDER BY outcome_delta DESC NULLS LAST, created_at DESC "
                "LIMIT ?",
                (role, task_type, status, limit),
            ).fetchall()

        return [self._row_to_lesson(r) for r in rows]

    def set_status(self, lesson_id: str, status: str) -> None:
        """Update a lesson's status (active/decayed/superseded)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE lessons SET status = ? WHERE lesson_id = ?",
                (status, lesson_id),
            )

    def record_use(self, lesson_id: str, run_id: str, run_score: int) -> None:
        """Record that a run used this lesson, with the run's final score.

        Idempotent per (lesson_id, run_id) — duplicate calls have no effect.
        """
        now = datetime.utcnow().isoformat() + "Z"
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO lesson_uses (lesson_id, run_id, run_score, used_at) "
                    "VALUES (?, ?, ?, ?)",
                    (lesson_id, run_id, run_score, now),
                )
            except sqlite3.IntegrityError:
                # Duplicate (lesson_id, run_id) — idempotent no-op
                return

            # Update denormalized uses count + last_used_at
            conn.execute(
                "UPDATE lessons SET uses = (SELECT COUNT(*) FROM lesson_uses "
                "WHERE lesson_id = ?), last_used_at = ? WHERE lesson_id = ?",
                (lesson_id, now, lesson_id),
            )

    def recompute_outcome_delta(
        self, lesson_id: str, baseline_score: float,
    ) -> Optional[float]:
        """Recompute and persist outcome_delta for a lesson.

        outcome_delta = avg(run_scores for uses of this lesson) - baseline_score

        Returns the new delta, or None if the lesson has no uses yet.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT AVG(run_score) AS avg_score, COUNT(*) AS n "
                "FROM lesson_uses WHERE lesson_id = ?",
                (lesson_id,),
            ).fetchone()

            if row is None or row["n"] == 0:
                return None

            delta = float(row["avg_score"]) - float(baseline_score)
            conn.execute(
                "UPDATE lessons SET outcome_delta = ? WHERE lesson_id = ?",
                (delta, lesson_id),
            )
            return delta

    def _row_to_lesson(self, row: sqlite3.Row) -> Lesson:
        return Lesson(
            lesson_id=row["lesson_id"],
            role=row["role"],
            task_type=row["task_type"],
            tag=row["tag"],
            lesson=row["lesson"],
            author_agent_id=row["author_agent_id"],
            author_run_id=row["author_run_id"],
            created_at=row["created_at"],
            uses=row["uses"],
            outcome_delta=row["outcome_delta"],
            last_used_at=row["last_used_at"],
            status=row["status"],
        )
