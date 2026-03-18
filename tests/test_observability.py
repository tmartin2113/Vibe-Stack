"""
Tests for observability infrastructure: metrics, health endpoints, request tracing.

Covers:
- MetricsRegistry (counters, gauges, histograms, Prometheus format, JSON export)
- Health server (/healthz, /readyz, /metrics, /status)
- Request context correlation (set/clear, JsonFormatter integration)
- Per-node metrics recording in CompiledWorkflow
- LLM retry metrics recording
"""

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error

import pytest

os.environ["GENESIA_DISABLE_REMOTE_SKILLS"] = "1"

from agents.metrics import MetricsRegistry, metrics, start_health_server
from agents.main import (
    JsonFormatter,
    set_request_context,
    clear_request_context,
    _request_context,
)


# ===== MetricsRegistry Tests =====


class TestMetricsRegistryCounters:
    """Test counter operations."""

    def test_increment_default(self):
        reg = MetricsRegistry()
        reg.increment("test_counter")
        assert reg.get_counter("test_counter") == 1.0

    def test_increment_custom_value(self):
        reg = MetricsRegistry()
        reg.increment("test_counter", value=5.0)
        assert reg.get_counter("test_counter") == 5.0

    def test_increment_accumulates(self):
        reg = MetricsRegistry()
        reg.increment("test_counter", value=3.0)
        reg.increment("test_counter", value=7.0)
        assert reg.get_counter("test_counter") == 10.0

    def test_increment_with_labels(self):
        reg = MetricsRegistry()
        reg.increment("test_counter", labels={"status": "success"})
        reg.increment("test_counter", labels={"status": "error"})
        reg.increment("test_counter", labels={"status": "success"})
        assert reg.get_counter("test_counter", labels={"status": "success"}) == 2.0
        assert reg.get_counter("test_counter", labels={"status": "error"}) == 1.0

    def test_get_counter_nonexistent(self):
        reg = MetricsRegistry()
        assert reg.get_counter("nonexistent") == 0.0


class TestMetricsRegistryGauges:
    """Test gauge operations."""

    def test_set_gauge(self):
        reg = MetricsRegistry()
        reg.set_gauge("test_gauge", 42.0)
        assert reg.get_gauge("test_gauge") == 42.0

    def test_set_gauge_overwrite(self):
        reg = MetricsRegistry()
        reg.set_gauge("test_gauge", 10.0)
        reg.set_gauge("test_gauge", 20.0)
        assert reg.get_gauge("test_gauge") == 20.0

    def test_set_gauge_with_labels(self):
        reg = MetricsRegistry()
        reg.set_gauge("test_gauge", 1.0, labels={"worker": "0"})
        reg.set_gauge("test_gauge", 2.0, labels={"worker": "1"})
        assert reg.get_gauge("test_gauge", labels={"worker": "0"}) == 1.0
        assert reg.get_gauge("test_gauge", labels={"worker": "1"}) == 2.0

    def test_get_gauge_nonexistent(self):
        reg = MetricsRegistry()
        assert reg.get_gauge("nonexistent") == 0.0


class TestMetricsRegistryHistograms:
    """Test histogram operations."""

    def test_observe_single(self):
        reg = MetricsRegistry()
        reg.observe("test_hist", 1.5)
        result = reg.format_json()
        assert result["test_hist_count"] == 1
        assert result["test_hist_sum"] == 1.5

    def test_observe_multiple(self):
        reg = MetricsRegistry()
        reg.observe("test_hist", 1.0)
        reg.observe("test_hist", 2.0)
        reg.observe("test_hist", 3.0)
        result = reg.format_json()
        assert result["test_hist_count"] == 3
        assert result["test_hist_sum"] == 6.0

    def test_observe_with_labels(self):
        reg = MetricsRegistry()
        reg.observe("test_hist", 1.0, labels={"node": "a"})
        reg.observe("test_hist", 2.0, labels={"node": "b"})
        result = reg.format_json()
        assert result['test_hist_count{node="a"}'] == 1
        assert result['test_hist_count{node="b"}'] == 1


