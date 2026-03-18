"""
Thread-safe OpenSandbox pool manager for Genesia.

Manages a pool of warm sandbox containers. Workers acquire a sandbox,
execute code or commands, then release it back to the pool.

Uses the opensandbox Python SDK (synchronous path via asyncio.run()).
Falls back gracefully if the SDK is unavailable.
"""

import asyncio
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from .config import SandboxConfig

logger = logging.getLogger(__name__)

# Lazy import — the opensandbox SDK is optional
_opensandbox_available = None


def _check_sdk() -> bool:
    """Check if the opensandbox SDK is installed."""
    global _opensandbox_available
    if _opensandbox_available is None:
        try:
            import opensandbox  # noqa: F401
            _opensandbox_available = True
        except ImportError:
            _opensandbox_available = False
    return _opensandbox_available


@dataclass
class SandboxHandle:
    """Wrapper around an opensandbox.Sandbox instance."""

    sandbox_id: str
    sandbox: Any  # opensandbox.Sandbox
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    def touch(self) -> None:
        self.last_used = time.monotonic()


class SandboxPoolManager:
    """Thread-safe pool of warm OpenSandbox containers.

    Lifecycle:
        pool = SandboxPoolManager(config)
        pool.start()      # Pre-warm containers, start maintenance
        ...
        result = pool.execute_in_sandbox(code)
        ...
        pool.stop()        # Kill all containers, stop threads
    """

    def __init__(self, config: SandboxConfig, lazy: bool = False):
        self.config = config
        self._lazy = lazy
        self._pool: queue.Queue[SandboxHandle] = queue.Queue()
        self._all_handles: list[SandboxHandle] = []
        self._lock = threading.Lock()
        self._maintenance_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = False
        self._warmed = False
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

    # ── Lifecycle ───────────────────────────────────────────────

    def start(self) -> None:
        """Start the sandbox pool.

        When ``lazy=True`` was passed to __init__, only the event loop
        and maintenance thread are started.  Container pre-warming is
        deferred to the first ``_acquire()`` call, cutting 10-30 s off
        the startup critical path.
        """
        if self._started:
            return

        if not _check_sdk():
            raise RuntimeError(
                "opensandbox SDK not installed. "
                "Install with: pip install opensandbox"
            )

        # Start a dedicated event loop thread for async SDK calls
        self._event_loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._event_loop.run_forever,
            daemon=True,
            name="sandbox-event-loop",
        )
        self._loop_thread.start()

        if not self._lazy:
            self._warm_pool()

        # Start maintenance thread (TTL cleanup, pool replenishment)
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            daemon=True,
            name="sandbox-maintenance",
        )
        self._maintenance_thread.start()

        self._started = True
        if self._lazy:
            logger.info("Sandbox pool started (lazy — containers deferred to first use)")
        else:
            logger.info(
                f"Sandbox pool started: {self._pool.qsize()} warm containers"
            )

    def _warm_pool(self) -> None:
        """Pre-warm pool containers.  Called eagerly or on first acquire."""
        if self._warmed:
            return
        self._warmed = True

        logger.info(
            f"Pre-warming sandbox pool ({self.config.pool_size} containers)..."
        )
        for i in range(self.config.pool_size):
            try:
                handle = self._create_sandbox()
                self._pool.put(handle)
                with self._lock:
                    self._all_handles.append(handle)
                logger.debug(f"Sandbox {i+1}/{self.config.pool_size} ready")
            except Exception as e:
                logger.warning(f"Failed to pre-warm sandbox {i+1}: {e}")

    def stop(self) -> None:
        """Kill all sandbox containers and stop maintenance."""
        if not self._started:
            return

        self._stop_event.set()

        if self._maintenance_thread:
            self._maintenance_thread.join(timeout=5)

        # Kill all tracked sandboxes
        with self._lock:
            handles = list(self._all_handles)
            self._all_handles.clear()

        for handle in handles:
            try:
                self._kill_sandbox(handle)
            except Exception as e:
                logger.debug(f"Error killing sandbox {handle.sandbox_id}: {e}")

        # Drain the queue
        while not self._pool.empty():
            try:
                self._pool.get_nowait()
            except queue.Empty:
                break

        # Stop the event loop
        if self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        if self._loop_thread:
            self._loop_thread.join(timeout=5)

        self._started = False
        logger.info("Sandbox pool stopped")

    # ── Public execution methods ────────────────────────────────

    def execute_in_sandbox(
        self, code: str, timeout: int = 30
    ) -> "ToolResult":
        """Execute Python code inside a sandbox container.

        Acquires a sandbox from the pool, writes the code to a temp file,
        runs it, and returns the result as a ToolResult.
        """
        from ..tools import ToolResult

        handle = self._acquire()
        try:
            # Write code to temp file inside sandbox
            script_path = f"/tmp/genesia_{uuid.uuid4().hex[:8]}.py"
            self._run_async(
                handle.sandbox.files.write_file(script_path, code)
            )

            # Execute the script
            result = self._run_async(
                handle.sandbox.commands.run(
                    f"python3 {script_path}",
                    timeout=timeout,
                )
            )

            handle.touch()

            return ToolResult(
                success=(result.exit_code == 0),
                output=result.stdout or "",
                error=result.stderr if result.exit_code != 0 else None,
                metadata={
                    "exit_code": result.exit_code,
                    "sandbox_id": handle.sandbox_id,
                    "sandboxed": True,
                },
            )
        except Exception as e:
            logger.error(f"Sandbox execution failed: {e}")
            return ToolResult(
                success=False,
                output="",
                error=f"Sandbox execution error: {e}",
                metadata={"sandboxed": True},
            )
        finally:
            self._release(handle)

    def run_command(
        self, command: str, timeout: int = 60
    ) -> "ToolResult":
        """Run a shell command inside a sandbox container."""
        from ..tools import ToolResult

        handle = self._acquire()
        try:
            result = self._run_async(
                handle.sandbox.commands.run(command, timeout=timeout)
            )

            handle.touch()

            return ToolResult(
                success=(result.exit_code == 0),
                output=result.stdout or "",
                error=result.stderr if result.exit_code != 0 else None,
                metadata={
                    "exit_code": result.exit_code,
                    "sandbox_id": handle.sandbox_id,
                    "sandboxed": True,
                },
            )
        except Exception as e:
            logger.error(f"Sandbox command failed: {e}")
            return ToolResult(
                success=False,
                output="",
                error=f"Sandbox command error: {e}",
                metadata={"sandboxed": True},
            )
        finally:
            self._release(handle)

    # ── Internal methods ────────────────────────────────────────

    def _acquire(self) -> SandboxHandle:
        """Get a sandbox from the pool, or create one on demand."""
        # Lazy warm-up: defer container creation until first actual use
        if not self._warmed:
            self._warm_pool()
        try:
            handle = self._pool.get(timeout=5)
            # Check if sandbox is still fresh
            if handle.age_seconds > self.config.sandbox_timeout:
                logger.debug(
                    f"Sandbox {handle.sandbox_id} expired, creating new one"
                )
                self._kill_sandbox(handle)
                with self._lock:
                    if handle in self._all_handles:
                        self._all_handles.remove(handle)
                handle = self._create_sandbox()
                with self._lock:
                    self._all_handles.append(handle)
            return handle
        except queue.Empty:
            logger.info("Pool exhausted, creating sandbox on demand")
            handle = self._create_sandbox()
            with self._lock:
                self._all_handles.append(handle)
            return handle

    def _release(self, handle: SandboxHandle) -> None:
        """Return a sandbox to the pool."""
        if self._stop_event.is_set():
            self._kill_sandbox(handle)
            return
        self._pool.put(handle)

    def _create_sandbox(self) -> SandboxHandle:
        """Create a new sandbox container via OpenSandbox SDK."""
        from opensandbox import Sandbox
        from opensandbox.config import ConnectionConfig

        config = ConnectionConfig(
            domain=self.config.server_url,
            api_key=self.config.api_key or None,
            request_timeout=timedelta(seconds=30),
        )

        image = (
            self.config.gpu_sandbox_image
            if self.config.gpu_enabled
            else self.config.sandbox_image
        )

        env = {"PYTHON_VERSION": "3.11"}

        # Pass GPU device IDs if enabled
        if self.config.gpu_enabled and self.config.gpu_device_ids:
            env["NVIDIA_VISIBLE_DEVICES"] = self.config.gpu_device_ids

        sandbox = self._run_async(
            Sandbox.create(
                image,
                connection_config=config,
                entrypoint=["/opt/opensandbox/code-interpreter.sh"],
                env=env,
                timeout=timedelta(seconds=self.config.sandbox_timeout),
            )
        )

        sandbox_id = getattr(sandbox, "id", uuid.uuid4().hex[:12])
        logger.debug(f"Created sandbox {sandbox_id}")

        return SandboxHandle(
            sandbox_id=str(sandbox_id),
            sandbox=sandbox,
        )

    def _kill_sandbox(self, handle: SandboxHandle) -> None:
        """Terminate a sandbox container."""
        try:
            self._run_async(handle.sandbox.kill())
        except Exception as e:
            logger.debug(f"Kill sandbox {handle.sandbox_id} error: {e}")

    def _run_async(self, coro: Any) -> Any:
        """Run an async coroutine on the dedicated event loop thread."""
        if self._event_loop is None or not self._event_loop.is_running():
            # Fallback: use asyncio.run() directly (blocks)
            return asyncio.run(coro)
        future = asyncio.run_coroutine_threadsafe(coro, self._event_loop)
        return future.result(timeout=60)

    def _maintenance_loop(self) -> None:
        """Background thread: recycle expired sandboxes, replenish pool."""
        while not self._stop_event.wait(timeout=30):
            try:
                self._recycle_expired()
                self._replenish_pool()
            except Exception as e:
                logger.debug(f"Maintenance error: {e}")

    def _recycle_expired(self) -> None:
        """Kill sandboxes that have exceeded their TTL."""
        fresh: list[SandboxHandle] = []
        expired: list[SandboxHandle] = []

        # Drain and sort
        while not self._pool.empty():
            try:
                handle = self._pool.get_nowait()
                if handle.age_seconds > self.config.sandbox_timeout:
                    expired.append(handle)
                else:
                    fresh.append(handle)
            except queue.Empty:
                break

        # Kill expired
        for handle in expired:
            self._kill_sandbox(handle)
            with self._lock:
                if handle in self._all_handles:
                    self._all_handles.remove(handle)

        # Return fresh ones
        for handle in fresh:
            self._pool.put(handle)

        if expired:
            logger.debug(f"Recycled {len(expired)} expired sandbox(es)")

    def _replenish_pool(self) -> None:
        """Create new sandboxes if pool is below target size."""
        current = self._pool.qsize()
        target = self.config.pool_size

        if current >= target:
            return

        needed = target - current
        for _ in range(needed):
            try:
                handle = self._create_sandbox()
                self._pool.put(handle)
                with self._lock:
                    self._all_handles.append(handle)
            except Exception as e:
                logger.debug(f"Failed to replenish pool: {e}")
                break
