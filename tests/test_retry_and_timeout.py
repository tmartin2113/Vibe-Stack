"""
Tests for LLM retry logic and workflow node timeout enforcement.

Covers:
- llm_retry.py: retry_llm_call, _is_retryable, _extract_retry_after, _compute_delay, LLMRetryExhausted
- graph.py: NodeTimeoutError, WorkflowTimeoutError, CompiledWorkflow timeout enforcement
- llm_backend.py: retry wiring in LLMBackend.generate()
"""

import time
import pytest
from unittest.mock import patch, MagicMock, call

import requests

from agents.llm_retry import (
    retry_llm_call,
    _is_retryable,
    _extract_retry_after,
    _compute_delay,
    LLMRetryExhausted,
    RETRYABLE_STATUS_CODES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_BASE_DELAY,
)
from agents.graph import (
    Workflow,
    CompiledWorkflow,
    NodeTimeoutError,
    WorkflowTimeoutError,
    WorkflowRecursionError,
    END,
)


# ===========================================================================
# _is_retryable
# ===========================================================================

class TestIsRetryable:
    """Tests for the _is_retryable classification function."""

    def test_timeout_error_is_retryable(self):
        assert _is_retryable(TimeoutError("timed out")) is True

    def test_connection_error_is_retryable(self):
        assert _is_retryable(requests.exceptions.ConnectionError("refused")) is True

    def test_requests_timeout_is_retryable(self):
        assert _is_retryable(requests.exceptions.Timeout("timed out")) is True

    def test_runtime_error_with_429_message_is_retryable(self):
        assert _is_retryable(RuntimeError("API error: 429 Too Many Requests")) is True

    def test_runtime_error_with_500_message_is_retryable(self):
        assert _is_retryable(RuntimeError("Server error: 500")) is True

    def test_runtime_error_with_502_message_is_retryable(self):
        assert _is_retryable(RuntimeError("Bad Gateway 502")) is True

    def test_runtime_error_with_503_message_is_retryable(self):
        assert _is_retryable(RuntimeError("Service unavailable: 503")) is True

    def test_runtime_error_with_504_message_is_retryable(self):
        assert _is_retryable(RuntimeError("Gateway timeout 504")) is True

    def test_runtime_error_with_connection_message_is_retryable(self):
        assert _is_retryable(RuntimeError("connection refused")) is True

    def test_runtime_error_with_timeout_message_is_retryable(self):
        assert _is_retryable(RuntimeError("request timed out")) is True

    def test_runtime_error_with_unavailable_message_is_retryable(self):
        assert _is_retryable(RuntimeError("service unavailable")) is True

    def test_runtime_error_with_http_error_cause_429(self):
        """RuntimeError wrapping an HTTPError with 429 status is retryable."""
        response = MagicMock()
        response.status_code = 429
        http_err = requests.exceptions.HTTPError(response=response)
        err = RuntimeError("API error")
        err.__cause__ = http_err
        assert _is_retryable(err) is True

    def test_runtime_error_with_http_error_cause_503(self):
        response = MagicMock()
        response.status_code = 503
        http_err = requests.exceptions.HTTPError(response=response)
        err = RuntimeError("API error")
        err.__cause__ = http_err
        assert _is_retryable(err) is True

    def test_value_error_not_retryable(self):
        assert _is_retryable(ValueError("bad json")) is False

    def test_key_error_not_retryable(self):
        assert _is_retryable(KeyError("missing_key")) is False

    def test_runtime_error_with_400_not_retryable(self):
        assert _is_retryable(RuntimeError("400 Bad Request")) is False

    def test_runtime_error_with_401_not_retryable(self):
        assert _is_retryable(RuntimeError("401 Unauthorized")) is False

    def test_runtime_error_with_403_not_retryable(self):
        assert _is_retryable(RuntimeError("403 Forbidden")) is False

    def test_runtime_error_with_404_not_retryable(self):
        assert _is_retryable(RuntimeError("Model not found: 404")) is False

    def test_runtime_error_generic_not_retryable(self):
        """A generic RuntimeError with no retryable signal is not retried."""
        assert _is_retryable(RuntimeError("Error parsing response: invalid JSON")) is False

    def test_runtime_error_with_http_error_cause_401(self):
        """RuntimeError wrapping a 401 HTTPError is NOT retryable."""
        response = MagicMock()
        response.status_code = 401
        http_err = requests.exceptions.HTTPError(response=response)
        err = RuntimeError("Auth error")
        err.__cause__ = http_err
        assert _is_retryable(err) is False