class TestPrometheusFormat:
    """Test Prometheus text exposition format."""

    def test_empty_registry(self):
        reg = MetricsRegistry()
        output = reg.format_prometheus()
        assert output.strip() == ""

    def test_counter_format(self):
        reg = MetricsRegistry()
        reg.describe("test_total", "Test counter", "counter")
        reg.increment("test_total", labels={"status": "ok"})
        output = reg.format_prometheus()
        assert "# HELP test_total Test counter" in output
        assert "# TYPE test_total counter" in output
        assert 'test_total{status="ok"} 1.0' in output

    def test_gauge_format(self):
        reg = MetricsRegistry()
        reg.describe("test_gauge", "Test gauge", "gauge")
        reg.set_gauge("test_gauge", 42.0)
        output = reg.format_prometheus()
        assert "# HELP test_gauge Test gauge" in output
        assert "test_gauge 42.0" in output

    def test_histogram_buckets(self):
        reg = MetricsRegistry()
        reg.observe("test_hist", 0.05, buckets=(0.1, 1.0, 10.0))
        reg.observe("test_hist", 0.5)
        reg.observe("test_hist", 5.0)
        output = reg.format_prometheus()
        # 0.05 is <= 0.1
        assert 'test_hist_bucket{le="0.1"} 1' in output
        # 0.05 and 0.5 are <= 1.0
        assert 'test_hist_bucket{le="1.0"} 2' in output
        # All three are <= 10.0
        assert 'test_hist_bucket{le="10.0"} 3' in output
        assert 'test_hist_bucket{le="+Inf"} 3' in output
        assert "test_hist_sum 5.55" in output
        assert "test_hist_count 3" in output

    def test_labels_key_no_labels(self):
        assert MetricsRegistry._labels_key(None) == ""
        assert MetricsRegistry._labels_key({}) == ""

    def test_labels_key_sorted(self):
        result = MetricsRegistry._labels_key({"b": "2", "a": "1"})
        assert result == '{a="1",b="2"}'

    def test_merge_labels_empty(self):
        result = MetricsRegistry._merge_labels("", 'le="0.5"')
        assert result == '{le="0.5"}'

    def test_merge_labels_existing(self):
        result = MetricsRegistry._merge_labels('{node="a"}', 'le="0.5"')
        assert result == '{node="a",le="0.5"}'


class TestJsonExport:
    """Test JSON export format."""

    def test_counters_in_json(self):
        reg = MetricsRegistry()
        reg.increment("c1")
        reg.increment("c2", labels={"a": "b"})
        result = reg.format_json()
        assert result["c1"] == 1.0
        assert result['c2{a="b"}'] == 1.0

    def test_gauges_in_json(self):
        reg = MetricsRegistry()
        reg.set_gauge("g1", 99.0)
        result = reg.format_json()
        assert result["g1"] == 99.0

    def test_histograms_in_json(self):
        reg = MetricsRegistry()
        reg.observe("h1", 1.0)
        reg.observe("h1", 2.0)
        result = reg.format_json()
        assert result["h1_count"] == 2
        assert result["h1_sum"] == 3.0


class TestThreadSafety:
    """Test thread safety of MetricsRegistry."""

    def test_concurrent_increments(self):
        reg = MetricsRegistry()
        errors = []

        def increment_many():
            try:
                for _ in range(1000):
                    reg.increment("concurrent_counter")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=increment_many) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert reg.get_counter("concurrent_counter") == 10000.0


# ===== Health Server Tests =====


class TestHealthServer:
    """Test the health/metrics HTTP server."""

    _ready_flag = True  # Class-level flag the readiness_fn reads

    @classmethod
    def _readiness_check(cls):
        return cls._ready_flag

    @pytest.fixture(autouse=True, scope="class")
    def _start_server(self, request):
        """Start a single health server for the entire test class."""
        port = 18234
        request.cls.port = port
        server = start_health_server(
            port=port,
            readiness_fn=TestHealthServer._readiness_check,
            daemon_status_fn=lambda: {"running": True, "workers": 3},
        )
        # Give the server thread a moment to start
        time.sleep(0.2)
        request.cls.server = server
        yield
        if server:
            server.shutdown()
            time.sleep(0.1)  # let the socket release

    def _get(self, path: str) -> tuple:
        """Helper to GET a path and return (status, body)."""
        url = f"http://localhost:{self.port}{path}"
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_healthz_ok(self):
        status, body = self._get("/healthz")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"

    def test_readyz_ready(self):
        TestHealthServer._ready_flag = True
        status, body = self._get("/readyz")
        assert status == 200
        data = json.loads(body)
        assert data["ready"] is True

    def test_readyz_not_ready(self):
        TestHealthServer._ready_flag = False
        status, body = self._get("/readyz")
        assert status == 503
        data = json.loads(body)
        assert data["ready"] is False
        TestHealthServer._ready_flag = True  # reset

    def test_metrics_endpoint(self):
        # Record a metric so there's something to export
        metrics.increment("genesia_requests_total", labels={"status": "success", "intent": "code"})
        status, body = self._get("/metrics")
        assert status == 200
        assert "genesia_requests_total" in body

    def test_status_endpoint(self):
        status, body = self._get("/status")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert "metrics" in data
        assert data["daemon"]["running"] is True
        assert data["daemon"]["workers"] == 3

    def test_404_unknown_path(self):
        status, _ = self._get("/unknown")
        assert status == 404


