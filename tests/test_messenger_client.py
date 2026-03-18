"""
Tests for the Messenger Client system.

Covers MattermostClient and SlackClient — all methods, success and error paths,
caching behavior, filtering, and edge cases.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from agents.messenger_client import MattermostClient, SlackClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mm_client():
    return MattermostClient("https://mattermost.example.com", "bot-token-123")


@pytest.fixture
def mm_client_trailing_slash():
    return MattermostClient("https://mattermost.example.com/", "bot-token-123")


@pytest.fixture
def slack_client():
    return SlackClient("xoxb-slack-bot-token")


def _mock_response(json_data=None, status_code=200):
    """Helper to build a mock requests response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# MattermostClient — Initialization
# ---------------------------------------------------------------------------

class TestMattermostInit:

    def test_url_stored_without_trailing_slash(self, mm_client):
        assert mm_client.url == "https://mattermost.example.com"

    def test_url_trailing_slash_stripped(self, mm_client_trailing_slash):
        assert mm_client_trailing_slash.url == "https://mattermost.example.com"

    def test_api_base_constructed(self, mm_client):
        assert mm_client.api_base == "https://mattermost.example.com/api/v4"

    def test_headers_contain_bearer_token(self, mm_client):
        assert mm_client.headers["Authorization"] == "Bearer bot-token-123"

    def test_headers_contain_content_type(self, mm_client):
        assert mm_client.headers["Content-Type"] == "application/json"

    def test_bot_user_id_initially_none(self, mm_client):
        assert mm_client._bot_user_id is None

    def test_double_trailing_slash_stripped(self):
        client = MattermostClient("https://mm.example.com//", "tok")
        assert not client.url.endswith("/")
        assert client.api_base == "https://mm.example.com/api/v4"


# ---------------------------------------------------------------------------
# MattermostClient — _get_bot_user_id
# ---------------------------------------------------------------------------

class TestMattermostGetBotUserId:

    @patch("agents.messenger_client.requests")
    def test_fetches_and_returns_bot_id(self, mock_requests, mm_client):
        mock_requests.get.return_value = _mock_response({"id": "bot-id-abc"})
        result = mm_client._get_bot_user_id()
        assert result == "bot-id-abc"
        mock_requests.get.assert_called_once_with(
            "https://mattermost.example.com/api/v4/users/me",
            headers=mm_client.headers,
            timeout=10,
        )

    @patch("agents.messenger_client.requests")
    def test_caches_bot_id_after_first_call(self, mock_requests, mm_client):
        mock_requests.get.return_value = _mock_response({"id": "bot-id-abc"})
        mm_client._get_bot_user_id()
        mm_client._get_bot_user_id()
        # Only one HTTP call — second uses cache
        assert mock_requests.get.call_count == 1

    @patch("agents.messenger_client.requests")
    def test_uses_precached_value(self, mock_requests, mm_client):
        mm_client._bot_user_id = "already-cached"
        result = mm_client._get_bot_user_id()
        assert result == "already-cached"
        mock_requests.get.assert_not_called()

    @patch("agents.messenger_client.requests")
    def test_raises_on_http_error(self, mock_requests, mm_client):
        mock_requests.get.side_effect = Exception("connection failed")
        with pytest.raises(Exception, match="connection failed"):
            mm_client._get_bot_user_id()


# ---------------------------------------------------------------------------
# MattermostClient — get_bot_username
# ---------------------------------------------------------------------------

class TestMattermostGetBotUsername:

    @patch("agents.messenger_client.requests")
    def test_returns_username(self, mock_requests, mm_client):
        mock_requests.get.return_value = _mock_response(
            {"id": "bot-id", "username": "vibe-bot"}
        )
        result = mm_client.get_bot_username()
        assert result == "vibe-bot"

    @patch("agents.messenger_client.requests")
    def test_raises_on_failure(self, mock_requests, mm_client):
        mock_requests.get.side_effect = Exception("timeout")
        with pytest.raises(Exception):
            mm_client.get_bot_username()


# ---------------------------------------------------------------------------
# MattermostClient — get_direct_channel_id
# ---------------------------------------------------------------------------

