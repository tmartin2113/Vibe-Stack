"""Tests for create_issue with assignee_user_id parameter."""
from unittest.mock import patch

from agents.paperclip_client import PaperclipClient


def _make_client():
    """Build a PaperclipClient with valid (mocked) connection params."""
    return PaperclipClient(
        api_url="http://test/api",
        api_key="test-key",
        agent_id="agent_1",
        company_id="co_1",
    )


def test_create_issue_passes_assignee_user_id_to_server():
    client = _make_client()

    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = {
            "id": "iss_1", "identifier": "TST-1",
            "title": "t", "description": "", "status": "todo",
            "priority": "medium", "labels": [],
        }
        client.create_issue(
            title="Test",
            description="body",
            labels=["self-upgrade"],
            assignee_user_id="user_abc",
        )

        # Inspect the body passed to _request via json_body kwarg
        call_kwargs = mock_request.call_args.kwargs
        body = call_kwargs.get("json_body", {})
        assert body.get("assigneeUserId") == "user_abc"
        assert body.get("labels") == ["self-upgrade"]


def test_create_issue_omits_assignee_user_id_when_none():
    client = _make_client()

    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = {
            "id": "iss_1", "identifier": "TST-1",
            "title": "t", "description": "", "status": "todo",
            "priority": "medium", "labels": [],
        }
        client.create_issue(title="Test")

        call_kwargs = mock_request.call_args.kwargs
        body = call_kwargs.get("json_body", {})
        assert "assigneeUserId" not in body


def test_create_issue_omits_assignee_user_id_when_empty_string():
    """Empty string should also be treated as 'no assignee'."""
    client = _make_client()

    with patch.object(client, "_request") as mock_request:
        mock_request.return_value = {
            "id": "iss_1", "identifier": "TST-1",
            "title": "t", "description": "", "status": "todo",
            "priority": "medium", "labels": [],
        }
        client.create_issue(title="Test", assignee_user_id="")

        call_kwargs = mock_request.call_args.kwargs
        body = call_kwargs.get("json_body", {})
        assert "assigneeUserId" not in body
