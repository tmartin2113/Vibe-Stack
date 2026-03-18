"""
Tests for APIKeyManager — secure API key retrieval and storage.

Covers:
- Key retrieval fallback chain (env → cache → disk)
- Key format validation (OpenAI, Anthropic, HuggingFace, Google)
- Secure local storage (file creation, permissions, JSON format)
- Cache behavior
- Error message generation
- Edge cases (empty keys, whitespace, non-ASCII)
"""

import json
import os
import stat
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Disable remote lookups in tests
os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

from agents.api_key_manager import APIKeyManager


@pytest.fixture
def manager(tmp_path):
    """APIKeyManager with storage in a temp directory."""
    mgr = APIKeyManager.__new__(APIKeyManager)
    mgr.config = None
    mgr.cache = {}
    mgr.storage_path = tmp_path / "api_keys.json"
    return mgr


@pytest.fixture
def manager_with_stored_keys(tmp_path):
    """APIKeyManager with pre-existing stored keys."""
    storage = tmp_path / "api_keys.json"
    storage.write_text(json.dumps({
        "EXISTING_KEY": "existing-value-1234567890"
    }))
    mgr = APIKeyManager.__new__(APIKeyManager)
    mgr.config = None
    mgr.cache = {}
    mgr.storage_path = storage
    mgr._load_stored_keys()
    return mgr


# ── Key validation ─────────────────────────────────────────────────

