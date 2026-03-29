"""MessageStore — SQLite + WAL + FTS5 + embeddings for inter-agent messages.

Replaces the file-based BULLETIN.md with a proper database-backed message store.
Follows the MemoryStore pattern: threading.Lock on writes, per-call _connect()
with WAL mode, lazy VLLMEmbedder for semantic search.

Database location: /shared/bulletin/messages.db (Docker volume) or configurable
via MESSAGE_STORE_PATH env var.

On first startup, auto-migrates existing BULLETIN.md entries if present.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .storage.base import StorageBackend

from .embedder import VLLMEmbedder, cosine_similarity, get_shared_embedder
from .message_types import (
    BROADCAST,
    DEFAULT_TTL_SECONDS,
    HIGH_PRIORITY_TYPES,
    Message,
    MessageType,
    validate_metadata,
)

# Backward-compat alias
_cosine_similarity = cosine_similarity

logger = logging.getLogger(__name__)

# Maximum messages before FIFO eviction
MAX_MESSAGES = 5000

# Default DB path on Docker volume
_DEFAULT_DB_DIR = Path("/shared/bulletin")
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "messages.db"


def _get_db_path() -> Path:
    """Return the configured database path."""
    env_path = os.environ.get("MESSAGE_STORE_PATH")
    if env_path:
        p = Path(env_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    # Fall back to shared volume path
    _DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_DB_PATH


def _get_agent_name() -> str:
    """Determine the current agent's display name."""
    return (
        os.environ.get("VIBE_AGENT_NAME")
        or os.environ.get("PAPERCLIP_AGENT_ID")
        or "unknown-agent"
    )


