"""
Artifact Store — SQLite-backed result cache for workflow outputs.

Caches specialist outputs keyed by a composite hash of the specification,
loaded skills, task type, and specialist adapter.  When an identical request
is encountered again, the cached result is returned directly — skipping the
entire specialist execution, tool-calling loop, and critic stages.

Cache lifecycle:
    1. cache_lookup (pre-specialist node) — hash state → probe SQLite
       • HIT  → populate specialist_output, score, tool_calls → skip to format
       • MISS → continue to specialist as normal
    2. cache_store (post-critic node, inside skill_cleanup_wrapper) — record
       approved results for future reuse

Design decisions:
    - SQLite + WAL mode (same pattern as session_store, memory_store)
    - Thread-safe: per-call connection (no shared cursors)
    - TTL-based expiration with LRU access tracking
    - Composite cache key: SHA-256(specification + skills_hash + task_type + adapter)
    - Only caches approved outputs (score >= quality_threshold)
"""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .storage.base import StorageBackend

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """A single cached result."""

    cache_key: str
    specification: str
    specialist_output: str
    output_critic_score: int
    final_score: int
    tool_calls: List[Dict[str, Any]]
    task_type: str
    specialist_adapter: str
    skills_hash: str
    num_iterations: int
    created_at: str
    last_accessed_at: str
    access_count: int
    ttl_seconds: int


