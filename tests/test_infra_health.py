"""
Tests for infrastructure health probe module (agents/infra_health.py)
and health server wiring in agents/metrics.py.

Covers:
- SERVICE_REGISTRY structure and expected entries
- normalize_response (JSON with status/version, HTTP errors, non-JSON)
- probe_service (healthy, unhealthy, unreachable, timeout)
- check_all (all ok, mixed statuses, includes timestamp)
- check_service (known service, unknown service)
- Health server integration (aggregate endpoint, single service, unknown)
"""

import json
import os
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch

import pytest

os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

from agents.infra_health import (
    SERVICE_REGISTRY,
    normalize_response,
    probe_service,
    check_all,
    check_service,
)


# ===== Helpers: mock HTTP servers =====


class _MockHealthyHandler(BaseHTTPRequestHandler):
    """Returns 200 with JSON body."""

    response_body = json.dumps({"status": "ok", "version": "1.2.3", "uptime_seconds": 42})

    def do_GET(self):
        body = self.response_body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class _MockUnhealthyHandler(BaseHTTPRequestHandler):
    """Returns 503 with JSON body."""

    def do_GET(self):
        body = json.dumps({"error": "overloaded"}).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class _MockSlowHandler(BaseHTTPRequestHandler):
    """Takes 10s to respond (exceeds any reasonable timeout)."""

    def do_GET(self):
        time.sleep(10)
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


