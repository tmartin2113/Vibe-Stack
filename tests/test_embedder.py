"""Tests for agents.embedder — shared VLLMEmbedder, cosine_similarity, singleton.

Covers:
- VLLMEmbedder: init, is_available, embed, embed_batch, graceful degradation
- cosine_similarity: identical vectors, orthogonal, zero vectors, different lengths
- get_shared_embedder: singleton behavior
"""

import threading
from unittest.mock import MagicMock, patch

import pytest


# ── cosine_similarity ──────────────────────────────────────────


class TestCosineSimilarity:
    """Pure math function — no mocks needed."""

    def test_identical_vectors(self):
        from agents.embedder import cosine_similarity

        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        from agents.embedder import cosine_similarity

        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        from agents.embedder import cosine_similarity

        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector_a(self):
        from agents.embedder import cosine_similarity

        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_zero_vector_b(self):
        from agents.embedder import cosine_similarity

        assert cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0

    def test_both_zero_vectors(self):
        from agents.embedder import cosine_similarity

        assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0

    def test_different_lengths(self):
        from agents.embedder import cosine_similarity

        assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_empty_vectors(self):
        from agents.embedder import cosine_similarity

        assert cosine_similarity([], []) == 0.0

    def test_similar_vectors(self):
        from agents.embedder import cosine_similarity

        sim = cosine_similarity([1.0, 1.0], [1.0, 0.9])
        assert 0.99 < sim < 1.0

    def test_3d_vectors(self):
        from agents.embedder import cosine_similarity

        sim = cosine_similarity([1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
        assert sim == pytest.approx(0.0)


# ── VLLMEmbedder ─────────────────────────────────────────────


class TestVLLMEmbedderInit:
    """Constructor and default values."""

    def test_defaults(self):
        from agents.embedder import VLLMEmbedder, DEFAULT_EMBED_MODEL, DEFAULT_VLLM_URL

        e = VLLMEmbedder()
        assert e.model == DEFAULT_EMBED_MODEL
        assert e.base_url == DEFAULT_VLLM_URL
        assert e.timeout == 10
        assert e._available is None

    def test_custom_params(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder(model="custom", vllm_url="http://host:1234/", timeout=5)
        assert e.model == "custom"
        assert e.base_url == "http://host:1234"  # trailing slash stripped
        assert e.timeout == 5


class TestVLLMEmbedderIsAvailable:
    """is_available caching and network behavior."""

    def test_cached_true(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = True
        assert e.is_available() is True

    def test_cached_false(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = False
        assert e.is_available() is False

    def test_network_success(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp):
            assert e.is_available() is True
            assert e._available is True

    def test_network_failure(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        with patch("requests.post", side_effect=ConnectionError("fail")):
            assert e.is_available() is False
            assert e._available is False

    def test_network_non_200(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("requests.post", return_value=mock_resp):
            assert e.is_available() is False


class TestVLLMEmbedderEmbed:
    """Single-text embedding."""

    def test_unavailable_returns_none(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = False
        assert e.embed("test") is None

    def test_success(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"embedding": [1.0, 2.0, 3.0]}]}
        with patch("requests.post", return_value=mock_resp):
            result = e.embed("test")
            assert result == [1.0, 2.0, 3.0]

    def test_non_200_returns_none(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("requests.post", return_value=mock_resp):
            assert e.embed("test") is None

    def test_empty_data_returns_none(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": []}
        with patch("requests.post", return_value=mock_resp):
            assert e.embed("test") is None

    def test_exception_returns_none(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = True
        with patch("requests.post", side_effect=ConnectionError("fail")):
            assert e.embed("test") is None


class TestVLLMEmbedderEmbedBatch:
    """Batch embedding."""

    def test_empty_list(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        assert e.embed_batch([]) == []

    def test_unavailable(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = False
        result = e.embed_batch(["a", "b"])
        assert result == [None, None]

    def test_success(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"embedding": [1.0, 0.0]},
                {"embedding": [0.0, 1.0]},
            ]
        }
        with patch("requests.post", return_value=mock_resp):
            result = e.embed_batch(["a", "b"])
            assert result == [[1.0, 0.0], [0.0, 1.0]]

    def test_partial_failure(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = True
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"embedding": [1.0]},
                {},  # Missing embedding key
            ]
        }
        with patch("requests.post", return_value=mock_resp):
            result = e.embed_batch(["a", "b"])
            assert result[0] == [1.0]
            assert result[1] is None

    def test_network_error(self):
        from agents.embedder import VLLMEmbedder

        e = VLLMEmbedder()
        e._available = True
        with patch("requests.post", side_effect=ConnectionError("fail")):
            result = e.embed_batch(["a", "b"])
            assert result == [None, None]


# ── get_shared_embedder ──────────────────────────────────────


class TestGetSharedEmbedder:
    """Module-level singleton."""

    def test_returns_embedder(self):
        import agents.embedder as mod

        mod._shared_embedder = None
        try:
            e = mod.get_shared_embedder()
            assert isinstance(e, mod.VLLMEmbedder)
        finally:
            mod._shared_embedder = None

    def test_singleton_same_instance(self):
        import agents.embedder as mod

        mod._shared_embedder = None
        try:
            e1 = mod.get_shared_embedder()
            e2 = mod.get_shared_embedder()
            assert e1 is e2
        finally:
            mod._shared_embedder = None

    def test_threadsafe(self):
        import agents.embedder as mod

        mod._shared_embedder = None
        results = []

        def get():
            results.append(mod.get_shared_embedder())

        try:
            threads = [threading.Thread(target=get) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert all(r is results[0] for r in results)
        finally:
            mod._shared_embedder = None