class TestMattermostGetDirectChannelId:

    @patch("agents.messenger_client.requests")
    def test_creates_direct_channel_and_returns_id(self, mock_requests, mm_client):
        mock_requests.get.return_value = _mock_response({"id": "bot-id"})
        mock_requests.post.return_value = _mock_response({"id": "dm-channel-id"})

        result = mm_client.get_direct_channel_id("user-42")
        assert result == "dm-channel-id"
        mock_requests.post.assert_called_once_with(
            "https://mattermost.example.com/api/v4/channels/direct",
            headers=mm_client.headers,
            json=["bot-id", "user-42"],
            timeout=10,
        )

    @patch("agents.messenger_client.requests")
    def test_raises_on_failure(self, mock_requests, mm_client):
        mm_client._bot_user_id = "bot-id"
        mock_requests.post.side_effect = Exception("forbidden")
        with pytest.raises(Exception):
            mm_client.get_direct_channel_id("user-42")


# ---------------------------------------------------------------------------
# MattermostClient — send_direct_message
# ---------------------------------------------------------------------------

class TestMattermostSendDirectMessage:

    @patch("agents.messenger_client.requests")
    def test_sends_dm_and_returns_post_id(self, mock_requests, mm_client):
        mm_client._bot_user_id = "bot-id"
        mock_requests.post.side_effect = [
            _mock_response({"id": "dm-channel-id"}),   # channels/direct
            _mock_response({"id": "post-id-99"}),       # posts
        ]
        result = mm_client.send_direct_message("user-42", "Hello there")
        assert result == "post-id-99"
        # Verify the second POST payload
        second_call = mock_requests.post.call_args_list[1]
        payload = second_call[1]["json"]
        assert payload["channel_id"] == "dm-channel-id"
        assert payload["message"] == "Hello there"

    @patch("agents.messenger_client.requests")
    def test_raises_on_failure(self, mock_requests, mm_client):
        mm_client._bot_user_id = "bot-id"
        mock_requests.post.side_effect = Exception("error")
        with pytest.raises(Exception):
            mm_client.send_direct_message("user-42", "Hello")


# ---------------------------------------------------------------------------
# MattermostClient — get_recent_messages
# ---------------------------------------------------------------------------

class TestMattermostGetRecentMessages:

    @patch("agents.messenger_client.requests")
    def test_returns_posts_sorted_by_create_at(self, mock_requests, mm_client):
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        since_ms = int(since.timestamp() * 1000)
        mock_requests.get.return_value = _mock_response({
            "order": ["p2", "p1"],
            "posts": {
                "p1": {"id": "p1", "create_at": since_ms + 1000, "message": "first"},
                "p2": {"id": "p2", "create_at": since_ms + 2000, "message": "second"},
            },
        })
        result = mm_client.get_recent_messages("ch-1", since=since, limit=10)
        assert len(result) == 2
        assert result[0]["create_at"] <= result[1]["create_at"]

    @patch("agents.messenger_client.requests")
    def test_filters_posts_before_since_timestamp(self, mock_requests, mm_client):
        """Posts with create_at < since_ms are filtered out client-side."""
        since = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        since_ms = int(since.timestamp() * 1000)
        mock_requests.get.return_value = _mock_response({
            "order": ["p2", "p1"],
            "posts": {
                "p1": {"id": "p1", "create_at": since_ms - 5000, "message": "too old"},
                "p2": {"id": "p2", "create_at": since_ms + 1000, "message": "new enough"},
            },
        })
        result = mm_client.get_recent_messages("ch-1", since=since, limit=10)
        assert len(result) == 1
        assert result[0]["id"] == "p2"

    @patch("agents.messenger_client.requests")
    def test_since_converted_to_milliseconds(self, mock_requests, mm_client):
        since = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        expected_ms = int(since.timestamp() * 1000)
        mock_requests.get.return_value = _mock_response({"order": [], "posts": {}})
        mm_client.get_recent_messages("ch-1", since=since, limit=5)
        call_kwargs = mock_requests.get.call_args[1]
        assert call_kwargs["params"]["since"] == expected_ms

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_exception(self, mock_requests, mm_client):
        mock_requests.get.side_effect = Exception("connection failed")
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        result = mm_client.get_recent_messages("ch-1", since=since, limit=10)
        assert result == []

    @patch("agents.messenger_client.requests")
    def test_empty_posts_returns_empty_list(self, mock_requests, mm_client):
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        mock_requests.get.return_value = _mock_response({"order": [], "posts": {}})
        result = mm_client.get_recent_messages("ch-1", since=since, limit=10)
        assert result == []