# ===========================================================================
# _extract_retry_after
# ===========================================================================

class TestExtractRetryAfter:
    """Tests for Retry-After header extraction."""

    def test_no_response_attr(self):
        assert _extract_retry_after(RuntimeError("no response")) is None

    def test_response_without_headers(self):
        err = MagicMock(spec=["response"])
        err.response = MagicMock(spec=[])  # no .headers
        assert _extract_retry_after(err) is None

    def test_no_retry_after_header(self):
        err = MagicMock()
        err.response.headers = {}
        assert _extract_retry_after(err) is None

    def test_numeric_retry_after(self):
        err = MagicMock()
        err.response.headers = {"Retry-After": "5"}
        assert _extract_retry_after(err) == 5.0

    def test_float_retry_after(self):
        err = MagicMock()
        err.response.headers = {"Retry-After": "2.5"}
        assert _extract_retry_after(err) == 2.5

    def test_lowercase_header(self):
        err = MagicMock()
        headers = MagicMock()
        headers.get = lambda k, d=None: {"Retry-After": None, "retry-after": "10"}.get(k, d)
        err.response.headers = headers
        assert _extract_retry_after(err) == 10.0

    def test_non_numeric_retry_after_returns_none(self):
        err = MagicMock()
        headers = MagicMock()
        headers.get = lambda k, d=None: {"Retry-After": "Wed, 21 Oct 2023 07:28:00 GMT"}.get(k, d)
        err.response.headers = headers
        assert _extract_retry_after(err) is None


# ===========================================================================
# _compute_delay
# ===========================================================================

class TestComputeDelay:
    """Tests for delay computation."""

    def test_first_attempt_bounded_by_base(self):
        """First attempt delay should be between 0 and base_delay."""
        for _ in range(50):
            delay = _compute_delay(attempt=0, base_delay=1.0, max_delay=30.0, retry_after=None)
            assert 0 <= delay <= 1.0

    def test_second_attempt_bounded_by_2x_base(self):
        for _ in range(50):
            delay = _compute_delay(attempt=1, base_delay=1.0, max_delay=30.0, retry_after=None)
            assert 0 <= delay <= 2.0

    def test_third_attempt_bounded_by_4x_base(self):
        for _ in range(50):
            delay = _compute_delay(attempt=2, base_delay=1.0, max_delay=30.0, retry_after=None)
            assert 0 <= delay <= 4.0

    def test_delay_capped_at_max(self):
        for _ in range(50):
            delay = _compute_delay(attempt=10, base_delay=1.0, max_delay=5.0, retry_after=None)
            assert delay <= 5.0

    def test_retry_after_overrides_low_delay(self):
        """When Retry-After is larger than computed delay, use Retry-After."""
        delay = _compute_delay(attempt=0, base_delay=0.1, max_delay=30.0, retry_after=10.0)
        assert delay >= 10.0

    def test_retry_after_capped_at_max(self):
        delay = _compute_delay(attempt=0, base_delay=0.1, max_delay=5.0, retry_after=100.0)
        assert delay <= 5.0

    def test_retry_after_none_ignored(self):
        delay = _compute_delay(attempt=0, base_delay=1.0, max_delay=30.0, retry_after=None)
        assert delay <= 1.0


# ===========================================================================
# retry_llm_call
# ===========================================================================