class TestValidation:
    def test_valid_generic_key(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", "a" * 20)
        assert valid is True
        assert err == ""

    def test_too_short(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", "short")
        assert valid is False
        assert "too short" in err

    def test_non_ascii_rejected(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", "key-with-émoji-1234")
        assert valid is False
        assert "non-ASCII" in err

    def test_whitespace_rejected(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", "key with space123")
        assert valid is False
        assert "whitespace" in err

    def test_leading_whitespace_rejected(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", " key-1234567890")
        assert valid is False
        assert "whitespace" in err

    def test_trailing_whitespace_rejected(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", "key-1234567890 ")
        assert valid is False
        assert "whitespace" in err

    def test_tab_rejected(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", "key\t1234567890")
        assert valid is False

    def test_newline_rejected(self, manager):
        valid, err = manager._validate_api_key("SOME_KEY", "key\n1234567890")
        assert valid is False

    # OpenAI
    def test_openai_valid(self, manager):
        valid, _ = manager._validate_api_key("OPENAI_API_KEY", "sk-" + "a" * 45)
        assert valid is True

    def test_openai_wrong_prefix(self, manager):
        valid, err = manager._validate_api_key("OPENAI_API_KEY", "wrong-" + "a" * 45)
        assert valid is False
        assert "sk-" in err

    def test_openai_too_short(self, manager):
        valid, err = manager._validate_api_key("OPENAI_API_KEY", "sk-short")
        assert valid is False

    # Anthropic
    def test_anthropic_valid(self, manager):
        valid, _ = manager._validate_api_key("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 50)
        assert valid is True

    def test_anthropic_wrong_prefix(self, manager):
        valid, err = manager._validate_api_key("ANTHROPIC_API_KEY", "sk-" + "a" * 50)
        assert valid is False
        assert "sk-ant-" in err

    def test_anthropic_too_short(self, manager):
        valid, err = manager._validate_api_key("ANTHROPIC_API_KEY", "sk-ant-short")
        assert valid is False

    def test_claude_key_uses_anthropic_rules(self, manager):
        valid, err = manager._validate_api_key("CLAUDE_API_KEY", "sk-" + "a" * 50)
        assert valid is False
        assert "sk-ant-" in err

    # HuggingFace
    def test_huggingface_valid(self, manager):
        valid, _ = manager._validate_api_key("HUGGINGFACE_TOKEN", "hf_" + "a" * 20)
        assert valid is True

    def test_huggingface_wrong_prefix(self, manager):
        valid, err = manager._validate_api_key("HF_TOKEN", "wrong_" + "a" * 20)
        assert valid is False
        assert "hf_" in err

    # Google
    def test_google_valid(self, manager):
        valid, _ = manager._validate_api_key("GOOGLE_API_KEY", "a" * 39)
        assert valid is True

    def test_google_too_short(self, manager):
        valid, err = manager._validate_api_key("GEMINI_API_KEY", "a" * 15)
        assert valid is False
        assert "too short" in err


# ── Key storage ────────────────────────────────────────────────────

class TestStorage:
    def test_save_creates_file(self, manager):
        manager._save_key("TEST_KEY", "test-value-1234567890")
        assert manager.storage_path.exists()

    def test_save_writes_json(self, manager):
        manager._save_key("TEST_KEY", "test-value-1234567890")
        data = json.loads(manager.storage_path.read_text())
        assert data["TEST_KEY"] == "test-value-1234567890"

    def test_save_sets_permissions(self, manager):
        manager._save_key("TEST_KEY", "test-value-1234567890")
        mode = manager.storage_path.stat().st_mode
        assert mode & 0o777 == 0o600

    def test_save_updates_cache(self, manager):
        manager._save_key("TEST_KEY", "test-value-1234567890")
        assert manager.cache["TEST_KEY"] == "test-value-1234567890"

    def test_save_preserves_existing(self, manager):
        manager._save_key("KEY1", "value1-1234567890")
        manager._save_key("KEY2", "value2-1234567890")
        data = json.loads(manager.storage_path.read_text())
        assert data["KEY1"] == "value1-1234567890"
        assert data["KEY2"] == "value2-1234567890"

    def test_save_overwrites_same_key(self, manager):
        manager._save_key("KEY1", "old-value-1234567890")
        manager._save_key("KEY1", "new-value-1234567890")
        data = json.loads(manager.storage_path.read_text())
        assert data["KEY1"] == "new-value-1234567890"

    def test_load_stored_keys(self, manager_with_stored_keys):
        assert "EXISTING_KEY" in manager_with_stored_keys.cache
        assert manager_with_stored_keys.cache["EXISTING_KEY"] == "existing-value-1234567890"

    def test_load_missing_file_no_error(self, manager):
        # storage_path doesn't exist yet
        manager._load_stored_keys()
        assert manager.cache == {}


# ── Key retrieval fallback chain ───────────────────────────────────

class TestGetApiKey:
    def test_env_var_first(self, manager):
        with patch.dict(os.environ, {"MY_KEY": "env-value-1234567890"}):
            key = manager.get_api_key("MY_KEY", prompt_user=False)
        assert key == "env-value-1234567890"

    def test_cache_second(self, manager):
        manager.cache["MY_KEY"] = "cached-value-12345678"
        key = manager.get_api_key("MY_KEY", prompt_user=False)
        assert key == "cached-value-12345678"

    def test_storage_third(self, manager_with_stored_keys):
        # Clear cache but keep storage
        manager_with_stored_keys.cache.clear()
        key = manager_with_stored_keys.get_api_key("EXISTING_KEY", prompt_user=False)
        assert key == "existing-value-1234567890"

    def test_env_beats_cache(self, manager):
        manager.cache["MY_KEY"] = "cached"
        with patch.dict(os.environ, {"MY_KEY": "from-env-1234567890"}):
            key = manager.get_api_key("MY_KEY", prompt_user=False)
        assert key == "from-env-1234567890"

    def test_not_found_returns_none(self, manager):
        key = manager.get_api_key("MISSING_KEY", prompt_user=False)
        assert key is None

    def test_prompt_user_false_skips_prompt(self, manager):
        with patch.object(manager, "_prompt_user_for_key") as mock_prompt:
            manager.get_api_key("MISSING_KEY", prompt_user=False)
            mock_prompt.assert_not_called()

    def test_prompt_user_true_calls_prompt(self, manager):
        with patch.object(manager, "_prompt_user_for_key", return_value=None) as mock_prompt:
            manager.get_api_key("MISSING_KEY", prompt_user=True)
            mock_prompt.assert_called_once_with("MISSING_KEY")


# ── Messenger prompting ───────────────────────────────────────────

class TestPromptUser:
    def test_no_config_returns_none(self, manager):
        result = manager._prompt_user_for_key("MY_KEY")
        assert result is None

    def test_slack_env_not_set_returns_none(self, manager):
        # Ensure SLACK vars are not set
        env = {k: v for k, v in os.environ.items()
               if k not in ("SLACK_BOT_TOKEN", "SLACK_USER_ID")}
        with patch.dict(os.environ, env, clear=True):
            result = manager._prompt_user_for_key("MY_KEY")
        assert result is None


# ── Error messages ─────────────────────────────────────────────────

class TestErrorMessage:
    def test_includes_key_name(self, manager):
        msg = manager.get_error_message("ANTHROPIC_API_KEY")
        assert "ANTHROPIC_API_KEY" in msg

    def test_includes_export_instruction(self, manager):
        msg = manager.get_error_message("MY_KEY")
        assert "export MY_KEY=" in msg

    def test_includes_json_instruction(self, manager):
        msg = manager.get_error_message("MY_KEY")
        assert "api_keys.json" in msg
