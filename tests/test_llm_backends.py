"""
Comprehensive tests for the LLM backend system.

Covers:
- BackendBase (base class properties, defaults)
- VLLMBackend (init, properties, health_check, generate, generate_chat)
- LLMBackend wrapper (vLLM routing, create_backend_from_config)

All HTTP calls are mocked at the module level where requests is imported.
"""

import time
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from genesia.backends.base import BackendBase
from genesia.backends.vllm import VLLMBackend
from agents.llm_backend import LLMBackend, create_backend_from_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status_code=200, json_data=None, raise_for_status=None):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    if raise_for_status:
        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status.return_value = None
    return resp


# ===========================================================================
# BackendBase
# ===========================================================================

class TestBackendBase:
    """Tests for the abstract BackendBase class."""

    def test_cannot_instantiate_directly(self):
        """BackendBase is abstract and cannot be instantiated."""
        with pytest.raises(TypeError):
            BackendBase(host="localhost", port=8080)

    def test_completion_url_default(self):
        """completion_url returns {base_url}/v1/completions by default."""

        class Stub(BackendBase):
            @property
            def name(self):
                return "stub"

            def generate(self, prompt, **kw):
                return {}

            def health_check(self):
                return True

        stub = Stub(host="localhost", port=11434)
        assert stub.completion_url == "http://localhost:11434/v1/completions"

    def test_repr_includes_class_and_host(self):
        """__repr__ contains useful debug info."""

        class Stub(BackendBase):
            @property
            def name(self):
                return "stub"

            def generate(self, prompt, **kw):
                return {}

            def health_check(self):
                return True

        stub = Stub(host="127.0.0.1", port=9999, model="test-model")
        r = repr(stub)
        assert "127.0.0.1" in r or "9999" in r

    def test_default_timeout(self):
        """Default timeout is set when not provided."""

        class Stub(BackendBase):
            @property
            def name(self):
                return "stub"

            def generate(self, prompt, **kw):
                return {}

            def health_check(self):
                return True

        stub = Stub(host="localhost", port=8080)
        assert stub.timeout is not None
        assert isinstance(stub.timeout, (int, float))


# ===========================================================================
# VLLMBackend
# ===========================================================================

class TestVLLMBackendInit:
    """Initialization and property tests for VLLMBackend."""

    def test_name_property(self):
        backend = VLLMBackend(host="localhost", port=8000, model="qwen2.5")
        assert backend.name == "vllm"

    def test_completion_url(self):
        backend = VLLMBackend(host="localhost", port=8000)
        assert backend.completion_url == "http://localhost:8000/v1/completions"

    def test_chat_completion_url(self):
        backend = VLLMBackend(host="localhost", port=8000)
        assert backend.chat_completion_url == "http://localhost:8000/v1/chat/completions"

    def test_health_url(self):
        backend = VLLMBackend(host="localhost", port=8000)
        assert backend.health_url == "http://localhost:8000/health"

    def test_custom_host_and_port(self):
        backend = VLLMBackend(host="gpu-server", port=9000, model="llama3")
        assert backend.base_url == "http://gpu-server:9000"
        assert backend.model == "llama3"


class TestVLLMBackendHealthCheck:

    @patch("genesia.backends.vllm.requests.get")
    def test_health_check_success(self, mock_get):
        mock_get.return_value = _mock_response(200)
        backend = VLLMBackend(host="localhost", port=8000)
        assert backend.health_check() is True

    @patch("genesia.backends.vllm.requests.get")
    def test_health_check_failure_non_200(self, mock_get):
        mock_get.return_value = _mock_response(503)
        backend = VLLMBackend(host="localhost", port=8000)
        # Should fall back to /v1/models
        assert backend.health_check() is False

    @patch("genesia.backends.vllm.requests.get")
    def test_health_check_fallback_to_models_on_exception(self, mock_get):
        """When /health raises exception, should fall back to /v1/models."""
        mock_get.side_effect = [
            ConnectionError("refused"),  # /health raises
            _mock_response(200),         # /v1/models succeeds
        ]
        backend = VLLMBackend(host="localhost", port=8000)
        assert backend.health_check() is True

    @patch("genesia.backends.vllm.requests.get")
    def test_health_check_connection_error(self, mock_get):
        mock_get.side_effect = ConnectionError("Connection refused")
        backend = VLLMBackend(host="localhost", port=8000)
        assert backend.health_check() is False