class TestRetryLlmCall:
    """Tests for the main retry_llm_call function."""

    def test_success_on_first_try(self):
        fn = MagicMock(return_value={"text": "hello", "tokens_used": 5})
        result = retry_llm_call(fn, max_retries=3, base_delay=0.01)
        assert result == {"text": "hello", "tokens_used": 5}
        assert fn.call_count == 1

    def test_success_after_transient_failure(self):
        fn = MagicMock(
            side_effect=[TimeoutError("timeout"), {"text": "ok", "tokens_used": 3}]
        )
        result = retry_llm_call(fn, max_retries=3, base_delay=0.01)
        assert result == {"text": "ok", "tokens_used": 3}
        assert fn.call_count == 2

    def test_success_after_two_failures(self):
        fn = MagicMock(
            side_effect=[
                TimeoutError("t1"),
                RuntimeError("503 Service Unavailable"),
                {"text": "finally", "tokens_used": 1},
            ]
        )
        result = retry_llm_call(fn, max_retries=3, base_delay=0.01)
        assert result == {"text": "finally", "tokens_used": 1}
        assert fn.call_count == 3

    def test_exhausted_after_max_retries(self):
        fn = MagicMock(side_effect=TimeoutError("always timeout"))
        with pytest.raises(LLMRetryExhausted) as exc_info:
            retry_llm_call(fn, max_retries=2, base_delay=0.01)
        assert exc_info.value.attempts == 3  # 1 initial + 2 retries
        assert fn.call_count == 3

    def test_non_retryable_error_raises_immediately(self):
        fn = MagicMock(side_effect=ValueError("bad json"))
        with pytest.raises(ValueError, match="bad json"):
            retry_llm_call(fn, max_retries=3, base_delay=0.01)
        assert fn.call_count == 1

    def test_auth_error_not_retried(self):
        fn = MagicMock(side_effect=RuntimeError("401 Unauthorized"))
        with pytest.raises(RuntimeError, match="401"):
            retry_llm_call(fn, max_retries=3, base_delay=0.01)
        assert fn.call_count == 1

    def test_zero_retries_means_single_attempt(self):
        fn = MagicMock(side_effect=TimeoutError("timeout"))
        with pytest.raises(LLMRetryExhausted) as exc_info:
            retry_llm_call(fn, max_retries=0, base_delay=0.01)
        assert exc_info.value.attempts == 1
        assert fn.call_count == 1

    def test_args_and_kwargs_passed_through(self):
        def fn(prompt, temperature=0.7, max_tokens=100):
            return {"text": f"{prompt}-{temperature}-{max_tokens}", "tokens_used": 1}

        result = retry_llm_call(fn, "hello", temperature=0.5, max_tokens=200, max_retries=0, base_delay=0.01)
        assert result == {"text": "hello-0.5-200", "tokens_used": 1}

    def test_connection_error_retried(self):
        fn = MagicMock(
            side_effect=[
                requests.exceptions.ConnectionError("refused"),
                {"text": "ok", "tokens_used": 1},
            ]
        )
        result = retry_llm_call(fn, max_retries=2, base_delay=0.01)
        assert result == {"text": "ok", "tokens_used": 1}

    @patch("agents.llm_retry.time.sleep")
    def test_sleep_called_between_retries(self, mock_sleep):
        fn = MagicMock(
            side_effect=[TimeoutError("t1"), TimeoutError("t2"), {"text": "ok", "tokens_used": 1}]
        )
        retry_llm_call(fn, max_retries=3, base_delay=0.5)
        assert mock_sleep.call_count == 2

    @patch("agents.llm_retry.time.sleep")
    def test_no_sleep_on_last_attempt(self, mock_sleep):
        fn = MagicMock(side_effect=TimeoutError("always"))
        with pytest.raises(LLMRetryExhausted):
            retry_llm_call(fn, max_retries=1, base_delay=0.01)
        # 1 sleep between attempt 1 and 2, no sleep after the final failure
        assert mock_sleep.call_count == 1

    def test_llm_retry_exhausted_preserves_last_error(self):
        original = TimeoutError("the real error")
        fn = MagicMock(side_effect=original)
        with pytest.raises(LLMRetryExhausted) as exc_info:
            retry_llm_call(fn, max_retries=0, base_delay=0.01)
        assert exc_info.value.last_error is original

    def test_llm_retry_exhausted_str(self):
        err = LLMRetryExhausted(attempts=3, last_error=TimeoutError("boom"))
        assert "3 attempts" in str(err)
        assert "boom" in str(err)


