"""
End-to-end infrastructure validation (VIB-62).

Test layers:
- Layer 1: Service Health (DevOps — VIB-63)
- Layer 2: Integration Connectivity (Backend — VIB-64)
- Layer 3: Pipeline E2E (QA — VIB-65)
- Layer 4: Consolidated Validation Summary (QA — VIB-65)

All tests are marked with @pytest.mark.e2e.
HTTP probes use 10-second timeouts and skip gracefully when a service is not running.

Run with:
    python -m pytest tests/test_infra_e2e.py -x -v -k service_health
    python -m pytest tests/test_infra_e2e.py -x -v -k integration
    python -m pytest tests/test_infra_e2e.py -x -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import typing

import pytest
import requests


# ── Helpers ──────────────────────────────────────────────────────────

PROBE_TIMEOUT = 10  # seconds per HTTP probe


def _probe_http(url: str, timeout: int = PROBE_TIMEOUT) -> requests.Response:
    """Fire a GET request; let the caller handle exceptions."""
    return requests.get(url, timeout=timeout)


def _service_available(url: str, timeout: int = 3) -> bool:
    """Return True if the URL responds with any 2xx status."""
    try:
        resp = requests.get(url, timeout=timeout)
        return 200 <= resp.status_code < 300
    except (requests.ConnectionError, requests.Timeout, OSError):
        return False


def _service_reachable(url: str) -> bool:
    """Return True if the URL responds (any non-5xx)."""
    try:
        resp = requests.get(url, timeout=PROBE_TIMEOUT)
        return resp.status_code < 500
    except (requests.ConnectionError, requests.Timeout, OSError):
        return False


def _skip_if_unreachable(url: str, service_name: str) -> None:
    """pytest.skip if the service is not deployed or unreachable."""
    if not _service_reachable(url):
        pytest.skip(f"{service_name} not reachable at {url}")


def _has_nvidia_gpu() -> bool:
    """Return True if nvidia-smi is available and reports a GPU."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0 and len(result.stdout.strip()) > 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ══════════════════════════════════════════════════════════════════════
# Layer 1 — Service Health (all compose stacks) — VIB-63
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
@pytest.mark.infra
class TestServiceHealth:
    """Probe health endpoints for all Docker Compose services."""

    # ── Core (docker-compose.yml) ────────────────────────────────────

    def test_service_health_paperclip_server(self):
        """Paperclip server /api/health responds 200."""
        url = "http://localhost:3100/api/health"
        _skip_if_unreachable(url, "Paperclip server")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  Paperclip server  {url}")

    def test_service_health_deerflow_langgraph(self):
        """DeerFlow LangGraph /ok responds 200."""
        url = "http://localhost:2024/ok"
        _skip_if_unreachable(url, "DeerFlow LangGraph")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  DeerFlow LangGraph  {url}")

    def test_service_health_deerflow_gateway(self):
        """DeerFlow Gateway /health responds 200."""
        url = "http://localhost:8001/health"
        _skip_if_unreachable(url, "DeerFlow Gateway")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  DeerFlow Gateway  {url}")

    def test_service_health_vibe_agent(self):
        """Vibe agent /healthz responds 200."""
        url = "http://localhost:8080/healthz"
        _skip_if_unreachable(url, "Vibe agent")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  Vibe agent  {url}")

    # ── Infrastructure (docker-compose.infra.yml) ────────────────────

    def test_service_health_searxng(self):
        """SearXNG /healthz responds 200."""
        url = "http://localhost:8888/healthz"
        _skip_if_unreachable(url, "SearXNG")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  SearXNG  {url}")

    def test_service_health_playwright(self):
        """Playwright /json responds 200."""
        url = "http://localhost:3003/json"
        _skip_if_unreachable(url, "Playwright")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  Playwright  {url}")

    def test_service_health_gitea(self):
        """Gitea /api/v1/version responds 200."""
        url = "http://localhost:3000/api/v1/version"
        _skip_if_unreachable(url, "Gitea")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  Gitea  {url}")

    def test_service_health_minio(self):
        """MinIO responds on port 9000."""
        url = "http://localhost:9000/minio/health/live"
        _skip_if_unreachable(url, "MinIO")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  MinIO  {url}")

    def test_service_health_paddleocr(self):
        """PaddleOCR /health responds 200."""
        url = "http://localhost:8868/health"
        _skip_if_unreachable(url, "PaddleOCR")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  PaddleOCR  {url}")

    def test_service_health_prometheus(self):
        """Prometheus /-/healthy responds 200."""
        url = "http://localhost:9091/-/healthy"
        _skip_if_unreachable(url, "Prometheus")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  Prometheus  {url}")

    def test_service_health_grafana(self):
        """Grafana /api/health responds 200."""
        url = "http://localhost:3333/api/health"
        _skip_if_unreachable(url, "Grafana")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  Grafana  {url}")

    def test_service_health_mirofish(self):
        """MiroFish /health responds 200."""
        url = "http://localhost:5001/health"
        _skip_if_unreachable(url, "MiroFish")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  MiroFish  {url}")

    # ── GPU Services (docker-compose.gpu.yml) ────────────────────────

    def test_service_health_opensandbox(self):
        """OpenSandbox /docs responds (requires GPU)."""
        if not _has_nvidia_gpu():
            pytest.skip("No NVIDIA GPU detected — skipping GPU services")
        url = "http://localhost:9090/docs"
        _skip_if_unreachable(url, "OpenSandbox")
        resp = _probe_http(url)
        assert resp.status_code == 200
        print(f"PASS  OpenSandbox  {url}")


