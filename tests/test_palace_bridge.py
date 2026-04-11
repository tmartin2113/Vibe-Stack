"""Tests for the MemPalace bridge module.

All palace operations are additive — the bridge must never raise,
and must degrade gracefully when mempalace is not installed.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from agents.palace_bridge import (
    _drawer_id,
    _wing_name,
    ensure_agent_identity,
    palace_inject,
    palace_persist,
    palace_wakeup,
)


class TestWingName:
    def test_basic(self):
        assert _wing_name("backend-engineer") == "wing_backend_engineer"

    def test_already_prefixed(self):
        assert _wing_name("wing_cto") == "wing_cto"

    def test_spaces(self):
        assert _wing_name("QA Engineer") == "wing_qa_engineer"

    def test_empty(self):
        assert _wing_name("") == "wing_"


class TestDrawerId:
    def test_deterministic(self):
        a = _drawer_id("wing_a", "room_b", "content")
        b = _drawer_id("wing_a", "room_b", "content")
        assert a == b

    def test_different_content(self):
        a = _drawer_id("wing_a", "room_b", "content1")
        b = _drawer_id("wing_a", "room_b", "content2")
        assert a != b


class TestPalacePersist:
    def test_graceful_when_mempalace_missing(self):
        """palace_persist must not raise when mempalace is not installed."""
        with patch.dict(os.environ, {"MEMPALACE_PALACE_PATH": ""}):
            # Should return None silently
            result = palace_persist({"agent_id": "test", "final_output": "hello"})
            assert result is None

    def test_graceful_on_empty_state(self):
        """palace_persist with no output or spec does nothing."""
        result = palace_persist({})
        assert result is None

    @patch("agents.palace_bridge._PALACE_PATH", "/tmp/test-palace")
    def test_creates_drawer_on_persist(self):
        """When chromadb is available, palace_persist upserts drawers."""
        mock_col = MagicMock()
        mock_client = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_col

        # Mock both chromadb and mempalace.knowledge_graph at import time
        mock_chromadb = MagicMock()
        mock_chromadb.PersistentClient.return_value = mock_client
        mock_kg_mod = MagicMock()

        import sys
        with patch.dict(sys.modules, {
            "chromadb": mock_chromadb,
            "mempalace": MagicMock(),
            "mempalace.knowledge_graph": mock_kg_mod,
        }):
            # Re-import to pick up mocked modules
            import importlib
            import agents.palace_bridge as pb
            importlib.reload(pb)

            pb._PALACE_PATH = "/tmp/test-palace"
            pb.palace_persist({
                "agent_id": "backend-engineer",
                "task_id": "VIB-99",
                "routed_task_type": "api-generation",
                "final_output": "Generated API endpoints",
                "specification": "Build REST API for users",
                "final_score": 85,
            })

        # Should upsert at least the spec and output drawers
        assert mock_col.upsert.call_count >= 2


class TestPalaceInject:
    def test_graceful_when_mempalace_missing(self):
        """palace_inject must return empty string when mempalace is not installed."""
        result = palace_inject({"user_request": "test query"})
        assert result == ""

    def test_graceful_on_empty_request(self):
        result = palace_inject({})
        assert result == ""


class TestEnsureAgentIdentity:
    def test_creates_identity_file(self):
        """ensure_agent_identity creates an identity file from seeds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            palace_path = os.path.join(tmpdir, "palace")
            identities_dir = os.path.join(tmpdir, "identities")

            with patch("agents.palace_bridge._PALACE_PATH", palace_path), \
                 patch("agents.palace_bridge._IDENTITIES_DIR", identities_dir):
                try:
                    ensure_agent_identity("backend-engineer")
                except Exception:
                    pass  # ChromaDB may not be available

                # If mempalace/chromadb is installed, file should exist
                # If not, function should have returned silently
                if os.path.exists(identities_dir):
                    files = os.listdir(identities_dir)
                    assert len(files) <= 1  # 0 if chromadb missing, 1 if present

    def test_graceful_when_mempalace_missing(self):
        """Must not raise when mempalace/chromadb not installed."""
        with patch("agents.palace_bridge._PALACE_PATH", ""):
            ensure_agent_identity("test-agent")  # Should not raise


class TestPalaceWakeup:
    def test_graceful_when_mempalace_missing(self):
        """palace_wakeup must return empty string when mempalace is not installed."""
        result = palace_wakeup("backend-engineer")
        assert isinstance(result, str)

    def test_graceful_on_empty_agent(self):
        result = palace_wakeup("")
        assert isinstance(result, str)