# ---------------------------------------------------------------------------
# MattermostClient — delete_message
# ---------------------------------------------------------------------------

class TestMattermostDeleteMessage:

    @patch("agents.messenger_client.requests")
    def test_delete_success_returns_true(self, mock_requests, mm_client):
        mock_requests.delete.return_value = _mock_response(status_code=200)
        result = mm_client.delete_message("post-42")
        assert result is True
        mock_requests.delete.assert_called_once_with(
            "https://mattermost.example.com/api/v4/posts/post-42",
            headers=mm_client.headers,
            timeout=10,
        )

    @patch("agents.messenger_client.requests")
    def test_delete_failure_returns_false(self, mock_requests, mm_client):
        mock_requests.delete.side_effect = Exception("not found")
        result = mm_client.delete_message("post-42")
        assert result is False


# ---------------------------------------------------------------------------
# MattermostClient — send_channel_message
# ---------------------------------------------------------------------------

class TestMattermostSendChannelMessage:

    @patch("agents.messenger_client.requests")
    def test_sends_and_returns_post_id(self, mock_requests, mm_client):
        mock_requests.post.return_value = _mock_response({"id": "new-post-id"})
        result = mm_client.send_channel_message("ch-1", "Hello channel")
        assert result == "new-post-id"

    @patch("agents.messenger_client.requests")
    def test_includes_root_id_when_provided(self, mock_requests, mm_client):
        mock_requests.post.return_value = _mock_response({"id": "reply-post"})
        mm_client.send_channel_message("ch-1", "Thread reply", root_id="parent-post")
        payload = mock_requests.post.call_args[1]["json"]
        assert payload["root_id"] == "parent-post"

    @patch("agents.messenger_client.requests")
    def test_omits_root_id_when_none(self, mock_requests, mm_client):
        mock_requests.post.return_value = _mock_response({"id": "new-post"})
        mm_client.send_channel_message("ch-1", "msg")
        payload = mock_requests.post.call_args[1]["json"]
        assert "root_id" not in payload

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_error(self, mock_requests, mm_client):
        mock_requests.post.side_effect = Exception("server error")
        result = mm_client.send_channel_message("ch-1", "msg")
        assert result is None


# ---------------------------------------------------------------------------
# MattermostClient — get_channels_for_user
# ---------------------------------------------------------------------------

class TestMattermostGetChannelsForUser:

    @patch("agents.messenger_client.requests")
    def test_returns_all_channels(self, mock_requests, mm_client):
        mock_requests.get.return_value = _mock_response([
            {"id": "ch-1", "team_id": "team-a"},
            {"id": "ch-2", "team_id": "team-b"},
        ])
        result = mm_client.get_channels_for_user("user-1")
        assert len(result) == 2

    @patch("agents.messenger_client.requests")
    def test_filters_by_team_id(self, mock_requests, mm_client):
        mock_requests.get.return_value = _mock_response([
            {"id": "ch-1", "team_id": "team-a"},
            {"id": "ch-2", "team_id": "team-b"},
            {"id": "ch-3", "team_id": "team-a"},
        ])
        result = mm_client.get_channels_for_user("user-1", team_id="team-a")
        assert len(result) == 2
        assert all(ch["team_id"] == "team-a" for ch in result)

    @patch("agents.messenger_client.requests")
    def test_team_id_none_returns_all(self, mock_requests, mm_client):
        mock_requests.get.return_value = _mock_response([
            {"id": "ch-1", "team_id": "team-a"},
            {"id": "ch-2", "team_id": "team-b"},
        ])
        result = mm_client.get_channels_for_user("user-1", team_id=None)
        assert len(result) == 2

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_error(self, mock_requests, mm_client):
        mock_requests.get.side_effect = Exception("timeout")
        result = mm_client.get_channels_for_user("user-1")
        assert result == []


# ---------------------------------------------------------------------------
# MattermostClient — search_posts
# ---------------------------------------------------------------------------

