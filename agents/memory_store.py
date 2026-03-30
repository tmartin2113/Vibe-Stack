"""
Persistent Memory Store — SQLite-backed long-term memory with citations.

Provides cross-session knowledge retention:
- Store facts, decisions, insights, and learned context with source tracking
- Recall relevant memories via full-text search (FTS5 / BM25 ranking)
- Recall relevant memories via semantic (vector) search using vLLM embeddings
- Hybrid recall merges BM25 keyword + vector semantic results
- Every memory entry carries a citation chain back to its source

Database location: ~/.vibe/memory.db (auto-created)

Architecture:
    Agent stores fact ─► MemoryStore.store() ─► SQLite (entries + FTS5 index + embeddings)
    Agent needs context ─► MemoryStore.recall() ─► BM25 search ─► ranked results with citations
    Agent needs context ─► MemoryStore.semantic_recall() ─► cosine similarity ─► ranked results
    Agent needs context ─► MemoryStore.hybrid_recall() ─► BM25 + vector merged ─► ranked results

Citations:
    Each memory records *where* the information came from:
    - "user" — stated by the user in conversation
    - "url:<url>" — scraped/crawled from a web page
    - "file:<path>" — read from a local file
    - "tool:<tool_name>" — output from a tool execution
    - "agent" — inferred/synthesized by the agent
    Free-form strings are accepted; the above are conventions.

Embeddings:
    Vector embeddings are generated via vLLM's /v1/embeddings endpoint using a
    configurable model (default: nomic-embed-text). Embeddings are optional —
    if vLLM is unreachable or the model isn't loaded, BM25 still works.
    Stored as JSON-serialized float arrays in a separate embeddings table.
"""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .storage.base import StorageBackend

from .embedder import VLLMEmbedder, cosine_similarity

# Backward-compat alias for internal usage
_cosine_similarity = cosine_similarity

logger = logging.getLogger(__name__)

_DB_DIR = Path.home() / ".vibe"
_DB_PATH = _DB_DIR / "memory.db"