class MessageStore:
    """SQLite-backed message store with FTS5 full-text search and embeddings.

    Thread-safe: write operations acquire self._lock, reads use WAL
    concurrent-read capability.
    """

    MAX_MESSAGES = MAX_MESSAGES

    _SCHEMA_DDL = """
        CREATE TABLE IF NOT EXISTS messages (
            id            TEXT PRIMARY KEY,
            sender        TEXT NOT NULL DEFAULT '',
            recipient     TEXT NOT NULL DEFAULT '*',
            msg_type      TEXT NOT NULL DEFAULT 'info',
            topic         TEXT NOT NULL DEFAULT '',
            content       TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            parent_id     TEXT REFERENCES messages(id) ON DELETE SET NULL,
            issue_id      TEXT,
            paperclip_comment_id TEXT,
            ttl_seconds   INTEGER NOT NULL DEFAULT 604800,
            created_at    TEXT NOT NULL,
            expires_at    TEXT,
            read_by       TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS idx_messages_created
            ON messages(created_at);
        CREATE INDEX IF NOT EXISTS idx_messages_recipient
            ON messages(recipient);
        CREATE INDEX IF NOT EXISTS idx_messages_msg_type
            ON messages(msg_type);
        CREATE INDEX IF NOT EXISTS idx_messages_parent_id
            ON messages(parent_id);
        CREATE INDEX IF NOT EXISTS idx_messages_topic
            ON messages(topic);
        CREATE INDEX IF NOT EXISTS idx_messages_expires_at
            ON messages(expires_at);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            topic,
            sender,
            content='messages',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, topic, sender)
            VALUES (new.rowid, new.content, new.topic, new.sender);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, topic, sender)
            VALUES ('delete', old.rowid, old.content, old.topic, old.sender);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content, topic, sender)
            VALUES ('delete', old.rowid, old.content, old.topic, old.sender);
            INSERT INTO messages_fts(rowid, content, topic, sender)
            VALUES (new.rowid, new.content, new.topic, new.sender);
        END;

        CREATE TABLE IF NOT EXISTS message_embeddings (
            message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            embedding  TEXT NOT NULL,
            model      TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        vllm_url: Optional[str] = None,
        embed_model: Optional[str] = None,
        config: "Optional[MessageStoreConfig]" = None,
        storage_backend: "Optional[StorageBackend]" = None,
    ):
        self.storage_backend = storage_backend

        if config and config.db_path:
            self._db_path = config.db_path
        elif self.storage_backend is None:
            self._db_path = str(db_path or _get_db_path())
        else:
            # storage_backend handles its own storage; skip Path.mkdir
            self._db_path = str(db_path) if db_path else str(_DEFAULT_DB_PATH)
        self._lock = threading.Lock()
        self._embedder: Optional[VLLMEmbedder] = None
        self._config = config
        self._init_db()

    @contextmanager
    def _connect(self):
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

    def _get_embedder(self) -> VLLMEmbedder:
        if self._embedder is None:
            self._embedder = get_shared_embedder()
        return self._embedder

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

    # Default embedding dimension (nomic-embed-text = 768).
    # Override via subclass or instance attribute if your model differs.
    EMBEDDING_DIM = 768

    def _init_db(self):
        if self.storage_backend is not None:
            self.storage_backend.execute_script(self._SCHEMA_DDL)
            # Create Postgres FTS index if supported
            if self.storage_backend.supports_fts:
                try:
                    self.storage_backend.execute(
                        "CREATE INDEX IF NOT EXISTS idx_messages_content_fts "
                        "ON messages USING GIN (to_tsvector('english', content))"
                    )
                except Exception as e:
                    logger.debug("FTS index creation skipped: %s", e)
            # Create pgvector embeddings table if supported
            if self.storage_backend.supports_vector:
                try:
                    self.storage_backend.execute(
                        "CREATE EXTENSION IF NOT EXISTS vector"
                    )
                    dim = self.EMBEDDING_DIM
                    self.storage_backend.execute(
                        "CREATE TABLE IF NOT EXISTS message_embeddings ("
                        "    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,"
                        f"    embedding vector({dim}),"
                        "    model TEXT NOT NULL,"
                        "    created_at TEXT NOT NULL"
                        ")"
                    )
                    self.storage_backend.execute(
                        "CREATE INDEX IF NOT EXISTS idx_message_embeddings_vector "
                        "ON message_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                    )
                except Exception as e:
                    logger.debug("pgvector setup skipped: %s", e)
            return

        with self._connect() as conn:
            conn.executescript(self._SCHEMA_DDL)

    def _row_to_message(self, row: sqlite3.Row) -> Message:
        """Convert a database row to a Message object."""
        read_by = json.loads(row["read_by"]) if row["read_by"] else []
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        return Message(
            id=row["id"],
            sender=row["sender"],
            recipient=row["recipient"],
            msg_type=row["msg_type"],
            topic=row["topic"],
            content=row["content"],
            metadata=metadata,
            parent_id=row["parent_id"],
            issue_id=row["issue_id"],
            paperclip_comment_id=row["paperclip_comment_id"],
            ttl_seconds=row["ttl_seconds"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            read_by=read_by,
            score=0.0,
        )

    # ── Write operations (locked) ────────────────────────────────

    def send(
        self,
        content: str,
        sender: Optional[str] = None,
        recipient: str = BROADCAST,
        msg_type: MessageType = MessageType.INFO,
        topic: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        parent_id: Optional[str] = None,
        issue_id: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> Message:
        """Post a new message. Returns the created Message."""
        ph = self._ph
        msg = Message(
            sender=sender or _get_agent_name(),
            recipient=recipient,
            msg_type=msg_type,
            topic=topic,
            content=content,
            metadata=metadata or {},
            parent_id=parent_id,
            issue_id=issue_id,
            ttl_seconds=ttl_seconds,
        )

        # Advisory payload validation
        warnings = validate_metadata(msg.msg_type, msg.metadata)
        for w in warnings:
            logger.debug("Metadata validation: %s", w)

        with self._lock:
            self._exec(
                f"""
                INSERT INTO messages
                    (id, sender, recipient, msg_type, topic, content,
                     metadata_json, parent_id, issue_id,
                     paperclip_comment_id, ttl_seconds, created_at,
                     expires_at, read_by)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                """,
                (
                    msg.id,
                    msg.sender,
                    msg.recipient,
                    msg.msg_type.value,
                    msg.topic,
                    msg.content,
                    json.dumps(msg.metadata),
                    msg.parent_id,
                    msg.issue_id,
                    msg.paperclip_comment_id,
                    msg.ttl_seconds,
                    msg.created_at,
                    msg.expires_at,
                    json.dumps(msg.read_by),
                ),
            )

            # FIFO eviction
            self._evict_if_needed()

            # Generate embedding (outside lock — network I/O)
            self._store_embedding(msg.id, msg.content)

        logger.info(
            f"Message sent: {msg.msg_type.value} from {msg.sender} "
            f"to {msg.recipient} (topic={msg.topic})"
        )
        return msg

    def reply(
        self,
        parent_id: str,
        content: str,
        sender: Optional[str] = None,
        msg_type: Optional[MessageType] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        """Reply to an existing message. Inherits topic/issue from parent."""
        ph = self._ph
        row = self._query_one(
            f"SELECT * FROM messages WHERE id = {ph}", (parent_id,)
        )
        if not row:
            raise ValueError(f"Parent message {parent_id} not found")

        parent = self._row_to_message(row)
        # Find thread root
        root_id = parent.parent_id or parent.id

        return self.send(
            content=content,
            sender=sender,
            recipient=parent.sender,  # Reply to sender
            msg_type=msg_type or parent.msg_type,
            topic=parent.topic,
            metadata=metadata or {},
            parent_id=root_id,
            issue_id=parent.issue_id,
            ttl_seconds=parent.ttl_seconds,
        )

    def mark_read(self, message_id: str, agent_name: Optional[str] = None) -> None:
        """Mark a message as read by the given agent."""
        ph = self._ph
        agent = agent_name or _get_agent_name()
        with self._lock:
            row = self._query_one(
                f"SELECT read_by FROM messages WHERE id = {ph}",
                (message_id,),
            )
            if not row:
                return
            read_by = json.loads(row["read_by"] or "[]")
            if agent not in read_by:
                read_by.append(agent)
                self._exec(
                    f"UPDATE messages SET read_by = {ph} WHERE id = {ph}",
                    (json.dumps(read_by), message_id),
                )

    def mark_many_read(
        self, message_ids: List[str], agent_name: Optional[str] = None
    ) -> None:
        """Mark multiple messages as read."""
        for mid in message_ids:
            self.mark_read(mid, agent_name)

    def update_paperclip_comment_id(
        self, message_id: str, comment_id: str
    ) -> None:
        """Update the Paperclip comment ID after dual-write."""
        ph = self._ph
        with self._lock:
            self._exec(
                f"UPDATE messages SET paperclip_comment_id = {ph} WHERE id = {ph}",
                (comment_id, message_id),
            )

    def cleanup_expired(self) -> int:
        """Delete expired messages. Returns count deleted."""
        ph = self._ph
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            if self.storage_backend is not None:
                # Backend path: SELECT COUNT first, then DELETE
                row = self._query_one(
                    f"SELECT COUNT(*) as cnt FROM messages "
                    f"WHERE expires_at IS NOT NULL AND expires_at < {ph}",
                    (now,),
                )
                count = row["cnt"] if row else 0
                if count > 0:
                    self._exec(
                        f"DELETE FROM messages "
                        f"WHERE expires_at IS NOT NULL AND expires_at < {ph}",
                        (now,),
                    )
            else:
                conn = sqlite3.connect(self._db_path, timeout=15)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                try:
                    cursor = conn.execute(
                        """
                        DELETE FROM messages
                        WHERE expires_at IS NOT NULL AND expires_at < ?
                        """,
                        (now,),
                    )
                    conn.commit()
                    count = cursor.rowcount
                finally:
                    conn.close()
        if count > 0:
            logger.info(f"Cleaned up {count} expired messages")
        return count

    def _evict_if_needed(self) -> int:
        """FIFO eviction when over MAX_MESSAGES. Must be called within lock."""
        ph = self._ph
        row = self._query_one("SELECT COUNT(*) as cnt FROM messages")
        count = row["cnt"]
        if count <= self.MAX_MESSAGES:
            return 0
        excess = count - self.MAX_MESSAGES
        self._exec(
            f"""
            DELETE FROM messages WHERE id IN (
                SELECT id FROM messages ORDER BY created_at ASC LIMIT {ph}
            )
            """,
            (excess,),
        )
        logger.info(f"Evicted {excess} oldest messages (FIFO)")
        return excess

    def _store_embedding(self, message_id: str, content: str) -> None:
        """Generate and store embedding for a message. Best-effort."""
        if self.storage_backend is not None:
            if not self.storage_backend.supports_vector:
                logger.debug("Embedding storage not available with external storage backend")
                return
            try:
                embedder = self._get_embedder()
                vec = embedder.embed(content)
                if vec is None:
                    return
                vec_str = "[" + ",".join(str(x) for x in vec) + "]"
                ph = self._ph
                self._exec(
                    f"INSERT INTO message_embeddings "
                    f"    (message_id, embedding, model, created_at) "
                    f"VALUES ({ph}, {ph}::vector, {ph}, {ph}) "
                    f"ON CONFLICT (message_id) DO UPDATE SET "
                    f"    embedding = EXCLUDED.embedding, "
                    f"    model = EXCLUDED.model, "
                    f"    created_at = EXCLUDED.created_at",
                    (
                        message_id,
                        vec_str,
                        embedder.model,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except Exception as e:
                logger.debug(f"Embedding storage failed for {message_id}: {e}")
            return
        try:
            embedder = self._get_embedder()
            vec = embedder.embed(content)
            if vec is None:
                return
            ph = self._ph
            self._exec(
                f"""
                INSERT OR REPLACE INTO message_embeddings
                    (message_id, embedding, model, created_at)
                VALUES ({ph}, {ph}, {ph}, {ph})
                """,
                (
                    message_id,
                    json.dumps(vec),
                    embedder.model,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except Exception as e:
            logger.debug(f"Embedding storage failed for {message_id}: {e}")

    def backfill_embeddings(self, batch_size: int = 50) -> int:
        """Generate embeddings for messages that don't have one yet.

        Returns count of newly embedded messages.
        """
        if self.storage_backend is not None:
            if not self.storage_backend.supports_vector:
                logger.debug("Embedding backfill not available with external storage backend")
                return 0

            embedder = self._get_embedder()
            if not embedder.is_available():
                return 0

            ph = self._ph
            count = 0
            with self._lock:
                rows = self._query_all(
                    f"""SELECT m.id, m.content FROM messages m
                       LEFT JOIN message_embeddings e ON m.id = e.message_id
                       WHERE e.message_id IS NULL
                       ORDER BY m.created_at
                       LIMIT {ph}""",
                    (batch_size,),
                )

                if not rows:
                    return 0

                texts = [row["content"] for row in rows]
                ids = [row["id"] for row in rows]
                vectors = embedder.embed_batch(texts)
                now = datetime.now(timezone.utc).isoformat()

                for mid, vec in zip(ids, vectors):
                    if vec is not None:
                        vec_str = "[" + ",".join(str(x) for x in vec) + "]"
                        self._exec(
                            f"INSERT INTO message_embeddings "
                            f"    (message_id, embedding, model, created_at) "
                            f"VALUES ({ph}, {ph}::vector, {ph}, {ph}) "
                            f"ON CONFLICT (message_id) DO UPDATE SET "
                            f"    embedding = EXCLUDED.embedding, "
                            f"    model = EXCLUDED.model, "
                            f"    created_at = EXCLUDED.created_at",
                            (mid, vec_str, embedder.model, now),
                        )
                        count += 1

            if count:
                logger.info("Backfilled %d/%d message embeddings", count, len(rows))
            return count

        embedder = self._get_embedder()
        if not embedder.is_available():
            return 0

        ph = self._ph
        count = 0
        with self._lock:
            rows = self._query_all(
                f"""SELECT m.id, m.content FROM messages m
                   LEFT JOIN message_embeddings e ON m.id = e.message_id
                   WHERE e.message_id IS NULL
                   ORDER BY m.created_at
                   LIMIT {ph}""",
                (batch_size,),
            )

            if not rows:
                return 0

            texts = [row["content"] for row in rows]
            ids = [row["id"] for row in rows]
            vectors = embedder.embed_batch(texts)
            now = datetime.now(timezone.utc).isoformat()

            for mid, vec in zip(ids, vectors):
                if vec is not None:
                    self._exec(
                        f"""INSERT OR REPLACE INTO message_embeddings
                           (message_id, embedding, model, created_at)
                           VALUES ({ph}, {ph}, {ph}, {ph})""",
                        (mid, json.dumps(vec), embedder.model, now),
                    )
                    count += 1

        if count:
            logger.info("Backfilled %d/%d message embeddings", count, len(rows))
        return count

    # ── Read operations (no lock — WAL concurrent reads) ─────────

    def read_recent(
        self,
        limit: int = 20,
        recipient: Optional[str] = None,
        type_filter: Optional[List[MessageType]] = None,
        topic: Optional[str] = None,
        include_expired: bool = False,
    ) -> List[Message]:
        """Read recent messages with optional filters."""
        ph = self._ph
        conditions = []
        params: List[Any] = []

        if not include_expired:
            now = datetime.now(timezone.utc).isoformat()
            conditions.append(
                f"(expires_at IS NULL OR expires_at >= {ph})"
            )
            params.append(now)

        if recipient:
            conditions.append(f"(recipient = {ph} OR recipient = '*')")
            params.append(recipient)

        if type_filter:
            placeholders = ",".join(ph for _ in type_filter)
            conditions.append(f"msg_type IN ({placeholders})")
            params.extend(t.value for t in type_filter)

        if topic:
            conditions.append(f"topic = {ph}")
            params.append(topic)

        where = " AND ".join(conditions) if conditions else "1=1"
        limit = max(1, min(limit, 500))
        params.append(limit)

        rows = self._query_all(
            f"""
            SELECT * FROM messages
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT {ph}
            """,
            tuple(params),
        )

        return [self._row_to_message(r) for r in reversed(rows)]

    def get_unread(
        self,
        agent_name: Optional[str] = None,
        limit: int = 50,
    ) -> List[Message]:
        """Get messages not yet read by the given agent."""
        ph = self._ph
        agent = agent_name or _get_agent_name()
        now = datetime.now(timezone.utc).isoformat()

        rows = self._query_all(
            f"""
            SELECT * FROM messages
            WHERE (recipient = {ph} OR recipient = '*')
              AND (expires_at IS NULL OR expires_at >= {ph})
              AND read_by NOT LIKE {ph}
            ORDER BY created_at DESC
            LIMIT {ph}
            """,
            (agent, now, f'%"{agent}"%', limit),
        )

        return [self._row_to_message(r) for r in reversed(rows)]

    def get_thread(self, message_id: str) -> List[Message]:
        """Get all messages in a thread (by root parent_id)."""
        ph = self._ph
        # Find root: if message_id is a root, parent_id is NULL
        row = self._query_one(
            f"SELECT parent_id FROM messages WHERE id = {ph}",
            (message_id,),
        )
        if not row:
            return []

        root_id = row["parent_id"] or message_id

        rows = self._query_all(
            f"""
            SELECT * FROM messages
            WHERE id = {ph} OR parent_id = {ph}
            ORDER BY created_at ASC
            """,
            (root_id, root_id),
        )

        return [self._row_to_message(r) for r in rows]

    def get_by_id(self, message_id: str) -> Optional[Message]:
        """Get a single message by ID."""
        ph = self._ph
        row = self._query_one(
            f"SELECT * FROM messages WHERE id = {ph}", (message_id,)
        )
        if not row:
            return None
        return self._row_to_message(row)

    # ── Search operations ────────────────────────────────────────

    def search(self, query: str, max_results: int = 20) -> List[Message]:
        """Full-text search using FTS5 BM25 ranking."""
        if not query.strip():
            return []

        if self.storage_backend is not None:
            if not self.storage_backend.supports_fts:
                logger.debug("FTS search not available with this storage backend")
                return []
            # Postgres tsvector search with ranking
            ph = self._ph
            max_results = max(1, min(max_results, 200))
            now = datetime.now(timezone.utc).isoformat()
            rows = self._query_all(
                f"SELECT *, ts_rank(to_tsvector('english', content), plainto_tsquery('english', {ph})) as score "
                f"FROM messages "
                f"WHERE to_tsvector('english', content) @@ plainto_tsquery('english', {ph}) "
                f"AND (expires_at IS NULL OR expires_at >= {ph}) "
                f"ORDER BY score DESC LIMIT {ph}",
                (query, query, now, max_results),
            )
            messages = []
            for r in rows:
                msg = self._row_to_message(r)
                msg.score = float(r["score"])
                messages.append(msg)
            return messages

        # Escape FTS5 special characters
        safe_query = re.sub(r'[^\w\s]', ' ', query).strip()
        if not safe_query:
            return []

        # Use prefix matching for better recall
        terms = safe_query.split()
        fts_query = " OR ".join(f'"{t}"*' for t in terms if t)

        now = datetime.now(timezone.utc).isoformat()
        max_results = max(1, min(max_results, 200))

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*, -rank as score
                FROM messages m
                JOIN messages_fts ON messages_fts.rowid = m.rowid
                WHERE messages_fts MATCH ?
                  AND (m.expires_at IS NULL OR m.expires_at >= ?)
                ORDER BY score DESC
                LIMIT ?
                """,
                (fts_query, now, max_results),
            ).fetchall()

        messages = []
        for r in rows:
            msg = self._row_to_message(r)
            msg.score = float(r["score"])
            messages.append(msg)
        return messages

    def semantic_search(
        self, query: str, max_results: int = 10
    ) -> List[Message]:
        """Semantic search using cosine similarity on embeddings."""
        if self.storage_backend is not None:
            if not self.storage_backend.supports_vector:
                logger.debug("Semantic search not available with this storage backend")
                return []
            # pgvector cosine distance search
            if not self._embedder:
                self._embedder = self._get_embedder()
            query_vec = self._embedder.embed(query)
            if query_vec is None:
                return []
            vec_str = "[" + ",".join(str(x) for x in query_vec) + "]"
            ph = self._ph
            now = datetime.now(timezone.utc).isoformat()
            rows = self._query_all(
                f"SELECT m.*, 1 - (e.embedding <=> {ph}::vector) as score "
                f"FROM messages m "
                f"JOIN message_embeddings e ON m.id = e.message_id "
                f"WHERE m.expires_at IS NULL OR m.expires_at >= {ph} "
                f"ORDER BY e.embedding <=> {ph}::vector "
                f"LIMIT {ph}",
                (vec_str, now, vec_str, max_results),
            )
            messages = []
            for r in rows:
                msg = self._row_to_message(r)
                msg.score = float(r["score"])
                messages.append(msg)
            return messages

        embedder = self._get_embedder()
        query_vec = embedder.embed(query)
        if query_vec is None:
            return []

        now = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*, me.embedding
                FROM messages m
                JOIN message_embeddings me ON me.message_id = m.id
                WHERE m.expires_at IS NULL OR m.expires_at >= ?
                """,
                (now,),
            ).fetchall()

        scored: List[Tuple[float, Message]] = []
        for r in rows:
            try:
                stored_vec = json.loads(r["embedding"])
                sim = _cosine_similarity(query_vec, stored_vec)
                if sim > 0.0:
                    msg = self._row_to_message(r)
                    msg.score = sim
                    scored.append((sim, msg))
            except (json.JSONDecodeError, TypeError):
                continue

        scored.sort(key=lambda x: x[0], reverse=True)
        return [msg for _, msg in scored[:max_results]]

    def hybrid_search(
        self,
        query: str,
        max_results: int = 10,
        keyword_weight: float = 0.4,
        semantic_weight: float = 0.6,
    ) -> List[Message]:
        """Merged BM25 + semantic search (same fusion as MemoryStore)."""
        bm25_results = self.search(query, max_results=max_results * 2)
        sem_results = self.semantic_search(query, max_results=max_results * 2)

        if not bm25_results and not sem_results:
            return []

        # Normalize BM25 scores
        if bm25_results:
            max_bm25 = max(m.score for m in bm25_results)
            if max_bm25 > 0:
                for m in bm25_results:
                    m.score = m.score / max_bm25

        # Semantic scores already in [0, 1]

        # Merge by message ID
        merged: Dict[str, Message] = {}
        for m in bm25_results:
            merged[m.id] = m
            m.score = m.score * keyword_weight

        for m in sem_results:
            if m.id in merged:
                merged[m.id].score += m.score * semantic_weight
            else:
                m.score = m.score * semantic_weight
                merged[m.id] = m

        results = sorted(merged.values(), key=lambda m: m.score, reverse=True)
        return results[:max_results]

    # ── Context injection helpers ────────────────────────────────

    def relevant_messages(
        self,
        query: str,
        agent_name: Optional[str] = None,
        max_results: int = 10,
    ) -> List[Message]:
        """Smart retrieval for inject_memory: combines hybrid search,
        unread messages, and high-priority types.

        Returns a deduplicated, scored list of the most relevant messages.
        """
        agent = agent_name or _get_agent_name()
        seen_ids: set = set()
        results: List[Message] = []

        # 1. Unread messages for this agent (always surface these)
        unread = self.get_unread(agent_name=agent, limit=max_results)
        for msg in unread:
            if msg.id not in seen_ids:
                msg.score = 1.0  # High priority
                results.append(msg)
                seen_ids.add(msg.id)

        # 2. High-priority types (decisions, blockers, handoffs)
        recent_priority = self.read_recent(
            limit=max_results,
            type_filter=list(HIGH_PRIORITY_TYPES),
        )
        for msg in recent_priority:
            if msg.id not in seen_ids:
                msg.score = 0.8
                results.append(msg)
                seen_ids.add(msg.id)

        # 3. Hybrid search on query (if we have a query)
        if query.strip():
            hybrid = self.hybrid_search(query, max_results=max_results)
            for msg in hybrid:
                if msg.id not in seen_ids:
                    results.append(msg)
                    seen_ids.add(msg.id)

        # Sort by score descending, then by created_at descending
        results.sort(key=lambda m: (m.score, m.created_at), reverse=True)
        return results[:max_results]

    def get_stats(self) -> Dict[str, Any]:
        """Return statistics about the message store."""
        total_row = self._query_one(
            "SELECT COUNT(*) as cnt FROM messages"
        )
        total = total_row["cnt"]

        by_type = self._query_all(
            """
            SELECT msg_type, COUNT(*) as cnt
            FROM messages GROUP BY msg_type
            """
        )

        threads_row = self._query_one(
            """
            SELECT COUNT(DISTINCT COALESCE(parent_id, id)) as cnt
            FROM messages
            """
        )
        threads = threads_row["cnt"]

        embedding_count_row = self._query_one(
            "SELECT COUNT(*) as cnt FROM message_embeddings"
        )
        embedding_count = embedding_count_row["cnt"]

        return {
            "total_messages": total,
            "by_type": {r["msg_type"]: r["cnt"] for r in by_type},
            "thread_count": threads,
            "embedding_count": embedding_count,
            "max_messages": self.MAX_MESSAGES,
        }


# ── v1 Migration ─────────────────────────────────────────────────


_V1_ENTRY_RE = re.compile(
    r"^### \[(?P<timestamp>[^\]]+)\] (?P<agent>.+?)$",
    re.MULTILINE,
)


def migrate_v1_bulletin(
    bulletin_path: str, store: MessageStore
) -> int:
    """Import entries from a v1 BULLETIN.md file into the MessageStore.

    Idempotent: skips if the store already has entries.
    Returns count of imported entries.
    """
    path = Path(bulletin_path)
    if not path.exists():
        return 0

    # Skip if store already has data (idempotent)
    stats = store.get_stats()
    if stats["total_messages"] > 0:
        logger.info("MessageStore already has data, skipping v1 migration")
        return 0

    text = path.read_text(encoding="utf-8")
    matches = list(_V1_ENTRY_RE.finditer(text))
    if not matches:
        return 0

    count = 0
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()

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

        body = "\n".join(body_lines).strip()
        if not body:
            continue

        store.send(
            content=body,
            sender=match.group("agent"),
            msg_type=MessageType.INFO,
            topic=topic,
            ttl_seconds=0,  # Never expire migrated entries
        )
        count += 1

    logger.info(f"Migrated {count} entries from v1 BULLETIN.md")
    return count


# ── Paperclip Bridge ─────────────────────────────────────────────


class PaperclipBridge:
    """Dual-write messages to Paperclip issues as comments.

    Best-effort: failure is logged, not fatal. SQLite is source of truth.
    """

    def __init__(self):
        self._client = None
        self._enabled: Optional[bool] = None

    def is_enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        self._enabled = bool(
            os.environ.get("PAPERCLIP_API_URL")
            and os.environ.get("PAPERCLIP_AGENT_ID")
        )
        return self._enabled

    def _get_client(self):
        if self._client is None:
            from .paperclip_client import PaperclipClient

            self._client = PaperclipClient()
        return self._client

    def post_comment(self, message: Message) -> Optional[str]:
        """Post message as a Paperclip comment. Returns comment ID or None."""
        if not self.is_enabled() or not message.issue_id:
            return None

        try:
            client = self._get_client()
            body = self._format_comment(message)
            comment = client.add_comment(message.issue_id, body)
            return comment.id
        except Exception as e:
            logger.warning(
                f"Paperclip dual-write failed for message {message.id}: {e}"
            )
            return None

    def _format_comment(self, message: Message) -> str:
        """Format a message as a structured Markdown comment."""
        lines = [
            f"**[{message.msg_type.value.upper()}]** from `{message.sender}`",
        ]
        if message.topic:
            lines.append(f"**Topic:** {message.topic}")
        lines.append("")
        lines.append(message.content)

        if message.metadata:
            lines.append("")
            lines.append("<details><summary>Metadata</summary>")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(message.metadata, indent=2))
            lines.append("```")
            lines.append("</details>")

        return "\n".join(lines)


# ── Singleton ────────────────────────────────────────────────────

_shared_message_store: Optional[MessageStore] = None
_message_store_lock = threading.Lock()


def get_shared_message_store() -> MessageStore:
    """Get or create the shared MessageStore singleton."""
    global _shared_message_store
    if _shared_message_store is None:
        with _message_store_lock:
            if _shared_message_store is None:
                store = MessageStore()
                # Auto-migrate v1 bulletin on first use
                bulletin_path = os.environ.get("BULLETIN_PATH")
                if bulletin_path:
                    try:
                        migrate_v1_bulletin(bulletin_path, store)
                    except Exception as e:
                        logger.warning(f"v1 bulletin migration failed: {e}")
                _shared_message_store = store
    return _shared_message_store
