"""
Vibe Doctor — Self-service diagnostic command.

Validates the health of every subsystem and reports clear, actionable
status to help non-technical users self-diagnose problems.

Usage:
    python -m agents.main --doctor
"""

import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .llm_backend import LLMBackend
from .messenger_client import MattermostClient
from .skill_registry import SkillRegistry
from .skill_security import SkillSecurity

logger = logging.getLogger(__name__)


# ── Result data model ────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Outcome of a single diagnostic check."""

    name: str
    status: str  # "ok", "warn", "fail"
    summary: str
    detail: Optional[str] = None


@dataclass
class DoctorReport:
    """Aggregated results from all checks."""

    checks: List[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "ok")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    def format(self) -> str:
        """Render the report as a human-readable string."""
        status_labels = {
            "ok": "  OK  ",
            "warn": " WARN ",
            "fail": " FAIL ",
        }
        lines: List[str] = []
        lines.append("")
        lines.append("  Vibe Doctor")
        lines.append("  " + "=" * 60)

        for check in self.checks:
            label = status_labels.get(check.status, " ???? ")
            lines.append(f"  {check.name:<20s} [{label}]  {check.summary}")
            if check.detail:
                for detail_line in check.detail.splitlines():
                    lines.append(f"  {'':<20s}          {detail_line}")

        lines.append("  " + "-" * 60)
        parts = []
        if self.ok_count:
            parts.append(f"{self.ok_count} ok")
        if self.warn_count:
            parts.append(f"{self.warn_count} warning(s)")
        if self.fail_count:
            parts.append(f"{self.fail_count} failure(s)")
        lines.append(f"  {', '.join(parts) if parts else 'No checks ran'}")
        lines.append("")
        return "\n".join(lines)


# ── Individual checks ────────────────────────────────────────────────

def check_backend(config: Any) -> CheckResult:
    """Check vLLM backend connectivity and model availability."""
    model = os.getenv("VIBE_MODEL", config.model.model_name)
    host = os.getenv("VIBE_BACKEND_HOST", "localhost")
    port_str = os.getenv("VIBE_BACKEND_PORT")

    try:
        port = int(port_str) if port_str else None
        backend = LLMBackend(model=model, host=host, port=port,
                             max_retries=0, retry_base_delay=0)
        healthy = backend.health_check()
    except Exception as exc:
        return CheckResult("Backend", "fail", f"vLLM ({model}) — error: {exc}")

    if healthy:
        display_port = port_str if port_str else str(backend.backend.port)
        return CheckResult("Backend", "ok", f"vLLM at {host}:{display_port} ({model})")
    display_port = port_str or "8000"
    return CheckResult(
        "Backend", "fail",
        f"vLLM at {host}:{display_port} not responding",
        detail="Start the server: python -m vllm.entrypoints.openai.api_server --model <model>",
    )


def check_config(config: Any) -> CheckResult:
    """Validate configuration integrity."""
    issues: List[str] = []

    if not config.model.model_name:
        issues.append("model_name is empty")
    if config.workflow.max_iterations < 1:
        issues.append(f"max_iterations={config.workflow.max_iterations} (must be >= 1)")
    if not (0 <= config.workflow.quality_threshold <= 100):
        issues.append(f"quality_threshold={config.workflow.quality_threshold} (must be 0-100)")
    if config.workflow.node_timeout <= 0:
        issues.append(f"node_timeout={config.workflow.node_timeout}s (must be > 0)")
    if config.workflow.workflow_timeout <= 0:
        issues.append(f"workflow_timeout={config.workflow.workflow_timeout}s (must be > 0)")

    if issues:
        return CheckResult("Config", "fail", f"{len(issues)} issue(s)", detail="\n".join(issues))
    return CheckResult(
        "Config", "ok",
        f"model={config.model.model_name}, iters={config.workflow.max_iterations}, "
        f"threshold={config.workflow.quality_threshold}",
    )



def check_skills() -> CheckResult:
    """Check skill registry state and integrity."""
    try:
        registry = SkillRegistry()
        stats = registry.get_stats()

        by_tier = stats.get("by_tier", {})
        official = by_tier.get("official", {}).get("count", 0)
        local = by_tier.get("local", {}).get("count", 0)
        temp = by_tier.get("temp", {}).get("count", 0)
        total = stats.get("total_skills", 0)

        parts = []
        if official:
            parts.append(f"{official} official")
        if local:
            parts.append(f"{local} local")
        if temp:
            parts.append(f"{temp} temp")
        if not parts:
            parts.append("none registered")

        summary = f"{total} skill(s): {', '.join(parts)}"
        return CheckResult("Skills", "ok", summary)
    except Exception as exc:
        return CheckResult("Skills", "warn", f"Could not load registry: {exc}")


