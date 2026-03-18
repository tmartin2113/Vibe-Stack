"""
Paperclip REST API Client

HTTP client for the Paperclip control plane. Reads connection details
from PAPERCLIP_* environment variables injected by the Paperclip adapter
at heartbeat invocation time.

Used by heartbeat.py to:
- Fetch assigned tasks
- Checkout/release issues
- Post results as comments
- Update issue status
- Report cost events
"""

import json
import logging
import os
import time
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import requests

logger = logging.getLogger(__name__)

# HTTP status codes that are safe to retry
RETRYABLE_STATUS_CODES: Set[int] = {429, 500, 502, 503, 504}

# Retry defaults
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 15.0


# ── Exceptions ──


class PaperclipAPIError(Exception):
    """Base exception for Paperclip API errors."""

    def __init__(self, status_code: int, message: str, response_body: Optional[str] = None):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"Paperclip API error {status_code}: {message}")


class PaperclipConflictError(PaperclipAPIError):
    """409 Conflict — e.g., task already checked out by another agent."""

    def __init__(self, message: str, response_body: Optional[str] = None):
        super().__init__(409, message, response_body)


class PaperclipAuthError(PaperclipAPIError):
    """401/403 — authentication or authorization failure."""

    def __init__(self, status_code: int, message: str, response_body: Optional[str] = None):
        super().__init__(status_code, message, response_body)


class PaperclipNotFoundError(PaperclipAPIError):
    """404 — resource not found."""

    def __init__(self, message: str, response_body: Optional[str] = None):
        super().__init__(404, message, response_body)


# ── Data Classes ──


@dataclass(frozen=True)
class AgentInfo:
    """Agent identity and metadata from Paperclip."""
    id: str
    company_id: str
    name: str
    role: str
    title: str = ""
    status: str = "active"
    reports_to: Optional[str] = None
    budget_monthly_cents: int = 0
    spent_monthly_cents: int = 0
    chain_of_command: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Issue:
    """A Paperclip issue/task."""
    id: str
    title: str
    description: str = ""
    status: str = "backlog"
    priority: str = "medium"
    assignee_agent_id: Optional[str] = None
    parent_id: Optional[str] = None
    project_id: Optional[str] = None
    goal_id: Optional[str] = None
    identifier: str = ""
    ancestors: List[Dict[str, Any]] = field(default_factory=list)
    comments_count: int = 0


@dataclass(frozen=True)
class Comment:
    """An issue comment."""
    id: str
    body: str
    author_agent_id: Optional[str] = None
    author_user_id: Optional[str] = None
    created_at: str = ""


@dataclass(frozen=True)
class CheckoutResult:
    """Result of attempting to checkout an issue."""
    success: bool
    conflict_owner: Optional[str] = None
    issue: Optional[Issue] = None


@dataclass(frozen=True)
class DashboardSummary:
    """Company dashboard summary."""
    agent_counts: Dict[str, int] = field(default_factory=dict)
    issue_counts: Dict[str, int] = field(default_factory=dict)
    spend: int = 0
    budget_utilization: float = 0.0


# ── Client ──