class TestMattermostSearchPosts:

    @patch("agents.messenger_client.requests")
    def test_returns_posts_sorted_newest_first(self, mock_requests, mm_client):
        mm_client._bot_user_id = "bot-id"
        mock_requests.post.return_value = _mock_response({
            "order": ["p2", "p1"],
            "posts": {
                "p1": {"id": "p1", "create_at": 1000},
                "p2": {"id": "p2", "create_at": 2000},
            },
        })
        result = mm_client.search_posts("search term")
        assert len(result) == 2
        assert result[0]["create_at"] >= result[1]["create_at"]

    @patch("agents.messenger_client.requests")
    def test_uses_bot_id_in_url(self, mock_requests, mm_client):
        mm_client._bot_user_id = "bot-id"
        mock_requests.post.return_value = _mock_response({"order": [], "posts": {}})
        mm_client.search_posts("query")
        url = mock_requests.post.call_args[0][0]
        assert "/users/bot-id/posts/search" in url

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_error(self, mock_requests, mm_client):
        mm_client._bot_user_id = "bot-id"
        mock_requests.post.side_effect = Exception("fail")
        result = mm_client.search_posts("query")
        assert result == []


# ---------------------------------------------------------------------------
# MattermostClient — get_user_by_username
# ---------------------------------------------------------------------------

class TestMattermostGetUserByUsername:

    @patch("agents.messenger_client.requests")
    def test_returns_user_dict(self, mock_requests, mm_client):
        mock_requests.get.return_value = _mock_response(
            {"id": "u1", "username": "alice"}
        )
        result = mm_client.get_user_by_username("alice")
        assert result["username"] == "alice"
        mock_requests.get.assert_called_once_with(
            "https://mattermost.example.com/api/v4/users/username/alice",
            headers=mm_client.headers,
            timeout=10,
        )

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_error(self, mock_requests, mm_client):
        mock_requests.get.side_effect = Exception("not found")
        result = mm_client.get_user_by_username("nonexistent")
        assert result is None


# ===========================================================================
# SlackClient Tests
# ===========================================================================


class TestSlackInit:

    def test_api_base(self, slack_client):
        assert slack_client.api_base == "https://slack.com/api"

    def test_headers_contain_bearer_token(self, slack_client):
        assert slack_client.headers["Authorization"] == "Bearer xoxb-slack-bot-token"

    def test_headers_contain_content_type(self, slack_client):
        assert slack_client.headers["Content-Type"] == "application/json"

    def test_bot_user_id_initially_none(self, slack_client):
        assert slack_client._bot_user_id is None


# ---------------------------------------------------------------------------
# SlackClient — get_bot_user_id
# ---------------------------------------------------------------------------

class TestSlackGetBotUserId:

    @patch("agents.messenger_client.requests")
    def test_returns_user_id_on_success(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": True, "user_id": "U_BOT_123"}
        )
        result = slack_client.get_bot_user_id()
        assert result == "U_BOT_123"

    @patch("agents.messenger_client.requests")
    def test_caches_user_id(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": True, "user_id": "U_BOT_123"}
        )
        slack_client.get_bot_user_id()
        slack_client.get_bot_user_id()
        assert mock_requests.post.call_count == 1

    @patch("agents.messenger_client.requests")
    def test_uses_precached_value(self, mock_requests, slack_client):
        slack_client._bot_user_id = "U_CACHED"
        result = slack_client.get_bot_user_id()
        assert result == "U_CACHED"
        mock_requests.post.assert_not_called()

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_not_ok(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": False, "error": "invalid_auth"}
        )
        result = slack_client.get_bot_user_id()
        assert result is None

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_exception(self, mock_requests, slack_client):
        mock_requests.post.side_effect = Exception("network error")
        result = slack_client.get_bot_user_id()
        assert result is None


# ---------------------------------------------------------------------------
# SlackClient — send_direct_message
# ---------------------------------------------------------------------------