def _get_db_path() -> Path:
    """Return the database path, creating the parent directory if needed."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


class MemoryEntry:
    """A single memory with its citation metadata."""

    __slots__ = (
        "memory_id", "content", "source", "tags", "created_at",
        "updated_at", "access_count", "score",
    )

    def __init__(
        self,
        memory_id: int,
        content: str,
        source: str,
        tags: str,
        created_at: str,
        updated_at: str,
        access_count: int = 0,
        score: float = 0.0,
    ):
        self.memory_id = memory_id
        self.content = content
        self.source = source
        self.tags = tags
        self.created_at = created_at
        self.updated_at = updated_at
        self.access_count = access_count
        self.score = score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "source": self.source,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "access_count": self.access_count,
            "score": self.score,
        }

    @property
    def citation(self) -> str:
        """Format a human-readable citation string."""
        if self.source.startswith("url:"):
            return self.source[4:]
        if self.source.startswith("file:"):
            return self.source[5:]
        if self.source.startswith("tool:"):
            return f"[tool: {self.source[5:]}]"
        if self.source == "user":
            return "[user statement]"
        if self.source == "agent":
            return "[agent inference]"
        return f"[{self.source}]"


class MemoryStore:
    """
    Thread-safe SQLite memory store with FTS5 full-text search and
    optional vLLM-based vector embeddings for semantic recall.

    Uses the same per-call connection pattern as SessionStore for
    thread safety under concurrent daemon workers.

    Search modes:
        recall()          — BM25 keyword search (always available)
        semantic_recall() — cosine similarity on vLLM embeddings
        hybrid_recall()   — merged BM25 + vector results (best of both)
    """

    # Maximum number of memories to keep (oldest evicted first)
    MAX_ENTRIES = 10000

    _SCHEMA_DDL = """
        CREATE TABLE IF NOT EXISTS memories (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            content      TEXT NOT NULL,
            source       TEXT NOT NULL DEFAULT 'agent',
            tags         TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_memories_source
            ON memories(source);

        CREATE INDEX IF NOT EXISTS idx_memories_tags
            ON memories(tags);

        CREATE INDEX IF NOT EXISTS idx_memories_updated
            ON memories(updated_at);

        CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id  INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
            embedding  TEXT NOT NULL,
            model      TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        max_entries: int = MAX_ENTRIES,
        embedder: Optional[VLLMEmbedder] = None,
        storage_backend: "Optional[StorageBackend]" = None,
    ):
        self.storage_backend = storage_backend

        if self.storage_backend is None:
            self._db_path = str(db_path or _get_db_path())
        else:
            # storage_backend handles its own storage; skip Path.mkdir
            self._db_path = str(db_path) if db_path else str(_DB_PATH)
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._embedder = embedder  # None = lazy-init on first use
        self._embedder_checked = embedder is not None
        self._init_db()

    # ── Internals ────────────────────────────────────────────────────

    @contextmanager
    def _connect(self):
        """Yield a connection that is committed and closed automatically."""
        conn = sqlite3.connect(self._db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        try:
            yield conn
            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
            raise
        finally:
            conn.close()

    @property
    def _ph(self) -> str:
        """Parameter placeholder — '?' for SQLite, '%s' for PostgreSQL."""
        if self.storage_backend is not None:
            return self.storage_backend.placeholder
        return "?"

    # ── Data-access helpers (route through storage_backend or SQLite) ─

    def _exec(self, sql: str, params: tuple = ()) -> None:
        """Execute a write statement through the appropriate backend."""
        if self.storage_backend is not None:
            self.storage_backend.execute(sql, params)
            return
        conn = sqlite3.connect(self._db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple = ()):
        """Fetch one row as a dict-like object, or None."""
        if self.storage_backend is not None:
            return self.storage_backend.fetchone_dict(sql, params)
        conn = sqlite3.connect(self._db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _query_all(self, sql: str, params: tuple = ()) -> list:
        """Fetch all rows as dict-like objects."""
        if self.storage_backend is not None:
            return self.storage_backend.fetchall_dict(sql, params)
        conn = sqlite3.connect(self._db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    # Default embedding dimension for pgvector (nomic-embed-text = 768).
    # Override via subclass or constructor if using a different model.
    EMBEDDING_DIM = 768

    def _init_db(self):
        """Create tables, FTS5 virtual table, and embeddings table."""
        if self.storage_backend is not None:
            self.storage_backend.execute_script(self._SCHEMA_DDL)
            # Create Postgres FTS index if supported
            if self.storage_backend.supports_fts:
                try:
                    self.storage_backend.execute(
                        "CREATE INDEX IF NOT EXISTS idx_memories_content_fts "
                        "ON memories USING GIN (to_tsvector('english', content))"
                    )
                except Exception as e:
                    logger.debug("FTS index creation skipped: %s", e)
            # Create pgvector embeddings table if supported
            if self.storage_backend.supports_vector:
                try:
                    self.storage_backend.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    dim = self.EMBEDDING_DIM
                    self.storage_backend.execute(
                        "CREATE TABLE IF NOT EXISTS memory_embeddings ("
                        "    memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,"
                        f"    embedding vector({dim}),"
                        "    model TEXT NOT NULL,"
                        "    created_at TEXT NOT NULL"
                        ")"
                    )
                    self.storage_backend.execute(
                        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_vector "
                        "ON memory_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                    )
                except Exception as e:
                    logger.debug("pgvector setup skipped: %s", e)
            return

        with self._connect() as conn:
            conn.executescript(self._SCHEMA_DDL)

            # FTS5 virtual table for full-text search with BM25 ranking.
            # content_rowid links to memories.id for synchronization.
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            ).fetchone()
            if not existing:
                conn.execute("""
                    CREATE VIRTUAL TABLE memories_fts USING fts5(
                        content,
                        tags,
                        content='memories',
                        content_rowid='id'
                    )
                """)
                # Populate FTS from any existing rows (migration path)
                conn.execute("""
                    INSERT INTO memories_fts(rowid, content, tags)
                    SELECT id, content, tags FROM memories
                """)

            # Triggers to keep FTS in sync with the main table
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content, tags)
                    VALUES (new.id, new.content, new.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                    VALUES ('delete', old.id, old.content, old.tags);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                    VALUES ('delete', old.id, old.content, old.tags);
                    INSERT INTO memories_fts(rowid, content, tags)
                    VALUES (new.id, new.content, new.tags);
                END;
            """)
        logger.info(f"Memory store initialized at {self._db_path}")

    # ── Public API ───────────────────────────────────────────────────

    def store(
        self,
        content: str,
        source: str = "agent",
        tags: str = "",
    ) -> int:
        """
        Store a memory entry.

        Args:
            content: The fact, decision, or insight to remember.
            source:  Where this information came from.
                     Conventions: "user", "url:<url>", "file:<path>",
                     "tool:<name>", "agent"
            tags:    Space-separated tags for categorization.
                     e.g. "architecture decision python"

        Returns:
            The memory ID of the stored entry.
        """
        if not content or not content.strip():
            raise ValueError("Memory content cannot be empty")

        content = content.strip()
        source = source.strip() if source else "agent"
        tags = tags.strip() if tags else ""

        now = datetime.utcnow().isoformat() + "Z"
        ph = self._ph

        with self._lock:
            if self.storage_backend is not None:
                # Insert then retrieve the new ID.
                # Can't use fetchone for INSERT RETURNING because
                # fetchone doesn't commit — use execute + fetchval.
                self._exec(
                    f"INSERT INTO memories (content, source, tags, created_at, updated_at) "
                    f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
                    (content, source, tags, now, now),
                )
                row = self._query_one(
                    f"SELECT id FROM memories WHERE content = {ph} AND created_at = {ph}",
                    (content, now),
                )
                memory_id = row["id"] if row else None

                # FIFO eviction if over capacity
                count_row = self._query_one(
                    "SELECT COUNT(*) as total FROM memories"
                )
                count = count_row["total"] if count_row else 0
                if count > self._max_entries:
                    excess = count - self._max_entries
                    self._exec(
                        f"DELETE FROM memories WHERE id IN ("
                        f"SELECT id FROM memories ORDER BY updated_at ASC LIMIT {ph}"
                        f")",
                        (excess,),
                    )
            else:
                with self._connect() as conn:
                    cursor = conn.execute(
                        f"INSERT INTO memories (content, source, tags, created_at, updated_at) "
                        f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
                        (content, source, tags, now, now),
                    )
                    memory_id = cursor.lastrowid

                    # Generate and store embedding (best-effort)
                    embedder = self._get_embedder()
                    if embedder is not None:
                        vec = embedder.embed(content)
                        if vec is not None:
                            conn.execute(
                                f"INSERT OR REPLACE INTO memory_embeddings "
                                f"(memory_id, embedding, model, created_at) "
                                f"VALUES ({ph}, {ph}, {ph}, {ph})",
                                (memory_id, json.dumps(vec), embedder.model, now),
                            )

                    # FIFO eviction if over capacity
                    count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                    if count > self._max_entries:
                        excess = count - self._max_entries
                        conn.execute(
                            f"DELETE FROM memories WHERE id IN ("
                            f"SELECT id FROM memories ORDER BY updated_at ASC LIMIT {ph}"
                            f")",
                            (excess,),
                        )

        logger.debug(f"Stored memory #{memory_id}: {content[:80]}...")
        return memory_id

    def recall(
        self,
        query: str,
        max_results: int = 5,
        tag_filter: str = "",
        source_filter: str = "",
    ) -> List[MemoryEntry]:
        """
        Search memories using full-text search (BM25 ranking).

        Args:
            query:         Search terms (FTS5 query syntax supported).
            max_results:   Maximum number of results to return.
            tag_filter:    If set, only return memories whose tags contain
                           this substring (case-insensitive).
            source_filter: If set, only return memories whose source starts
                           with this prefix (e.g. "url:" for web sources).

        Returns:
            List of MemoryEntry objects, sorted by relevance (best first).
        """
        if not query or not query.strip():
            return []

        max_results = max(1, min(max_results, 50))
        ph = self._ph

        if self.storage_backend is not None:
            if self.storage_backend.supports_fts:
                # Postgres tsvector search with ranking
                conditions = [f"to_tsvector('english', content) @@ plainto_tsquery('english', {ph})"]
                params: list = [query]
                if tag_filter:
                    conditions.append(f"tags LIKE {ph}")
                    params.append(f"%{tag_filter}%")
                if source_filter:
                    conditions.append(f"source = {ph}")
                    params.append(source_filter)
                where = " AND ".join(conditions)
                rows = self._query_all(
                    f"SELECT id, content, source, tags, created_at, updated_at, access_count, "
                    f"ts_rank(to_tsvector('english', content), plainto_tsquery('english', {ph})) as score "
                    f"FROM memories WHERE {where} "
                    f"ORDER BY score DESC LIMIT {ph}",
                    tuple(params + [query] + [max_results]),
                )
                # Update access counts
                for r in rows:
                    self._exec(
                        f"UPDATE memories SET access_count = access_count + 1 WHERE id = {ph}",
                        (r["id"],),
                    )
                results = []
                for row in rows:
                    results.append(MemoryEntry(
                        memory_id=row["id"],
                        content=row["content"],
                        source=row["source"],
                        tags=row["tags"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        access_count=row["access_count"],
                        score=row["score"],
                    ))
                return results
            else:
                # LIKE fallback for backends without FTS
                like_param = f"%{query.strip()}%"
                conditions = [f"content LIKE {ph}"]
                params: list = [like_param]

                if tag_filter:
                    conditions.append(f"LOWER(tags) LIKE {ph}")
                    params.append(f"%{tag_filter.lower()}%")

                if source_filter:
                    conditions.append(f"source LIKE {ph}")
                    params.append(f"{source_filter}%")

                where_clause = " AND ".join(conditions)

                rows = self._query_all(
                    f"SELECT id, content, source, tags, created_at, updated_at, access_count "
                    f"FROM memories WHERE {where_clause} "
                    f"ORDER BY updated_at DESC LIMIT {ph}",
                    tuple(params + [max_results]),
                )

                results = []
                memory_ids = []
                for row in rows:
                    results.append(MemoryEntry(
                        memory_id=row["id"],
                        content=row["content"],
                        source=row["source"],
                        tags=row["tags"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        access_count=row["access_count"],
                        score=0.0,
                    ))
                    memory_ids.append(row["id"])

                # Bump access counts for returned results
                if memory_ids:
                    now = datetime.utcnow().isoformat() + "Z"
                    placeholders = ",".join(ph for _ in memory_ids)
                    self._exec(
                        f"UPDATE memories SET access_count = access_count + 1, "
                        f"updated_at = {ph} WHERE id IN ({placeholders})",
                        tuple([now] + memory_ids),
                    )

                return results

        # SQLite path: use FTS5
        # Build FTS5 query: quote each token to avoid syntax errors
        tokens = query.strip().split()
        fts_query = " OR ".join(f'"{t}"' for t in tokens if t)

        if not fts_query:
            return []

        results = []
        with self._connect() as conn:
            # BM25 returns negative scores (lower = better match).
            # We negate to get positive scores where higher = better.
            rows = conn.execute(
                """SELECT m.id, m.content, m.source, m.tags,
                          m.created_at, m.updated_at, m.access_count,
                          -rank AS score
                   FROM memories_fts fts
                   JOIN memories m ON m.id = fts.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, max_results * 3),  # Over-fetch for filtering
            ).fetchall()

            memory_ids = []
            for row in rows:
                # Apply optional filters
                if tag_filter and tag_filter.lower() not in row["tags"].lower():
                    continue
                if source_filter and not row["source"].startswith(source_filter):
                    continue

                results.append(MemoryEntry(
                    memory_id=row["id"],
                    content=row["content"],
                    source=row["source"],
                    tags=row["tags"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    access_count=row["access_count"],
                    score=row["score"],
                ))
                memory_ids.append(row["id"])

                if len(results) >= max_results:
                    break

            # Bump access counts for returned results
            if memory_ids:
                now = datetime.utcnow().isoformat() + "Z"
                placeholders = ",".join("?" for _ in memory_ids)
                conn.execute(
                    f"UPDATE memories SET access_count = access_count + 1, "
                    f"updated_at = ? WHERE id IN ({placeholders})",
                    [now] + memory_ids,
                )

        return results

    def get_by_id(self, memory_id: int) -> Optional[MemoryEntry]:
        """Retrieve a single memory by its ID."""
        ph = self._ph
        row = self._query_one(
            f"SELECT id, content, source, tags, created_at, "
            f"updated_at, access_count "
            f"FROM memories WHERE id = {ph}",
            (memory_id,),
        )
        if row is None:
            return None
        return MemoryEntry(
            memory_id=row["id"],
            content=row["content"],
            source=row["source"],
            tags=row["tags"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            access_count=row["access_count"],
        )

    def delete(self, memory_id: int) -> bool:
        """Delete a memory by its ID. Returns True if it existed."""
        ph = self._ph
        with self._lock:
            if self.storage_backend is not None:
                # Check existence first, then delete
                row = self._query_one(
                    f"SELECT 1 FROM memories WHERE id = {ph}",
                    (memory_id,),
                )
                if row is None:
                    return False
                self._exec(
                    f"DELETE FROM memories WHERE id = {ph}", (memory_id,)
                )
                return True
            else:
                with self._connect() as conn:
                    cursor = conn.execute(
                        f"DELETE FROM memories WHERE id = {ph}", (memory_id,)
                    )
                    return cursor.rowcount > 0

    def get_stats(self) -> Dict[str, Any]:
        """Return summary statistics about the memory store."""
        ph = self._ph

        total_row = self._query_one("SELECT COUNT(*) as total FROM memories")
        total = total_row["total"] if total_row else 0
        if total == 0:
            return {"total": 0, "by_source": {}, "by_tag": {}}

        # Count by source type
        source_rows = self._query_all(
            """SELECT
                   CASE
                       WHEN source LIKE 'url:%' THEN 'web'
                       WHEN source LIKE 'file:%' THEN 'file'
                       WHEN source LIKE 'tool:%' THEN 'tool'
                       ELSE source
                   END AS source_type,
                   COUNT(*) AS cnt
               FROM memories GROUP BY source_type"""
        )
        by_source = {row["source_type"]: row["cnt"] for row in source_rows}

        # Most common tags
        tag_counts: Dict[str, int] = {}
        tag_rows = self._query_all("SELECT tags FROM memories WHERE tags != ''")
        for row in tag_rows:
            for tag in row["tags"].split():
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        # Top 10 tags
        top_tags = dict(sorted(tag_counts.items(), key=lambda x: -x[1])[:10])

        # Most accessed
        most_accessed = self._query_all(
            "SELECT id, content, access_count FROM memories "
            "ORDER BY access_count DESC LIMIT 5"
        )

        # Embedding coverage
        embedded_row = self._query_one(
            "SELECT COUNT(*) as total FROM memory_embeddings"
        )
        embedded = embedded_row["total"] if embedded_row else 0

        return {
            "total": total,
            "embedded": embedded,
            "by_source": by_source,
            "top_tags": top_tags,
            "most_accessed": [
                {"id": r["id"], "content": r["content"][:100], "access_count": r["access_count"]}
                for r in most_accessed
            ],
        }

    def list_recent(self, limit: int = 10) -> List[MemoryEntry]:
        """Return the most recently updated memories."""
        ph = self._ph
        limit = max(1, min(limit, 100))
        rows = self._query_all(
            f"SELECT id, content, source, tags, created_at, "
            f"updated_at, access_count "
            f"FROM memories ORDER BY updated_at DESC LIMIT {ph}",
            (limit,),
        )
        return [
            MemoryEntry(
                memory_id=row["id"],
                content=row["content"],
                source=row["source"],
                tags=row["tags"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                access_count=row["access_count"],
            )
            for row in rows
        ]

    def cleanup(self, keep: int = 0) -> int:
        """
        Remove oldest memories exceeding the keep limit.

        Args:
            keep: Number of memories to retain. 0 means use max_entries.

        Returns:
            Number of memories deleted.
        """
        ph = self._ph
        keep = keep or self._max_entries
        with self._lock:
            count_row = self._query_one("SELECT COUNT(*) as total FROM memories")
            count = count_row["total"] if count_row else 0
            if count <= keep:
                return 0
            excess = count - keep
            self._exec(
                f"DELETE FROM memories WHERE id IN ("
                f"SELECT id FROM memories ORDER BY updated_at ASC LIMIT {ph}"
                f")",
                (excess,),
            )
            return excess

    # ── Embeddings / Semantic Search ─────────────────────────────────

    def _get_embedder(self) -> Optional[VLLMEmbedder]:
        """Lazy-init the embedder. Returns None if vLLM is unavailable."""
        if not self._embedder_checked:
            self._embedder_checked = True
            self._embedder = VLLMEmbedder()
            if not self._embedder.is_available():
                self._embedder = None
        return self._embedder

    @property
    def has_embeddings(self) -> bool:
        """Whether semantic search is available (embedder reachable)."""
        return self._get_embedder() is not None

    def backfill_embeddings(self, batch_size: int = 50) -> int:
        """Generate embeddings for memories that don't have one yet.

        Useful after enabling embeddings on an existing memory store.

        Returns:
            Number of embeddings generated.
        """
        if self.storage_backend is not None:
            if not self.storage_backend.supports_vector:
                logger.debug("backfill_embeddings: skipped (backend does not support vectors)")
                return 0

            embedder = self._get_embedder()
            if embedder is None:
                return 0

            ph = self._ph
            count = 0
            with self._lock:
                rows = self._query_all(
                    f"SELECT m.id, m.content FROM memories m "
                    f"LEFT JOIN memory_embeddings e ON m.id = e.memory_id "
                    f"WHERE e.memory_id IS NULL "
                    f"ORDER BY m.id LIMIT {ph}",
                    (batch_size,),
                )

                if not rows:
                    return 0

                texts = [row["content"] for row in rows]
                ids = [row["id"] for row in rows]
                vectors = embedder.embed_batch(texts)
                now = datetime.utcnow().isoformat() + "Z"

                for mid, vec in zip(ids, vectors):
                    if vec is not None:
                        vec_str = "[" + ",".join(str(x) for x in vec) + "]"
                        self._exec(
                            f"INSERT INTO memory_embeddings "
                            f"(memory_id, embedding, model, created_at) "
                            f"VALUES ({ph}, {ph}::vector, {ph}, {ph}) "
                            f"ON CONFLICT (memory_id) DO UPDATE SET "
                            f"embedding = EXCLUDED.embedding, model = EXCLUDED.model, "
                            f"created_at = EXCLUDED.created_at",
                            (mid, vec_str, embedder.model, now),
                        )
                        count += 1

            logger.info(f"Backfilled {count}/{len(rows)} embeddings")
            return count

        embedder = self._get_embedder()
        if embedder is None:
            return 0

        count = 0
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT m.id, m.content FROM memories m
                       LEFT JOIN memory_embeddings e ON m.id = e.memory_id
                       WHERE e.memory_id IS NULL
                       ORDER BY m.id
                       LIMIT ?""",
                    (batch_size,),
                ).fetchall()

                if not rows:
                    return 0

                texts = [row["content"] for row in rows]
                ids = [row["id"] for row in rows]
                vectors = embedder.embed_batch(texts)
                now = datetime.utcnow().isoformat() + "Z"

                for mid, vec in zip(ids, vectors):
                    if vec is not None:
                        conn.execute(
                            """INSERT OR REPLACE INTO memory_embeddings
                               (memory_id, embedding, model, created_at)
                               VALUES (?, ?, ?, ?)""",
                            (mid, json.dumps(vec), embedder.model, now),
                        )
                        count += 1

        logger.info(f"Backfilled {count}/{len(rows)} embeddings")
        return count

    def semantic_recall(
        self,
        query: str,
        max_results: int = 5,
        tag_filter: str = "",
        source_filter: str = "",
        min_similarity: float = 0.3,
    ) -> List[MemoryEntry]:
        """
        Search memories using vector cosine similarity.

        Requires vLLM embeddings. Returns empty list if unavailable.

        Args:
            query:          Natural language query to search for.
            max_results:    Maximum number of results to return.
            tag_filter:     If set, only memories with matching tag substring.
            source_filter:  If set, only memories whose source starts with prefix.
            min_similarity: Minimum cosine similarity threshold (0.0-1.0).

        Returns:
            List of MemoryEntry objects sorted by similarity (best first).
        """
        if not query or not query.strip():
            return []

        if self.storage_backend is not None:
            if not self.storage_backend.supports_vector:
                logger.debug("Semantic search not available with this storage backend")
                return []
            if not self._embedder:
                return []
            query_vec = self._embedder.embed(query)
            if query_vec is None:
                return []
            vec_str = "[" + ",".join(str(x) for x in query_vec) + "]"

            max_results = max(1, min(max_results, 50))

            conditions = ["1=1"]
            params_list: list = []
            if tag_filter:
                conditions.append(f"m.tags LIKE {ph}")
                params_list.append(f"%{tag_filter}%")
            if source_filter:
                conditions.append(f"m.source = {ph}")
                params_list.append(source_filter)
            where = " AND ".join(conditions)

            rows = self._query_all(
                f"SELECT m.id, m.content, m.source, m.tags, m.created_at, m.updated_at, "
                f"m.access_count, 1 - (e.embedding <=> {ph}::vector) as score "
                f"FROM memories m JOIN memory_embeddings e ON m.id = e.memory_id "
                f"WHERE {where} AND 1 - (e.embedding <=> {ph}::vector) >= {ph} "
                f"ORDER BY e.embedding <=> {ph}::vector LIMIT {ph}",
                tuple([vec_str] + params_list + [vec_str, min_similarity, vec_str, max_results]),
            )
            for r in rows:
                self._exec(
                    f"UPDATE memories SET access_count = access_count + 1 WHERE id = {ph}",
                    (r["id"],),
                )
            results = []
            for row in rows:
                results.append(MemoryEntry(
                    memory_id=row["id"],
                    content=row["content"],
                    source=row["source"],
                    tags=row["tags"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    access_count=row["access_count"],
                    score=row["score"],
                ))
            return results

        embedder = self._get_embedder()
        if embedder is None:
            return []

        query_vec = embedder.embed(query.strip())
        if query_vec is None:
            return []

        max_results = max(1, min(max_results, 50))

        results: List[MemoryEntry] = []
        with self._connect() as conn:
            # Fetch all embeddings — for stores up to 10K entries this is fast.
            # A full vector DB would use an ANN index; here we brute-force since
            # the max store size is bounded at 10K.
            rows = conn.execute(
                """SELECT m.id, m.content, m.source, m.tags,
                          m.created_at, m.updated_at, m.access_count,
                          e.embedding
                   FROM memory_embeddings e
                   JOIN memories m ON m.id = e.memory_id"""
            ).fetchall()

            scored: List[Tuple[float, sqlite3.Row]] = []
            for row in rows:
                # Apply filters before computing similarity
                if tag_filter and tag_filter.lower() not in row["tags"].lower():
                    continue
                if source_filter and not row["source"].startswith(source_filter):
                    continue

                stored_vec = json.loads(row["embedding"])
                sim = _cosine_similarity(query_vec, stored_vec)
                if sim >= min_similarity:
                    scored.append((sim, row))

            # Sort by similarity descending
            scored.sort(key=lambda x: -x[0])

            memory_ids = []
            for sim, row in scored[:max_results]:
                results.append(MemoryEntry(
                    memory_id=row["id"],
                    content=row["content"],
                    source=row["source"],
                    tags=row["tags"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    access_count=row["access_count"],
                    score=sim,
                ))
                memory_ids.append(row["id"])

            # Bump access counts
            if memory_ids:
                now = datetime.utcnow().isoformat() + "Z"
                placeholders = ",".join("?" for _ in memory_ids)
                conn.execute(
                    f"UPDATE memories SET access_count = access_count + 1, "
                    f"updated_at = ? WHERE id IN ({placeholders})",
                    [now] + memory_ids,
                )

        return results

    def hybrid_recall(
        self,
        query: str,
        max_results: int = 5,
        tag_filter: str = "",
        source_filter: str = "",
        keyword_weight: float = 0.4,
        semantic_weight: float = 0.6,
    ) -> List[MemoryEntry]:
        """
        Merged BM25 keyword + vector semantic search.

        If embeddings are unavailable, falls back to BM25-only.
        Score fusion: normalized BM25 score * keyword_weight +
                      cosine similarity * semantic_weight.

        Args:
            query:           Search query.
            max_results:     Maximum results to return.
            tag_filter:      Tag substring filter.
            source_filter:   Source prefix filter.
            keyword_weight:  Weight for BM25 results (0.0-1.0).
            semantic_weight: Weight for semantic results (0.0-1.0).

        Returns:
            List of MemoryEntry objects sorted by fused score (best first).
        """
        # Get BM25 results (always available)
        bm25_results = self.recall(
            query, max_results=max_results * 2,
            tag_filter=tag_filter, source_filter=source_filter,
        )

        # Get semantic results (may be empty if no embeddings)
        sem_results = self.semantic_recall(
            query, max_results=max_results * 2,
            tag_filter=tag_filter, source_filter=source_filter,
        )

        # If only one source available, return it directly
        if not sem_results:
            return bm25_results[:max_results]
        if not bm25_results:
            return sem_results[:max_results]

        # Normalize BM25 scores to 0-1 range
        bm25_max = max(e.score for e in bm25_results) if bm25_results else 1.0
        bm25_max = max(bm25_max, 0.001)  # avoid division by zero

        # Build lookup: memory_id -> (normalized_bm25, cosine_sim, entry)
        fused: Dict[int, Tuple[float, float, MemoryEntry]] = {}

        for entry in bm25_results:
            norm_bm25 = entry.score / bm25_max
            fused[entry.memory_id] = (norm_bm25, 0.0, entry)

        for entry in sem_results:
            if entry.memory_id in fused:
                old = fused[entry.memory_id]
                fused[entry.memory_id] = (old[0], entry.score, entry)
            else:
                fused[entry.memory_id] = (0.0, entry.score, entry)

        # Compute fused score and sort
        ranked: List[Tuple[float, MemoryEntry]] = []
        for mid, (bm25_score, sem_score, entry) in fused.items():
            combined = bm25_score * keyword_weight + sem_score * semantic_weight
            entry.score = combined
            ranked.append((combined, entry))

        ranked.sort(key=lambda x: -x[0])

        return [entry for _, entry in ranked[:max_results]]