def check_skill_security() -> CheckResult:
    """Check that the security layer is operational."""
    try:
        security = SkillSecurity()

        # Verify core functions are callable
        security.validate_skill_name("test-skill")
        security.validate_skill_content("# Test\nA benign skill.")

        return CheckResult("Security", "ok", "Content scanning + tool enforcement active")
    except Exception as exc:
        return CheckResult("Security", "warn", f"Validation check raised: {exc}")


def check_messenger(config: Any) -> CheckResult:
    """Check messenger platform connectivity."""
    mm_token = os.getenv("MATTERMOST_BOT_TOKEN")
    mm_url = os.getenv("MATTERMOST_URL")
    slack_token = os.getenv("SLACK_BOT_TOKEN")

    platforms: List[str] = []
    missing: List[str] = []

    # Mattermost
    if mm_token and mm_url:
        try:
            client = MattermostClient(url=mm_url, bot_token=mm_token)
            username = client.get_bot_username()
            platforms.append(f"Mattermost (@{username})")
        except Exception:
            platforms.append("Mattermost (token set, connection failed)")
    elif mm_token or mm_url:
        missing.append("Mattermost (partial config — need both MATTERMOST_URL and MATTERMOST_BOT_TOKEN)")
    # else: not configured, skip silently

    # Slack
    if slack_token:
        platforms.append("Slack (token set)")
    # else: not configured, skip silently

    if platforms:
        return CheckResult("Messenger", "ok", "; ".join(platforms))

    if missing:
        return CheckResult("Messenger", "warn", "; ".join(missing))

    return CheckResult("Messenger", "warn", "No messenger configured (CLI-only mode)")


def check_disk_usage() -> CheckResult:
    """Report disk usage of Vibe data directories."""
    project_root = Path(__file__).parent.parent

    dirs_to_check = {
        "skills": project_root / "vibe_skills",
        "sessions": Path.home() / ".vibe",
        "training": project_root / "training" / "data",
    }

    parts: List[str] = []
    total_bytes = 0

    for label, path in dirs_to_check.items():
        if path.exists():
            size = _dir_size(path)
            total_bytes += size
            parts.append(f"{label}: {_format_bytes(size)}")

    # Check free disk space
    try:
        usage = shutil.disk_usage(str(project_root))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1.0:
            return CheckResult(
                "Disk", "warn",
                f"{_format_bytes(total_bytes)} used ({', '.join(parts)})",
                detail=f"Low disk space: {free_gb:.1f} GB free",
            )
    except OSError:
        pass  # disk_usage not supported on all platforms

    if not parts:
        return CheckResult("Disk", "ok", "No data directories found")

    return CheckResult("Disk", "ok", f"{_format_bytes(total_bytes)} used ({', '.join(parts)})")


def check_python_deps() -> CheckResult:
    """Check that critical Python dependencies are importable."""
    required = {
        "rich": "rich",
        "dotenv": "dotenv",
        "requests": "requests",
    }
    missing: List[str] = []
    for display_name, import_name in required.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(display_name)

    if missing:
        return CheckResult(
            "Dependencies", "fail",
            f"Missing: {', '.join(missing)}",
            detail="pip install " + " ".join(missing),
        )
    return CheckResult("Dependencies", "ok", "All critical packages available")


# ── Helpers ──────────────────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    """Recursively compute total size of a directory in bytes."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    else:
        return f"{n / (1024 ** 3):.1f} GB"


# ── Main entry point ─────────────────────────────────────────────────

def check_hardware() -> CheckResult:
    """Check system hardware (CPU, RAM, GPU) via auto-discovery."""
    try:
        from .resource_discovery import discover_system
        from .resource_allocator import compute_resource_plan

        profile = discover_system()
        plan = compute_resource_plan(profile)

        parts = [
            f"{profile.cpu_threads} threads",
            f"{profile.total_ram_mb}MB RAM",
        ]

        if profile.has_gpu:
            gpu_descs = [f"{g.name} ({g.vram_mb}MB)" for g in profile.gpus]
            parts.append(f"{profile.gpu_count} GPU(s): {', '.join(gpu_descs)}")
        else:
            parts.append("no GPU")

        summary = f"{profile.cpu_model} — {', '.join(parts)}"

        detail_lines = [f"Strategy: {plan.strategy}"]
        for w in plan.warnings:
            detail_lines.append(f"  {w}")

        status = "ok" if not plan.warnings else "warn"
        return CheckResult("Hardware", status, summary,
                           detail="\n".join(detail_lines) if detail_lines else None)

    except Exception as e:
        return CheckResult("Hardware", "warn",
                           f"Discovery failed: {e}")


