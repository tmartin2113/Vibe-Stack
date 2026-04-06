"""
Tests for SkillEmbeddingCache.

The cache is a thin sidecar over the shared VLLMEmbedder singleton that
stores per-skill vectors in ``.embeddings.json`` next to ``.index.json``.
These tests exercise it in isolation with a MagicMock embedder so they
run with no network and no vLLM.
"""

import json
from unittest.mock import MagicMock

import pytest

from agents.embedder import VLLMEmbedder
from agents.skill_embeddings import SkillEmbeddingCache, _compose_text, _hash_text


# ── Fixtures ──────────────────────────────────────────────────────


def _make_embedder(
    model: str = "test-model",
    side_effect=None,
    return_value=None,
):
    """Return a MagicMock shaped like VLLMEmbedder."""
    embedder = MagicMock(spec=VLLMEmbedder)
    embedder.model = model
    embedder.is_available.return_value = True
    if side_effect is not None:
        embedder.embed.side_effect = side_effect
    elif return_value is not None:
        embedder.embed.return_value = return_value
    else:
        embedder.embed.return_value = [1.0, 0.0, 0.0]
    return embedder


@pytest.fixture
def cache_dir(tmp_path):
    d = tmp_path / "vibe_skills"
    d.mkdir()
    return d


# ── Basic cache behavior ──────────────────────────────────────────


class TestEmbedSkillCache:
    def test_first_call_embeds_and_persists(self, cache_dir):
        embedder = _make_embedder(return_value=[0.1, 0.2, 0.3])
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)

        vec = cache.embed_skill("my-skill", "does a thing", ["task_a"])

        assert vec == [0.1, 0.2, 0.3]
        assert embedder.embed.call_count == 1
        # Persisted to disk
        path = cache_dir / ".embeddings.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["version"] == "1.0"
        assert data["model"] == "test-model"
        assert "my-skill" in data["entries"]
        assert data["entries"]["my-skill"]["vec"] == [0.1, 0.2, 0.3]
        assert data["entries"]["my-skill"]["text_hash"].startswith("sha256:")

    def test_second_call_hits_cache(self, cache_dir):
        embedder = _make_embedder(return_value=[0.1, 0.2, 0.3])
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)

        cache.embed_skill("my-skill", "does a thing", ["task_a"])
        cache.embed_skill("my-skill", "does a thing", ["task_a"])

        # Only one network call — second was served from the cache
        assert embedder.embed.call_count == 1

    def test_text_hash_mismatch_reembeds(self, cache_dir):
        embedder = _make_embedder(
            side_effect=[[0.1, 0.2, 0.3], [0.9, 0.8, 0.7]],
        )
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)

        v1 = cache.embed_skill("my-skill", "does a thing", ["task_a"])
        # Description changed → hash mismatch → re-embed
        v2 = cache.embed_skill("my-skill", "now it does something else", ["task_a"])

        assert v1 == [0.1, 0.2, 0.3]
        assert v2 == [0.9, 0.8, 0.7]
        assert embedder.embed.call_count == 2

    def test_task_types_change_reembeds(self, cache_dir):
        embedder = _make_embedder(
            side_effect=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
        )
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)

        cache.embed_skill("my-skill", "does a thing", ["task_a"])
        cache.embed_skill("my-skill", "does a thing", ["task_a", "task_b"])

        assert embedder.embed.call_count == 2


# ── Graceful degradation ──────────────────────────────────────────


class TestDegradation:
    def test_embedder_returns_none(self, cache_dir):
        embedder = _make_embedder()
        embedder.embed.return_value = None
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)

        result = cache.embed_skill("my-skill", "desc", [])

        assert result is None
        # Nothing persisted
        assert not (cache_dir / ".embeddings.json").exists()

    def test_embedder_unavailable_at_construction(self, cache_dir):
        embedder = MagicMock(spec=VLLMEmbedder)
        embedder.is_available.return_value = False
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)
        # is_available=False still wires into the lazy-init: the injected
        # embedder was handed in explicitly so it's "checked". A call
        # should still flow through and embed() will be invoked — but
        # production code never reaches here because is_available is
        # checked in get_shared_embedder's wrapper. Test the public
        # outcome: None when embed() returns None.
        embedder.embed.return_value = None

        assert cache.embed_skill("s", "d", []) is None
        assert cache.embed_query("q") is None

    def test_semantic_score_with_no_query_vec(self, cache_dir):
        cache = SkillEmbeddingCache(cache_dir, embedder=_make_embedder())
        assert cache.semantic_score(None, "my-skill") == 0.0

    def test_semantic_score_with_missing_skill_vec(self, cache_dir):
        cache = SkillEmbeddingCache(cache_dir, embedder=_make_embedder())
        # Skill was never embedded
        assert cache.semantic_score([1.0, 0.0, 0.0], "unknown-skill") == 0.0

    def test_semantic_score_clamps_negative(self, cache_dir):
        embedder = _make_embedder(return_value=[1.0, 0.0, 0.0])
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)
        cache.embed_skill("my-skill", "desc", [])
        # Opposite vector → cosine = -1, clamp to 0
        score = cache.semantic_score([-1.0, 0.0, 0.0], "my-skill")
        assert score == 0.0

    def test_semantic_score_perfect_match(self, cache_dir):
        embedder = _make_embedder(return_value=[1.0, 0.0, 0.0])
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)
        cache.embed_skill("my-skill", "desc", [])
        score = cache.semantic_score([1.0, 0.0, 0.0], "my-skill")
        assert score == pytest.approx(1.0)


