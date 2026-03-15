"""
dev_runner/main.py

Staging app host for the vibe coding superstack.

Manages the lifecycle of apps the agent builds:
  - Allocates a port from the dynamic range (8100-8119)
  - Starts the app process inside /workspace
  - Returns the staging URL for human review
  - Tears down on rejection or approval
  - Cleans up zombie processes

The agent calls this via Open WebUI's claude-proxy → dev-runner flow.
You access staging apps via Tailscale on your phone.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dev-runner")

# ── Config ────────────────────────────────────────────────────────────────────
PORT_RANGE_START  = int(os.environ.get("PORT_RANGE_START", "8100"))
PORT_RANGE_END    = int(os.environ.get("PORT_RANGE_END", "8119"))
WORKSPACE_PATH    = os.environ.get("WORKSPACE_PATH", "/workspace")
PUBLIC_BASE_URL   = os.environ.get("PUBLIC_BASE_URL", "https://localhost")
LIGHTPANDA_CDP_URL = os.environ.get("LIGHTPANDA_CDP_URL", "ws://lightpanda:9222")
BROWSER_TIMEOUT    = 15_000  # ms

MAX_CONCURRENT_APPS = 10
MAX_APP_LIFETIME    = 1800  # 30 minutes
STDERR_RING_SIZE    = 200   # max lines of stderr kept per app

# ── Port pool ─────────────────────────────────────────────────────────────────
_available_ports: set[int] = set(range(PORT_RANGE_START, PORT_RANGE_END + 1))
_allocated_ports: Dict[str, int] = {}   # app_id → port

# ── Stderr ring buffer ────────────────────────────────────────────────────────
def _drain_stderr(pipe, ring: collections.deque):
    """Read stderr line-by-line in a daemon thread. Avoids pipe deadlock."""
    try:
        for raw_line in iter(pipe.readline, b""):
            ring.append(raw_line.decode("utf-8", errors="replace").rstrip("\n"))
    except ValueError:
        pass  # pipe closed
    finally:
        pipe.close()


# ── Process registry ──────────────────────────────────────────────────────────
class ManagedApp:
    def __init__(
        self,
        app_id:    str,
        port:      int,
        process:   subprocess.Popen,
        app_dir:   str,
        command:   str,
        started_at: float,
        stderr_ring: collections.deque,
    ):
        self.app_id      = app_id
        self.port        = port
        self.process     = process
        self.app_dir     = app_dir
        self.command     = command
        self.started_at  = started_at
        self.status      = "running"
        self.stderr_ring = stderr_ring

    @property
    def staging_url(self) -> str:
        return f"{PUBLIC_BASE_URL}:{self.port}"

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    def is_alive(self) -> bool:
        return self.process.poll() is None

    @property
    def stderr_tail(self) -> list[str]:
        """Return the last STDERR_RING_SIZE lines of stderr output."""
        return list(self.stderr_ring)


_apps: Dict[str, ManagedApp] = {}

# ── Models ────────────────────────────────────────────────────────────────────
class DeployRequest(BaseModel):
    app_id:          str            = Field(..., description="Unique identifier for this app instance")
    app_dir:         str            = Field(..., description="Path relative to /workspace (e.g. 'my-app')")
    command:         str            = Field(..., description="Start command (e.g. 'uvicorn main:app --port {port}')")
    runtime:         str            = Field("python", description="Runtime: python | node | bash")
    install_command: Optional[str]  = Field(None, description="Optional install step run before start (e.g. 'npm install')")


class DeployResponse(BaseModel):
    app_id:      str
    port:        int
    staging_url: str
    status:      str


class AppStatus(BaseModel):
    app_id:         str
    port:           int
    staging_url:    str
    status:         str
    uptime_seconds: float
    alive:          bool
    exit_code:      Optional[int] = None
    stderr_tail:    list[str] = Field(default_factory=list, description="Last lines of stderr output")


# ── Browser models ───────────────────────────────────────────────────────
class BrowseRequest(BaseModel):
    width:   int = Field(1280, description="Viewport width")
    height:  int = Field(720, description="Viewport height")
    wait_ms: int = Field(2000, description="Wait after load before extraction (ms)", ge=0, le=10000)


class BrowseResponse(BaseModel):
    app_id:      str
    staging_url: str
    title:       str
    html:        str
    text:        str


class ScreenshotRequest(BaseModel):
    width:     int  = Field(1280, description="Viewport width")
    height:    int  = Field(720, description="Viewport height")
    full_page: bool = Field(False, description="Capture full scrollable page")
    wait_ms:   int  = Field(2000, description="Wait after load before capture (ms)", ge=0, le=10000)


class ScreenshotResponse(BaseModel):
    app_id:         str
    staging_url:    str
    screenshot_b64: str
    content_type:   str = "image/png"
    viewport:       dict


class BrowserTestStep(BaseModel):
    action:     str          = Field(..., description="Action: click | fill | assert_text | assert_visible | wait")
    selector:   Optional[str] = Field(None, description="CSS selector for the target element")
    value:      Optional[str] = Field(None, description="Value for fill or expected text for assert_text")
    timeout_ms: int          = Field(5000, description="Timeout for this step in ms")


class BrowserTestRequest(BaseModel):
    steps:  list[BrowserTestStep] = Field(..., description="Ordered list of browser test steps")
    width:  int = Field(1280, description="Viewport width")
    height: int = Field(720, description="Viewport height")


class BrowserTestResult(BaseModel):
    app_id:          str
    staging_url:     str
    passed:          bool
    steps_completed: int
    total_steps:     int
    error:           Optional[str] = None
    final_text:      Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _allocate_port(app_id: str) -> int:
    if not _available_ports:
        raise HTTPException(status_code=503, detail="No staging ports available")
    port = min(_available_ports)
    _available_ports.discard(port)
    _allocated_ports[app_id] = port
    return port


def _release_port(app_id: str):
    port = _allocated_ports.pop(app_id, None)
    if port is not None:
        _available_ports.add(port)


def _resolve_runtime(runtime: str) -> str:
    """Return the executable path for a given runtime name."""
    candidates = {
        "python": ["python3", "python"],
        "node":   ["node"],
        "bash":   ["bash"],
    }
    for binary in candidates.get(runtime, [runtime]):
        path = shutil.which(binary)
        if path:
            return path
    raise HTTPException(
        status_code=400,
        detail=f"Runtime '{runtime}' not found in container",
    )


def _sanitize_command(command: str, port: int) -> str:
    """Inject the allocated port and strip dangerous shell constructs."""
    dangerous = [";", "&&", "||", "`", "$(", ">", "<", "|", "\n", "\r"]
    for char in dangerous:
        if char in command:
            raise HTTPException(
                status_code=400,
                detail=f"Dangerous character in command: {char!r}",
            )
    return command.replace("{port}", str(port))


def _validate_app_dir(app_dir_str: str) -> Path:
    """Resolve app_dir and ensure it stays within WORKSPACE_PATH."""
    workspace_root = Path(WORKSPACE_PATH).resolve()
    app_dir = (workspace_root / app_dir_str).resolve()
    if not str(app_dir).startswith(str(workspace_root)):
        raise HTTPException(
            status_code=400,
            detail="app_dir must be within /workspace (path traversal blocked)",
        )
    if not app_dir.exists():
        raise HTTPException(
            status_code=400,
            detail=f"App directory not found: {app_dir}",
        )
    return app_dir


# ── Suspicious pattern scanner ──────────────────────────────────────────
_SUSPICIOUS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("outbound fetch", re.compile(
        r"""(?:fetch\s*\(|XMLHttpRequest|navigator\.sendBeacon|new\s+Image\s*\(\s*\)\s*\.src\s*=)""",
        re.IGNORECASE,
    )),
    ("external script", re.compile(
        r"""<script[^>]+src\s*=\s*["']https?://""",
        re.IGNORECASE,
    )),
    ("external websocket", re.compile(
        r"""new\s+WebSocket\s*\(\s*["']wss?://(?!localhost|127\.0\.0\.1)""",
        re.IGNORECASE,
    )),
    ("cookie/storage exfil", re.compile(
        r"""(?:document\.cookie|localStorage\.\w+|sessionStorage\.\w+)\s*[+,]""",
        re.IGNORECASE,
    )),
    ("hidden exfil element", re.compile(
        r"""(?:display\s*:\s*none|visibility\s*:\s*hidden)[^}]*(?:src|href)\s*=\s*["']https?://""",
        re.IGNORECASE | re.DOTALL,
    )),
    ("dynamic url construction", re.compile(
        r"""(?:window\.location|document\.location)\s*=\s*[^"';\n]*\+""",
        re.IGNORECASE,
    )),
]

