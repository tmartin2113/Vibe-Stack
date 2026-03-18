"""
API Key Management with Messenger Integration

Handles secure API key retrieval with user prompting via Slack/Mattermost
when keys are missing from environment.
"""

import os
import logging
from typing import Optional, Dict
from pathlib import Path
import json

logger = logging.getLogger(__name__)

# Configurable via environment variables
DEFAULT_PROMPT_TIMEOUT_MINUTES = int(os.getenv("GENESIA_PROMPT_TIMEOUT", "5"))
DEFAULT_POLL_INTERVAL_SECONDS = int(os.getenv("GENESIA_POLL_INTERVAL", "2"))


class APIKeyManager:
    """
    Manages API keys with support for user prompting via messengers.

    Features:
    - Environment variable lookup
    - Secure local storage
    - Messenger-based user prompting (Slack/Mattermost)
    - In-memory caching
    """

    def __init__(self, config=None):
        """
        Initialize API key manager.

        Args:
            config: Optional SystemConfig with messenger settings
        """
        self.config = config
        self.cache: Dict[str, str] = {}

        # Path for secure local storage
        self.storage_path = Path.home() / ".genesia" / "api_keys.json"
        self._load_stored_keys()

    def _load_stored_keys(self):
        """Load API keys from secure local storage"""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, 'r') as f:
                    stored = json.load(f)
                    self.cache.update(stored)
                logger.info(f"Loaded {len(stored)} API keys from storage")
            except Exception as e:
                logger.error(f"Failed to load stored API keys: {e}")

    def _validate_api_key(self, key_name: str, api_key: str) -> tuple[bool, str]:
        """
        Validate API key format based on provider.

        Args:
            key_name: Name of the API key (e.g., "ANTHROPIC_API_KEY")
            api_key: The API key value to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Basic validation: minimum length
        if len(api_key) < 10:
            return False, "API key is too short (minimum 10 characters)"

        # Check for valid ASCII characters only
        if not api_key.isascii():
            return False, "API key contains invalid non-ASCII characters"

        # Check for whitespace
        if api_key != api_key.strip() or ' ' in api_key or '\t' in api_key or '\n' in api_key:
            return False, "API key contains whitespace characters"

        # Provider-specific validation
        key_upper = key_name.upper()

        if "OPENAI" in key_upper:
            if not api_key.startswith("sk-"):
                return False, "OpenAI API keys must start with 'sk-'"
            if len(api_key) < 40:
                return False, "OpenAI API key appears too short (expected 40+ characters)"

        elif "ANTHROPIC" in key_upper or "CLAUDE" in key_upper:
            if not api_key.startswith("sk-ant-"):
                return False, "Anthropic API keys must start with 'sk-ant-'"
            if len(api_key) < 50:
                return False, "Anthropic API key appears too short (expected 50+ characters)"

        elif "HUGGINGFACE" in key_upper or "HF" in key_upper:
            if not api_key.startswith("hf_"):
                return False, "HuggingFace API keys must start with 'hf_'"

        elif "GOOGLE" in key_upper or "GEMINI" in key_upper:
            # Google API keys are typically 39 characters
            if len(api_key) < 30:
                return False, "Google API key appears too short (expected 30+ characters)"

        # Generic validation passed
        return True, ""

    def _save_key(self, key_name: str, key_value: str):
        """
        Save API key to secure local storage.

        Args:
            key_name: Name of the API key (e.g., "ANTHROPIC_API_KEY")
            key_value: The actual API key value
        """
        try:
            # Ensure directory exists
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)

            # Load existing keys
            existing = {}
            if self.storage_path.exists():
                with open(self.storage_path, 'r') as f:
                    existing = json.load(f)

            # Add new key
            existing[key_name] = key_value

            # Save with restricted permissions (0600)
            with open(self.storage_path, 'w') as f:
                json.dump(existing, f, indent=2)

            # Set file permissions (owner read/write only)
            os.chmod(self.storage_path, 0o600)

            # Update cache
            self.cache[key_name] = key_value

            logger.info(f"Saved API key '{key_name}' to secure storage")

        except Exception as e:
            logger.error(f"Failed to save API key: {e}")
            raise

    def get_api_key(self, key_name: str, prompt_user: bool = True) -> Optional[str]:
        """
        Get API key with fallback chain:
        1. Check environment variables
        2. Check in-memory cache
        3. Check local storage
        4. Prompt user via messenger (if enabled and prompt_user=True)

        Args:
            key_name: Name of the API key (e.g., "ANTHROPIC_API_KEY")
            prompt_user: Whether to prompt user if key is missing

        Returns:
            API key value or None if not found
        """
        # 1. Check environment
        env_value = os.environ.get(key_name)
        if env_value:
            logger.debug(f"Found '{key_name}' in environment")
            return env_value

        # 2. Check cache
        if key_name in self.cache:
            logger.debug(f"Found '{key_name}' in cache")
            return self.cache[key_name]

        # 3. Check local storage (if not in cache, try reloading)
        self._load_stored_keys()
        if key_name in self.cache:
            logger.debug(f"Found '{key_name}' in storage")
            return self.cache[key_name]

        # 4. Prompt user if enabled
        if prompt_user:
            prompted_key = self._prompt_user_for_key(key_name)
            if prompted_key:
                return prompted_key

        logger.warning(f"API key '{key_name}' not found")
        return None

    def _prompt_user_for_key(self, key_name: str) -> Optional[str]:
        """
        Prompt user for API key via messenger (Mattermost or Slack).

        Args:
            key_name: Name of the API key to prompt for

        Returns:
            API key provided by user or None
        """
        try:
            # Try Mattermost first if configured
            if self.config and self.config.mattermost.enabled and self.config.mattermost.bot_enabled:
                logger.info("Attempting to prompt via Mattermost...")
                return self._prompt_via_mattermost(key_name)

            # Try Slack if configured
            if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_USER_ID"):
                logger.info("Attempting to prompt via Slack...")
                return self._prompt_via_slack(key_name)

            # No messenger configured
            logger.info("No messenger integration configured for prompting")
            return None

        except Exception as e:
            logger.error(f"Failed to prompt user for API key: {e}")
            return None

    def _prompt_via_mattermost(self, key_name: str) -> Optional[str]:
        """
        Prompt user via Mattermost for API key.

        Sends a direct message to the user requesting the API key,
        polls for response, validates, stores, and deletes the message
        containing the key for security.

        Args:
            key_name: Name of the API key

        Returns:
            API key from user or None
        """
        try:
            from .messenger_client import MattermostClient
            from datetime import datetime, timedelta
            import time

            # Validate configuration
            if not self.config.mattermost.bot_token or not self.config.mattermost.mattermost_url:
                logger.error("Mattermost bot_token or mattermost_url not configured")
                return None

            # Get username from environment or config
            user_username = os.environ.get("MATTERMOST_USERNAME") or os.environ.get("USER")
            if not user_username:
                logger.error("Cannot determine Mattermost username (set MATTERMOST_USERNAME env var)")
                return None

            # Initialize client
            client = MattermostClient(
                self.config.mattermost.mattermost_url,
                self.config.mattermost.bot_token
            )

            # Get user info
            user_data = client.get_user_by_username(user_username)
            if not user_data:
                logger.error(f"User '{user_username}' not found in Mattermost")
                return None

            user_id = user_data["id"]

            # Send prompt message
            prompt_message = f"""**API Key Required: {key_name}**