@pytest.fixture(scope="module")
def healthy_server():
    """Start a mock healthy HTTP server on an ephemeral port."""
    server = HTTPServer(("127.0.0.1", 0), _MockHealthyHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()


@pytest.fixture(scope="module")
def unhealthy_server():
    """Start a mock unhealthy HTTP server (503) on an ephemeral port."""
    server = HTTPServer(("127.0.0.1", 0), _MockUnhealthyHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()


@pytest.fixture(scope="module")
def slow_server():
    """Start a mock slow HTTP server on an ephemeral port."""
    server = HTTPServer(("127.0.0.1", 0), _MockSlowHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()


# ===== SERVICE_REGISTRY Tests =====


class TestServiceRegistry:
    """Validate SERVICE_REGISTRY structure."""

    EXPECTED_SERVICES = [
        "mirofish", "paddleocr", "searxng", "playwright",
        "penpot-backend", "penpot-frontend", "minio", "gitea",
        "ollama", "prometheus", "grafana",
    ]

    def test_expected_services_present(self):
        for svc in self.EXPECTED_SERVICES:
            assert svc in SERVICE_REGISTRY, f"Missing service: {svc}"

    def test_all_entries_have_url(self):
        for name, entry in SERVICE_REGISTRY.items():
            assert "url" in entry, f"{name} missing 'url'"
            assert entry["url"].startswith("http"), f"{name} url doesn't start with http"

    def test_all_entries_have_label(self):
        for name, entry in SERVICE_REGISTRY.items():
            assert "label" in entry, f"{name} missing 'label'"
            assert isinstance(entry["label"], str)
            assert len(entry["label"]) > 0

    def test_registry_is_dict(self):
        assert isinstance(SERVICE_REGISTRY, dict)
        assert len(SERVICE_REGISTRY) >= 11


# ===== normalize_response Tests =====


class TestNormalizeResponse:
    """Test response normalization logic."""

    def test_ok_json_with_status_and_version(self):
        raw = {"status": "ok", "version": "1.2.3", "uptime_seconds": 100}
        result = normalize_response("mirofish", raw, 200)
        assert result["status"] == "ok"
        assert result["service"] == "mirofish"
        assert result["version"] == "1.2.3"
        assert result["uptime_seconds"] == 100

    def test_http_error_sets_error_status(self):
        raw = {"error": "internal"}
        result = normalize_response("ollama", raw, 500)
        assert result["status"] == "error"
        assert result["service"] == "ollama"

    def test_http_4xx_sets_error_status(self):
        result = normalize_response("gitea", {"msg": "not found"}, 404)
        assert result["status"] == "error"

    def test_none_raw_returns_error(self):
        result = normalize_response("minio", None, 0)
        assert result["status"] == "error"
        assert result["service"] == "minio"

    def test_extra_fields_go_to_checks(self):
        raw = {"status": "ok", "version": "2.0", "db": "connected", "cache": "warm"}
        result = normalize_response("test", raw, 200)
        assert result["checks"]["db"] == "connected"
        assert result["checks"]["cache"] == "warm"
        assert "status" not in result["checks"]
        assert "version" not in result["checks"]

    def test_missing_version_defaults_to_none(self):
        raw = {"status": "ok"}
        result = normalize_response("svc", raw, 200)
        assert result["version"] is None

    def test_missing_uptime_defaults_to_none(self):
        raw = {"status": "ok"}
        result = normalize_response("svc", raw, 200)
        assert result["uptime_seconds"] is None

    def test_degraded_status_preserved(self):
        raw = {"status": "degraded"}
        result = normalize_response("svc", raw, 200)
        assert result["status"] == "degraded"

    def test_2xx_non_200_still_ok(self):
        raw = {"status": "ok"}
        result = normalize_response("svc", raw, 201)
        assert result["status"] == "ok"


# ===== probe_service Tests =====


class TestProbeService:
    """Test HTTP probing of services."""

    def test_healthy_service(self, healthy_server):
        host, port = healthy_server.server_address
        url = f"http://{host}:{port}/health"
        result = probe_service("test-svc", url, timeout=3)
        assert result["status"] == "ok"
        assert result["service"] == "test-svc"
        assert result["version"] == "1.2.3"
        assert result["uptime_seconds"] == 42

    def test_unhealthy_service(self, unhealthy_server):
        host, port = unhealthy_server.server_address
        url = f"http://{host}:{port}/health"
        result = probe_service("bad-svc", url, timeout=3)
        assert result["status"] == "error"
        assert result["service"] == "bad-svc"

    def test_unreachable_service(self):
        # Port 1 is almost certainly not listening
        result = probe_service("ghost-svc", "http://127.0.0.1:1/health", timeout=1)
        assert result["status"] == "error"
        assert result["service"] == "ghost-svc"
        assert "error" in result["checks"]

    def test_timeout_service(self, slow_server):
        host, port = slow_server.server_address
        url = f"http://{host}:{port}/health"
        result = probe_service("slow-svc", url, timeout=1)
        assert result["status"] == "error"
        assert result["service"] == "slow-svc"
        assert "error" in result["checks"]


# ===== check_all Tests =====


class TestCheckAll:
    """Test aggregate health checking."""

    def test_all_ok(self, healthy_server):
        host, port = healthy_server.server_address
        url = f"http://{host}:{port}/health"
        mock_registry = {
            "svc-a": {"url": url, "label": "Service A"},
            "svc-b": {"url": url, "label": "Service B"},
        }
        with patch("agents.infra_health.SERVICE_REGISTRY", mock_registry):
            result = check_all(timeout=3)

        assert result["status"] == "ok"
        assert result["summary"]["total"] == 2
        assert result["summary"]["ok"] == 2
        assert result["summary"]["error"] == 0
        assert result["summary"]["degraded"] == 0
        assert "timestamp" in result
        assert "svc-a" in result["services"]
        assert "svc-b" in result["services"]

    def test_mixed_statuses(self, healthy_server, unhealthy_server):
        h_host, h_port = healthy_server.server_address
        u_host, u_port = unhealthy_server.server_address
        mock_registry = {
            "good": {"url": f"http://{h_host}:{h_port}/health", "label": "Good"},
            "bad": {"url": f"http://{u_host}:{u_port}/health", "label": "Bad"},
        }
        with patch("agents.infra_health.SERVICE_REGISTRY", mock_registry):
            result = check_all(timeout=3)

        assert result["status"] == "error"
        assert result["summary"]["ok"] == 1
        assert result["summary"]["error"] == 1

    def test_includes_timestamp(self, healthy_server):
        host, port = healthy_server.server_address
        url = f"http://{host}:{port}/health"
        mock_registry = {"svc": {"url": url, "label": "S"}}
        with patch("agents.infra_health.SERVICE_REGISTRY", mock_registry):
            result = check_all(timeout=3)

        # ISO 8601 timestamp should be parseable
        from datetime import datetime
        ts = result["timestamp"]
        assert isinstance(ts, str)
        # Should contain 'T' separator
        assert "T" in ts

    def test_unreachable_services_counted(self):
        mock_registry = {
            "ghost": {"url": "http://127.0.0.1:1/nope", "label": "Ghost"},
        }
        with patch("agents.infra_health.SERVICE_REGISTRY", mock_registry):
            result = check_all(timeout=1)

        assert result["status"] == "error"
        assert result["summary"]["error"] == 1


# ===== check_service Tests =====


class TestCheckService:
    """Test single-service health check."""

    def test_known_service(self, healthy_server):
        host, port = healthy_server.server_address
        url = f"http://{host}:{port}/health"
        mock_registry = {"my-svc": {"url": url, "label": "My Service"}}
        with patch("agents.infra_health.SERVICE_REGISTRY", mock_registry):
            result = check_service("my-svc", timeout=3)

        assert result["status"] == "ok"
        assert result["service"] == "my-svc"

    def test_unknown_service(self):
        result = check_service("nonexistent-service", timeout=1)
        assert result["status"] == "error"
        assert "not found" in result.get("error", "").lower() or "not found" in json.dumps(result).lower()


# ===== Health Server Integration Tests =====


class TestHealthServerInfraEndpoints:
    """Test the /api/infrastructure/health endpoints wired into metrics.py."""

    @pytest.fixture(autouse=True)
    def _start_server(self, healthy_server):
        """Start a health server on an ephemeral port for integration tests."""
        from agents.metrics import _HealthHandler, start_health_server

        # Use an ephemeral port
        self._port = 0
        server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
        self._port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        # Patch the registry to use our mock healthy server
        h_host, h_port = healthy_server.server_address
        self._mock_registry = {
            "test-svc": {
                "url": f"http://{h_host}:{h_port}/health",
                "label": "Test Service",
            },
        }
        self._server = server
        yield
        server.shutdown()

    def test_aggregate_endpoint_returns_json(self):
        with patch("agents.infra_health.SERVICE_REGISTRY", self._mock_registry):
            url = f"http://127.0.0.1:{self._port}/api/infrastructure/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode())
                assert "status" in data
                assert "services" in data
                assert "summary" in data
                assert "timestamp" in data

    def test_single_service_endpoint(self):
        with patch("agents.infra_health.SERVICE_REGISTRY", self._mock_registry):
            url = f"http://127.0.0.1:{self._port}/api/infrastructure/health/test-svc"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode())
                assert data["service"] == "test-svc"
                assert data["status"] == "ok"

    def test_unknown_service_endpoint(self):
        url = f"http://127.0.0.1:{self._port}/api/infrastructure/health/no-such-svc"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                # Some implementations return 200 with error status
                data = json.loads(resp.read().decode())
                assert data["status"] == "error"
        except urllib.error.HTTPError as e:
            # 404 is also acceptable
            assert e.code == 404
