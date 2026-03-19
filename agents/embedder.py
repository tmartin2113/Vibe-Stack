"""Shared VLLMEmbedder — vector embedding via vLLM's OpenAI-compatible API.

Provides:
- VLLMEmbedder: embed(), embed_batch(), is_available()
- cosine_similarity(): cosine distance between two vectors
- get_shared_embedder(): module-level singleton (double-checked locking)

Used by both MemoryStore and MessageStore for semantic search.
"""

import logging
import math
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default embedding model — lightweight, fast, good for semantic similarity
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_VLLM_URL = "http://localhost:8000"


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class VLLMEmbedder:
    """Generate embeddings via vLLM's OpenAI-compatible /v1/embeddings endpoint.

    Gracefully degrades: if vLLM is unreachable or the model isn't loaded,
    all methods return None instead of raising.
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBED_MODEL,
        vllm_url: str = DEFAULT_VLLM_URL,
        timeout: int = 10,
    ):
        self.model = model
        self.base_url = vllm_url.rstrip("/")
        self.timeout = timeout
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        """Check if the embedding model is reachable (cached after first call)."""
        if self._available is not None:
            return self._available
        try:
            import requests
            resp = requests.post(
                f"{self.base_url}/v1/embeddings",
                json={"model": self.model, "input": "test"},
                timeout=self.timeout,
            )
            self._available = resp.status_code == 200
        except Exception:
            self._available = False
        if not self._available:
            logger.info(
                f"vLLM embeddings unavailable (model={self.model}). "
                f"Search will use BM25 only."
            )
        return self._available

    def embed(self, text: str) -> Optional[List[float]]:
        """Return embedding vector for text, or None on failure."""
        if not self.is_available():
            return None
        try:
            import requests
            resp = requests.post(
                f"{self.base_url}/v1/embeddings",
                json={"model": self.model, "input": text},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            # OpenAI-compatible: {"data": [{"embedding": [...]}]}
            items = data.get("data", [])
            if items and len(items) > 0:
                return items[0].get("embedding")
            return None
        except Exception as e:
            logger.debug(f"Embedding failed: {e}")
            return None

    def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed multiple texts. Returns list of vectors (None on per-item failure)."""
        if not texts:
            return []
        if not self.is_available():
            return [None] * len(texts)
        try:
            import requests
            resp = requests.post(
                f"{self.base_url}/v1/embeddings",
                json={"model": self.model, "input": texts},
                timeout=self.timeout * 2,  # longer timeout for batch
            )
            if resp.status_code != 200:
                return [None] * len(texts)
            data = resp.json()
            # OpenAI-compatible: {"data": [{"embedding": [...]}, ...]}
            items = data.get("data", [])
            result: List[Optional[List[float]]] = []
            for i in range(len(texts)):
                if i < len(items) and items[i].get("embedding"):
                    result.append(items[i]["embedding"])
                else:
                    result.append(None)
            return result
        except Exception as e:
            logger.debug(f"Batch embedding failed: {e}")
            return [None] * len(texts)


# ── Module-level singleton ──────────────────────────────────────

_shared_embedder: Optional[VLLMEmbedder] = None
_embedder_lock = threading.Lock()


def get_shared_embedder() -> VLLMEmbedder:
    """Get or create the shared VLLMEmbedder singleton (double-checked locking)."""
    global _shared_embedder
    if _shared_embedder is None:
        with _embedder_lock:
            if _shared_embedder is None:
                _shared_embedder = VLLMEmbedder()
    return _shared_embedder