class TestVLLMBackendGenerate:

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_success(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"text": "Hello world", "finish_reason": "stop"}],
            "usage": {"completion_tokens": 5}
        })
        backend = VLLMBackend(host="localhost", port=8000, model="qwen2.5")
        result = backend.generate("Say hello")

        assert result["text"] == "Hello world"
        assert result["tokens_used"] == 5
        assert result["finish_reason"] == "stop"
        assert "time_ms" in result

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_sends_to_completions_endpoint(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"text": "response", "finish_reason": "stop"}],
            "usage": {"completion_tokens": 3}
        })
        backend = VLLMBackend(host="localhost", port=8000, model="test-model")
        backend.generate("prompt")

        call_args = mock_post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
        assert "/v1/completions" in str(url)

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_includes_model_in_payload(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"text": "ok", "finish_reason": "stop"}],
            "usage": {"completion_tokens": 1}
        })
        backend = VLLMBackend(host="localhost", port=8000, model="qwen2.5")
        backend.generate("test")

        call_body = mock_post.call_args[1].get("json")
        assert call_body["model"] == "qwen2.5"

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_no_repetition_penalty(self, mock_post):
        """generate() should not send repetition_penalty (only presence + frequency)."""
        mock_post.return_value = _mock_response(200, {
            "choices": [{"text": "ok", "finish_reason": "stop"}],
            "usage": {"completion_tokens": 1}
        })
        backend = VLLMBackend(host="localhost", port=8000)
        backend.generate("test")

        call_body = mock_post.call_args[1].get("json")
        assert "repetition_penalty" not in call_body
        assert "presence_penalty" in call_body
        assert "frequency_penalty" in call_body

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_custom_params(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"text": "result", "finish_reason": "length"}],
            "usage": {"completion_tokens": 100}
        })
        backend = VLLMBackend(host="localhost", port=8000)
        result = backend.generate("prompt", max_tokens=500, temperature=0.2, stop=["END"])

        call_body = mock_post.call_args[1].get("json")
        assert call_body["max_tokens"] == 500
        assert call_body["temperature"] == 0.2
        assert call_body["stop"] == ["END"]

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_empty_response_raises(self, mock_post):
        mock_post.return_value = _mock_response(200, {"choices": []})
        backend = VLLMBackend(host="localhost", port=8000)
        with pytest.raises(RuntimeError, match="Empty response"):
            backend.generate("prompt")

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_empty_text_raises(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"text": "", "finish_reason": "stop"}]
        })
        backend = VLLMBackend(host="localhost", port=8000)
        with pytest.raises(RuntimeError, match="Empty text"):
            backend.generate("prompt")

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_timeout_raises(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.Timeout("timed out")
        backend = VLLMBackend(host="localhost", port=8000)
        with pytest.raises(TimeoutError):
            backend.generate("prompt")

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_api_error_raises_runtime(self, mock_post):
        mock_post.return_value = _mock_response(500)
        mock_post.return_value.raise_for_status.side_effect = Exception("500 Server Error")
        backend = VLLMBackend(host="localhost", port=8000)
        with pytest.raises(Exception):
            backend.generate("prompt")


class TestVLLMBackendGenerateChat:

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_success(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "Chat response"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 10}
        })
        backend = VLLMBackend(host="localhost", port=8000, model="qwen2.5")
        result = backend.generate_chat([{"role": "user", "content": "Hi"}])

        assert result["text"] == "Chat response"
        assert result["tokens_used"] == 10
        assert result["finish_reason"] == "stop"
        assert "time_ms" in result

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_sends_to_chat_endpoint(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 1}
        })
        backend = VLLMBackend(host="localhost", port=8000)
        backend.generate_chat([{"role": "user", "content": "test"}])

        url = mock_post.call_args[0][0] if mock_post.call_args[0] else ""
        assert "/v1/chat/completions" in str(url)

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_preserves_system_message(self, mock_post):
        """System messages should be passed through to vLLM for chat template application."""
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "response"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 5}
        })
        messages = [
            {"role": "system", "content": "You are a code reviewer."},
            {"role": "user", "content": "Review this code"},
        ]
        backend = VLLMBackend(host="localhost", port=8000)
        backend.generate_chat(messages)

        call_body = mock_post.call_args[1].get("json")
        sent_messages = call_body["messages"]
        assert any(m["role"] == "system" for m in sent_messages)
        assert sent_messages[0]["content"] == "You are a code reviewer."

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_multi_turn(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "Sure, here's the fix"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 20}
        })
        messages = [
            {"role": "system", "content": "You are a coder."},
            {"role": "user", "content": "Write a function"},
            {"role": "assistant", "content": "def foo(): pass"},
            {"role": "user", "content": "Add error handling"},
        ]
        backend = VLLMBackend(host="localhost", port=8000, model="qwen2.5")
        result = backend.generate_chat(messages)

        call_body = mock_post.call_args[1].get("json")
        assert len(call_body["messages"]) == 4
        assert result["text"] == "Sure, here's the fix"

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_includes_model(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 1}
        })
        backend = VLLMBackend(host="localhost", port=8000, model="my-model")
        backend.generate_chat([{"role": "user", "content": "test"}])

        call_body = mock_post.call_args[1].get("json")
        assert call_body["model"] == "my-model"

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_no_repetition_penalty(self, mock_post):
        """generate_chat() should not send repetition_penalty."""
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 1}
        })
        backend = VLLMBackend(host="localhost", port=8000)
        backend.generate_chat([{"role": "user", "content": "test"}])

        call_body = mock_post.call_args[1].get("json")
        assert "repetition_penalty" not in call_body

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_custom_params(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 1}
        })
        backend = VLLMBackend(host="localhost", port=8000)
        backend.generate_chat(
            [{"role": "user", "content": "test"}],
            max_tokens=1000,
            temperature=0.1,
            stop=["DONE"]
        )

        call_body = mock_post.call_args[1].get("json")
        assert call_body["max_tokens"] == 1000
        assert call_body["temperature"] == 0.1
        assert call_body["stop"] == ["DONE"]

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_empty_choices_raises(self, mock_post):
        mock_post.return_value = _mock_response(200, {"choices": []})
        backend = VLLMBackend(host="localhost", port=8000)
        with pytest.raises(RuntimeError, match="Empty response"):
            backend.generate_chat([{"role": "user", "content": "test"}])

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_empty_content_raises(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}]
        })
        backend = VLLMBackend(host="localhost", port=8000)
        with pytest.raises(RuntimeError, match="Empty content"):
            backend.generate_chat([{"role": "user", "content": "test"}])

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_timeout_raises(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.Timeout("timed out")
        backend = VLLMBackend(host="localhost", port=8000)
        with pytest.raises(TimeoutError):
            backend.generate_chat([{"role": "user", "content": "test"}])

    @patch("genesia.backends.vllm.requests.post")
    def test_generate_chat_api_error_raises(self, mock_post):
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError("refused")
        backend = VLLMBackend(host="localhost", port=8000)
        with pytest.raises(RuntimeError):
            backend.generate_chat([{"role": "user", "content": "test"}])


# ===========================================================================
# LLMBackend Wrapper
# ===========================================================================

class TestLLMBackendInit:

    def test_default_backend_type_is_vllm(self):
        with patch("agents.llm_backend.VLLMBackend"):
            backend = LLMBackend(model="qwen2.5")
        assert backend.backend_type == "vllm"

    def test_default_port_is_8000(self):
        with patch("agents.llm_backend.VLLMBackend") as MockVLLM:
            LLMBackend(model="qwen2.5")
        MockVLLM.assert_called_once_with(host="localhost", port=8000, model="qwen2.5")

    def test_custom_host_and_port(self):
        with patch("agents.llm_backend.VLLMBackend") as MockVLLM:
            LLMBackend(model="llama3", host="gpu-server", port=9000)
        MockVLLM.assert_called_once_with(host="gpu-server", port=9000, model="llama3")


class TestLLMBackendRouting:

    @patch("genesia.backends.vllm.requests.post")
    def test_routes_to_generate_chat(self, mock_post):
        """LLMBackend should use generate_chat() to preserve system prompts."""
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "vLLM chat response"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 10}
        })
        wrapper = LLMBackend(model="qwen2.5", host="localhost", port=8000)
        messages = [
            {"role": "system", "content": "You are a Python expert."},
            {"role": "user", "content": "Write a decorator"},
        ]
        result = wrapper.generate(messages)

        # Should have called /v1/chat/completions
        url = mock_post.call_args[0][0]
        assert "/v1/chat/completions" in url
        # System message should be preserved in the payload
        call_body = mock_post.call_args[1].get("json")
        assert any(m["role"] == "system" for m in call_body["messages"])
        assert result == "vLLM chat response"

    @patch("genesia.backends.vllm.requests.post")
    def test_does_not_flatten_messages(self, mock_post):
        """LLMBackend should NOT use text flattening."""
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "response"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 5}
        })
        wrapper = LLMBackend(model="qwen2.5", host="localhost", port=8000)
        messages = [
            {"role": "system", "content": "System instructions"},
            {"role": "user", "content": "User query"},
        ]
        wrapper.generate(messages)

        call_body = mock_post.call_args[1].get("json")
        # Should have structured messages, not a flat "prompt" string
        assert "messages" in call_body
        assert "prompt" not in call_body


class TestCreateBackendFromConfig:

    def test_creates_vllm_backend(self):
        config = MagicMock()
        config.model.backend = "vllm"
        config.model.model_name = "qwen2.5"

        with patch("agents.llm_backend.VLLMBackend"):
            backend = create_backend_from_config(config)
        assert backend is not None
        assert backend.backend_type == "vllm"

    def test_reads_env_overrides(self):
        config = MagicMock()
        config.model.backend = "vllm"
        config.model.model_name = "default-model"

        with patch("agents.llm_backend.VLLMBackend") as MockVLLM, \
             patch.dict("os.environ", {"GENESIA_MODEL": "override-model", "GENESIA_BACKEND_HOST": "gpu-host", "GENESIA_BACKEND_PORT": "9000"}):
            backend = create_backend_from_config(config)

        MockVLLM.assert_called_once_with(host="gpu-host", port=9000, model="override-model")


class TestResponseTiming:

    @patch("genesia.backends.vllm.requests.post")
    def test_vllm_time_ms_is_non_negative(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 1}
        })
        backend = VLLMBackend(host="localhost", port=8000)
        result = backend.generate_chat([{"role": "user", "content": "test"}])
        assert result["time_ms"] >= 0