class TestSlackSendDirectMessage:

    @patch("agents.messenger_client.requests")
    def test_returns_ts_channel_tuple(self, mock_requests, slack_client):
        mock_requests.post.side_effect = [
            _mock_response({"ok": True, "channel": {"id": "D_DM_CH"}}),
            _mock_response({"ok": True, "ts": "1234567890.123456", "channel": "D_DM_CH"}),
        ]
        result = slack_client.send_direct_message("U_USER_1", "Hello!")
        assert isinstance(result, tuple)
        assert result == ("1234567890.123456", "D_DM_CH")

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_open_failure(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": False, "error": "user_not_found"}
        )
        result = slack_client.send_direct_message("U_INVALID", "Hello!")
        assert result is None

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_send_failure(self, mock_requests, slack_client):
        mock_requests.post.side_effect = [
            _mock_response({"ok": True, "channel": {"id": "D_DM_CH"}}),
            _mock_response({"ok": False, "error": "channel_not_found"}),
        ]
        result = slack_client.send_direct_message("U_USER_1", "Hello!")
        assert result is None

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_exception(self, mock_requests, slack_client):
        mock_requests.post.side_effect = Exception("network")
        result = slack_client.send_direct_message("U_USER_1", "Hello!")
        assert result is None


# ---------------------------------------------------------------------------
# SlackClient — get_conversation_history
# ---------------------------------------------------------------------------