# ===========================================================================
# LLMBackend retry wiring
# ===========================================================================

class TestLLMBackendRetryWiring:
    """Test that LLMBackend.generate() uses retry_llm_call."""

    @patch("agents.llm_backend.retry_llm_call")
    def test_backend_uses_retry(self, mock_retry):
        """vLLM backend routes through retry_llm_call with generate_chat."""
        from agents.llm_backend import LLMBackend

        mock_retry.return_value = {"text": "response", "tokens_used": 10}

        with patch.object(LLMBackend, "__init__", lambda self, **kw: None):
            backend = LLMBackend.__new__(LLMBackend)
            backend.backend_type = "vllm"
            backend.model_name = "qwen2.5"
            backend.max_retries = 2
            backend.retry_base_delay = 0.5
            backend.backend = MagicMock()

            messages = [{"role": "user", "content": "test"}]
            result = backend.generate(messages, temperature=0.3)

        assert result == "response"
        mock_retry.assert_called_once()
        call_kwargs = mock_retry.call_args
        assert call_kwargs.kwargs["max_retries"] == 2
        assert call_kwargs.kwargs["base_delay"] == 0.5

    def test_retry_config_stored(self):
        """max_retries and retry_base_delay stored from constructor."""
        from agents.llm_backend import LLMBackend
        with patch("agents.llm_backend.VLLMBackend"):
            backend = LLMBackend(
                model="test",
                max_retries=5,
                retry_base_delay=2.0,
            )
        assert backend.max_retries == 5
        assert backend.retry_base_delay == 2.0

    def test_retry_config_defaults(self):
        """Defaults match module-level constants."""
        from agents.llm_backend import LLMBackend
        with patch("agents.llm_backend.VLLMBackend"):
            backend = LLMBackend(model="test")
        assert backend.max_retries == DEFAULT_MAX_RETRIES
        assert backend.retry_base_delay == DEFAULT_BASE_DELAY


# ===========================================================================
# Node Timeout Enforcement
# ===========================================================================

