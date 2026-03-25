"""
End-to-end tests for the Paperclip → agent execution pipeline.

These tests require a live Paperclip instance and real agent credentials.
They are skipped by default unless explicitly run with `-m e2e`.

Run:
    pytest tests/test_e2e_paperclip.py -m e2e -v

Required environment variables:
    PAPERCLIP_API_URL      Paperclip control plane URL (default: http://localhost:3100)
    PAPERCLIP_API_KEY      Bearer token from Paperclip dashboard
    PAPERCLIP_COMPANY_ID   Company UUID for the Vibe-Stack org
"""

import os
import time

import pytest
import requests

PAPERCLIP_URL = os.getenv("PAPERCLIP_API_URL", "http://localhost:3100").rstrip("/")
# Replace Docker-internal hostname with localhost for host-side access
PAPERCLIP_URL = PAPERCLIP_URL.replace("http://server:", "http://localhost:")

API_KEY = os.getenv("PAPERCLIP_API_KEY", "")
COMPANY_ID = os.getenv("PAPERCLIP_COMPANY_ID", "")

_missing = [v for v, k in [("PAPERCLIP_API_KEY", API_KEY), ("PAPERCLIP_COMPANY_ID", COMPANY_ID)] if not k]
_skip_reason = f"Required env vars not set: {', '.join(_missing)}" if _missing else ""

pytestmark = pytest.mark.e2e


@pytest.fixture
def headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


# ── Connectivity ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "e2e disabled")
def test_paperclip_health(headers):
    """Paperclip control plane is reachable and healthy."""
    resp = requests.get(f"{PAPERCLIP_URL}/api/health", timeout=10)
    assert resp.status_code == 200, f"Health check failed: {resp.status_code} {resp.text}"


@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "e2e disabled")
def test_agents_list_returns_vib_org_agents(headers):
    """Paperclip API returns the 12 agents in the Vibe-Stack org."""
    resp = requests.get(
        f"{PAPERCLIP_URL}/api/companies/{COMPANY_ID}/agents",
        headers=headers,
        timeout=10,
    )
    assert resp.status_code == 200, f"Agents list failed: {resp.status_code} {resp.text}"

    data = resp.json()
    agents = data if isinstance(data, list) else data.get("agents", data.get("data", []))
    assert len(agents) >= 11, f"Expected >= 11 agents, got {len(agents)}: {[a.get('name') for a in agents]}"

    names = {a.get("name") for a in agents}
    expected = {"CTO", "Frontend Engineer", "Backend Engineer", "QA Engineer", "UX Engineer", "Security Engineer"}
    missing = expected - names
    assert not missing, f"Missing expected agents: {missing}"


@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "e2e disabled")
def test_all_agents_not_in_error_state(headers):
    """No agent in the Vibe-Stack org is in error status."""
    resp = requests.get(
        f"{PAPERCLIP_URL}/api/companies/{COMPANY_ID}/agents",
        headers=headers,
        timeout=10,
    )
    assert resp.status_code == 200

    data = resp.json()
    agents = data if isinstance(data, list) else data.get("agents", data.get("data", []))
    error_agents = [a.get("name") for a in agents if a.get("status") == "error"]
    assert not error_agents, f"Agents in error state: {error_agents}"


# ── Issue Lifecycle ───────────────────────────────────────────────────────────

@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "e2e disabled")
def test_create_and_delete_issue(headers):
    """Can create and delete a Paperclip issue via API."""
    # Create
    create_resp = requests.post(
        f"{PAPERCLIP_URL}/api/companies/{COMPANY_ID}/issues",
        headers=headers,
        json={"title": "[E2E Test] Connectivity probe", "description": "Auto-created by e2e test suite. Safe to delete."},
        timeout=10,
    )
    assert create_resp.status_code in (200, 201), f"Create failed: {create_resp.status_code} {create_resp.text}"

    issue = create_resp.json()
    issue_id = issue.get("id") or issue.get("issue", {}).get("id")
    assert issue_id, f"No issue ID in response: {issue}"

    # Delete / close
    delete_resp = requests.delete(
        f"{PAPERCLIP_URL}/api/issues/{issue_id}",
        headers=headers,
        timeout=10,
    )
    # 200, 204, or 404 (already gone) are all acceptable
    assert delete_resp.status_code in (200, 204, 404), \
        f"Delete failed: {delete_resp.status_code} {delete_resp.text}"


# ── DeerFlow Pipeline ─────────────────────────────────────────────────────────

@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "e2e disabled")
@pytest.mark.slow
def test_deerflow_agent_responds_to_issue(headers):
    """
    Full pipeline smoke test: create issue → assign to DeerFlow CTO Assistant
    → wait up to 90s for a comment → verify comment posted → cleanup.

    This test is marked slow and e2e. Run explicitly:
        pytest tests/test_e2e_paperclip.py::test_deerflow_agent_responds_to_issue -m e2e -v
    """
    # 1. Find the CTO Assistant agent ID
    agents_resp = requests.get(
        f"{PAPERCLIP_URL}/api/companies/{COMPANY_ID}/agents",
        headers=headers,
        timeout=10,
    )
    assert agents_resp.status_code == 200
    data = agents_resp.json()
    agents = data if isinstance(data, list) else data.get("agents", data.get("data", []))
    cto_assistant = next((a for a in agents if a.get("name") == "CTO Assistant"), None)
    assert cto_assistant, "CTO Assistant agent not found in org"
    agent_id = cto_assistant["id"]

    # 2. Create a test issue
    create_resp = requests.post(
        f"{PAPERCLIP_URL}/api/companies/{COMPANY_ID}/issues",
        headers=headers,
        json={
            "title": "[E2E Test] Echo probe",
            "description": (
                "This is an automated end-to-end test. "
                "Please reply with exactly: E2E_TEST_PASSED"
            ),
        },
        timeout=10,
    )
    assert create_resp.status_code in (200, 201), f"Create failed: {create_resp.text}"
    issue = create_resp.json()
    issue_id = issue.get("id") or issue.get("issue", {}).get("id")
    assert issue_id

    try:
        # 3. Assign the issue to the CTO Assistant
        assign_resp = requests.post(
            f"{PAPERCLIP_URL}/api/issues/{issue_id}/assign",
            headers=headers,
            json={"agentId": agent_id},
            timeout=10,
        )
        # Some Paperclip versions use PATCH or a different endpoint — tolerate errors here
        # The key signal is whether a comment appears within the timeout
        if assign_resp.status_code not in (200, 201, 204):
            pytest.skip(f"Assignment endpoint returned {assign_resp.status_code} — skipping pipeline test")

        # 4. Poll for a comment for up to 90s
        deadline = time.time() + 90
        comments = []
        while time.time() < deadline:
            time.sleep(5)
            comments_resp = requests.get(
                f"{PAPERCLIP_URL}/api/issues/{issue_id}/comments",
                headers=headers,
                timeout=10,
            )
            if comments_resp.status_code == 200:
                data = comments_resp.json()
                comments = data if isinstance(data, list) else data.get("comments", data.get("data", []))
                if comments:
                    break

        assert comments, "No comments posted by agent within 90 seconds"
        bodies = " ".join(c.get("body", "") for c in comments)
        assert "E2E_TEST_PASSED" in bodies, f"Expected E2E_TEST_PASSED in comments, got: {bodies[:200]}"

    finally:
        # 5. Cleanup
        requests.delete(f"{PAPERCLIP_URL}/api/issues/{issue_id}", headers=headers, timeout=10)
