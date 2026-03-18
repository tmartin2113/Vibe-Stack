"""
Paperclip Bridge — Slack/Mattermost ↔ Paperclip

Polls Slack/Mattermost for @mentions, creates Paperclip issues for each
request, polls for completion, and posts results back to the messenger.

All workflow execution is handled by Paperclip heartbeat agents — the bridge
never invokes the graph directly.
"""

import os
import re
import time
import signal
import logging
import threading
from typing import Optional, Dict, List, Any
from queue import Queue, Empty, Full
from datetime import datetime, timedelta

from .config import SystemConfig, get_production_config
from .messenger_client import MattermostClient, SlackClient
from .metrics import metrics, start_health_server
from .paperclip_client import PaperclipClient, PaperclipAPIError

logger = logging.getLogger(__name__)

# Configuration from environment variables
POLL_INTERVAL = int(os.getenv("GENESIA_DAEMON_POLL_INTERVAL", "5"))  # seconds
COMPLETION_POLL_INTERVAL = int(os.getenv("GENESIA_BRIDGE_COMPLETION_POLL", "10"))  # seconds
COMPLETION_TIMEOUT = int(os.getenv("GENESIA_BRIDGE_COMPLETION_TIMEOUT", "600"))  # seconds
MAX_QUEUE_SIZE = int(os.getenv("GENESIA_DAEMON_QUEUE_SIZE", "100"))  # max pending requests
# Maximum age (seconds) for dedup cache entries before eviction
DEDUP_TTL_SECONDS = int(os.getenv("GENESIA_DAEMON_DEDUP_TTL", "3600"))  # 1 hour default
DEDUP_MAX_SIZE = 5000  # hard cap — evict oldest when exceeded