class PaperclipClient:
    """
    HTTP client for the Paperclip control plane REST API.

    Reads connection details from environment variables:
    - PAPERCLIP_API_URL: Base URL (e.g., http://localhost:3100)
    - PAPERCLIP_API_KEY: Bearer token for authentication
    - PAPERCLIP_AGENT_ID: This agent's UUID
    - PAPERCLIP_COMPANY_ID: Company UUID
    - PAPERCLIP_RUN_ID: Current heartbeat run UUID
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        agent_id: Optional[str] = None,
        company_id: Optional[str] = None,
        run_id: Optional[str] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY,
        timeout: float = 30.0,
    ):
        self.api_url = (api_url or os.environ.get("PAPERCLIP_API_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("PAPERCLIP_API_KEY", "")
        self.agent_id = agent_id or os.environ.get("PAPERCLIP_AGENT_ID", "")
        self.company_id = company_id or os.environ.get("PAPERCLIP_COMPANY_ID", "")
        self.run_id = run_id or os.environ.get("PAPERCLIP_RUN_ID", "")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.timeout = timeout

        if not self.api_url:
            raise ValueError("PAPERCLIP_API_URL not set")
        if not self.api_key:
            raise ValueError("PAPERCLIP_API_KEY not set")

    def _headers(self) -> Dict[str, str]:
        """Build request headers with auth and run tracing."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.run_id:
            headers["X-Paperclip-Run-Id"] = self.run_id
        return headers

    def _request(
        self,
        method: str,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Make an HTTP request with retry on transient failures.

        Returns parsed JSON response body.
        Raises PaperclipAPIError subclasses for non-retryable errors.
        """
        url = f"{self.api_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    json=json_body,
                    params=params,
                    timeout=self.timeout,
                )

                # Non-retryable client errors
                if response.status_code == 409:
                    raise PaperclipConflictError(
                        response.text, response.text
                    )
                if response.status_code in (401, 403):
                    raise PaperclipAuthError(
                        response.status_code, response.text, response.text
                    )
                if response.status_code == 404:
                    raise PaperclipNotFoundError(
                        response.text, response.text
                    )
                if response.status_code == 400:
                    raise PaperclipAPIError(
                        400, response.text, response.text
                    )

                # Retryable server errors
                if response.status_code in RETRYABLE_STATUS_CODES:
                    retry_after = _extract_retry_after_header(response)
                    raise _RetryableHTTPError(response.status_code, retry_after)

                # Success
                response.raise_for_status()

                if not response.content:
                    return {}
                return response.json()

            except PaperclipAPIError:
                raise
            except _RetryableHTTPError as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                delay = _compute_delay(attempt, self.base_delay, DEFAULT_MAX_DELAY, e.retry_after)
                logger.warning(
                    "Paperclip API %s %s failed (HTTP %d, attempt %d/%d). Retrying in %.1fs...",
                    method, path, e.status_code, attempt + 1, self.max_retries + 1, delay,
                )
                time.sleep(delay)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                delay = _compute_delay(attempt, self.base_delay, DEFAULT_MAX_DELAY, None)
                logger.warning(
                    "Paperclip API %s %s connection error (attempt %d/%d): %s. Retrying in %.1fs...",
                    method, path, attempt + 1, self.max_retries + 1, e, delay,
                )
                time.sleep(delay)

        raise PaperclipAPIError(
            0, f"Request failed after {self.max_retries + 1} attempts: {last_error}"
        )

    # ── Identity ──

    def get_identity(self) -> AgentInfo:
        """GET /api/agents/me — fetch this agent's identity and metadata."""
        data = self._request("GET", "/api/agents/me")
        return _parse_agent_info(data)

    # ── Assignments ──

    def get_assignments(
        self,
        statuses: Optional[List[str]] = None,
    ) -> List[Issue]:
        """GET /api/companies/{companyId}/issues — fetch assigned tasks."""
        if statuses is None:
            statuses = ["todo", "in_progress", "blocked"]

        params: Dict[str, str] = {
            "assigneeAgentId": self.agent_id,
            "status": ",".join(statuses),
        }
        data = self._request(
            "GET",
            f"/api/companies/{self.company_id}/issues",
            params=params,
        )
        issues_list = data if isinstance(data, list) else data.get("issues", data.get("data", []))
        return [_parse_issue(item) for item in issues_list]

    # ── Issue Operations ──

    def checkout_issue(
        self,
        issue_id: str,
        expected_statuses: Optional[List[str]] = None,
    ) -> CheckoutResult:
        """POST /api/issues/{issueId}/checkout — atomic task checkout."""
        if expected_statuses is None:
            expected_statuses = ["todo", "backlog", "blocked"]

        try:
            data = self._request(
                "POST",
                f"/api/issues/{issue_id}/checkout",
                json_body={
                    "agentId": self.agent_id,
                    "expectedStatuses": expected_statuses,
                },
            )
            return CheckoutResult(success=True, issue=_parse_issue(data) if data else None)
        except PaperclipConflictError:
            return CheckoutResult(success=False, conflict_owner=issue_id)

    def get_issue(self, issue_id: str) -> Issue:
        """GET /api/issues/{issueId} — fetch issue with ancestors and project."""
        data = self._request("GET", f"/api/issues/{issue_id}")
        return _parse_issue(data)

    def get_comments(self, issue_id: str) -> List[Comment]:
        """GET /api/issues/{issueId}/comments — fetch issue comments."""
        data = self._request("GET", f"/api/issues/{issue_id}/comments")
        comments_list = data if isinstance(data, list) else data.get("comments", data.get("data", []))
        return [_parse_comment(item) for item in comments_list]

    def update_issue(
        self,
        issue_id: str,
        status: Optional[str] = None,
        comment: Optional[str] = None,
        **fields: Any,
    ) -> Issue:
        """PATCH /api/issues/{issueId} — update issue status/fields with optional comment."""
        body: Dict[str, Any] = {}
        if status is not None:
            body["status"] = status
        if comment is not None:
            body["comment"] = comment
        body.update(fields)
        data = self._request("PATCH", f"/api/issues/{issue_id}", json_body=body)
        return _parse_issue(data)

    def add_comment(self, issue_id: str, body: str) -> Comment:
        """POST /api/issues/{issueId}/comments — add a comment."""
        data = self._request(
            "POST",
            f"/api/issues/{issue_id}/comments",
            json_body={"body": body},
        )
        return _parse_comment(data)

    def release_issue(self, issue_id: str) -> None:
        """POST /api/issues/{issueId}/release — release checkout."""
        self._request("POST", f"/api/issues/{issue_id}/release")

    def create_subtask(
        self,
        title: str,
        description: str,
        parent_id: str,
        goal_id: Optional[str] = None,
        assignee_agent_id: Optional[str] = None,
        priority: str = "medium",
    ) -> Issue:
        """POST /api/companies/{companyId}/issues — create a subtask."""
        body: Dict[str, Any] = {
            "title": title,
            "description": description,
            "parentId": parent_id,
            "priority": priority,
        }
        if goal_id:
            body["goalId"] = goal_id
        if assignee_agent_id:
            body["assigneeAgentId"] = assignee_agent_id
        data = self._request(
            "POST",
            f"/api/companies/{self.company_id}/issues",
            json_body=body,
        )
        return _parse_issue(data)

    def create_issue(
        self,
        title: str,
        description: str = "",
        priority: str = "medium",
        labels: Optional[List[str]] = None,
    ) -> Issue:
        """POST /api/companies/{companyId}/issues — create a top-level issue."""
        body: Dict[str, Any] = {
            "title": title,
            "description": description,
            "priority": priority,
        }
        if labels:
            body["labels"] = labels
        data = self._request(
            "POST",
            f"/api/companies/{self.company_id}/issues",
            json_body=body,
        )
        return _parse_issue(data)

    # ── Cost Reporting ──

    def report_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_cents: int,
        issue_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> None:
        """POST /api/companies/{companyId}/cost-events — report token usage."""
        from datetime import datetime, timezone

        body: Dict[str, Any] = {
            "agentId": self.agent_id,
            "provider": provider,
            "model": model,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "costCents": cost_cents,
            "occurredAt": occurred_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }
        if issue_id:
            body["issueId"] = issue_id
        self._request(
            "POST",
            f"/api/companies/{self.company_id}/cost-events",
            json_body=body,
        )

    # ── Dashboard & Agents ──

    def get_dashboard(self) -> DashboardSummary:
        """GET /api/companies/{companyId}/dashboard — fetch dashboard summary."""
        data = self._request("GET", f"/api/companies/{self.company_id}/dashboard")
        return DashboardSummary(
            agent_counts=data.get("agentCounts", data.get("agents", {})),
            issue_counts=data.get("issueCounts", data.get("issues", {})),
            spend=data.get("spend", data.get("monthToDateSpendCents", 0)),
            budget_utilization=data.get("budgetUtilization", 0.0),
        )

    def list_agents(self) -> List[AgentInfo]:
        """GET /api/companies/{companyId}/agents — list all agents."""
        data = self._request("GET", f"/api/companies/{self.company_id}/agents")
        agents_list = data if isinstance(data, list) else data.get("agents", data.get("data", []))
        return [_parse_agent_info(item) for item in agents_list]

    def get_children(self, parent_id: str, statuses: Optional[List[str]] = None) -> List[Issue]:
        """GET /api/companies/{companyId}/issues?parentId={parentId} — fetch child issues."""
        params: Dict[str, str] = {"parentId": parent_id}
        if statuses:
            params["status"] = ",".join(statuses)
        data = self._request(
            "GET",
            f"/api/companies/{self.company_id}/issues",
            params=params,
        )
        issues_list = data if isinstance(data, list) else data.get("issues", data.get("data", []))
        return [_parse_issue(item) for item in issues_list]


# ── Internal Helpers ──


class _RetryableHTTPError(Exception):
    """Internal: marks an HTTP error as retryable."""

    def __init__(self, status_code: int, retry_after: Optional[float] = None):
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code}")