# ── Model invalidation ───────────────────────────────────────────


class TestModelInvalidation:
    def test_model_change_drops_entries(self, cache_dir):
        # Seed with model A
        embedder_a = _make_embedder(model="model-a", return_value=[1.0, 0.0])
        cache_a = SkillEmbeddingCache(cache_dir, embedder=embedder_a)
        cache_a.embed_skill("s1", "d1", [])
        cache_a.embed_skill("s2", "d2", [])
        assert len((cache_dir / ".embeddings.json").read_text()) > 0

        # Open a fresh cache with a different model — next embed should
        # clear the prior entries and persist only the new one.
        embedder_b = _make_embedder(model="model-b", return_value=[0.0, 1.0])
        cache_b = SkillEmbeddingCache(cache_dir, embedder=embedder_b)
        cache_b.embed_skill("s3", "d3", [])

        data = json.loads((cache_dir / ".embeddings.json").read_text())
        assert data["model"] == "model-b"
        # Old entries were dropped when the model flipped
        assert "s1" not in data["entries"]
        assert "s2" not in data["entries"]
        assert "s3" in data["entries"]


# ── Corrupt file recovery ─────────────────────────────────────────


class TestCorruptFile:
    def test_corrupt_json_falls_back_to_empty(self, cache_dir):
        (cache_dir / ".embeddings.json").write_text("{not valid json")
        embedder = _make_embedder(return_value=[0.5, 0.5])
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)

        vec = cache.embed_skill("my-skill", "desc", [])

        assert vec == [0.5, 0.5]
        # File was rewritten cleanly
        data = json.loads((cache_dir / ".embeddings.json").read_text())
        assert "my-skill" in data["entries"]

    def test_non_dict_json_falls_back(self, cache_dir):
        (cache_dir / ".embeddings.json").write_text("[1, 2, 3]")
        embedder = _make_embedder(return_value=[0.5, 0.5])
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)

        vec = cache.embed_skill("my-skill", "desc", [])
        assert vec == [0.5, 0.5]


# ── Invalidation ──────────────────────────────────────────────────


class TestInvalidate:
    def test_invalidate_drops_entry(self, cache_dir):
        embedder = _make_embedder(return_value=[0.1, 0.2])
        cache = SkillEmbeddingCache(cache_dir, embedder=embedder)
        cache.embed_skill("my-skill", "desc", [])

        cache.invalidate("my-skill")

        data = json.loads((cache_dir / ".embeddings.json").read_text())
        assert "my-skill" not in data["entries"]

    def test_invalidate_missing_is_noop(self, cache_dir):
        cache = SkillEmbeddingCache(cache_dir, embedder=_make_embedder())
        # Should not raise
        cache.invalidate("never-existed")


# ── Helper functions ──────────────────────────────────────────────


class TestHelpers:
    def test_compose_text_combines_description_and_task_types(self):
        assert _compose_text("pdf tools", ["extract", "parse"]) == (
            "pdf tools\nextract parse"
        )

    def test_compose_text_empty_task_types(self):
        assert _compose_text("pdf tools", []) == "pdf tools"

    def test_compose_text_none_task_types(self):
        assert _compose_text("pdf tools", None) == "pdf tools"

    def test_hash_text_is_stable(self):
        h1 = _hash_text("hello")
        h2 = _hash_text("hello")
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_hash_text_differs_for_different_inputs(self):
        assert _hash_text("hello") != _hash_text("world")