The system needs your {key_name.replace('_', ' ').title()} to continue.

**How to provide it:**
1. Reply to this message with ONLY the API key (nothing else)
2. The key will be securely stored and this message will be deleted

**Security Notes:**
- Your key will be stored in ~/.genesia/api_keys.json with restricted permissions
- The message containing your key will be automatically deleted
- You have 5 minutes to respond before this request times out

Please send your {key_name} now:"""

            # Set poll start BEFORE sending message to avoid race condition
            poll_start = datetime.now()
            timeout = timedelta(minutes=DEFAULT_PROMPT_TIMEOUT_MINUTES)
            poll_interval = DEFAULT_POLL_INTERVAL_SECONDS

            # Send prompt message
            prompt_post_id = client.send_direct_message(user_id, prompt_message)
            logger.info(f"Sent API key prompt to user '{user_username}'")

            # Get channel ID for polling
            channel_id = client.get_direct_channel_id(user_id)

            while datetime.now() - poll_start < timeout:
                # Get messages since we sent the prompt
                messages = client.get_recent_messages(channel_id, poll_start)

                # Look for user's response (not from bot)
                bot_user_id = client._get_bot_user_id()
                for msg in messages:
                    # Skip bot's own messages
                    if msg.get("user_id") == bot_user_id:
                        continue

                    # Skip the original prompt message
                    if msg.get("id") == prompt_post_id:
                        continue

                    # Found a user message - treat as API key
                    api_key = msg.get("message", "").strip()

                    if api_key:
                        logger.info("Received API key from user via Mattermost")

                        # Delete the message containing the key (security)
                        deletion_success = client.delete_message(msg["id"])

                        if not deletion_success:
                            logger.error(f"SECURITY: Failed to delete message containing API key!")
                            client.send_direct_message(
                                user_id,
                                "⚠️ **SECURITY WARNING**: Failed to delete your message containing the API key!\n\n"
                                "**Please manually delete it from the chat immediately** to prevent exposure.\n\n"
                                "The key will still be stored securely, but the message is currently visible in chat history."
                            )

                        # Validate key format
                        is_valid, error_message = self._validate_api_key(key_name, api_key)
                        if not is_valid:
                            client.send_direct_message(
                                user_id,
                                f"❌ Invalid API key format: {error_message}\n\nPlease try again."
                            )
                            continue

                        # Save the key
                        try:
                            self._save_key(key_name, api_key)

                            # Confirm to user
                            confirmation_msg = f"✅ {key_name} received and stored securely!\n\nYour key has been saved to ~/.genesia/api_keys.json"
                            if not deletion_success:
                                confirmation_msg += "\n\n⚠️ **Remember to manually delete your message with the key!**"

                            client.send_direct_message(user_id, confirmation_msg)

                            # Clean up prompt message
                            prompt_deleted = client.delete_message(prompt_post_id)
                            if not prompt_deleted:
                                logger.warning(f"Failed to delete prompt message {prompt_post_id}")

                            return api_key  # type: ignore[no-any-return]

                        except Exception as e:
                            logger.error(f"Failed to save API key: {e}")
                            client.send_direct_message(
                                user_id,
                                f"❌ Error storing API key: {str(e)}\nPlease try setting it manually."
                            )
                            return None

                # Wait before polling again
                time.sleep(poll_interval)

            # Timeout
            logger.warning(f"Mattermost API key prompt timed out after 5 minutes")
            client.send_direct_message(
                user_id,
                f"⏱️ API key request timed out. Please set {key_name} manually:\n\n"
                f"```\nexport {key_name}=your-api-key-here\n```"
            )
            return None

        except Exception as e:
            logger.error(f"Mattermost prompt failed: {e}", exc_info=True)
            return None

    def _prompt_via_slack(self, key_name: str) -> Optional[str]:
        """
        Prompt user via Slack for API key.

        Sends a direct message to the user requesting the API key,
        polls for response, validates, stores, and deletes the message
        containing the key for security.

        Args:
            key_name: Name of the API key

        Returns:
            API key from user or None
        """
        try:
            from .messenger_client import SlackClient
            from datetime import datetime
            import time
            import requests

            # Get Slack bot token from environment
            slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
            if not slack_bot_token:
                logger.error("SLACK_BOT_TOKEN not configured in environment")
                return None

            # Get Slack user ID from environment
            slack_user_id = os.environ.get("SLACK_USER_ID")
            if not slack_user_id:
                logger.error("SLACK_USER_ID not configured (set in environment)")
                return None

            # Initialize client
            client = SlackClient(slack_bot_token)

            # Send prompt message
            prompt_message = f"""*API Key Required: {key_name}*