_SCAN_EXTENSIONS = {".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".svelte", ".vue"}
_MAX_SCAN_FILE_SIZE = 1_000_000  # 1 MB


def _scan_for_suspicious_patterns(app_dir: Path) -> list[dict]:
    """Scan app directory for suspicious exfiltration patterns.
    Returns list of findings: [{file, line, pattern_name, match}]."""
    findings = []
    for filepath in app_dir.rglob("*"):
        if not filepath.is_file():
            continue
        if filepath.suffix.lower() not in _SCAN_EXTENSIONS:
            continue
        if filepath.stat().st_size > _MAX_SCAN_FILE_SIZE:
            continue
        # Skip node_modules and common vendored dirs
        parts = filepath.parts
        if "node_modules" in parts or ".git" in parts or "vendor" in parts:
            continue
        try:
            content = filepath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_num, line in enumerate(content.splitlines(), start=1):
            for pattern_name, pattern in _SUSPICIOUS_PATTERNS:
                if pattern.search(line):
                    findings.append({
                        "file": str(filepath.relative_to(app_dir)),
                        "line": line_num,
                        "pattern": pattern_name,
                        "match": line.strip()[:200],
                    })
    return findings


async def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    """Poll until the port is accepting connections or timeout."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=1.0):
                return True
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.5)
    return False


def _terminate_app(app: ManagedApp):
    """Gracefully terminate a managed app process."""
    if app.process.poll() is None:
        try:
            app.process.send_signal(signal.SIGTERM)
            try:
                app.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.process.kill()
                app.process.wait()
        except ProcessLookupError:
            pass
    app.status = "stopped"
    _release_port(app.app_id)
    logger.info("Terminated app %s (port %d)", app.app_id, app.port)


# ── Browser helpers ──────────────────────────────────────────────────────────
_browser_semaphore = asyncio.Semaphore(3)  # max 3 concurrent browser sessions
CDP_CONNECT_RETRIES = 3
CDP_RETRY_BACKOFF   = [1.0, 2.0, 4.0]  # seconds between retries


def _remote_url(managed_app: ManagedApp) -> str:
    """URL to reach the staging app from a separate container (Lightpanda)."""
    return f"http://dev-runner:{managed_app.port}"


def _local_url(managed_app: ManagedApp) -> str:
    """URL to reach the staging app from within this container (local Chromium)."""
    return f"http://localhost:{managed_app.port}"


async def _connect_cdp_with_retry(pw):
    """Connect to Lightpanda CDP with retry + backoff for flaky connections."""
    last_error = None
    for attempt in range(CDP_CONNECT_RETRIES):
        try:
            browser = await pw.chromium.connect_over_cdp(LIGHTPANDA_CDP_URL)
            if attempt > 0:
                logger.info("CDP connection succeeded on attempt %d", attempt + 1)
            return browser
        except Exception as e:
            last_error = e
            if attempt < CDP_CONNECT_RETRIES - 1:
                delay = CDP_RETRY_BACKOFF[attempt]
                logger.warning(
                    "CDP connection attempt %d failed: %s — retrying in %.1fs",
                    attempt + 1, e, delay,
                )
                await asyncio.sleep(delay)
    raise last_error  # type: ignore[misc]


async def _wait_for_body_content(page, timeout_ms: int = 3000):
    """After domcontentloaded, wait for <body> to have non-empty text.
    Helps with SPAs that render client-side. Best-effort — does not raise."""
    try:
        await page.wait_for_function(
            "() => document.body && document.body.textContent.trim().length > 0",
            timeout=timeout_ms,
        )
    except Exception:
        pass  # page may legitimately have empty body; don't fail


async def _with_lightpanda_page(managed_app: ManagedApp, width: int, height: int, callback):
    """Connect to Lightpanda via CDP, run callback(page), cleanup.

    Lightpanda supports only 1 context + 1 page per CDP connection, so we
    create a fresh connection per call (protected by the semaphore).
    """
    async with _browser_semaphore:
        async with async_playwright() as pw:
            browser = await _connect_cdp_with_retry(pw)
            try:
                # Lightpanda ignores viewport (no renderer) — skip setting it
                context = await browser.new_context()
                page = await context.new_page()
                try:
                    await page.goto(
                        _remote_url(managed_app),
                        wait_until="domcontentloaded",
                        timeout=BROWSER_TIMEOUT,
                    )
                    await _wait_for_body_content(page)
                    return await callback(page)
                finally:
                    await page.close()
                    await context.close()
            finally:
                await browser.close()


async def _with_chromium_page(managed_app: ManagedApp, width: int, height: int, callback):
    """Launch local Chromium (for rendering/screenshots), run callback(page), cleanup."""
    async with _browser_semaphore:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(viewport={"width": width, "height": height})
                page = await context.new_page()
                try:
                    await page.goto(
                        _local_url(managed_app),
                        wait_until="networkidle",
                        timeout=BROWSER_TIMEOUT,
                    )
                    return await callback(page)
                finally:
                    await page.close()
                    await context.close()
            finally:
                await browser.close()


def _get_managed_app(app_id: str) -> ManagedApp:
    """Look up a running app or raise appropriate HTTP error."""
    managed = _apps.get(app_id)
    if not managed:
        raise HTTPException(status_code=404, detail=f"App {app_id!r} not found")
    if not managed.is_alive():
        raise HTTPException(status_code=409, detail=f"App {app_id!r} is not running")
    return managed


def _sanitize_browser_error(error: Exception) -> str:
    """Strip internal hostnames/URLs from browser error messages."""
    msg = str(error)
    msg = re.sub(r"http://(?:dev-runner|localhost|127\.0\.0\.1):\d+", "<staging-app>", msg)
    msg = re.sub(r"ws://[^/\s]+:\d+", "<cdp-endpoint>", msg)
    return msg


_VALID_SELECTOR_RE = re.compile(r"^[a-zA-Z0-9\s\-_.*#\[\]=:()\"',>+~^$|@]+$")


def _validate_selector(selector: Optional[str], action: str):
    """Validate CSS selector to prevent injection via Playwright selectors."""
    if selector is None:
        raise HTTPException(status_code=400, detail=f"Selector required for '{action}' action")
    if len(selector) > 500:
        raise HTTPException(status_code=400, detail="Selector too long (max 500 chars)")
    if not _VALID_SELECTOR_RE.match(selector):
        raise HTTPException(status_code=400, detail=f"Invalid characters in selector: {selector[:100]}")


# ── Background cleanup task ───────────────────────────────────────────────────
async def _cleanup_loop():
    """Periodically reap dead processes, enforce max lifetime, release ports."""
    while True:
        await asyncio.sleep(30)
        to_remove = []
        for app_id, managed in _apps.items():
            if not managed.is_alive() and managed.status == "running":
                to_remove.append((app_id, "exited unexpectedly"))
            elif managed.uptime_seconds > MAX_APP_LIFETIME and managed.status == "running":
                to_remove.append((app_id, f"exceeded {MAX_APP_LIFETIME}s lifetime"))
        for app_id, reason in to_remove:
            managed = _apps.pop(app_id)
            stderr_snippet = managed.stderr_tail[-10:]
            _terminate_app(managed)
            logger.warning(
                "App %s removed: %s (port %d released)%s",
                app_id, reason, managed.port,
                f" | last stderr: {stderr_snippet}" if stderr_snippet else "",
            )


# ── App ───────────────────────────────────────────────────────────────────────
_cleanup_task: Optional[asyncio.Task] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task
    _cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info(
        "Dev-runner ready | ports %d-%d | workspace=%s",
        PORT_RANGE_START, PORT_RANGE_END, WORKSPACE_PATH,
    )
    yield
    _cleanup_task.cancel()
    try:
        await _cleanup_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Dev Runner",
    description="Staging app lifecycle manager for vibe coding superstack",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status":          "ok",
        "available_ports": len(_available_ports),
        "running_apps":    len(_apps),
    }


@app.get("/push-status")
async def push_status():
    """Return the last git push status written by workspace-watchdog."""
    import json as _json
    status_file = Path(WORKSPACE_PATH) / ".push-status.json"
    if not status_file.exists():
        return {"status": "unknown", "message": "No push attempts recorded yet"}
    try:
        return _json.loads(status_file.read_text())
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read push status: {e}")


@app.post("/deploy", response_model=DeployResponse)
async def deploy(req: DeployRequest):
    """
    Deploy a staging app from /workspace.
    Returns the staging URL for phone-based review.
    """
    # Tear down existing app with same ID if re-deploying
    if req.app_id in _apps:
        existing = _apps.pop(req.app_id)
        _terminate_app(existing)
        logger.info("Re-deploy: terminated previous instance of %s", req.app_id)

    if len(_apps) >= MAX_CONCURRENT_APPS:
        raise HTTPException(
            status_code=503,
            detail=f"Max concurrent apps ({MAX_CONCURRENT_APPS}) reached — tear down an existing app first",
        )

    # Resolve and validate app directory (blocks path traversal)
    app_dir = _validate_app_dir(req.app_dir)

    # Security scan — block suspicious exfiltration patterns
    findings = _scan_for_suspicious_patterns(app_dir)
    if findings:
        logger.warning(
            "Deploy BLOCKED for %s — %d suspicious pattern(s) found",
            req.app_id, len(findings),
        )
        raise HTTPException(
            status_code=403,
            detail={
                "message": f"Deploy blocked — {len(findings)} suspicious pattern(s) detected",
                "findings": findings[:20],
            },
        )

    # Run optional install command (e.g. "npm install") before allocating a port
    if req.install_command:
        install_cmd = _sanitize_command(req.install_command, 0)  # no port needed
        logger.info("Running install step for %s: %s", req.app_id, install_cmd)
        try:
            result = subprocess.run(
                shlex.split(install_cmd),
                cwd=str(app_dir),
                capture_output=True,
                timeout=120,
                env={**os.environ},
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=504,
                detail=f"Install command timed out after 120s: {install_cmd}",
            )
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Install command not found: {e}",
            )
        if result.returncode != 0:
            stderr_out = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise HTTPException(
                status_code=422,
                detail={
                    "message": f"Install command failed (exit {result.returncode}): {install_cmd}",
                    "stderr": stderr_out,
                },
            )
        logger.info("Install step completed for %s (exit 0)", req.app_id)

    port = _allocate_port(req.app_id)

    try:
        runtime = _resolve_runtime(req.runtime)
        command = _sanitize_command(req.command, port)
    except Exception:
        _release_port(req.app_id)
        raise

    logger.info(
        "Deploying app %s | dir=%s | port=%d | cmd=%s",
        req.app_id, app_dir, port, command,
    )

    # Build argv: parse with shlex, resolve runtime binary
    args = shlex.split(command)
    resolved = shutil.which(args[0]) if args else None
    if resolved == runtime:
        args[0] = runtime
    else:
        args = [runtime] + args

    # Start the process — stdout to /dev/null, stderr piped to ring buffer
    stderr_ring: collections.deque = collections.deque(maxlen=STDERR_RING_SIZE)
    try:
        process = subprocess.Popen(
            args,
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "PORT": str(port),
                # Bind to all interfaces so Lightpanda container can reach the app.
                # Different frameworks check different env vars, so set them all.
                "HOST": "0.0.0.0",
                "BIND_ADDR": "0.0.0.0",
                "HOSTNAME": "0.0.0.0",
                "LISTEN_ADDR": f"0.0.0.0:{port}",
            },
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as e:
        _release_port(req.app_id)
        raise HTTPException(status_code=500, detail=f"Failed to start process: {e}")

    # Drain stderr in a daemon thread to avoid pipe deadlock
    t = threading.Thread(target=_drain_stderr, args=(process.stderr, stderr_ring), daemon=True)
    t.start()

    managed = ManagedApp(
        app_id=req.app_id,
        port=port,
        process=process,
        app_dir=str(app_dir),
        command=command,
        started_at=time.time(),
        stderr_ring=stderr_ring,
    )
    _apps[req.app_id] = managed

    # Wait for port to be ready
    ready = await _wait_for_port(port, timeout=30.0)
    if not ready:
        stderr_snapshot = managed.stderr_tail
        _terminate_app(managed)
        _apps.pop(req.app_id, None)
        raise HTTPException(
            status_code=504,
            detail={
                "message": f"App {req.app_id} did not bind to port {port} within 30s",
                "stderr_tail": stderr_snapshot[-50:],
            },
        )

    logger.info("App %s ready at %s", req.app_id, managed.staging_url)

    return DeployResponse(
        app_id=req.app_id,
        port=port,
        staging_url=managed.staging_url,
        status="running",
    )


@app.get("/status/{app_id}", response_model=AppStatus)
async def get_status(app_id: str):
    app_instance = _apps.get(app_id)
    if not app_instance:
        raise HTTPException(status_code=404, detail=f"App {app_id!r} not found")

    return AppStatus(
        app_id=app_instance.app_id,
        port=app_instance.port,
        staging_url=app_instance.staging_url,
        status=app_instance.status,
        uptime_seconds=app_instance.uptime_seconds,
        alive=app_instance.is_alive(),
        exit_code=app_instance.process.poll(),
        stderr_tail=app_instance.stderr_tail[-50:],
    )


@app.delete("/teardown/{app_id}")
async def teardown(app_id: str):
    """Tear down a staging app — called on approval or rejection."""
    app_instance = _apps.pop(app_id, None)
    if not app_instance:
        raise HTTPException(status_code=404, detail=f"App {app_id!r} not found")

    _terminate_app(app_instance)
    logger.info("Torn down app %s on request", app_id)

    return {"status": "torn_down", "app_id": app_id}


@app.get("/list")
async def list_apps():
    return {
        app_id: {
            "port":           a.port,
            "staging_url":    a.staging_url,
            "status":         a.status,
            "uptime_seconds": a.uptime_seconds,
            "alive":          a.is_alive(),
        }
        for app_id, a in _apps.items()
    }


# ── Browser endpoints ────────────────────────────────────────────────────────

@app.post("/browse/{app_id}", response_model=BrowseResponse)
async def browse(app_id: str, req: BrowseRequest = BrowseRequest()):
    """Navigate to a staging app and return its DOM content."""
    managed = _get_managed_app(app_id)

    try:
        async def extract(page):
            if req.wait_ms > 0:
                await page.wait_for_timeout(req.wait_ms)
            title = await page.title()
            html = await page.content()
            text = await page.evaluate("() => document.body ? document.body.textContent : ''")
            return title, html, text

        title, html, text = await _with_lightpanda_page(managed, req.width, req.height, extract)
        return BrowseResponse(
            app_id=app_id,
            staging_url=managed.staging_url,
            title=title,
            html=html[:500_000],
            text=(text or "")[:100_000],
        )
    except PlaywrightTimeout:
        raise HTTPException(status_code=504, detail="Browser timed out loading the page")
    except Exception as e:
        logger.error("Browse failed for %s: %s", app_id, e)
        raise HTTPException(status_code=502, detail=f"Browser error: {_sanitize_browser_error(e)}")


@app.post("/screenshot/{app_id}", response_model=ScreenshotResponse)
async def screenshot(app_id: str, req: ScreenshotRequest = ScreenshotRequest()):
    """Take a rendered screenshot of a staging app (uses local Chromium)."""
    managed = _get_managed_app(app_id)

    try:
        async def capture(page):
            if req.wait_ms > 0:
                await page.wait_for_timeout(req.wait_ms)
            png_bytes = await page.screenshot(full_page=req.full_page)
            return base64.b64encode(png_bytes).decode("ascii")

        b64 = await _with_chromium_page(managed, req.width, req.height, capture)
        return ScreenshotResponse(
            app_id=app_id,
            staging_url=managed.staging_url,
            screenshot_b64=b64,
            viewport={"width": req.width, "height": req.height},
        )
    except PlaywrightTimeout:
        raise HTTPException(status_code=504, detail="Browser timed out loading the page")
    except Exception as e:
        logger.error("Screenshot failed for %s: %s", app_id, e)
        raise HTTPException(status_code=502, detail=f"Browser error: {_sanitize_browser_error(e)}")


@app.post("/browser-test/{app_id}", response_model=BrowserTestResult)
async def browser_test(app_id: str, req: BrowserTestRequest):
    """Run a sequence of browser actions against a staging app."""
    managed = _get_managed_app(app_id)

    # Validate selectors upfront before launching a browser session
    for i, step in enumerate(req.steps):
        if step.action in ("click", "fill", "assert_text", "assert_visible"):
            _validate_selector(step.selector, step.action)

    try:
        async def run_steps(page):
            for i, step in enumerate(req.steps):
                try:
                    if step.action == "click":
                        # Use JS click fallback — Lightpanda may not support coordinate-based clicks
                        try:
                            await page.click(step.selector, timeout=step.timeout_ms)
                        except Exception as click_err:
                            logger.warning(
                                "Step %d: native click failed (%s), falling back to JS click for %r",
                                i, click_err, step.selector,
                            )
                            await page.evaluate(
                                "(sel) => { const el = document.querySelector(sel); if (el) el.click(); else throw new Error('Element not found: ' + sel); }",
                                step.selector,
                            )
                    elif step.action == "fill":
                        # Use JS value assignment as fallback for Lightpanda
                        try:
                            await page.fill(step.selector, step.value or "", timeout=step.timeout_ms)
                        except Exception as fill_err:
                            logger.warning(
                                "Step %d: native fill failed (%s), falling back to JS fill for %r",
                                i, fill_err, step.selector,
                            )
                            await page.evaluate(
                                "([sel, val]) => { const el = document.querySelector(sel); if (el) { el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); } else throw new Error('Element not found: ' + sel); }",
                                [step.selector, step.value or ""],
                            )
                    elif step.action == "assert_text":
                        locator = page.locator(step.selector)
                        await locator.wait_for(timeout=step.timeout_ms)
                        text = await locator.text_content()
                        if step.value not in (text or ""):
                            return (False, i, f"Step {i}: expected '{step.value}' in '{text}'", None)
                    elif step.action == "assert_visible":
                        await page.wait_for_selector(step.selector, state="visible", timeout=step.timeout_ms)
                    elif step.action == "wait":
                        await page.wait_for_timeout(step.timeout_ms)
                    else:
                        return (False, i, f"Step {i}: unknown action '{step.action}'", None)
                except Exception as e:
                    return (False, i, f"Step {i} ({step.action}): {_sanitize_browser_error(e)}", None)
            # All passed — capture final page text (use textContent, not innerText)
            final_text = await page.evaluate("() => document.body ? document.body.textContent : ''")
            return (True, len(req.steps), None, (final_text or "")[:50_000])

        passed, completed, error, final_text = await _with_lightpanda_page(
            managed, req.width, req.height, run_steps,
        )
        return BrowserTestResult(
            app_id=app_id,
            staging_url=managed.staging_url,
            passed=passed,
            steps_completed=completed,
            total_steps=len(req.steps),
            error=error,
            final_text=final_text,
        )
    except PlaywrightTimeout:
        raise HTTPException(status_code=504, detail="Browser timed out loading the page")
    except Exception as e:
        logger.error("Browser test failed for %s: %s", app_id, e)
        raise HTTPException(status_code=502, detail=f"Browser error: {_sanitize_browser_error(e)}")