class TestNodeTimeout:
    """Tests for per-node timeout in CompiledWorkflow."""

    def _make_workflow(self, node_fn, node_timeout=0, workflow_timeout=0):
        """Helper: single-node workflow."""
        wf = Workflow()
        wf.add_node("test_node", node_fn)
        wf.add_edge("test_node", END)
        wf.set_entry_point("test_node")
        return wf.compile(node_timeout=node_timeout, workflow_timeout=workflow_timeout)

    def test_no_timeout_works_normally(self):
        """With timeout=0, nodes run without any timeout enforcement."""
        def fast_node(state):
            state["done"] = True
            return state

        app = self._make_workflow(fast_node, node_timeout=0)
        result = app.invoke({"done": False})
        assert result["done"] is True

    def test_fast_node_within_timeout(self):
        """A fast node completes within the timeout."""
        def fast_node(state):
            state["result"] = "ok"
            return state

        app = self._make_workflow(fast_node, node_timeout=5)
        result = app.invoke({"result": None})
        assert result["result"] == "ok"

    def test_slow_node_raises_timeout(self):
        """A node that exceeds its timeout raises NodeTimeoutError."""
        def slow_node(state):
            time.sleep(10)
            return state

        app = self._make_workflow(slow_node, node_timeout=1)
        with pytest.raises(NodeTimeoutError) as exc_info:
            app.invoke({})
        assert exc_info.value.node_name == "test_node"
        assert exc_info.value.timeout == 1

    def test_timeout_in_stream_mode(self):
        """Timeout enforcement works in stream() mode too."""
        def slow_node(state):
            time.sleep(10)
            return state

        app = self._make_workflow(slow_node, node_timeout=1)
        with pytest.raises(NodeTimeoutError):
            list(app.stream({}))

    def test_node_timeout_error_message(self):
        err = NodeTimeoutError("specialist", 120)
        assert "specialist" in str(err)
        assert "120" in str(err)

    def test_node_timeout_stored(self):
        wf = Workflow()
        wf.add_node("a", lambda s: s)
        wf.add_edge("a", END)
        wf.set_entry_point("a")
        app = wf.compile(node_timeout=42)
        assert app._node_timeout == 42

    def test_multi_node_timeout_first_slow(self):
        """In a multi-node workflow, timeout fires on the slow node."""
        def fast(state):
            state["steps"] = state.get("steps", [])
            state["steps"].append("fast")
            return state

        def slow(state):
            time.sleep(10)
            return state

        wf = Workflow()
        wf.add_node("fast", fast)
        wf.add_node("slow", slow)
        wf.add_edge("fast", "slow")
        wf.add_edge("slow", END)
        wf.set_entry_point("fast")
        app = wf.compile(node_timeout=1)

        with pytest.raises(NodeTimeoutError) as exc_info:
            app.invoke({})
        assert exc_info.value.node_name == "slow"


# ===========================================================================
# Workflow Timeout Enforcement
# ===========================================================================

class TestWorkflowTimeout:
    """Tests for total workflow timeout."""

    def test_workflow_timeout_not_exceeded(self):
        """Fast workflow completes within budget."""
        def fast(state):
            state["done"] = True
            return state

        wf = Workflow()
        wf.add_node("a", fast)
        wf.add_edge("a", END)
        wf.set_entry_point("a")
        app = wf.compile(workflow_timeout=10)

        result = app.invoke({})
        assert result["done"] is True

    def test_workflow_timeout_exceeded(self):
        """Workflow that cumulatively exceeds budget raises WorkflowTimeoutError."""
        call_count = 0

        def slow_step(state):
            nonlocal call_count
            call_count += 1
            time.sleep(0.3)
            state["count"] = call_count
            return state

        # Build a looping workflow that will exceed the budget
        wf = Workflow()
        wf.add_node("step", slow_step)
        wf.add_conditional_edges(
            "step",
            lambda s: "again" if s.get("count", 0) < 20 else "done",
            {"again": "step", "done": END},
        )
        wf.set_entry_point("step")
        app = wf.compile(workflow_timeout=1)

        with pytest.raises(WorkflowTimeoutError) as exc_info:
            app.invoke({})
        assert exc_info.value.timeout == 1

    def test_workflow_timeout_in_stream(self):
        """Workflow timeout works in stream() mode."""
        call_count = 0

        def slow_step(state):
            nonlocal call_count
            call_count += 1
            time.sleep(0.3)
            state["count"] = call_count
            return state

        wf = Workflow()
        wf.add_node("step", slow_step)
        wf.add_conditional_edges(
            "step",
            lambda s: "again" if s.get("count", 0) < 20 else "done",
            {"again": "step", "done": END},
        )
        wf.set_entry_point("step")
        app = wf.compile(workflow_timeout=1)

        with pytest.raises(WorkflowTimeoutError):
            list(app.stream({}))

    def test_workflow_timeout_error_message(self):
        err = WorkflowTimeoutError(elapsed=123.4, timeout=120)
        assert "120" in str(err)
        assert "123.4" in str(err)

    def test_zero_workflow_timeout_means_no_limit(self):
        """workflow_timeout=0 means no enforcement."""
        def node(state):
            return state

        wf = Workflow()
        wf.add_node("a", node)
        wf.add_edge("a", END)
        wf.set_entry_point("a")
        app = wf.compile(workflow_timeout=0)
        result = app.invoke({"ok": True})
        assert result["ok"] is True


