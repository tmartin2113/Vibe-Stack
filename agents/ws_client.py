"""
WebSocket Client for Paperclip Live Events

Connects to Paperclip's WebSocket pub/sub endpoint and dispatches events
to registered subscribers. Runs an asyncio event loop in a daemon thread
so callers can use plain threading primitives (Event, Lock) for coordination.

Reconnects automatically with exponential backoff + jitter.

Usage:
    ws = PaperclipWSClient(api_url, company_id, api_key)
    ws.start()
    unsub = ws.subscribe(
        filter_fn=lambda e: e.get("type") == "issue.status_changed",
        handler_fn=lambda e: my_event.set(),
    )
    ...
    unsub()
    ws.stop()
"""

import asyncio
import json
import logging
import random
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Type aliases
EventDict = Dict[str, Any]
FilterFn = Callable[[EventDict], bool]
HandlerFn = Callable[[EventDict], None]


class PaperclipWSClient:
    """
    WebSocket client for Paperclip live events.

    Runs a websockets async loop in a daemon thread. Subscribers register
    a (filter_fn, handler_fn) pair; when an event passes the filter, the
    handler is called synchronously from the WS thread (keep handlers fast).
    """

    # Reconnect backoff: 1s → 2s → 4s → ... capped at 15s
    _BACKOFF_BASE = 1.0
    _BACKOFF_MAX = 15.0
    _PING_INTERVAL = 20  # Server expects pong within 30s

    def __init__(self, api_url: str, company_id: str, api_key: str):
        self._api_url = api_url.rstrip("/")
        self._company_id = company_id
        self._api_key = api_key

        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Subscriber registry: list of (filter_fn, handler_fn) tuples
        self._lock = threading.Lock()
        self._subscribers: List[Tuple[FilterFn, HandlerFn]] = []
        self._next_sub_id = 0
        self._sub_ids: Dict[int, Tuple[FilterFn, HandlerFn]] = {}

    @property
    def is_connected(self) -> bool:
        """Whether the WS connection is currently established."""
        return self._connected.is_set()

    def start(self) -> None:
        """Start the WebSocket connection in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="paperclip-ws",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the WS thread to stop and wait up to 5s for it to exit."""
        self._stop_event.set()
        self._connected.clear()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def wait_connected(self, timeout: float = 3.0) -> bool:
        """Block until connected or timeout. Returns True if connected."""
        return self._connected.wait(timeout=timeout)

    def subscribe(
        self,
        filter_fn: FilterFn,
        handler_fn: HandlerFn,
    ) -> Callable[[], None]:
        """
        Register a subscriber. Returns an unsubscribe callable.

        filter_fn: Called with each event dict; return True to dispatch.
        handler_fn: Called from the WS thread when filter matches.
                    Must be fast (just set an Event, etc.).
        """
        with self._lock:
            sub_id = self._next_sub_id
            self._next_sub_id += 1
            entry = (filter_fn, handler_fn)
            self._subscribers.append(entry)
            self._sub_ids[sub_id] = entry

        def _unsubscribe():
            with self._lock:
                if sub_id in self._sub_ids:
                    try:
                        self._subscribers.remove(self._sub_ids[sub_id])
                    except ValueError:
                        pass
                    del self._sub_ids[sub_id]

        return _unsubscribe

    def _build_ws_url(self) -> str:
        """Convert HTTP API URL to WebSocket endpoint URL."""
        url = self._api_url
        if url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        return f"{url}/api/companies/{self._company_id}/events/ws"

    def _run_loop(self) -> None:
        """Entry point for the daemon thread — runs asyncio event loop."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._connect_loop())
        except Exception:
            logger.debug("WS event loop exited", exc_info=True)
        finally:
            loop.close()

    async def _connect_loop(self) -> None:
        """Reconnection loop with exponential backoff + jitter."""
        try:
            import websockets
            from websockets.exceptions import ConnectionClosed
        except ImportError:
            logger.warning("websockets package not installed — WS client disabled")
            return

        attempt = 0
        while not self._stop_event.is_set():
            try:
                ws_url = self._build_ws_url()
                headers = {"Authorization": f"Bearer {self._api_key}"}

                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=self._PING_INTERVAL,
                    close_timeout=5,
                ) as ws:
                    self._connected.set()
                    attempt = 0
                    logger.info("WebSocket connected to %s", ws_url)

                    try:
                        async for raw_message in ws:
                            if self._stop_event.is_set():
                                break
                            self._dispatch(raw_message)
                    except ConnectionClosed:
                        logger.debug("WebSocket connection closed")

            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.debug("WebSocket connection failed: %s", e)

            self._connected.clear()

            if self._stop_event.is_set():
                break

            # Exponential backoff with jitter
            delay = min(self._BACKOFF_BASE * (2 ** attempt), self._BACKOFF_MAX)
            delay = delay * (0.5 + random.random() * 0.5)  # jitter: 50-100% of delay
            attempt += 1
            logger.debug("WebSocket reconnecting in %.1fs (attempt %d)", delay, attempt)

            # Use stop_event to allow fast exit during backoff
            self._stop_event.wait(timeout=delay)

    def _dispatch(self, raw_message: Any) -> None:
        """Parse a WS message and dispatch to matching subscribers."""
        try:
            if isinstance(raw_message, bytes):
                raw_message = raw_message.decode("utf-8")
            event = json.loads(raw_message)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug("Ignoring unparseable WS message: %s", e)
            return

        with self._lock:
            subscribers = list(self._subscribers)

        for filter_fn, handler_fn in subscribers:
            try:
                if filter_fn(event):
                    handler_fn(event)
            except Exception:
                logger.debug("Subscriber handler error", exc_info=True)