The system needs your {key_name.replace('_', ' ').title()} to continue.

*How to provide it:*
1. Reply to this thread with ONLY the API key (nothing else)
2. The key will be securely stored and this message will be deleted

*Security Notes:*
• Your key will be stored in ~/.genesia/api_keys.json with restricted permissions
• The message containing your key will be automatically deleted
• You have 5 minutes to respond before this request times out

Please send your {key_name} now:"""

            # Set poll start BEFORE sending message to avoid race condition
            poll_start = datetime.now()
            timeout_seconds = DEFAULT_PROMPT_TIMEOUT_MINUTES * 60
            poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
            start_timestamp = time.time()

            # Send prompt message
            result = client.send_direct_message(slack_user_id, prompt_message)
            if not result:
                logger.error("Failed to send Slack prompt message")
                return None

            prompt_ts, channel_id = result  # Unpack tuple - no redundant API call needed
            logger.info(f"Sent API key prompt to Slack user {slack_user_id}")

            while time.time() - start_timestamp < timeout_seconds:
                # Get messages since the prompt
                messages = client.get_conversation_history(channel_id, start_timestamp)

                # Look for user's response (not from bot)
                for msg in messages:
                    # Skip bot messages
                    if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                        continue

                    # Skip the prompt message
                    if msg.get("ts") == prompt_ts:
                        continue

                    # Found a user message - treat as API key
                    api_key = msg.get("text", "").strip()

                    if api_key:
                        logger.info("Received API key from user via Slack")
                        msg_ts = msg.get("ts")

                        # Delete the message containing the key (security)
                        deletion_success = False
                        if msg_ts:
                            deletion_success = client.delete_message(channel_id, msg_ts)

                        if not deletion_success:
                            logger.error(f"SECURITY: Failed to delete Slack message containing API key!")
                            client.send_direct_message(
                                slack_user_id,
                                "⚠️ **SECURITY WARNING**: Failed to delete your message containing the API key!\n\n"
                                "**Please manually delete it from the chat immediately** to prevent exposure.\n\n"
                                "The key will still be stored securely, but the message is currently visible in chat history."
                            )

                        # Validate key format
                        is_valid, error_message = self._validate_api_key(key_name, api_key)
                        if not is_valid:
                            client.send_direct_message(
                                slack_user_id,
                                f"❌ Invalid API key format: {error_message}\n\nPlease try again."
                            )
                            continue

                        # Save the key
                        try:
                            self._save_key(key_name, api_key)

                            # Confirm to user
                            confirmation_msg = f"✅ {key_name} received and stored securely!\n\nYour key has been saved to ~/.genesia/api_keys.json"
                            if not deletion_success:
                                confirmation_msg += "\n\n⚠️ **Remember to manually delete your message with the key!**"

                            client.send_direct_message(slack_user_id, confirmation_msg)

                            # Clean up prompt message
                            prompt_deleted = False
                            if prompt_ts:
                                prompt_deleted = client.delete_message(channel_id, prompt_ts)
                            if not prompt_deleted:
                                logger.warning(f"Failed to delete Slack prompt message {prompt_ts}")

                            return api_key  # type: ignore[no-any-return]

                        except Exception as e:
                            logger.error(f"Failed to save API key: {e}")
                            client.send_direct_message(
                                slack_user_id,
                                f"❌ Error storing API key: {str(e)}\nPlease try setting it manually."
                            )
                            return None

                # Wait before polling again
                time.sleep(poll_interval)

            # Timeout
            logger.warning(f"Slack API key prompt timed out after 5 minutes")
            client.send_direct_message(
                slack_user_id,
                f"⏱️ API key request timed out. Please set {key_name} manually:\n\n"
                f"```\nexport {key_name}=your-api-key-here\n```"
            )
            return None

        except Exception as e:
            logger.error(f"Slack prompt failed: {e}", exc_info=True)
            return None

    def get_error_message(self, key_name: str) -> str:
        """
        Get user-friendly error message for missing API key.

        Args:
            key_name: Name of the missing API key

        Returns:
            Helpful error message with setup instructions
        """
        msg = f"API key '{key_name}' is not configured.\n\n"
        msg += "To configure it, you can:\n\n"
        msg += f"1. Set environment variable:\n   export {key_name}=your-api-key-here\n\n"
        msg += f"2. Add to ~/.genesia/api_keys.json:\n"
        msg += f'   {{\n     "{key_name}": "your-api-key-here"\n   }}\n\n'

        if self.config and self.config.mattermost.enabled:
            msg += "3. Interactive prompting via messenger (coming soon)\n\n"

        msg += f"Once configured, the {key_name.replace('_', ' ').title()} will be used for API requests."

        return msg


# Global instance for easy access
_api_key_manager: Optional[APIKeyManager] = None


def get_api_key_manager(config=None) -> APIKeyManager:
    """
    Get global API key manager instance.

    Args:
        config: Optional SystemConfig (used only on first call)

    Returns:
        Global APIKeyManager instance
    """
    global _api_key_manager

    if _api_key_manager is None:
        _api_key_manager = APIKeyManager(config)

    return _api_key_manager