# ===========================================================================
# Workflow compile() with timeout params
# ===========================================================================

class TestWorkflowCompileTimeout:
    """Test that compile() passes timeout to CompiledWorkflow."""

    def test_compile_default_no_timeout(self):
        wf = Workflow()
        wf.add_node("a", lambda s: s)
        wf.add_edge("a", END)
        wf.set_entry_point("a")
        app = wf.compile()
        assert app._node_timeout == 0
        assert app._workflow_timeout == 0

    def test_compile_with_both_timeouts(self):
        wf = Workflow()
        wf.add_node("a", lambda s: s)
        wf.add_edge("a", END)
        wf.set_entry_point("a")
        app = wf.compile(node_timeout=60, workflow_timeout=300)
        assert app._node_timeout == 60
        assert app._workflow_timeout == 300


# ===========================================================================
# Config fields
# ===========================================================================

class TestConfigRetryFields:
    """Test that config dataclass has the new retry fields."""

    def test_workflow_config_has_retry_fields(self):
        from agents.config import WorkflowConfig
        cfg = WorkflowConfig()
        assert cfg.llm_max_retries == 3
        assert cfg.llm_retry_base_delay == 1.0

    def test_workflow_config_custom_retry(self):
        from agents.config import WorkflowConfig
        cfg = WorkflowConfig(llm_max_retries=5, llm_retry_base_delay=2.0)
        assert cfg.llm_max_retries == 5
        assert cfg.llm_retry_base_delay == 2.0

    def test_system_config_inherits_retry(self):
        from agents.config import SystemConfig
        cfg = SystemConfig()
        assert cfg.workflow.llm_max_retries == 3
        assert cfg.workflow.llm_retry_base_delay == 1.0


# ===========================================================================
# Integration: both features combined
# ===========================================================================

class TestIntegration:
    """Integration tests combining retry + timeout."""

    def test_node_exception_propagates_without_timeout(self):
        """Without timeout, node exceptions propagate normally."""
        def failing_node(state):
            raise RuntimeError("node crashed")

        wf = Workflow()
        wf.add_node("bad", failing_node)
        wf.add_edge("bad", END)
        wf.set_entry_point("bad")
        app = wf.compile(node_timeout=0)

        with pytest.raises(RuntimeError, match="node crashed"):
            app.invoke({})

    def test_node_exception_propagates_with_timeout(self):
        """With timeout, node exceptions still propagate (not masked)."""
        def failing_node(state):
            raise ValueError("bad state")

        wf = Workflow()
        wf.add_node("bad", failing_node)
        wf.add_edge("bad", END)
        wf.set_entry_point("bad")
        app = wf.compile(node_timeout=5)

        with pytest.raises(ValueError, match="bad state"):
            app.invoke({})

    def test_existing_recursion_limit_still_works(self):
        """WorkflowRecursionError still fires for infinite loops."""
        wf = Workflow()
        wf.add_node("loop", lambda s: s)
        wf.add_conditional_edges("loop", lambda s: "again", {"again": "loop"})
        wf.set_entry_point("loop")
        app = wf.compile()

        with pytest.raises(WorkflowRecursionError):
            app.invoke({})

    def test_stream_yields_correct_state(self):
        """Stream mode yields intermediate states correctly with timeout enabled."""
        def step1(state):
            state["step1"] = True
            return state

        def step2(state):
            state["step2"] = True
            return state

        wf = Workflow()
        wf.add_node("s1", step1)
        wf.add_node("s2", step2)
        wf.add_edge("s1", "s2")
        wf.add_edge("s2", END)
        wf.set_entry_point("s1")
        app = wf.compile(node_timeout=5, workflow_timeout=10)

        results = list(app.stream({}))
        assert len(results) == 2
        assert "s1" in results[0]
        assert results[0]["s1"]["step1"] is True
        assert "s2" in results[1]
        assert results[1]["s2"]["step2"] is True