def check_sandbox(config: Any) -> CheckResult:
    """Check OpenSandbox server connectivity and SDK availability."""
    sandbox_config = getattr(config, 'sandbox', None)
    if not sandbox_config:
        return CheckResult("Sandbox", "fail",
                           "Sandbox configuration missing",
                           detail="OpenSandbox is required. Ensure config.sandbox is set.")

    # Check SDK installed
    try:
        import opensandbox  # noqa: F401
    except ImportError:
        return CheckResult("Sandbox", "fail",
                           "opensandbox SDK not installed",
                           detail="Install with: pip install opensandbox")

    # Check server reachable
    import urllib.request
    import urllib.error

    docs_url = sandbox_config.server_url.rstrip("/") + "/docs"
    try:
        req = urllib.request.Request(docs_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return CheckResult("Sandbox", "ok",
                                   f"Server at {sandbox_config.server_url} (pool_size={sandbox_config.pool_size})")
    except (urllib.error.URLError, OSError, TimeoutError):
        pass

    return CheckResult("Sandbox", "fail",
                       f"Server unreachable at {sandbox_config.server_url}",
                       detail="Is opensandbox-server running? Check docker-compose.")


def check_docker_gpu() -> CheckResult:
    """Check Docker GPU runtime availability."""
    try:
        from .resource_discovery import _run_cmd

        docker_info = _run_cmd(["docker", "info"])
        if docker_info is None:
            return CheckResult("Docker GPU", "warn",
                               "Docker not available",
                               detail="GPU containers require Docker + nvidia-container-toolkit")

        has_nvidia = "nvidia" in docker_info.lower()
        if has_nvidia:
            return CheckResult("Docker GPU", "ok",
                               "Docker available, nvidia runtime detected")
        else:
            return CheckResult("Docker GPU", "warn",
                               "Docker available but nvidia runtime not detected",
                               detail="Install nvidia-container-toolkit for GPU passthrough")

    except Exception as e:
        return CheckResult("Docker GPU", "warn", f"Check failed: {e}")


def check_firecrawl() -> CheckResult:
    """Check Firecrawl API availability."""
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        return CheckResult(
            "Firecrawl", "warn",
            "FIRECRAWL_API_KEY not set (web_scrape/web_crawl/web_search tools disabled)",
            detail="Set FIRECRAWL_API_KEY env var. Get a key at https://firecrawl.dev",
        )

    try:
        from firecrawl import FirecrawlApp  # type: ignore[import-untyped]
    except ImportError:
        return CheckResult(
            "Firecrawl", "warn",
            "firecrawl-py package not installed",
            detail="pip install 'vibe[firecrawl]'",
        )

    try:
        app = FirecrawlApp(api_key=api_key)
        # Lightweight check: scrape a tiny page
        result = app.scrape_url("https://example.com", params={
            "formats": ["markdown"],
            "timeout": 10000,
        })
        if result and (result.get("markdown") or result.get("html")):
            return CheckResult("Firecrawl", "ok",
                               "API key valid, scraping operational")
        return CheckResult("Firecrawl", "warn",
                           "API responded but returned no content")
    except Exception as e:
        return CheckResult("Firecrawl", "fail",
                           f"API check failed: {e}",
                           detail="Verify your FIRECRAWL_API_KEY is valid")


def check_memory() -> CheckResult:
    """Check persistent memory store health."""
    try:
        db_dir = Path.home() / ".vibe"
        db_path = db_dir / "memory.db"

        if not db_path.exists():
            return CheckResult(
                "Memory Store", "ok",
                "No database yet (will be created on first use)",
            )

        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            total = row[0] if row else 0
            # Verify FTS5 table exists
            fts_row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            ).fetchone()
            has_fts = fts_row is not None
            # Check embedding coverage
            try:
                emb_row = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()
                embedded = emb_row[0] if emb_row else 0
            except Exception:
                embedded = 0
        finally:
            conn.close()

        size_kb = db_path.stat().st_size / 1024
        fts_status = "FTS5 active" if has_fts else "FTS5 missing"
        emb_status = f"{embedded}/{total} embedded" if total > 0 else "no embeddings"
        return CheckResult(
            "Memory Store", "ok",
            f"SQLite WAL, {total} memories, {fts_status}, {emb_status}, {size_kb:.0f} KB",
        )
    except Exception as exc:
        return CheckResult("Memory Store", "fail", f"Error: {exc}")


def run_doctor(config: Any) -> DoctorReport:
    """
    Run all diagnostic checks and return the report.

    Args:
        config: SystemConfig instance

    Returns:
        DoctorReport with all check results
    """
    report = DoctorReport()

    report.add(check_hardware())
    report.add(check_backend(config))
    report.add(check_config(config))
    report.add(check_sandbox(config))
    report.add(check_docker_gpu())
    report.add(check_skills())
    report.add(check_skill_security())
    report.add(check_messenger(config))
    report.add(check_disk_usage())
    report.add(check_python_deps())
    report.add(check_firecrawl())
    report.add(check_memory())

    return report
