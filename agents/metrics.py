"""
Lightweight metrics collection and health endpoint for Vibe daemon.

Provides:
- Thread-safe counters, gauges, and histograms
- Prometheus-compatible text exposition at /metrics
- Health (/healthz) and readiness (/readyz) probes
- No external dependencies (uses stdlib http.server)

Usage:
    from agents.metrics import metrics, start_health_server

    # Record metrics
    metrics.increment("requests_total", labels={"status": "success"})
    metrics.observe("request_duration_seconds", 1.23, labels={"path": "code"})
    metrics.set_gauge("queue_depth", 5)

    # Start health server (call once, in daemon.start())
    start_health_server(port=8080, readiness_fn=my_readiness_check)
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default histogram buckets (seconds) — suited for LLM call latencies
DEFAULT_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)


class MetricsRegistry:
    """Thread-safe metrics registry with Prometheus text format export."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._gauges: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._histograms: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        self._histogram_buckets: Dict[str, Tuple[float, ...]] = {}
        # Help text for metrics
        self._help: Dict[str, str] = {}
        self._types: Dict[str, str] = {}

    def describe(self, name: str, help_text: str, metric_type: str = "counter"):
        """Register help text and type for a metric."""
        self._help[name] = help_text
        self._types[name] = metric_type

    def increment(self, name: str, value: float = 1.0, labels: Optional[Dict[str, str]] = None):
        """Increment a counter."""
        key = self._labels_key(labels)
        with self._lock:
            self._counters[name][key] += value

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None):
        """Set a gauge to an absolute value."""
        key = self._labels_key(labels)
        with self._lock:
            self._gauges[name][key] = value

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None,
                buckets: Optional[Tuple[float, ...]] = None):
        """Record an observation in a histogram."""
        key = self._labels_key(labels)
        with self._lock:
            self._histograms[name][key].append(value)
            if name not in self._histogram_buckets:
                self._histogram_buckets[name] = buckets or DEFAULT_BUCKETS

    def get_counter(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        """Read a counter value."""
        key = self._labels_key(labels)
        with self._lock:
            return self._counters[name][key]

    def get_gauge(self, name: str, labels: Optional[Dict[str, str]] = None) -> float:
        """Read a gauge value."""
        key = self._labels_key(labels)
        with self._lock:
            return self._gauges[name][key]

    def format_prometheus(self) -> str:
        """Export all metrics in Prometheus text exposition format."""
        lines: List[str] = []

        with self._lock:
            # Counters
            for name, counter_map in sorted(self._counters.items()):
                self._emit_help(lines, name)
                for label_key, value in sorted(counter_map.items()):
                    lines.append(f"{name}{label_key} {value}")

            # Gauges
            for name, gauge_map in sorted(self._gauges.items()):
                self._emit_help(lines, name)
                for label_key, value in sorted(gauge_map.items()):
                    lines.append(f"{name}{label_key} {value}")

            # Histograms
            for name, hist_map in sorted(self._histograms.items()):
                self._emit_help(lines, name)
                buckets = self._histogram_buckets.get(name, DEFAULT_BUCKETS)
                for label_key, observations in sorted(hist_map.items()):
                    total = len(observations)
                    obs_sum = sum(observations)

                    # Build bucket counts
                    for bound in buckets:
                        count = sum(1 for v in observations if v <= bound)
                        bucket_labels = self._merge_labels(label_key, f'le="{bound}"')
                        lines.append(f"{name}_bucket{bucket_labels} {count}")

                    # +Inf bucket
                    inf_labels = self._merge_labels(label_key, 'le="+Inf"')
                    lines.append(f"{name}_bucket{inf_labels} {total}")

                    lines.append(f"{name}_sum{label_key} {obs_sum}")
                    lines.append(f"{name}_count{label_key} {total}")

        lines.append("")  # trailing newline
        return "\n".join(lines)

    def format_json(self) -> Dict[str, Any]:
        """Export metrics as a JSON-friendly dict (for /healthz)."""
        result: Dict[str, Any] = {}
        with self._lock:
            for name, counter_map in self._counters.items():
                for label_key, value in counter_map.items():
                    result[f"{name}{label_key}"] = value
            for name, gauge_map in self._gauges.items():
                for label_key, value in gauge_map.items():
                    result[f"{name}{label_key}"] = value
            for name, hist_map in self._histograms.items():
                for label_key, observations in hist_map.items():
                    if observations:
                        result[f"{name}_count{label_key}"] = len(observations)
                        result[f"{name}_sum{label_key}"] = sum(observations)
        return result

    # ---- internal ----

    @staticmethod
    def _labels_key(labels: Optional[Dict[str, str]]) -> str:
        """Convert label dict to Prometheus label string."""
        if not labels:
            return ""
        parts = [f'{k}="{v}"' for k, v in sorted(labels.items())]
        return "{" + ",".join(parts) + "}"

    @staticmethod
    def _merge_labels(existing_key: str, extra: str) -> str:
        """Merge an additional label into an existing label key string."""
        if not existing_key:
            return "{" + extra + "}"
        # Insert before closing brace
        return existing_key[:-1] + "," + extra + "}"

    def _emit_help(self, lines: List[str], name: str):
        """Emit HELP and TYPE lines if registered."""
        if name in self._help:
            lines.append(f"# HELP {name} {self._help[name]}")
        if name in self._types:
            lines.append(f"# TYPE {name} {self._types[name]}")


# ===== GLOBAL REGISTRY =====

metrics = MetricsRegistry()

# Pre-register metric descriptions
metrics.describe("vibe_requests_total", "Total workflow requests processed", "counter")
metrics.describe("vibe_requests_failed_total", "Total workflow requests that failed", "counter")
metrics.describe("vibe_request_duration_seconds", "Workflow request duration in seconds", "histogram")
metrics.describe("vibe_node_duration_seconds", "Per-node execution duration in seconds", "histogram")
metrics.describe("vibe_queue_depth", "Current request queue depth", "gauge")
metrics.describe("vibe_active_workers", "Number of active worker threads", "gauge")
metrics.describe("vibe_uptime_seconds", "Daemon uptime in seconds", "gauge")
metrics.describe("vibe_llm_calls_total", "Total LLM backend calls", "counter")
metrics.describe("vibe_llm_retries_total", "Total LLM retry attempts", "counter")
metrics.describe("vibe_llm_call_duration_seconds", "LLM call duration in seconds", "histogram")
metrics.describe("vibe_requests_dropped_total", "Requests dropped due to full queue", "counter")

# Heartbeat-specific metrics
metrics.describe("vibe_heartbeat_total", "Total heartbeat executions", "counter")
metrics.describe("vibe_heartbeat_duration_seconds", "Heartbeat execution duration in seconds", "histogram")
metrics.describe("vibe_heartbeat_tokens_total", "Total tokens consumed by heartbeat runs", "counter")
metrics.describe("vibe_heartbeat_workflow_duration_seconds", "Workflow-only duration within a heartbeat", "histogram")
metrics.describe("vibe_paperclip_api_calls_total", "Total Paperclip API calls", "counter")
metrics.describe("vibe_paperclip_api_errors_total", "Paperclip API call errors", "counter")
metrics.describe("vibe_paperclip_api_duration_seconds", "Paperclip API call duration in seconds", "histogram")


# ===== HEALTH SERVER =====

class _HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health, readiness, and metrics endpoints."""

    # Class-level references set by start_health_server()
    readiness_fn: Optional[Callable[[], bool]] = None
    daemon_status_fn: Optional[Callable[[], Dict[str, Any]]] = None

    def do_GET(self):
        if self.path == "/healthz":
            self._handle_healthz()
        elif self.path == "/readyz":
            self._handle_readyz()
        elif self.path == "/metrics":
            self._handle_metrics()
        elif self.path == "/status":
            self._handle_status()
        else:
            self.send_error(404)

    def _handle_healthz(self):
        """Liveness probe — checks LLM backend and disk health."""
        checks: Dict[str, Any] = {}
        healthy = True

        # Check 1: LLM backend reachable
        llm_host = os.environ.get("VIBE_BACKEND_HOST", "")
        llm_port = os.environ.get("VIBE_BACKEND_PORT", "8000")
        if llm_host:
            try:
                import urllib.request
                url = f"http://{llm_host}:{llm_port}/health"
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    checks["llm_backend"] = "ok" if resp.status == 200 else "degraded"
            except Exception:
                checks["llm_backend"] = "unreachable"
                healthy = False
        else:
            checks["llm_backend"] = "not_configured"

        # Check 2: Data directory writable
        data_dir = os.path.join(os.environ.get("HOME", "/tmp"), ".vibe")
        try:
            test_file = os.path.join(data_dir, ".healthcheck")
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            checks["data_dir"] = "ok"
        except Exception:
            checks["data_dir"] = "not_writable"
            healthy = False

        # Check 3: Paperclip API reachable (if configured)
        paperclip_url = os.environ.get("PAPERCLIP_API_URL", "")
        if paperclip_url:
            try:
                import urllib.request
                url = f"{paperclip_url.rstrip('/')}/health"
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    checks["paperclip_api"] = "ok" if resp.status == 200 else "degraded"
            except Exception:
                checks["paperclip_api"] = "unreachable"
                # Paperclip unreachable is degraded, not fatal for liveness
                checks["paperclip_api_note"] = "degraded_but_not_fatal"

        status_code = 200 if healthy else 503
        body = json.dumps({"status": "ok" if healthy else "unhealthy", "checks": checks})
        self._respond(status_code, body, "application/json")

    def _handle_readyz(self):
        """Readiness probe — is the daemon ready to accept work?"""
        ready = True
        fn = _HealthHandler.readiness_fn
        if fn:
            ready = fn()
        status = 200 if ready else 503
        body = json.dumps({"ready": ready})
        self._respond(status, body, "application/json")

    def _handle_metrics(self):
        """Prometheus text exposition."""
        body = metrics.format_prometheus()
        self._respond(200, body, "text/plain; version=0.0.4; charset=utf-8")

    def _handle_status(self):
        """JSON status endpoint (superset of healthz)."""
        status_data = {"status": "ok", "metrics": metrics.format_json()}
        fn = _HealthHandler.daemon_status_fn
        if fn:
            status_data["daemon"] = fn()
        body = json.dumps(status_data, default=str)
        self._respond(200, body, "application/json")

    def _respond(self, code: int, body: str, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        """Suppress default stderr logging — use our logger instead."""
        logger.debug("Health endpoint: %s", format % args)


def start_health_server(
    port: int = 8080,
    readiness_fn: Optional[Callable[[], bool]] = None,
    daemon_status_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Optional[HTTPServer]:
    """
    Start the health/metrics HTTP server in a background daemon thread.

    Args:
        port: Port to listen on (default 8080, configurable via VIBE_HEALTH_PORT)
        readiness_fn: Callable returning True when daemon is ready
        daemon_status_fn: Callable returning daemon status dict (for /status)

    Returns:
        The HTTPServer instance, or None if startup failed.
    """
    port = int(os.environ.get("VIBE_HEALTH_PORT", str(port)))

    _HealthHandler.readiness_fn = readiness_fn
    _HealthHandler.daemon_status_fn = daemon_status_fn

    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        thread = threading.Thread(
            target=server.serve_forever,
            name="HealthServer",
            daemon=True,
        )
        thread.start()
        logger.info(f"Health server started on port {port} "
                     f"(endpoints: /healthz, /readyz, /metrics, /status)")
        return server
    except OSError as e:
        logger.warning(f"Failed to start health server on port {port}: {e}")
        return None