def _extract_retry_after_header(response: requests.Response) -> Optional[float]:
    """Extract Retry-After header value from an HTTP response."""
    retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if retry_after is None:
        return None
    try:
        return float(retry_after)
    except (ValueError, TypeError):
        return None


def _compute_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    retry_after: Optional[float],
) -> float:
    """Exponential backoff with jitter, respecting Retry-After."""
    exp_delay = base_delay * (2 ** attempt)
    jittered = random.uniform(0, exp_delay)
    delay = min(jittered, max_delay)
    if retry_after is not None:
        delay = max(delay, min(retry_after, max_delay))
    return delay


def _parse_agent_info(data: Dict[str, Any]) -> AgentInfo:
    """Parse agent JSON into AgentInfo dataclass."""
    return AgentInfo(
        id=data.get("id", ""),
        company_id=data.get("companyId", ""),
        name=data.get("name", ""),
        role=data.get("role", ""),
        title=data.get("title", ""),
        status=data.get("status", "active"),
        reports_to=data.get("reportsTo"),
        budget_monthly_cents=data.get("budgetMonthlyCents", 0),
        spent_monthly_cents=data.get("spentMonthlyCents", 0),
        chain_of_command=data.get("chainOfCommand", []),
    )


def _parse_issue(data: Dict[str, Any]) -> Issue:
    """Parse issue JSON into Issue dataclass."""
    return Issue(
        id=data.get("id", ""),
        title=data.get("title", ""),
        description=data.get("description", ""),
        status=data.get("status", "backlog"),
        priority=data.get("priority", "medium"),
        assignee_agent_id=data.get("assigneeAgentId"),
        parent_id=data.get("parentId"),
        project_id=data.get("projectId"),
        goal_id=data.get("goalId"),
        identifier=data.get("identifier", ""),
        ancestors=data.get("ancestors", []),
        comments_count=data.get("commentsCount", 0),
    )


def _parse_comment(data: Dict[str, Any]) -> Comment:
    """Parse comment JSON into Comment dataclass."""
    return Comment(
        id=data.get("id", ""),
        body=data.get("body", ""),
        author_agent_id=data.get("authorAgentId"),
        author_user_id=data.get("authorUserId"),
        created_at=data.get("createdAt", ""),
    )
