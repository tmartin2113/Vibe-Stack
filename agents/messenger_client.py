"""
Messenger Client for Mattermost and Slack Integration

Handles secure communication with messaging platforms for API key prompting
and other interactive features.
"""

import requests
import time
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class MattermostClient:
    """
    Mattermost REST API client for bot interactions.

    Handles:
    - Direct message posting
    - Message deletion (for security)
    - User lookup
    - Channel management
    """

    def __init__(self, url: str, bot_token: str):
        """
        Initialize Mattermost client.

        Args:
            url: Mattermost server URL (e.g., https://mattermost.example.com)
            bot_token: Bot access token
        """
        self.url = url.rstrip('/')
        self.bot_token = bot_token
        self.api_base = f"{self.url}/api/v4"
        self.headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json"
        }
        self._bot_user_id = None

    def _get_bot_user_id(self) -> str:
        """Get the bot's user ID"""
        if self._bot_user_id:
            return self._bot_user_id

        try:
            response = requests.get(
                f"{self.api_base}/users/me",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            self._bot_user_id = response.json()["id"]
            return self._bot_user_id  # type: ignore[return-value]
        except Exception as e:
            logger.error(f"Failed to get bot user ID: {e}")
            raise

    def get_bot_username(self) -> str:
        """
        Get the bot's username.

        Returns:
            Bot username (e.g., "vibe-bot")

        Raises:
            Exception if API call fails
        """
        try:
            response = requests.get(
                f"{self.api_base}/users/me",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            username = response.json()["username"]
            logger.info(f"Mattermost bot username: @{username}")
            return username  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Failed to get bot username: {e}")
            raise

    def get_direct_channel_id(self, user_id: str) -> str:
        """
        Get or create a direct message channel with a user.

        Args:
            user_id: Target user's ID

        Returns:
            Channel ID for direct messages
        """
        try:
            bot_id = self._get_bot_user_id()
            response = requests.post(
                f"{self.api_base}/channels/direct",
                headers=self.headers,
                json=[bot_id, user_id],
                timeout=10
            )
            response.raise_for_status()
            return response.json()["id"]  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Failed to get/create direct channel: {e}")
            raise

    def send_direct_message(self, user_id: str, message: str) -> str:
        """
        Send a direct message to a user.

        Args:
            user_id: Target user's ID
            message: Message text

        Returns:
            Message ID (post ID)
        """
        try:
            channel_id = self.get_direct_channel_id(user_id)
            response = requests.post(
                f"{self.api_base}/posts",
                headers=self.headers,
                json={
                    "channel_id": channel_id,
                    "message": message
                },
                timeout=10
            )
            response.raise_for_status()
            post_id = response.json()["id"]
            logger.info(f"Sent direct message to user {user_id}")
            return post_id  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Failed to send direct message: {e}")
            raise

    def get_recent_messages(self, channel_id: str, since: datetime, limit: int = 60) -> list:
        """
        Get recent messages from a channel since a specific time.

        Args:
            channel_id: Channel ID
            since: Only get messages after this time
            limit: Maximum number of messages to retrieve

        Returns:
            List of messages
        """
        try:
            # Convert datetime to Unix timestamp in milliseconds
            since_ms = int(since.timestamp() * 1000)

            response = requests.get(
                f"{self.api_base}/channels/{channel_id}/posts",
                headers=self.headers,
                params={"since": since_ms, "per_page": limit},
                timeout=10
            )
            response.raise_for_status()

            data = response.json()
            posts = []

            # Mattermost returns posts in a dict structure
            if "posts" in data:
                for post_id, post in data["posts"].items():
                    # Filter by timestamp (double-check server-side filtering)
                    if post.get("create_at", 0) >= since_ms:
                        posts.append(post)

            # Sort by creation time
            posts.sort(key=lambda p: p.get("create_at", 0))
            return posts

        except Exception as e:
            logger.error(f"Failed to get recent messages: {e}")
            return []

    def delete_message(self, post_id: str) -> bool:
        """
        Delete a message (for security - remove API key from chat).

        Args:
            post_id: Message/post ID to delete

        Returns:
            True if successful
        """
        try:
            response = requests.delete(
                f"{self.api_base}/posts/{post_id}",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"Deleted message {post_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    def send_channel_message(self, channel_id: str, message: str, root_id: Optional[str] = None) -> Optional[str]:
        """
        Send a message to a channel, optionally as a threaded reply.

        Args:
            channel_id: Channel ID
            message: Message text
            root_id: If set, post as a reply in this thread

        Returns:
            Message ID (post ID) or None if failed
        """
        try:
            payload = {
                "channel_id": channel_id,
                "message": message,
            }
            if root_id:
                payload["root_id"] = root_id

            response = requests.post(
                f"{self.api_base}/posts",
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            post_id = response.json()["id"]
            logger.info(f"Sent message to channel {channel_id}")
            return post_id  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Failed to send channel message: {e}")
            return None

    def get_channels_for_user(self, user_id: str, team_id: Optional[str] = None) -> list:
        """
        Get all channels the bot is a member of.

        Args:
            user_id: User ID (typically bot's user ID)
            team_id: Optional team ID to filter channels

        Returns:
            List of channel dictionaries
        """
        try:
            endpoint = f"{self.api_base}/users/{user_id}/channels"
            response = requests.get(
                endpoint,
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            channels = response.json()

            # Filter by team if specified
            if team_id:
                channels = [c for c in channels if c.get("team_id") == team_id]

            return channels  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Failed to get channels: {e}")
            return []

    def search_posts(self, terms: str, is_or_search: bool = False) -> list:
        """
        Search for posts matching terms (useful for finding @mentions).

        Args:
            terms: Search terms
            is_or_search: If True, matches any term; if False, matches all terms

        Returns:
            List of matching posts
        """
        try:
            bot_user_id = self._get_bot_user_id()
            response = requests.post(
                f"{self.api_base}/users/{bot_user_id}/posts/search",
                headers=self.headers,
                json={
                    "terms": terms,
                    "is_or_search": is_or_search
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            # Extract posts from response
            posts = []
            if "posts" in data:
                for post_id, post in data["posts"].items():
                    posts.append(post)

            # Sort by creation time (newest first)
            posts.sort(key=lambda p: p.get("create_at", 0), reverse=True)
            return posts

        except Exception as e:
            logger.error(f"Failed to search posts: {e}")
            return []

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """
        Get user information by username.

        Args:
            username: Mattermost username

        Returns:
            User data dict or None
        """
        try:
            response = requests.get(
                f"{self.api_base}/users/username/{username}",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
        except Exception as e:
            logger.error(f"Failed to get user by username: {e}")
            return None


class SlackClient:
    """
    Slack Web API client for bot interactions.

    Handles:
    - Direct message posting
    - Message deletion (for security)
    - User lookup
    """

    def __init__(self, bot_token: str):
        """
        Initialize Slack client.

        Args:
            bot_token: Slack bot token (starts with xoxb-)
        """
        self.bot_token = bot_token
        self.api_base = "https://slack.com/api"
        self.headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json"
        }
        self._bot_user_id = None

    def get_bot_user_id(self) -> Optional[str]:
        """
        Get the bot's Slack user ID.

        Returns:
            Bot user ID (e.g., "U01AB2C3D4E") or None if failed
        """
        if self._bot_user_id:
            return self._bot_user_id

        try:
            response = requests.post(
                f"{self.api_base}/auth.test",
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                self._bot_user_id = data.get("user_id")
                logger.info(f"Slack bot user ID: {self._bot_user_id}")
                return self._bot_user_id
            else:
                logger.error(f"Failed to get bot user ID: {data.get('error')}")
                return None

        except Exception as e:
            logger.error(f"Failed to call auth.test: {e}")
            return None

    def send_direct_message(self, user_id: str, message: str) -> Optional[tuple]:
        """
        Send a direct message to a user.

        Args:
            user_id: Slack user ID
            message: Message text

        Returns:
            Tuple of (timestamp, channel_id) or None if failed
        """
        try:
            # Open/get DM channel
            response = requests.post(
                f"{self.api_base}/conversations.open",
                headers=self.headers,
                json={"users": user_id},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                logger.error(f"Slack API error: {data.get('error')}")
                return None

            channel_id = data["channel"]["id"]

            # Send message
            response = requests.post(
                f"{self.api_base}/chat.postMessage",
                headers=self.headers,
                json={
                    "channel": channel_id,
                    "text": message
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                logger.info(f"Sent Slack DM to user {user_id}")
                return (data.get("ts"), channel_id)  # Return both timestamp and channel_id
            else:
                logger.error(f"Failed to send Slack message: {data.get('error')}")
                return None

        except Exception as e:
            logger.error(f"Failed to send Slack direct message: {e}")
            return None

    def get_conversation_history(self, channel_id: str, oldest: float, limit: int = 100) -> list:
        """
        Get conversation history since a timestamp.

        Args:
            channel_id: Channel/DM ID
            oldest: Unix timestamp (oldest message to include)
            limit: Max messages to retrieve

        Returns:
            List of messages
        """
        try:
            response = requests.get(
                f"{self.api_base}/conversations.history",
                headers=self.headers,
                params={
                    "channel": channel_id,
                    "oldest": oldest,
                    "limit": limit
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                return data.get("messages", [])  # type: ignore[no-any-return]
            else:
                logger.error(f"Failed to get conversation history: {data.get('error')}")
                return []

        except Exception as e:
            logger.error(f"Failed to get Slack conversation history: {e}")
            return []

    def get_thread_replies(self, channel_id: str, thread_ts: str, oldest: float = 0, limit: int = 100) -> list:
        """
        Get replies in a thread (conversations.replies).

        Unlike conversations.history, this returns threaded replies.

        Args:
            channel_id: Channel ID containing the thread
            thread_ts: Parent message timestamp (thread root)
            oldest: Only return messages after this Unix timestamp
            limit: Max messages to retrieve

        Returns:
            List of reply messages (excludes the parent message)
        """
        try:
            params = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": limit,
            }
            if oldest:
                params["oldest"] = oldest

            response = requests.get(
                f"{self.api_base}/conversations.replies",
                headers=self.headers,
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                messages = data.get("messages", [])
                # First message is the parent; return only replies
                return [m for m in messages if m.get("ts") != thread_ts]
            else:
                logger.error(f"Failed to get thread replies: {data.get('error')}")
                return []

        except Exception as e:
            logger.error(f"Failed to get Slack thread replies: {e}")
            return []

    def send_channel_message(self, channel_id: str, message: str, thread_ts: Optional[str] = None) -> Optional[str]:
        """
        Send a message to a channel, optionally as a threaded reply.

        Args:
            channel_id: Channel ID
            message: Message text
            thread_ts: If set, post as a reply in this thread

        Returns:
            Message timestamp (ts) or None if failed
        """
        try:
            payload = {
                "channel": channel_id,
                "text": message,
            }
            if thread_ts:
                payload["thread_ts"] = thread_ts

            response = requests.post(
                f"{self.api_base}/chat.postMessage",
                headers=self.headers,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                logger.info(f"Sent message to Slack channel {channel_id}")
                return data.get("ts")  # type: ignore[no-any-return]
            else:
                logger.error(f"Failed to send Slack channel message: {data.get('error')}")
                return None

        except Exception as e:
            logger.error(f"Failed to send Slack channel message: {e}")
            return None

    def get_conversations_list(self, types: str = "public_channel,private_channel") -> list:
        """
        Get list of conversations (channels) the bot is a member of.

        Args:
            types: Comma-separated list of conversation types

        Returns:
            List of channel dictionaries
        """
        try:
            response = requests.get(
                f"{self.api_base}/conversations.list",
                headers=self.headers,
                params={
                    "types": types,
                    "exclude_archived": True
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                return data.get("channels", [])  # type: ignore[no-any-return]
            else:
                logger.error(f"Failed to get conversations: {data.get('error')}")
                return []

        except Exception as e:
            logger.error(f"Failed to get Slack conversations: {e}")
            return []

    def search_messages(self, query: str, count: int = 20) -> list:
        """
        Search for messages matching query.

        Args:
            query: Search query (e.g., "mention:@botname")
            count: Number of results to return

        Returns:
            List of matching messages
        """
        try:
            response = requests.get(
                f"{self.api_base}/search.messages",
                headers=self.headers,
                params={
                    "query": query,
                    "count": count,
                    "sort": "timestamp",
                    "sort_dir": "desc"
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                matches = data.get("messages", {}).get("matches", [])
                return matches  # type: ignore[no-any-return]
            else:
                logger.error(f"Failed to search messages: {data.get('error')}")
                return []

        except Exception as e:
            logger.error(f"Failed to search Slack messages: {e}")
            return []

    def delete_message(self, channel_id: str, timestamp: str) -> bool:
        """
        Delete a message (for security).

        Args:
            channel_id: Channel ID
            timestamp: Message timestamp (ts)

        Returns:
            True if successful
        """
        try:
            response = requests.post(
                f"{self.api_base}/chat.delete",
                headers=self.headers,
                json={
                    "channel": channel_id,
                    "ts": timestamp
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            if data.get("ok"):
                logger.info(f"Deleted Slack message {timestamp}")
                return True
            else:
                logger.error(f"Failed to delete Slack message: {data.get('error')}")
                return False

        except Exception as e:
            logger.error(f"Failed to delete Slack message: {e}")
            return False