class TestSlackGetConversationHistory:

    @patch("agents.messenger_client.requests")
    def test_returns_messages(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response({
            "ok": True,
            "messages": [
                {"ts": "100.0", "text": "msg1"},
                {"ts": "200.0", "text": "msg2"},
            ],
        })
        result = slack_client.get_conversation_history("C_CH", oldest=0.0, limit=10)
        assert len(result) == 2

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_not_ok(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response(
            {"ok": False, "error": "channel_not_found"}
        )
        result = slack_client.get_conversation_history("C_CH", oldest=0.0, limit=10)
        assert result == []

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_exception(self, mock_requests, slack_client):
        mock_requests.get.side_effect = Exception("error")
        result = slack_client.get_conversation_history("C_CH", oldest=0.0, limit=10)
        assert result == []


# ---------------------------------------------------------------------------
# SlackClient — get_thread_replies
# ---------------------------------------------------------------------------

class TestSlackGetThreadReplies:

    @patch("agents.messenger_client.requests")
    def test_excludes_parent_message(self, mock_requests, slack_client):
        thread_ts = "1000.000"
        mock_requests.get.return_value = _mock_response({
            "ok": True,
            "messages": [
                {"ts": thread_ts, "text": "parent"},
                {"ts": "1001.000", "text": "reply1"},
                {"ts": "1002.000", "text": "reply2"},
            ],
        })
        result = slack_client.get_thread_replies("C_CH", thread_ts, oldest=0, limit=100)
        assert len(result) == 2
        assert all(m["ts"] != thread_ts for m in result)

    @patch("agents.messenger_client.requests")
    def test_returns_empty_when_only_parent(self, mock_requests, slack_client):
        thread_ts = "1000.000"
        mock_requests.get.return_value = _mock_response({
            "ok": True,
            "messages": [{"ts": thread_ts, "text": "parent only"}],
        })
        result = slack_client.get_thread_replies("C_CH", thread_ts, oldest=0, limit=100)
        assert result == []

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_not_ok(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response(
            {"ok": False, "error": "thread_not_found"}
        )
        result = slack_client.get_thread_replies("C_CH", "1000.000", oldest=0, limit=10)
        assert result == []

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_exception(self, mock_requests, slack_client):
        mock_requests.get.side_effect = Exception("error")
        result = slack_client.get_thread_replies("C_CH", "1000.000", oldest=0, limit=10)
        assert result == []


# ---------------------------------------------------------------------------
# SlackClient — send_channel_message
# ---------------------------------------------------------------------------

class TestSlackSendChannelMessage:

    @patch("agents.messenger_client.requests")
    def test_returns_ts_on_success(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": True, "ts": "9999.000"}
        )
        result = slack_client.send_channel_message("C_CH", "Hello channel")
        assert result == "9999.000"

    @patch("agents.messenger_client.requests")
    def test_includes_thread_ts_when_provided(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": True, "ts": "9999.001"}
        )
        slack_client.send_channel_message("C_CH", "Thread reply", thread_ts="9998.000")
        payload = mock_requests.post.call_args[1]["json"]
        assert payload["thread_ts"] == "9998.000"

    @patch("agents.messenger_client.requests")
    def test_omits_thread_ts_when_none(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": True, "ts": "9999.001"}
        )
        slack_client.send_channel_message("C_CH", "msg")
        payload = mock_requests.post.call_args[1]["json"]
        assert "thread_ts" not in payload

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_not_ok(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": False, "error": "not_in_channel"}
        )
        result = slack_client.send_channel_message("C_CH", "msg")
        assert result is None

    @patch("agents.messenger_client.requests")
    def test_returns_none_on_exception(self, mock_requests, slack_client):
        mock_requests.post.side_effect = Exception("fail")
        result = slack_client.send_channel_message("C_CH", "msg")
        assert result is None


# ---------------------------------------------------------------------------
# SlackClient — get_conversations_list
# ---------------------------------------------------------------------------

class TestSlackGetConversationsList:

    @patch("agents.messenger_client.requests")
    def test_returns_channels(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response({
            "ok": True,
            "channels": [
                {"id": "C1", "name": "general"},
                {"id": "C2", "name": "random"},
            ],
        })
        result = slack_client.get_conversations_list()
        assert len(result) == 2

    @patch("agents.messenger_client.requests")
    def test_passes_types_parameter(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response(
            {"ok": True, "channels": []}
        )
        slack_client.get_conversations_list(types="public_channel")
        params = mock_requests.get.call_args[1]["params"]
        assert params["types"] == "public_channel"

    @patch("agents.messenger_client.requests")
    def test_passes_exclude_archived(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response(
            {"ok": True, "channels": []}
        )
        slack_client.get_conversations_list()
        params = mock_requests.get.call_args[1]["params"]
        assert params["exclude_archived"] is True

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_not_ok(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response(
            {"ok": False, "error": "missing_scope"}
        )
        result = slack_client.get_conversations_list()
        assert result == []

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_exception(self, mock_requests, slack_client):
        mock_requests.get.side_effect = Exception("error")
        result = slack_client.get_conversations_list()
        assert result == []


# ---------------------------------------------------------------------------
# SlackClient — search_messages
# ---------------------------------------------------------------------------

class TestSlackSearchMessages:

    @patch("agents.messenger_client.requests")
    def test_returns_matches(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response({
            "ok": True,
            "messages": {
                "matches": [{"ts": "100", "text": "found it"}],
            },
        })
        result = slack_client.search_messages("search query")
        assert len(result) == 1
        assert result[0]["text"] == "found it"

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_not_ok(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response(
            {"ok": False, "error": "missing_scope"}
        )
        result = slack_client.search_messages("query")
        assert result == []

    @patch("agents.messenger_client.requests")
    def test_returns_empty_on_exception(self, mock_requests, slack_client):
        mock_requests.get.side_effect = Exception("error")
        result = slack_client.search_messages("query")
        assert result == []

    @patch("agents.messenger_client.requests")
    def test_passes_count_and_sort_params(self, mock_requests, slack_client):
        mock_requests.get.return_value = _mock_response(
            {"ok": True, "messages": {"matches": []}}
        )
        slack_client.search_messages("query", count=50)
        params = mock_requests.get.call_args[1]["params"]
        assert params["count"] == 50
        assert params["query"] == "query"
        assert params["sort"] == "timestamp"
        assert params["sort_dir"] == "desc"


# ---------------------------------------------------------------------------
# SlackClient — delete_message
# ---------------------------------------------------------------------------

class TestSlackDeleteMessage:

    @patch("agents.messenger_client.requests")
    def test_returns_true_on_ok(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response({"ok": True})
        result = slack_client.delete_message("C_CH", "1234567890.123456")
        assert result is True

    @patch("agents.messenger_client.requests")
    def test_sends_correct_payload(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response({"ok": True})
        slack_client.delete_message("C_CH", "1234567890.123456")
        payload = mock_requests.post.call_args[1]["json"]
        assert payload["channel"] == "C_CH"
        assert payload["ts"] == "1234567890.123456"

    @patch("agents.messenger_client.requests")
    def test_returns_false_on_not_ok(self, mock_requests, slack_client):
        mock_requests.post.return_value = _mock_response(
            {"ok": False, "error": "cant_delete_message"}
        )
        result = slack_client.delete_message("C_CH", "1234567890.123456")
        assert result is False

    @patch("agents.messenger_client.requests")
    def test_returns_false_on_exception(self, mock_requests, slack_client):
        mock_requests.post.side_effect = Exception("network failure")
        result = slack_client.delete_message("C_CH", "1234567890.123456")
        assert result is False