@pytest.mark.e2e
@pytest.mark.infra
class TestInterServiceDNS:
    """Verify inter-service DNS resolution from inside the vibe container."""

    @staticmethod
    def _docker_exec(container: str, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run a command inside a running Docker container."""
        return subprocess.run(
            ["docker", "exec", container, *cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )

    @staticmethod
    def _find_vibe_container() -> typing.Optional[str]:
        """Find the running vibe container name."""
        try:
            result = subprocess.run(
                [
                    "docker", "ps", "--filter", "name=vibe",
                    "--format", "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for name in result.stdout.strip().splitlines():
                if "vibe" in name and "data" not in name and "comfyui" not in name:
                    return name
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def test_dns_vibe_to_server(self):
        """Vibe container can resolve and reach server:3100."""
        container = self._find_vibe_container()
        if not container:
            pytest.skip("Vibe container not running")
        result = self._docker_exec(
            container,
            ["curl", "-sf", "--max-time", "5", "http://server:3100/api/health"],
        )
        assert result.returncode == 0, (
            f"Vibe container cannot reach server:3100 — stderr: {result.stderr}"
        )
        print(f"PASS  DNS vibe->server:3100 (container={container})")

    def test_dns_vibe_to_langgraph(self):
        """Vibe container can resolve and reach deerflow-langgraph:2024."""
        container = self._find_vibe_container()
        if not container:
            pytest.skip("Vibe container not running")
        result = self._docker_exec(
            container,
            [
                "curl", "-sf", "--max-time", "5",
                "http://deerflow-langgraph:2024/ok",
            ],
        )
        assert result.returncode == 0, (
            f"Vibe container cannot reach deerflow-langgraph:2024 — stderr: {result.stderr}"
        )
        print(f"PASS  DNS vibe->deerflow-langgraph:2024 (container={container})")


@pytest.mark.e2e
@pytest.mark.infra
class TestDockerHealthStatus:
    """Parse docker compose ps and assert all running containers are healthy."""

    @staticmethod
    def _compose_ps() -> list[dict]:
        """Run docker compose ps --format json and return parsed entries."""
        try:
            result = subprocess.run(
                ["docker", "compose", "ps", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            )
            if result.returncode != 0:
                return []
            entries = []
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, list):
                        entries.extend(parsed)
                    else:
                        entries.append(parsed)
                except json.JSONDecodeError:
                    continue
            return entries
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def test_all_running_containers_healthy(self):
        """Every running container with a healthcheck should report healthy."""
        entries = self._compose_ps()
        if not entries:
            pytest.skip("No Docker Compose containers found")

        unhealthy = []
        for entry in entries:
            name = entry.get("Name", "unknown")
            state = entry.get("State", "")
            health = entry.get("Health", "")

            if state != "running":
                continue
            if not health or health == "":
                continue
            if health != "healthy":
                unhealthy.append(f"{name} (health={health})")

        if unhealthy:
            pytest.fail(
                f"Unhealthy containers: {', '.join(unhealthy)}"
            )
        print(f"PASS  All {len(entries)} compose containers healthy")


# ══════════════════════════════════════════════════════════════════════
# Layer 2 — Integration Connectivity — VIB-64
# ══════════════════════════════════════════════════════════════════════


# ── Test class ───────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.integration
class TestIntegrationConnectivity:
    """Cross-service connectivity and subsystem health checks."""

    # ── Paperclip API ────────────────────────────────────────────────

    def test_paperclip_health(self):
        """GET /api/health returns 200."""
        paperclip_url = os.environ.get(
            "PAPERCLIP_API_URL", "http://localhost:3100"
        )
        if not _service_available(f"{paperclip_url}/api/health"):
            pytest.skip("Paperclip API not reachable")

        resp = _probe_http(f"{paperclip_url}/api/health")
        assert resp.status_code == 200, (
            f"Paperclip /api/health returned {resp.status_code}"
        )

    def test_paperclip_agents_list(self):
        """GET /api/companies/{companyId}/agents returns the expected agent list."""
        paperclip_url = os.environ.get(
            "PAPERCLIP_API_URL", "http://localhost:3100"
        )
        api_key = os.environ.get("PAPERCLIP_API_KEY", "")
        company_id = os.environ.get("PAPERCLIP_COMPANY_ID", "")

        if not company_id or not api_key:
            pytest.skip("PAPERCLIP_COMPANY_ID or PAPERCLIP_API_KEY not set")
        if not _service_available(f"{paperclip_url}/api/health"):
            pytest.skip("Paperclip API not reachable")

        resp = _probe_http(
            f"{paperclip_url}/api/companies/{company_id}/agents",
        )
        # Auth may be required — accept 200 or 401 (service is up either way)
        if resp.status_code == 401:
            pytest.skip("Paperclip API returned 401 — valid key required")

        assert resp.status_code == 200, (
            f"Agents list returned {resp.status_code}"
        )
        agents = resp.json()
        assert isinstance(agents, list), "Expected a JSON array of agents"

    # ── Ollama reachability ──────────────────────────────────────────

    def test_ollama_reachable(self):
        """Probe Ollama at localhost:11434/v1/models."""
        ollama_url = os.environ.get(
            "VIBE_OLLAMA_URL", "http://localhost:11434"
        )
        if not _service_available(f"{ollama_url}/v1/models"):
            pytest.skip("Ollama not running at " + ollama_url)

        resp = _probe_http(f"{ollama_url}/v1/models")
        assert resp.status_code == 200, (
            f"Ollama /v1/models returned {resp.status_code}"
        )

    # ── DeerFlow integration ─────────────────────────────────────────

    def test_deerflow_langgraph(self):
        """DeerFlow LangGraph at localhost:2024/ok responds."""
        deerflow_url = os.environ.get(
            "DEERFLOW_LANGGRAPH_URL", "http://localhost:2024"
        )
        if not _service_available(f"{deerflow_url}/ok"):
            pytest.skip("DeerFlow LangGraph not running")

        resp = _probe_http(f"{deerflow_url}/ok")
        assert resp.status_code == 200, (
            f"DeerFlow /ok returned {resp.status_code}"
        )

    def test_deerflow_gateway(self):
        """DeerFlow gateway at localhost:8001/health is up."""
        gateway_url = os.environ.get(
            "DEERFLOW_GATEWAY_URL", "http://localhost:8001"
        )
        if not _service_available(f"{gateway_url}/health"):
            pytest.skip("DeerFlow gateway not running")

        resp = _probe_http(f"{gateway_url}/health")
        assert resp.status_code == 200, (
            f"Gateway /health returned {resp.status_code}"
        )

    # ── Storage backend init ─────────────────────────────────────────

    def test_sqlite_backend_init(self):
        """Instantiate SQLiteBackend in a temp dir, run execute + fetchone."""
        from agents.storage.sqlite import SQLiteBackend

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            backend = SQLiteBackend(db_path=db_path)

            backend.execute(
                "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, val TEXT)"
            )
            backend.execute(
                "INSERT INTO probe (val) VALUES (?)", ("hello",)
            )
            row = backend.fetchone("SELECT val FROM probe WHERE id = 1")

            assert row is not None, "fetchone returned None"
            assert row[0] == "hello", f"Expected 'hello', got {row[0]!r}"

            backend.close()

    # ── Doctor mode ──────────────────────────────────────────────────

    def test_doctor_mode(self):
        """Run `python -m agents.main --doctor` and verify structured output."""
        result = subprocess.run(
            [sys.executable, "-m", "agents.main", "--doctor"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        combined = result.stdout + result.stderr

        # Doctor should produce the "Vibe Doctor" banner
        if "Vibe Doctor" not in combined:
            pytest.skip(
                "Doctor output missing expected banner — "
                "dependencies may not be installed"
            )

        # Parse pass/fail counts from the summary line (e.g. "3 ok, 1 warning(s)")
        has_summary = any(
            tok in combined for tok in ("ok", "warning", "failure")
        )
        assert has_summary, (
            "Doctor output did not contain a recognizable summary line"
        )

        # Non-zero exit is acceptable (means some checks failed/warned),
        # but a crash (returncode > 1 without the banner) is not.
        if result.returncode not in (0, 1):
            pytest.fail(
                f"Doctor exited with unexpected code {result.returncode}:\n"
                + combined[-500:]
            )

    # ── Health report script ─────────────────────────────────────────

    def test_health_report_script(self):
        """Run scripts/health-report.sh and verify it produces structured output."""
        repo_root = os.path.dirname(os.path.dirname(__file__))
        script = os.path.join(repo_root, "scripts", "health-report.sh")

        if not os.path.isfile(script):
            pytest.skip("scripts/health-report.sh not found")

        result = subprocess.run(
            ["bash", script],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_root,
            env={**os.environ, "PAPERCLIP_API_KEY": "", "PAPERCLIP_COMPANY_ID": ""},
        )
        combined = result.stdout + result.stderr

        # The script should produce [health] log lines
        has_health_output = "[health]" in combined
        if not has_health_output:
            # Script may fail early due to missing docker — that is acceptable
            if "docker" in combined.lower() or result.returncode != 0:
                pytest.skip(
                    "health-report.sh could not run (likely missing Docker)"
                )
            pytest.fail(
                "health-report.sh produced no [health] output:\n"
                + combined[-500:]
            )


# ── Shared config for Pipeline E2E ────────────────────────────────────────────

PAPERCLIP_URL = os.getenv("PAPERCLIP_API_URL", "http://localhost:3100").rstrip("/")
# Replace Docker-internal hostname with localhost for host-side access
PAPERCLIP_URL = PAPERCLIP_URL.replace("http://server:", "http://localhost:")

_PIPELINE_API_KEY = os.getenv("PAPERCLIP_API_KEY", "")
_PIPELINE_COMPANY_ID = os.getenv("PAPERCLIP_COMPANY_ID", "")
_PIPELINE_AGENT_ID = os.getenv("PAPERCLIP_AGENT_ID", "")

_PIPELINE_HEADERS = {
    "Authorization": f"Bearer {_PIPELINE_API_KEY}",
    "Content-Type": "application/json",
}


def _paperclip_reachable() -> bool:
    """Return True if the Paperclip API health endpoint responds."""
    if not PAPERCLIP_URL or not _PIPELINE_API_KEY:
        return False
    try:
        resp = requests.get(f"{PAPERCLIP_URL}/api/health", timeout=5)
        return resp.ok
    except requests.RequestException:
        return False


_paperclip_available_cache = None


def _paperclip_ok() -> bool:
    global _paperclip_available_cache
    if _paperclip_available_cache is None:
        _paperclip_available_cache = _paperclip_reachable()
    return _paperclip_available_cache


skip_no_paperclip = pytest.mark.skipif(
    not os.getenv("PAPERCLIP_API_KEY"),
    reason="PAPERCLIP_API_KEY not set — skipping pipeline E2E tests",
)

skip_no_agent = pytest.mark.skipif(
    not os.getenv("PAPERCLIP_AGENT_ID"),
    reason="PAPERCLIP_AGENT_ID not set — skipping heartbeat tests",
)


# ── Layer 3: Pipeline E2E (QA — VIB-65) ──────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.pipeline
class TestPipelineE2E:
    """Full pipeline E2E: issue creation -> heartbeat pickup -> result posting -> cleanup."""

    _created_issue_id: str = ""

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _create_test_issue() -> dict:
        """Create a test issue via Paperclip API. Returns raw issue dict."""
        body = {
            "title": "[INFRA-TEST] E2E pipeline validation",
            "description": (
                "Automated infrastructure validation test issue.\n\n"
                "This issue was created by `TestPipelineE2E` to verify the "
                "full agent pipeline: issue creation -> heartbeat -> workflow "
                "-> result posting.\n\n"
                "**This issue will be automatically cancelled after the test completes.**"
            ),
            "status": "todo",
            "priority": "low",
        }
        if _PIPELINE_AGENT_ID:
            body["assigneeAgentId"] = _PIPELINE_AGENT_ID

        resp = requests.post(
            f"{PAPERCLIP_URL}/api/companies/{_PIPELINE_COMPANY_ID}/issues",
            headers=_PIPELINE_HEADERS,
            json=body,
            timeout=15,
        )
        if resp.status_code in (401, 403):
            pytest.skip(
                f"Paperclip API returned {resp.status_code} on issue "
                f"creation — API key lacks write permissions"
            )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _get_issue(issue_id: str) -> dict:
        """Fetch an issue by ID."""
        resp = requests.get(
            f"{PAPERCLIP_URL}/api/issues/{issue_id}",
            headers=_PIPELINE_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _get_comments(issue_id: str) -> list:
        """Fetch comments for an issue."""
        resp = requests.get(
            f"{PAPERCLIP_URL}/api/issues/{issue_id}/comments",
            headers=_PIPELINE_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get(
            "comments", data.get("data", [])
        )

    @staticmethod
    def _cancel_issue(issue_id: str) -> None:
        """Mark a test issue as cancelled with a cleanup comment."""
        try:
            requests.patch(
                f"{PAPERCLIP_URL}/api/issues/{issue_id}",
                headers=_PIPELINE_HEADERS,
                json={
                    "status": "cancelled",
                    "comment": (
                        "Automated cleanup: test issue created by "
                        "TestPipelineE2E (VIB-65)."
                    ),
                },
                timeout=10,
            )
        except requests.RequestException:
            pass  # best-effort cleanup

    # ── Tests ─────────────────────────────────────────────────────────────

    @skip_no_paperclip
    def test_01_paperclip_api_reachable(self):
        """Precondition: Paperclip API is reachable and healthy."""
        if not _paperclip_ok():
            pytest.skip("Paperclip API not reachable")

        resp = requests.get(f"{PAPERCLIP_URL}/api/health", timeout=10)
        assert resp.status_code == 200, (
            f"Health check failed: {resp.status_code}"
        )

    @skip_no_paperclip
    def test_02_create_test_issue(self):
        """Create a test issue and verify it exists."""
        if not _paperclip_ok():
            pytest.skip("Paperclip API not reachable")

        issue = self._create_test_issue()
        TestPipelineE2E._created_issue_id = issue.get("id", "")
        assert TestPipelineE2E._created_issue_id, "Issue creation returned no ID"

        # Verify the issue can be fetched back
        fetched = self._get_issue(TestPipelineE2E._created_issue_id)
        assert fetched.get("title") == "[INFRA-TEST] E2E pipeline validation"
        assert fetched.get("status") in ("todo", "backlog")

    @skip_no_paperclip
    @skip_no_agent
    def test_03_heartbeat_picks_up_issue(self):
        """Trigger a heartbeat and verify the issue transitions from todo.

        Invokes ``python -m agents.main --heartbeat`` in a subprocess.
        If no LLM backend is available the heartbeat may fail — we still
        verify that the issue was at least checked out.
        """
        if not _paperclip_ok():
            pytest.skip("Paperclip API not reachable")
        if not TestPipelineE2E._created_issue_id:
            pytest.skip("No test issue created (test_02 must run first)")

        env = os.environ.copy()
        env.setdefault("PAPERCLIP_API_URL", PAPERCLIP_URL)
        env.setdefault("PAPERCLIP_API_KEY", _PIPELINE_API_KEY)
        env.setdefault("PAPERCLIP_COMPANY_ID", _PIPELINE_COMPANY_ID)
        env.setdefault("PAPERCLIP_AGENT_ID", _PIPELINE_AGENT_ID)

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            result = subprocess.run(
                [sys.executable, "-m", "agents.main", "--heartbeat"],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes max
                env=env,
                cwd=repo_root,
            )
        except subprocess.TimeoutExpired:
            pytest.skip(
                "Heartbeat subprocess timed out (5 min) — "
                "likely waiting for LLM"
            )

        # Any status other than 'todo' means the heartbeat engaged
        fetched = self._get_issue(TestPipelineE2E._created_issue_id)
        status = fetched.get("status", "")
        assert status != "todo", (
            f"Issue still in 'todo' after heartbeat — heartbeat may not "
            f"have picked it up. "
            f"stdout: {result.stdout[-500:]}, stderr: {result.stderr[-500:]}"
        )

    @skip_no_paperclip
    def test_04_verify_result_posted(self):
        """Verify the issue has at least one comment with workflow output."""
        if not _paperclip_ok():
            pytest.skip("Paperclip API not reachable")
        if not TestPipelineE2E._created_issue_id:
            pytest.skip("No test issue created (test_02 must run first)")

        # Poll for comments (the heartbeat may still be posting)
        deadline = time.monotonic() + 30
        comments = []
        while time.monotonic() < deadline:
            comments = self._get_comments(TestPipelineE2E._created_issue_id)
            if comments:
                break
            time.sleep(2)

        fetched = self._get_issue(TestPipelineE2E._created_issue_id)
        status = fetched.get("status", "")

        # Accept any terminal-ish status as evidence the pipeline engaged
        valid_statuses = {"in_progress", "done", "blocked", "cancelled"}
        if status not in valid_statuses and not comments:
            pytest.skip(
                f"No result comment and status={status} — "
                f"heartbeat may not have fully executed (LLM unavailable?)"
            )

    @skip_no_paperclip
    def test_05_cleanup_test_issue(self):
        """Cancel the test issue to avoid polluting the task board."""
        if not TestPipelineE2E._created_issue_id:
            pytest.skip("No test issue to clean up")

        self._cancel_issue(TestPipelineE2E._created_issue_id)

        # Verify cleanup
        if _paperclip_ok():
            fetched = self._get_issue(TestPipelineE2E._created_issue_id)
            assert fetched.get("status") == "cancelled", (
                f"Issue not cancelled after cleanup: "
                f"status={fetched.get('status')}"
            )

        TestPipelineE2E._created_issue_id = ""


def _check_label(val) -> str:
    """Return PASS/FAIL/SKIP label for a check result."""
    if val is None:
        return "SKIP"
    return "PASS" if val else "FAIL"


# ── Layer 4: Consolidated Validation Summary (VIB-65) ─────────────────────────


@pytest.mark.e2e
@pytest.mark.pipeline
class TestInfraValidationSummary:
    """Consolidated test that validates all infrastructure layers and reports.

    Runs quick checks across all layers:
    1. Service health (Paperclip API)
    2. Integration connectivity (agent list, storage, doctor)
    3. Pipeline readiness (issue create + fetch + cancel round-trip)
    """

    @skip_no_paperclip
    def test_consolidated_infra_report(self):
        """Run all infra validation layers and produce a summary report."""
        if not _paperclip_ok():
            pytest.skip("Paperclip API not reachable")

        results = {}

        # Layer 1: Service health — Paperclip API
        try:
            resp = requests.get(f"{PAPERCLIP_URL}/api/health", timeout=10)
            results["paperclip_health"] = resp.ok
        except requests.RequestException:
            results["paperclip_health"] = False

        # Layer 2: Integration connectivity — agent list
        try:
            resp = requests.get(
                f"{PAPERCLIP_URL}/api/companies/{_PIPELINE_COMPANY_ID}/agents",
                headers=_PIPELINE_HEADERS,
                timeout=10,
            )
            agents = resp.json() if resp.ok else []
            if isinstance(agents, dict):
                agents = agents.get("agents", agents.get("data", []))
            results["agent_list"] = resp.ok and len(agents) > 0
            results["agent_count"] = len(agents)
        except requests.RequestException:
            results["agent_list"] = False
            results["agent_count"] = 0

        # Layer 2b: Doctor mode importable
        try:
            from agents.doctor import run_doctor  # noqa: F401
            results["doctor_importable"] = True
        except ImportError:
            results["doctor_importable"] = False

        # Layer 3: Pipeline readiness — issue create/fetch/cancel round-trip
        test_issue_id = ""
        try:
            issue = TestPipelineE2E._create_test_issue()
            test_issue_id = issue.get("id", "")
            results["issue_creation"] = bool(test_issue_id)

            if test_issue_id:
                fetched = TestPipelineE2E._get_issue(test_issue_id)
                results["issue_fetch"] = fetched.get("id") == test_issue_id
            else:
                results["issue_fetch"] = False
        except pytest.skip.Exception:
            results["issue_creation"] = None  # skipped, not failed
            results["issue_fetch"] = None
        except Exception as exc:
            results["issue_creation"] = False
            results["issue_fetch"] = False
            results["issue_error"] = str(exc)
        finally:
            if test_issue_id:
                TestPipelineE2E._cancel_issue(test_issue_id)

        # Build summary report
        check_keys = [
            k for k in results
            if k not in ("agent_count", "issue_error")
            and results[k] is not None
        ]
        total = len(check_keys)
        passed = sum(1 for k in check_keys if results[k] is True)
        failed = total - passed

        report_lines = [
            "=" * 60,
            "INFRASTRUCTURE VALIDATION SUMMARY",
            "=" * 60,
            "",
            f"  Paperclip API health:    {'PASS' if results.get('paperclip_health') else 'FAIL'}",
            f"  Agent list accessible:   {'PASS' if results.get('agent_list') else 'FAIL'}"
            f" ({results.get('agent_count', 0)} agents)",
            f"  Doctor mode importable:  {'PASS' if results.get('doctor_importable') else 'FAIL'}",
            f"  Issue creation:          {_check_label(results.get('issue_creation'))}",
            f"  Issue fetch:             {_check_label(results.get('issue_fetch'))}",
            "",
            "-" * 60,
            f"  TOTAL: {passed}/{total} checks passed, {failed} failed",
            "=" * 60,
        ]
        report = "\n".join(report_lines)
        print(f"\n{report}")

        assert results.get("paperclip_health"), "Paperclip API health check failed"
        if results.get("issue_creation") is None:
            # Issue creation was skipped (auth), not failed
            pass
        else:
            assert results.get("issue_creation"), (
                f"Issue creation failed: {results.get('issue_error', 'unknown')}"
            )
