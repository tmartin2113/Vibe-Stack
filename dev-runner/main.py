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
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
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

MAX_CONCURRENT_APPS = 10
MAX_APP_LIFETIME    = 1800  # 30 minutes

# ── Port pool ─────────────────────────────────────────────────────────────────
_available_ports: set[int] = set(range(PORT_RANGE_START, PORT_RANGE_END + 1))
_allocated_ports: Dict[str, int] = {}   # app_id → port

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
    ):
        self.app_id     = app_id
        self.port       = port
        self.process    = process
        self.app_dir    = app_dir
        self.command    = command
        self.started_at = started_at
        self.status     = "running"

    @property
    def staging_url(self) -> str:
        return f"{PUBLIC_BASE_URL}:{self.port}"

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    def is_alive(self) -> bool:
        return self.process.poll() is None


_apps: Dict[str, ManagedApp] = {}

# ── Models ────────────────────────────────────────────────────────────────────
class DeployRequest(BaseModel):
    app_id:    str  = Field(..., description="Unique identifier for this app instance")
    app_dir:   str  = Field(..., description="Path relative to /workspace (e.g. 'my-app')")
    command:   str  = Field(..., description="Start command (e.g. 'uvicorn main:app --port {port}')")
    runtime:   str  = Field("python", description="Runtime: python | node | bash")


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
            _terminate_app(managed)
            logger.warning("App %s removed: %s (port %d released)", app_id, reason, managed.port)


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

    # Start the process — stdout to /dev/null to avoid pipe deadlock
    try:
        process = subprocess.Popen(
            args,
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PORT": str(port)},
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as e:
        _release_port(req.app_id)
        raise HTTPException(status_code=500, detail=f"Failed to start process: {e}")

    managed = ManagedApp(
        app_id=req.app_id,
        port=port,
        process=process,
        app_dir=str(app_dir),
        command=command,
        started_at=time.time(),
    )
    _apps[req.app_id] = managed

    # Wait for port to be ready
    ready = await _wait_for_port(port, timeout=30.0)
    if not ready:
        _terminate_app(managed)
        _apps.pop(req.app_id, None)
        raise HTTPException(
            status_code=504,
            detail=f"App {req.app_id} did not bind to port {port} within 30s",
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