class PaperclipBridge:
    """
    Thin bridge between Slack/Mattermost and Paperclip.

    Polls messengers for @mentions → creates Paperclip issues →
    polls for completion → posts results back.
    """

    def __init__(self, config: Optional[SystemConfig] = None):
        self.config = config or get_production_config()
        self.running = False
        self.shutdown_event = threading.Event()

        # Messenger clients
        self.mattermost_client: Optional[MattermostClient] = None
        self.slack_client: Optional[SlackClient] = None
        self.mattermost_bot_username: Optional[str] = None
        self.slack_bot_user_id: Optional[str] = None

        # Paperclip client
        self.paperclip_client: Optional[PaperclipClient] = None

        # Request queue
        self.request_queue: Queue = Queue(maxsize=MAX_QUEUE_SIZE)

        # Message dedup (message_id -> monotonic timestamp)
        self.processed_messages: Dict[str, float] = {}
        self.message_lock = threading.Lock()

        # In-flight issue tracking (issue_id -> messenger context)
        self.inflight: Dict[str, Dict[str, Any]] = {}
        self.inflight_lock = threading.Lock()

        # Metrics
        self.metrics_lock = threading.Lock()
        self.metrics: Dict[str, Any] = {
            "requests_created": 0,
            "requests_completed": 0,
            "requests_failed": 0,
            "start_time": None,
        }

    # ── Setup ──

    def _setup_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        logger.info("Signal handlers registered (SIGINT, SIGTERM)")

    def _signal_handler(self, signum, frame):
        logger.info("Received signal %d, initiating shutdown...", signum)
        self.stop()

    def _initialize_messengers(self):
        """Initialize messenger clients based on environment variables."""
        mattermost_url = os.getenv("MATTERMOST_URL")
        mattermost_token = os.getenv("MATTERMOST_BOT_TOKEN")

        if mattermost_url and mattermost_token:
            try:
                self.mattermost_client = MattermostClient(
                    url=mattermost_url, bot_token=mattermost_token,
                )
                self.mattermost_bot_username = self.mattermost_client.get_bot_username()
                logger.info("Mattermost client initialized (@%s)", self.mattermost_bot_username)
            except Exception as e:
                logger.warning("Failed to initialize Mattermost: %s", e)

        slack_token = os.getenv("SLACK_BOT_TOKEN")
        if slack_token:
            try:
                self.slack_client = SlackClient(bot_token=slack_token)
                self.slack_bot_user_id = self.slack_client.get_bot_user_id()
                logger.info("Slack client initialized (bot ID: %s)", self.slack_bot_user_id)
            except Exception as e:
                logger.warning("Failed to initialize Slack: %s", e)

        if not self.mattermost_client and not self.slack_client:
            raise RuntimeError(
                "No messenger configured! Set MATTERMOST_URL + MATTERMOST_BOT_TOKEN "
                "or SLACK_BOT_TOKEN environment variables."
            )

    def _initialize_paperclip(self):
        """Initialize Paperclip client from environment."""
        api_url = os.getenv("PAPERCLIP_API_URL", self.config.paperclip.api_url)
        api_key = os.getenv("PAPERCLIP_API_KEY", self.config.paperclip.api_key)
        agent_id = os.getenv("PAPERCLIP_AGENT_ID", "")
        company_id = os.getenv("PAPERCLIP_COMPANY_ID", "")

        if not api_url:
            raise RuntimeError(
                "Paperclip not configured! Set PAPERCLIP_API_URL environment variable."
            )

        self.paperclip_client = PaperclipClient(
            api_url=api_url,
            api_key=api_key,
            agent_id=agent_id,
            company_id=company_id,
        )
        logger.info("Paperclip client initialized (url: %s)", api_url)

    # ── Mention polling ──

    def _poll_mattermost_mentions(self) -> List[Dict[str, Any]]:
        """Poll Mattermost for messages mentioning the bot."""
        if not self.mattermost_client:
            return []

        try:
            bot_user_id = self.mattermost_client._get_bot_user_id()
            search_results = self.mattermost_client.search_posts(
                f"@{self.mattermost_bot_username}"
            )
            channels = self.mattermost_client.get_channels_for_user(bot_user_id)

            mentions = []
            seen_ids: set = set()
            cutoff_time = datetime.now() - timedelta(seconds=60)
            cutoff_ms = int(cutoff_time.timestamp() * 1000)

            for post in search_results:
                if post.get("create_at", 0) < cutoff_ms:
                    continue
                if post.get("user_id") == bot_user_id:
                    continue
                post_id = post.get("id")
                seen_ids.add(post_id)
                mentions.append({
                    "id": post_id,
                    "platform": "mattermost",
                    "channel_id": post.get("channel_id"),
                    "user_id": post.get("user_id"),
                    "text": post.get("message", ""),
                    "timestamp": post.get("create_at", 0),
                })

            for channel in channels:
                channel_id = channel.get("id")
                recent = self.mattermost_client.get_recent_messages(
                    channel_id, since=cutoff_time, limit=10,
                )
                for msg in recent:
                    msg_id = msg.get("id")
                    if msg_id in seen_ids:
                        continue
                    message_text = msg.get("message", "")
                    if (
                        f"@{self.mattermost_bot_username}" in message_text
                        or "@all" in message_text
                    ):
                        if msg.get("user_id") == bot_user_id:
                            continue
                        seen_ids.add(msg_id)
                        mentions.append({
                            "id": msg_id,
                            "platform": "mattermost",
                            "channel_id": channel_id,
                            "user_id": msg.get("user_id"),
                            "text": message_text,
                            "timestamp": msg.get("create_at", 0),
                        })

            return mentions

        except Exception as e:
            logger.error("Error polling Mattermost: %s", e)
            return []

    def _poll_slack_mentions(self) -> List[Dict[str, Any]]:
        """Poll Slack for messages mentioning the bot."""
        if not self.slack_client:
            return []

        try:
            search_results = self.slack_client.search_messages(
                query=f"<@{self.slack_bot_user_id}>", count=20,
            )
            conversations = self.slack_client.get_conversations_list()

            mentions = []
            seen_ids: set = set()
            cutoff_time = datetime.now() - timedelta(seconds=60)
            cutoff_ts = cutoff_time.timestamp()

            for message in search_results:
                ts = float(message.get("ts", 0))
                if ts < cutoff_ts:
                    continue
                if message.get("bot_id"):
                    continue
                msg_ts = message.get("ts")
                seen_ids.add(msg_ts)
                mentions.append({
                    "id": msg_ts,
                    "platform": "slack",
                    "channel_id": message.get("channel", {}).get("id"),
                    "user_id": message.get("user"),
                    "text": message.get("text", ""),
                    "timestamp": ts,
                })

            for conv in conversations:
                channel_id = conv.get("id")
                recent = self.slack_client.get_conversation_history(
                    channel_id, oldest=cutoff_ts, limit=20,
                )
                for msg in recent:
                    msg_ts = msg.get("ts")
                    if msg_ts in seen_ids:
                        continue
                    message_text = msg.get("text", "")
                    if (
                        f"<@{self.slack_bot_user_id}>" in message_text
                        or "@channel" in message_text
                    ):
                        if msg.get("bot_id"):
                            continue
                        seen_ids.add(msg_ts)
                        mentions.append({
                            "id": msg_ts,
                            "platform": "slack",
                            "channel_id": channel_id,
                            "user_id": msg.get("user"),
                            "text": message_text,
                            "timestamp": float(msg_ts),
                        })

            return mentions

        except Exception as e:
            logger.error("Error polling Slack: %s", e)
            return []

    # ── Dedup ──

    def _is_message_processed(self, message_id: str) -> bool:
        with self.message_lock:
            ts = self.processed_messages.get(message_id)
            if ts is None:
                return False
            if time.monotonic() - ts > DEDUP_TTL_SECONDS:
                del self.processed_messages[message_id]
                return False
            return True

    def _mark_message_processed(self, message_id: str):
        now = time.monotonic()
        with self.message_lock:
            self.processed_messages[message_id] = now

            if len(self.processed_messages) > DEDUP_MAX_SIZE:
                expired = [
                    mid for mid, ts in self.processed_messages.items()
                    if now - ts > DEDUP_TTL_SECONDS
                ]
                for mid in expired:
                    del self.processed_messages[mid]

            while len(self.processed_messages) > DEDUP_MAX_SIZE:
                self.processed_messages.pop(next(iter(self.processed_messages)))

    # ── Message extraction ──

    def _extract_request_from_message(self, message: Dict[str, Any]) -> Optional[str]:
        """Extract request text from a mention message."""
        text = message.get("text", "").strip()
        if not text:
            return None

        # Remove bot mention patterns (Slack first — more specific pattern)
        text = re.sub(r'<@[A-Z0-9]+>', '', text)  # Slack <@U123>
        text = re.sub(r'@[\w-]+', '', text)        # Mattermost @username
        text = text.strip()

        if len(text) < 5:
            return None
        return text

    # ── Response formatting ──

    def _format_response_for_chat(self, result: str, max_length: int = 4000) -> List[str]:
        """Split long output into chat-sized chunks."""
        if len(result) <= max_length:
            return [result]

        chunks = []
        current_chunk = ""
        for line in result.split('\n'):
            if len(current_chunk) + len(line) + 1 > max_length:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = line + '\n'
            else:
                current_chunk += line + '\n'
        if current_chunk:
            chunks.append(current_chunk)

        formatted = []
        for i, chunk in enumerate(chunks):
            if i == 0 and len(chunks) > 1:
                formatted.append(chunk + "\n\n*[Continued in next message...]*")
            elif i == len(chunks) - 1 and len(chunks) > 1:
                formatted.append(f"*[Continued from previous message]*\n\n" + chunk)
            elif len(chunks) > 1:
                formatted.append(
                    f"*[Continued - Part {i+1}/{len(chunks)}]*\n\n"
                    + chunk + "\n\n*[Continued...]*"
                )
            else:
                formatted.append(chunk)
        return formatted

    def _send_response(
        self,
        platform: str,
        channel_id: str,
        response: str,
        thread_id: Optional[str] = None,
    ) -> Optional[str]:
        """Send response back to the messenger platform."""
        first_id = None
        try:
            chunks = self._format_response_for_chat(response)
            for chunk in chunks:
                if platform == "mattermost" and self.mattermost_client:
                    post_id = self.mattermost_client.send_channel_message(
                        channel_id, chunk, root_id=thread_id,
                    )
                    if first_id is None and post_id:
                        first_id = post_id
                        thread_id = post_id
                elif platform == "slack" and self.slack_client:
                    ts = self.slack_client.send_channel_message(
                        channel_id, chunk, thread_ts=thread_id,
                    )
                    if first_id is None and ts:
                        first_id = ts
                        thread_id = ts
                time.sleep(0.5)
        except Exception as e:
            logger.error("Failed to send response: %s", e, exc_info=True)
        return first_id

    # ── Paperclip integration ──

    def _create_issue_from_mention(self, mention: Dict[str, Any], request_text: str) -> Optional[str]:
        """Create a Paperclip issue from a Slack/Mattermost mention."""
        if not self.paperclip_client:
            return None

        platform = mention.get("platform", "unknown")
        user_id = mention.get("user_id", "unknown")

        title = request_text[:200]
        description = (
            f"<!-- source:bridge platform:{platform} user:{user_id} "
            f"channel:{mention.get('channel_id', '')} -->\n\n"
            f"{request_text}"
        )

        try:
            issue = self.paperclip_client.create_issue(
                title=title,
                description=description,
                labels=["bridge", platform],
            )
            logger.info("Created Paperclip issue %s for %s mention", issue.id, platform)
            return issue.id
        except PaperclipAPIError as e:
            logger.error("Failed to create Paperclip issue: %s", e)
            return None

    def _poll_issue_completion(self, issue_id: str) -> Optional[Dict[str, Any]]:
        """Poll Paperclip until the issue reaches a terminal state."""
        if not self.paperclip_client:
            return None

        deadline = time.monotonic() + COMPLETION_TIMEOUT
        terminal_statuses = {"done", "blocked", "closed", "cancelled"}

        while time.monotonic() < deadline:
            if self.shutdown_event.is_set():
                return None

            try:
                issue = self.paperclip_client.get_issue(issue_id)
                if issue.status in terminal_statuses:
                    # Fetch comments to find the agent's output
                    comments = self.paperclip_client.get_comments(issue_id)
                    output = ""
                    for comment in reversed(comments):
                        body = comment.body or ""
                        if body.startswith("## Completed") or body.startswith("## Blocked"):
                            output = body
                            break
                    if not output and comments:
                        output = comments[-1].body or ""

                    return {
                        "status": issue.status,
                        "output": output,
                        "issue_id": issue_id,
                    }
            except PaperclipAPIError as e:
                logger.warning("Failed to poll issue %s: %s", issue_id, e)

            self.shutdown_event.wait(COMPLETION_POLL_INTERVAL)

        logger.warning("Completion timeout for issue %s", issue_id)
        return {"status": "timeout", "output": "Task timed out.", "issue_id": issue_id}

    # ── Main loops ──

    def _handle_mention(self, mention: Dict[str, Any]):
        """Process a single mention: create issue, poll, post result."""
        platform = mention["platform"]
        channel_id = mention["channel_id"]

        request_text = self._extract_request_from_message(mention)
        if not request_text:
            return

        # Acknowledge
        thread_id = self._send_response(
            platform, channel_id,
            f"Processing your request...\n\n_{request_text[:100]}_",
        )

        # Create Paperclip issue
        issue_id = self._create_issue_from_mention(mention, request_text)
        if not issue_id:
            self._send_response(
                platform, channel_id,
                "Failed to create task. Please try again.",
                thread_id=thread_id,
            )
            with self.metrics_lock:
                self.metrics["requests_failed"] += 1
            return

        with self.metrics_lock:
            self.metrics["requests_created"] += 1

        # Track in-flight
        with self.inflight_lock:
            self.inflight[issue_id] = {
                "platform": platform,
                "channel_id": channel_id,
                "thread_id": thread_id,
                "request": request_text,
            }

        # Poll for completion (blocking — runs in handler thread)
        result = self._poll_issue_completion(issue_id)

        # Post result back
        if result:
            output = result.get("output", "No output")
            status = result.get("status", "unknown")
            footer = f"\n\n---\n*Status: {status} | Issue: {issue_id}*"
            self._send_response(platform, channel_id, output + footer, thread_id=thread_id)

            with self.metrics_lock:
                self.metrics["requests_completed"] += 1
        else:
            self._send_response(
                platform, channel_id,
                "Task processing was interrupted.",
                thread_id=thread_id,
            )

        # Clean up in-flight
        with self.inflight_lock:
            self.inflight.pop(issue_id, None)

    def _handler_thread(self):
        """Thread that processes queued mentions."""
        while self.running and not self.shutdown_event.is_set():
            try:
                mention = self.request_queue.get(timeout=1.0)
            except Empty:
                continue

            try:
                self._handle_mention(mention)
            except Exception as e:
                logger.error("Handler error: %s", e, exc_info=True)
            finally:
                self.request_queue.task_done()

    def _polling_loop(self):
        """Main polling loop that checks for new mentions."""
        logger.info("Starting polling loop (interval: %ds)", POLL_INTERVAL)

        while self.running and not self.shutdown_event.is_set():
            try:
                all_mentions: List[Dict[str, Any]] = []

                if self.mattermost_client:
                    all_mentions.extend(self._poll_mattermost_mentions())
                if self.slack_client:
                    all_mentions.extend(self._poll_slack_mentions())

                for mention in all_mentions:
                    message_id = mention.get("id")
                    if self._is_message_processed(message_id):  # type: ignore[arg-type]
                        continue

                    request_text = self._extract_request_from_message(mention)
                    if not request_text:
                        self._mark_message_processed(message_id)  # type: ignore[arg-type]
                        continue

                    try:
                        self.request_queue.put(mention, block=False)
                        self._mark_message_processed(message_id)  # type: ignore[arg-type]
                        logger.info(
                            "Queued mention from %s: %s...",
                            mention.get("platform"), request_text[:50],
                        )
                    except Full:
                        logger.warning("Queue full (%d)! Dropping request.", MAX_QUEUE_SIZE)
                        metrics.increment("genesia_requests_dropped_total")
                        self._send_response(
                            mention.get("platform", ""),
                            mention.get("channel_id", ""),
                            "I'm currently at capacity. Please try again in a few minutes.",
                        )
                        self._mark_message_processed(message_id)  # type: ignore[arg-type]

                self.shutdown_event.wait(POLL_INTERVAL)

            except Exception as e:
                logger.error("Polling loop error: %s", e, exc_info=True)
                time.sleep(POLL_INTERVAL)

        logger.info("Polling loop stopped")

    # ── Lifecycle ──

    def _is_ready(self) -> bool:
        return (
            self.running
            and self.paperclip_client is not None
            and (self.mattermost_client is not None or self.slack_client is not None)
        )

    def start(self):
        """Start the bridge service."""
        if self.running:
            logger.warning("Bridge already running")
            return

        logger.info("Starting Paperclip Bridge...")

        self._setup_signal_handlers()
        self._initialize_messengers()
        self._initialize_paperclip()

        self.metrics["start_time"] = datetime.now()
        self._start_time_monotonic = time.monotonic()

        self._health_server = start_health_server(
            readiness_fn=self._is_ready,
            daemon_status_fn=self.status,
        )

        self.running = True

        # Start handler thread (processes queued mentions)
        handler = threading.Thread(
            target=self._handler_thread,
            name="BridgeHandler",
            daemon=True,
        )
        handler.start()
        self._handler = handler

        logger.info("Bridge service started")

        # Polling loop runs in main thread
        self._polling_loop()

    def stop(self):
        """Stop the bridge service gracefully."""
        if not self.running:
            return

        logger.info("Stopping bridge service...")
        self.running = False
        self.shutdown_event.set()

        if hasattr(self, "_handler"):
            self._handler.join(timeout=10.0)

        if hasattr(self, "_health_server") and self._health_server:
            self._health_server.shutdown()

        with self.metrics_lock:
            start_time = self.metrics["start_time"]
            created = self.metrics["requests_created"]
            completed = self.metrics["requests_completed"]
            failed = self.metrics["requests_failed"]

        uptime = (datetime.now() - start_time) if start_time else None
        logger.info("Bridge Metrics:")
        logger.info("  Uptime: %s", uptime)
        logger.info("  Issues created: %d", created)
        logger.info("  Completed: %d", completed)
        logger.info("  Failed: %d", failed)
        logger.info("Bridge service stopped")

    def status(self) -> Dict[str, Any]:
        """Get bridge status."""
        uptime = None
        if self.metrics["start_time"]:
            uptime = str(datetime.now() - self.metrics["start_time"])

        with self.inflight_lock:
            inflight_count = len(self.inflight)

        return {
            "running": self.running,
            "uptime": uptime,
            "inflight_issues": inflight_count,
            "queue_size": self.request_queue.qsize(),
            "metrics": dict(self.metrics),
            "messengers": {
                "mattermost": {
                    "enabled": self.mattermost_client is not None,
                    "bot_username": self.mattermost_bot_username,
                },
                "slack": {
                    "enabled": self.slack_client is not None,
                    "bot_user_id": self.slack_bot_user_id,
                },
            },
        }


def run_daemon(config: Optional[SystemConfig] = None):
    """
    Run the Paperclip Bridge (Slack/Mattermost ↔ Paperclip).

    This replaces the old daemon mode. Instead of running workflows directly,
    mentions are converted to Paperclip issues and handled by heartbeat agents.
    """
    bridge = PaperclipBridge(config)
    try:
        bridge.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        bridge.stop()