# ===== Request Context Tracing Tests =====


class TestRequestContext:
    """Test request ID correlation in structured logs."""

    def setup_method(self):
        clear_request_context()

    def teardown_method(self):
        clear_request_context()

    def test_set_request_context(self):
        set_request_context(request_id="req-123", session_id="sess-456")
        assert _request_context.request_id == "req-123"
        assert _request_context.session_id == "sess-456"

    def test_clear_request_context(self):
        set_request_context(request_id="req-123", session_id="sess-456")
        clear_request_context()
        assert _request_context.request_id == ""
        assert _request_context.session_id == ""

    def test_json_formatter_includes_request_id(self):
        set_request_context(request_id="req-abc", session_id="sess-def")
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py",
            lineno=1, msg="test message", args=(), exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["request_id"] == "req-abc"
        assert data["session_id"] == "sess-def"
        assert data["message"] == "test message"

    def test_json_formatter_omits_empty_context(self):
        clear_request_context()
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py",
            lineno=1, msg="test message", args=(), exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "request_id" not in data
        assert "session_id" not in data

    def test_json_formatter_includes_exception(self):
        formatter = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="test.py",
            lineno=1, msg="error", args=(), exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError: boom" in data["exception"]

    def test_context_is_thread_local(self):
        """Verify request context is isolated per thread."""
        results = {}

        def thread_fn(thread_name, req_id):
            set_request_context(request_id=req_id)
            time.sleep(0.05)  # let other threads set theirs
            results[thread_name] = _request_context.request_id
            clear_request_context()

        t1 = threading.Thread(target=thread_fn, args=("t1", "req-1"))
        t2 = threading.Thread(target=thread_fn, args=("t2", "req-2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["t1"] == "req-1"
        assert results["t2"] == "req-2"


# ===== Workflow Node Metrics Tests =====


class TestWorkflowNodeMetrics:
    """Test that CompiledWorkflow records per-node duration metrics."""

    def test_node_duration_recorded(self):
        """Executing a node records genesia_node_duration_seconds."""
        from agents.graph import Workflow, END

        # Fresh registry to avoid cross-test pollution
        from agents.metrics import metrics as global_metrics

        # Record baseline
        baseline = global_metrics.get_counter("genesia_node_duration_seconds")

        wf = Workflow()
        wf.add_node("a", lambda state: state)
        wf.add_edge("a", END)
        wf.set_entry_point("a")
        app = wf.compile()
        app.invoke({"user_request": "test"})

        # Check that histogram observations were recorded
        prom = global_metrics.format_prometheus()
        assert 'genesia_node_duration_seconds_count{node="a"}' in prom

    def test_multiple_nodes_tracked(self):
        """Each node gets its own label in the histogram."""
        from agents.graph import Workflow, END
        from agents.metrics import metrics as global_metrics

        wf = Workflow()
        wf.add_node("x", lambda state: state)
        wf.add_node("y", lambda state: state)
        wf.add_edge("x", "y")
        wf.add_edge("y", END)
        wf.set_entry_point("x")
        app = wf.compile()
        app.invoke({"user_request": "test"})

        prom = global_metrics.format_prometheus()
        assert 'genesia_node_duration_seconds_count{node="x"}' in prom
        assert 'genesia_node_duration_seconds_count{node="y"}' in prom


# ===== Global Metrics Pre-registration Tests =====


class TestGlobalMetricsSetup:
    """Test that the global metrics registry has expected descriptions."""

    def test_requests_total_described(self):
        assert "genesia_requests_total" in metrics._help

    def test_node_duration_described(self):
        assert "genesia_node_duration_seconds" in metrics._help

    def test_llm_calls_described(self):
        assert "genesia_llm_calls_total" in metrics._help

    def test_queue_depth_described(self):
        assert "genesia_queue_depth" in metrics._help

    def test_uptime_described(self):
        assert "genesia_uptime_seconds" in metrics._help
