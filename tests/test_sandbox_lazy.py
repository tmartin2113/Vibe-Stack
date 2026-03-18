"""
Tests for lazy sandbox pool initialization.
"""

from unittest.mock import MagicMock, patch

import pytest

from agents.sandbox.config import SandboxConfig


class TestLazySandboxPool:
    """Tests for lazy=True sandbox pool startup."""

    @patch("agents.sandbox.client._check_sdk", return_value=True)
    def test_lazy_start_does_not_pre_warm(self, mock_sdk):
        """With lazy=True, start() should not create any containers."""
        from agents.sandbox.client import SandboxPoolManager

        config = SandboxConfig(pool_size=2)
        pool = SandboxPoolManager(config, lazy=True)

        with patch.object(pool, "_create_sandbox") as mock_create:
            pool.start()

            # No containers should be created during start()
            mock_create.assert_not_called()
            assert pool._started is True
            assert pool._warmed is False

        pool.stop()

    @patch("agents.sandbox.client._check_sdk", return_value=True)
    def test_eager_start_pre_warms(self, mock_sdk):
        """With lazy=False (default), start() should pre-warm containers."""
        from agents.sandbox.client import SandboxPoolManager

        config = SandboxConfig(pool_size=2)
        pool = SandboxPoolManager(config, lazy=False)

        mock_handle = MagicMock()
        with patch.object(pool, "_create_sandbox", return_value=mock_handle) as mock_create:
            pool.start()

            assert mock_create.call_count == 2
            assert pool._warmed is True

        pool.stop()

    @patch("agents.sandbox.client._check_sdk", return_value=True)
    def test_lazy_warms_on_first_acquire(self, mock_sdk):
        """Lazy pool should warm containers on first _acquire() call."""
        from agents.sandbox.client import SandboxPoolManager

        config = SandboxConfig(pool_size=1)
        pool = SandboxPoolManager(config, lazy=True)

        mock_handle = MagicMock()
        mock_handle.age_seconds = 0
        with patch.object(pool, "_create_sandbox", return_value=mock_handle) as mock_create:
            pool.start()
            assert pool._warmed is False

            # First acquire triggers warm-up
            handle = pool._acquire()
            assert pool._warmed is True
            # pool_size(1) containers + the acquire itself may create via pool
            assert mock_create.call_count >= 1

        pool.stop()

    @patch("agents.sandbox.client._check_sdk", return_value=True)
    def test_warm_pool_only_runs_once(self, mock_sdk):
        """_warm_pool should be idempotent."""
        from agents.sandbox.client import SandboxPoolManager

        config = SandboxConfig(pool_size=1)
        pool = SandboxPoolManager(config, lazy=True)

        mock_handle = MagicMock()
        with patch.object(pool, "_create_sandbox", return_value=mock_handle) as mock_create:
            pool.start()
            pool._warm_pool()
            pool._warm_pool()  # Second call should be no-op

            assert mock_create.call_count == 1  # Only warmed once

        pool.stop()
