"""
Skill embedding cache — sidecar ``.embeddings.json`` storing per-skill vectors
for semantic matching in :mod:`agents.skill_search`.

Design:

- Vectors live in ``<base_dir>/.embeddings.json`` (sibling of ``.index.json``).
  Kept out of the main index so the security-hashed index stays small and
  stable across runs without embeddings.
- A ``text_hash`` is stored with each vector. When a skill's description or
  task_types change, the hash mismatch triggers a lazy re-embed on next use.
- The top-level ``model`` field invalidates the whole file if the embedder
  model is swapped.
- Every public method returns ``None``/``0.0`` on failure rather than raising.
  When the embedder is unavailable the registry transparently falls back to
  the existing keyword scoring path (see :func:`_calculate_match_confidence`).

Reuses:
- :func:`agents.embedder.get_shared_embedder` — module singleton
- :func:`agents.embedder.cosine_similarity` — vector distance
- Atomic write pattern from :meth:`SkillRegistryIndexMixin._save_index_atomic`
"""

import hashlib
import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .embedder import VLLMEmbedder, cosine_similarity, get_shared_embedder

logger = logging.getLogger(__name__)

__all__ = ["SkillEmbeddingCache"]

_CACHE_VERSION = "1.0"


def _compose_text(description: str, task_types: List[str]) -> str:
    """Canonical text we embed for a skill (description + task types)."""
    joined = " ".join(task_types or [])
    return f"{description}\n{joined}".strip()


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class SkillEmbeddingCache:
    """Lazy, on-disk embedding cache for skill descriptions.

    The cache is lazily loaded from ``<base_dir>/.embeddings.json`` on first
    access and persisted after each mutation. The embedder itself is fetched
    via :func:`get_shared_embedder` on first use (not at construction), so a
    ``SkillEmbeddingCache`` can be instantiated in environments without a
    running vLLM.
    """

    def __init__(
        self,
        base_dir: Path,
        embedder: Optional[VLLMEmbedder] = None,
    ):
        self.base_dir = Path(base_dir)
        self._path = self.base_dir / ".embeddings.json"
        # Lazy-init pattern (matches MemoryStore._get_embedder): None means
        # "not yet probed", so tests can inject a mock by passing embedder=.
        self._embedder: Optional[VLLMEmbedder] = embedder
        self._embedder_checked: bool = embedder is not None
        self._data: Optional[Dict[str, Any]] = None  # Loaded on first access

    # ── Embedder acquisition ──────────────────────────────────────

    def _get_embedder(self) -> Optional[VLLMEmbedder]:
        """Fetch the shared embedder on first use. ``None`` if unavailable."""
        if not self._embedder_checked:
            self._embedder_checked = True
            try:
                candidate = get_shared_embedder()
            except Exception as e:  # pragma: no cover — defensive
                logger.debug(f"get_shared_embedder failed: {e}")
                self._embedder = None
                return None
            if candidate is None or not candidate.is_available():
                self._embedder = None
            else:
                self._embedder = candidate
        return self._embedder

    def _current_model(self) -> str:
        """Model tag of the active embedder, or the cached file's tag."""
        emb = self._embedder if self._embedder_checked else None
        if emb is not None:
            return getattr(emb, "model", "unknown")
        data = self._load()
        return data.get("model", "unknown")

    # ── Persistence ───────────────────────────────────────────────

    def _empty(self) -> Dict[str, Any]:
        return {
            "version": _CACHE_VERSION,
            "model": "",
            "entries": {},
        }

    def _load(self) -> Dict[str, Any]:
        """Load the sidecar file. Returns an empty cache on any failure."""
        if self._data is not None:
            return self._data
        if not self._path.exists():
            self._data = self._empty()
            return self._data
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict) or "entries" not in raw:
                raise ValueError("malformed cache file")
            self._data = raw
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.warning(
                f"Skill embedding cache unreadable ({self._path}): {e}. "
                f"Falling back to empty cache."
            )
            self._data = self._empty()
        return self._data

    def _save(self) -> None:
        """Atomic write via temp file + rename (same pattern as _save_index)."""
        if self._data is None:
            return
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.debug(f"Cannot create {self.base_dir}: {e}")
            return
        temp_fd, temp_path = tempfile.mkstemp(
            dir=self.base_dir,
            prefix=".embeddings-",
            suffix=".json.tmp",
        )
        try:
            with open(temp_fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
                f.flush()
            Path(temp_path).replace(self._path)
        except Exception as e:
            try:
                Path(temp_path).unlink()
            except OSError:
                pass
            logger.debug(f"Failed to save embedding cache: {e}")

    def _invalidate_if_model_changed(self) -> None:
        """Drop all entries if the active embedder model differs from cached.

        No-op (no disk write) when the cache is empty or the models agree —
        ``embed_skill`` will persist the new model tag alongside its first
        successful embedding.
        """
        data = self._load()
        active_model = self._current_model()
        cached_model = data.get("model", "")
        if cached_model and active_model and cached_model != active_model:
            logger.info(
                f"Embedder model changed ({cached_model} -> {active_model}); "
                f"clearing {len(data.get('entries', {}))} cached skill vectors."
            )
            data["entries"] = {}
            data["model"] = active_model
            self._save()

    # ── Public API ────────────────────────────────────────────────

    def embed_skill(
        self,
        name: str,
        description: str,
        task_types: List[str],
    ) -> Optional[List[float]]:
        """Return (and cache) the embedding for a skill. ``None`` on failure."""
        embedder = self._get_embedder()
        if embedder is None:
            return None

        self._invalidate_if_model_changed()
        data = self._load()
        entries: Dict[str, Any] = data.setdefault("entries", {})

        text = _compose_text(description, task_types)
        text_hash = _hash_text(text)

        cached = entries.get(name)
        if cached and cached.get("text_hash") == text_hash:
            vec = cached.get("vec")
            if isinstance(vec, list) and vec:
                return vec  # type: ignore[return-value]

        vec = embedder.embed(text)
        if vec is None:
            return None

        entries[name] = {
            "vec": vec,
            "text_hash": text_hash,
            "updated": datetime.utcnow().isoformat() + "Z",
        }
        data["model"] = getattr(embedder, "model", data.get("model", ""))
        self._save()
        return vec

    def embed_query(self, requirement: str) -> Optional[List[float]]:
        """Embed a search query (uncached — queries are unbounded)."""
        embedder = self._get_embedder()
        if embedder is None or not requirement:
            return None
        return embedder.embed(requirement)

    def get_cached_vec(self, name: str) -> Optional[List[float]]:
        """Return the stored vector for a skill, or ``None`` if absent."""
        data = self._load()
        entry = data.get("entries", {}).get(name)
        if not entry:
            return None
        vec = entry.get("vec")
        return vec if isinstance(vec, list) and vec else None

    def semantic_score(
        self,
        query_vec: Optional[List[float]],
        skill_name: str,
    ) -> float:
        """Clamped cosine similarity between query and cached skill vector.

        Returns ``0.0`` if either vector is missing so callers can simply add
        the result to the keyword score without guarding for ``None``.
        """
        if query_vec is None:
            return 0.0
        skill_vec = self.get_cached_vec(skill_name)
        if skill_vec is None:
            return 0.0
        sim = cosine_similarity(query_vec, skill_vec)
        # Cosine can be negative; clamp so the blended score never dips
        # below the keyword floor.
        if sim < 0.0:
            return 0.0
        if sim > 1.0:
            return 1.0
        return sim

    def invalidate(self, name: str) -> None:
        """Drop a single skill's entry. No-op if absent."""
        data = self._load()
        entries = data.get("entries", {})
        if name in entries:
            entries.pop(name, None)
            self._save()