class ArtifactStore:
    """
    SQLite-backed result cache for specialist workflow outputs.

    Thread-safe via per-call connections (no shared cursor state).
    Uses WAL mode for concurrent read/write performance.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        max_entries: int = 1000,
        default_ttl_seconds: int = 3600,
        min_score_to_cache: int = 70,
        storage_backend: "Optional[StorageBackend]" = None,
    ):
        """
        Initialize the artifact store.

        Args:
            db_path: Path to the SQLite database.  Defaults to
                     ~/.vibe/artifact_cache.db
            max_entries: Maximum number of cached entries (LRU eviction).
            default_ttl_seconds: Default time-to-live for entries.
            min_score_to_cache: Minimum critic score required to cache a result.
            storage_backend: Optional StorageBackend for multi-node deployment.
                           When provided, all SQL is routed through this backend
                           instead of the built-in SQLite connection.  The backend
                           handles connection pooling, dialect differences, and
                           distributed locking.
        """
        self.storage_backend = storage_backend

        if db_path is None:
            db_path = str(Path.home() / ".vibe" / "artifact_cache.db")

        self.db_path = db_path
        self.max_entries = max_entries
        self.default_ttl_seconds = default_ttl_seconds
        self.min_score_to_cache = min_score_to_cache
        self._lock = threading.Lock()

        if self.storage_backend is None:
            # Ensure parent directory exists
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize schema
        self._init_db()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    _SCHEMA_DDL = """
        CREATE TABLE IF NOT EXISTS artifacts (
            cache_key       TEXT PRIMARY KEY,
            specification   TEXT NOT NULL,
            specialist_output TEXT NOT NULL,
            output_critic_score INTEGER NOT NULL,
            final_score     INTEGER NOT NULL,
            tool_calls_json TEXT NOT NULL DEFAULT '[]',
            task_type       TEXT NOT NULL,
            specialist_adapter TEXT NOT NULL,
            skills_hash     TEXT NOT NULL,
            num_iterations  INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL,
            last_accessed_at TEXT NOT NULL,
            access_count    INTEGER NOT NULL DEFAULT 0,
            ttl_seconds     INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_artifacts_task_type
            ON artifacts(task_type);
        CREATE INDEX IF NOT EXISTS idx_artifacts_last_accessed
            ON artifacts(last_accessed_at);
        CREATE INDEX IF NOT EXISTS idx_artifacts_created
            ON artifacts(created_at);
    """

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        if self.storage_backend is not None:
            self.storage_backend.execute_script(self._SCHEMA_DDL)
            return

        conn = self._connect()
        try:
            conn.executescript(self._SCHEMA_DDL)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection with WAL mode."""
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
    # Cache key computation
    # ------------------------------------------------------------------

    @staticmethod
    def compute_cache_key(
        specification: str,
        loaded_skills: List[Dict[str, Any]],
        task_type: str,
        specialist_adapter: str,
    ) -> str:
        """
        Compute a deterministic cache key from the workflow inputs that
        determine specialist output.

        Args:
            specification: The validated specification string.
            loaded_skills: List of loaded skill dicts (name + content).
            task_type: Routed task type (e.g. "test_generation").
            specialist_adapter: Specialist adapter name.

        Returns:
            SHA-256 hex digest.
        """
        # Sort skills by name for determinism
        skill_names = sorted(s.get("name", "") for s in loaded_skills)
        skills_str = "|".join(skill_names)

        composite = f"{specification}\x00{skills_str}\x00{task_type}\x00{specialist_adapter}"
        return hashlib.sha256(composite.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_skills_hash(loaded_skills: List[Dict[str, Any]]) -> str:
        """Compute a hash of loaded skill names for storage/comparison."""
        skill_names = sorted(s.get("name", "") for s in loaded_skills)
        return hashlib.sha256("|".join(skill_names).encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # Lookup (read path)
    # ------------------------------------------------------------------

    def lookup(self, cache_key: str) -> Optional[CacheEntry]:
        """
        Look up a cached result by key.

        Returns None on miss, expired entry, or error.
        Updates access_count and last_accessed_at on hit.

        Args:
            cache_key: SHA-256 hex digest.

        Returns:
            CacheEntry on hit, None on miss.
        """
        ph = self._ph
        try:
            row = self._query_one(
                f"SELECT * FROM artifacts WHERE cache_key = {ph}",
                (cache_key,),
            )

            if row is None:
                return None

            # Check TTL expiration
            created_str = row["created_at"].rstrip("Z")
            created = datetime.fromisoformat(created_str)
            ttl = row["ttl_seconds"]
            if ttl > 0 and datetime.utcnow() - created > timedelta(seconds=ttl):
                # Expired — delete and return miss
                self._exec(
                    f"DELETE FROM artifacts WHERE cache_key = {ph}",
                    (cache_key,),
                )
                logger.debug(f"Cache entry expired: {cache_key[:12]}...")
                return None

            # Update access metadata
            now = datetime.utcnow().isoformat()
            new_count = row["access_count"] + 1
            self._exec(
                f"UPDATE artifacts SET last_accessed_at = {ph}, access_count = {ph} "
                f"WHERE cache_key = {ph}",
                (now, new_count, cache_key),
            )

            return CacheEntry(
                cache_key=row["cache_key"],
                specification=row["specification"],
                specialist_output=row["specialist_output"],
                output_critic_score=row["output_critic_score"],
                final_score=row["final_score"],
                tool_calls=json.loads(row["tool_calls_json"]),
                task_type=row["task_type"],
                specialist_adapter=row["specialist_adapter"],
                skills_hash=row["skills_hash"],
                num_iterations=row["num_iterations"],
                created_at=row["created_at"],
                last_accessed_at=now,
                access_count=new_count,
                ttl_seconds=row["ttl_seconds"],
            )

        except Exception as e:
            logger.warning(f"Cache lookup failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Store (write path)
    # ------------------------------------------------------------------

    def store(
        self,
        cache_key: str,
        specification: str,
        specialist_output: str,
        output_critic_score: int,
        final_score: int,
        tool_calls: List[Dict[str, Any]],
        task_type: str,
        specialist_adapter: str,
        skills_hash: str,
        num_iterations: int = 1,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """
        Store a result in the cache.

        Only stores if the score meets the minimum threshold.
        Uses INSERT OR REPLACE to handle duplicates (higher score wins).

        Args:
            cache_key: Pre-computed SHA-256 key.
            specification: The specification text.
            specialist_output: The specialist's output text.
            output_critic_score: Critic Stage 2 score (0-100).
            final_score: Final score after all quality gates.
            tool_calls: List of tool call records.
            task_type: Classified task type.
            specialist_adapter: Adapter name used.
            skills_hash: Hash of loaded skill names.
            num_iterations: How many iterations the specialist ran.
            ttl_seconds: Override default TTL (None = use default).

        Returns:
            True if stored, False if rejected (score too low, error, etc.).
        """
        effective_score = final_score if final_score > 0 else output_critic_score
        if effective_score < self.min_score_to_cache:
            logger.debug(
                f"Skipping cache store: score {effective_score} < "
                f"min {self.min_score_to_cache}"
            )
            return False

        if not specialist_output or not specialist_output.strip():
            logger.debug("Skipping cache store: empty specialist output")
            return False

        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        now = datetime.utcnow().isoformat()
        ph = self._ph

        with self._lock:
            try:
                # Check if a higher-scoring entry already exists
                existing = self._query_one(
                    f"SELECT final_score, output_critic_score FROM artifacts "
                    f"WHERE cache_key = {ph}",
                    (cache_key,),
                )

                if existing:
                    existing_score = (
                        existing["final_score"]
                        if existing["final_score"] > 0
                        else existing["output_critic_score"]
                    )
                    if existing_score >= effective_score:
                        logger.debug(
                            f"Cache entry already exists with equal/higher score "
                            f"({existing_score} >= {effective_score})"
                        )
                        return False

                self._exec(
                    f"""
                    INSERT OR REPLACE INTO artifacts (
                        cache_key, specification, specialist_output,
                        output_critic_score, final_score, tool_calls_json,
                        task_type, specialist_adapter, skills_hash,
                        num_iterations, created_at, last_accessed_at,
                        access_count, ttl_seconds
                    ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, 0, {ph})
                    """,
                    (
                        cache_key,
                        specification[:2000],  # Truncate for storage
                        specialist_output,
                        output_critic_score,
                        final_score,
                        json.dumps(tool_calls),
                        task_type,
                        specialist_adapter,
                        skills_hash,
                        num_iterations,
                        now,
                        now,
                        ttl,
                    ),
                )

                # LRU eviction if over capacity
                self._evict_if_needed()

                logger.info(
                    f"Cached artifact: {cache_key[:12]}... "
                    f"(score={effective_score}, task={task_type})"
                )
                return True

            except Exception as e:
                logger.warning(f"Cache store failed: {e}")
                return False

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> int:
        """
        Evict least-recently-used entries if over max_entries.

        Returns:
            Number of entries evicted.
        """
        ph = self._ph
        count_row = self._query_one("SELECT COUNT(*) as cnt FROM artifacts")
        count = count_row["cnt"] if count_row else 0

        if count <= self.max_entries:
            return 0

        excess = count - self.max_entries
        # Delete oldest-accessed entries first
        self._exec(
            f"""
            DELETE FROM artifacts WHERE cache_key IN (
                SELECT cache_key FROM artifacts
                ORDER BY last_accessed_at ASC
                LIMIT {ph}
            )
            """,
            (excess,),
        )
        logger.debug(f"Evicted {excess} cache entries (LRU)")
        return excess

    def cleanup_expired(self) -> int:
        """
        Remove all expired entries.  Intended to be called periodically
        (e.g. from daemon cleanup thread).

        Returns:
            Number of entries removed.
        """
        ph = self._ph
        try:
            now = datetime.utcnow().isoformat()
            if self.storage_backend is not None:
                # storage_backend: use SELECT COUNT then DELETE
                count = self.storage_backend.fetchval(
                    f"SELECT COUNT(*) FROM artifacts "
                    f"WHERE ttl_seconds > 0 "
                    f"  AND datetime(created_at) < datetime({ph}, '-' || ttl_seconds || ' seconds')",
                    (now,),
                )
                count = count or 0
                if count > 0:
                    self._exec(
                        f"DELETE FROM artifacts "
                        f"WHERE ttl_seconds > 0 "
                        f"  AND datetime(created_at) < datetime({ph}, '-' || ttl_seconds || ' seconds')",
                        (now,),
                    )
                    logger.info(f"Cleaned up {count} expired cache entries")
                return count

            # SQLite: use cursor.rowcount
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    DELETE FROM artifacts
                    WHERE ttl_seconds > 0
                      AND datetime(created_at) < datetime(?, '-' || ttl_seconds || ' seconds')
                    """,
                    (now,),
                )
                conn.commit()
                removed = cursor.rowcount
                if removed > 0:
                    logger.info(f"Cleaned up {removed} expired cache entries")
                return removed
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"Cache cleanup failed: {e}")
            return 0

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def invalidate(self, cache_key: str) -> bool:
        """Remove a specific cache entry."""
        ph = self._ph
        if self.storage_backend is not None:
            # storage_backend: use SELECT COUNT then DELETE
            count = self.storage_backend.fetchval(
                f"SELECT COUNT(*) FROM artifacts WHERE cache_key = {ph}",
                (cache_key,),
            )
            count = count or 0
            if count > 0:
                self._exec(
                    f"DELETE FROM artifacts WHERE cache_key = {ph}",
                    (cache_key,),
                )
            return count > 0

        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM artifacts WHERE cache_key = ?",
                (cache_key,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def invalidate_by_task_type(self, task_type: str) -> int:
        """Remove all cache entries for a given task type."""
        ph = self._ph
        if self.storage_backend is not None:
            count = self.storage_backend.fetchval(
                f"SELECT COUNT(*) FROM artifacts WHERE task_type = {ph}",
                (task_type,),
            )
            count = count or 0
            if count > 0:
                self._exec(
                    f"DELETE FROM artifacts WHERE task_type = {ph}",
                    (task_type,),
                )
            return count

        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM artifacts WHERE task_type = ?",
                (task_type,),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def clear(self) -> int:
        """Remove all cache entries."""
        ph = self._ph
        if self.storage_backend is not None:
            count = self.storage_backend.fetchval(
                "SELECT COUNT(*) FROM artifacts",
            )
            count = count or 0
            if count > 0:
                self._exec("DELETE FROM artifacts")
            return count

        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM artifacts")
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return summary statistics about the cache."""
        ph = self._ph
        try:
            total_row = self._query_one(
                "SELECT COUNT(*) as cnt FROM artifacts"
            )
            total = total_row["cnt"] if total_row else 0

            if total == 0:
                return {
                    "total_entries": 0,
                    "total_hits": 0,
                    "by_task_type": {},
                    "oldest_entry": None,
                    "newest_entry": None,
                }

            # Aggregate by task type
            rows = self._query_all(
                """
                SELECT task_type,
                       COUNT(*) as cnt,
                       AVG(final_score) as avg_score,
                       SUM(access_count) as total_hits,
                       AVG(num_iterations) as avg_iterations
                FROM artifacts
                GROUP BY task_type
                """
            )

            by_type = {}
            total_hits = 0
            for row in rows:
                by_type[row["task_type"]] = {
                    "count": row["cnt"],
                    "avg_score": round(row["avg_score"], 1),
                    "total_hits": row["total_hits"],
                    "avg_iterations": round(row["avg_iterations"], 1),
                }
                total_hits += row["total_hits"]

            oldest_row = self._query_one(
                "SELECT MIN(created_at) as val FROM artifacts"
            )
            oldest = oldest_row["val"] if oldest_row else None
            newest_row = self._query_one(
                "SELECT MAX(created_at) as val FROM artifacts"
            )
            newest = newest_row["val"] if newest_row else None

            return {
                "total_entries": total,
                "total_hits": total_hits,
                "max_entries": self.max_entries,
                "by_task_type": by_type,
                "oldest_entry": oldest,
                "newest_entry": newest,
            }
        except Exception as e:
            logger.warning(f"Cache stats failed: {e}")
            return {"total_entries": 0, "error": str(e)}
