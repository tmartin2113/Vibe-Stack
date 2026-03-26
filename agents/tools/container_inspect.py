"""Container Inspection Tool — inspect Docker container status, logs, and health."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)

_LOG_TAIL_DEFAULT = 100
_LOG_TAIL_MAX = 1000
_TIMEOUT = 15


class ContainerInspectTool(Tool):
    """Inspect Docker containers: status, logs, health, networking, and resource usage.

    Requires the Docker socket to be mounted (``/var/run/docker.sock``).

    Actions:
        ps       — list running containers (or all with include_stopped=true)
        logs     — tail container logs
        inspect  — detailed container config and state
        health   — health check status and recent results
        stats    — live CPU/memory/network usage snapshot
    """

    def __init__(self):
        super().__init__(
            name="container_inspect",
            description=(
                "Inspect Docker containers: list running services, read logs, "
                "check health status, view config, and get resource usage. "
                "Use for diagnosing infrastructure issues."
            ),
            category=ToolCategory.SPECIALIZED,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action: 'ps', 'logs', 'inspect', 'health', or 'stats'",
                },
                "container": {
                    "type": "string",
                    "description": "Container name or ID (required for logs, inspect, health, stats)",
                },
                "tail": {
                    "type": "integer",
                    "description": f"Number of log lines to return (default {_LOG_TAIL_DEFAULT}, max {_LOG_TAIL_MAX})",
                    "default": _LOG_TAIL_DEFAULT,
                },
                "include_stopped": {
                    "type": "boolean",
                    "description": "Include stopped containers in 'ps' output (default: false)",
                    "default": False,
                },
                "filter": {
                    "type": "string",
                    "description": "Filter containers by name substring (for 'ps' action)",
                },
            },
            "required": ["action"],
        }

    def execute(  # type: ignore[override]
        self,
        action: str,
        container: str = "",
        tail: int = _LOG_TAIL_DEFAULT,
        include_stopped: bool = False,
        filter: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        valid_actions = ("ps", "logs", "inspect", "health", "stats")
        if action not in valid_actions:
            return ToolResult(
                success=False, output="",
                error=f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}",
            )

        if action in ("logs", "inspect", "health", "stats") and not container:
            return ToolResult(
                success=False, output="",
                error=f"'container' parameter is required for action '{action}'",
            )

        tail = min(max(tail, 1), _LOG_TAIL_MAX)

        if action == "ps":
            return self._ps(include_stopped, filter)
        elif action == "logs":
            return self._logs(container, tail)
        elif action == "inspect":
            return self._inspect(container)
        elif action == "health":
            return self._health(container)
        elif action == "stats":
            return self._stats(container)

        return ToolResult(success=False, output="", error="Unreachable")

    def _run(self, cmd: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"),
                "HOME": os.environ.get("HOME", "/tmp"),
                "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
            },
        )

    def _docker_error(self, result: subprocess.CompletedProcess) -> Optional[ToolResult]:
        if result.returncode != 0:
            return ToolResult(success=False, output="", error=result.stderr.strip())
        return None

    # ── Actions ───────────────────────────────────────────────────────

    def _ps(self, include_stopped: bool, name_filter: str) -> ToolResult:
        cmd = ["docker", "ps", "--format", "json"]
        if include_stopped:
            cmd.append("-a")
        if name_filter:
            cmd.extend(["--filter", f"name={name_filter}"])

        try:
            result = self._run(cmd)
            if err := self._docker_error(result):
                return err

            lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
            if not lines:
                return ToolResult(success=True, output="No containers found.", metadata={"count": 0})

            containers = []
            for line in lines:
                try:
                    c = json.loads(line)
                    containers.append(c)
                except json.JSONDecodeError:
                    continue

            # Format as markdown table
            md = ["| Name | Image | Status | Ports |"]
            md.append("| --- | --- | --- | --- |")
            for c in containers:
                name = c.get("Names", "")
                image = c.get("Image", "")
                status = c.get("Status", "")
                ports = c.get("Ports", "")
                md.append(f"| {name} | {image} | {status} | {ports} |")

            return ToolResult(
                success=True,
                output="\n".join(md),
                metadata={"count": len(containers)},
            )

        except FileNotFoundError:
            return ToolResult(success=False, output="", error="docker CLI not found")
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="docker ps timed out")

    def _logs(self, container: str, tail: int) -> ToolResult:
        try:
            result = self._run(
                ["docker", "logs", "--tail", str(tail), "--timestamps", container]
            )
            if err := self._docker_error(result):
                return err

            # Docker logs go to both stdout and stderr
            output = result.stdout + result.stderr
            lines = output.strip().split("\n")

            return ToolResult(
                success=True,
                output=output.strip(),
                metadata={"container": container, "lines": len(lines)},
            )

        except FileNotFoundError:
            return ToolResult(success=False, output="", error="docker CLI not found")
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"docker logs timed out for {container}")

    def _inspect(self, container: str) -> ToolResult:
        try:
            result = self._run(["docker", "inspect", container])
            if err := self._docker_error(result):
                return err

            data = json.loads(result.stdout)
            if not data:
                return ToolResult(success=False, output="", error=f"Container not found: {container}")

            c = data[0]
            state = c.get("State", {})
            config = c.get("Config", {})
            network = c.get("NetworkSettings", {})
            host_config = c.get("HostConfig", {})

            # Extract key info
            networks = {}
            for net_name, net_info in network.get("Networks", {}).items():
                networks[net_name] = {
                    "ip": net_info.get("IPAddress", ""),
                    "aliases": net_info.get("Aliases", []),
                }

            env_vars = config.get("Env", [])
            # Redact secrets
            safe_env = []
            for e in env_vars:
                key = e.split("=", 1)[0] if "=" in e else e
                key_upper = key.upper()
                if any(s in key_upper for s in ("SECRET", "PASSWORD", "TOKEN", "KEY", "AUTH")):
                    safe_env.append(f"{key}=<redacted>")
                else:
                    safe_env.append(e)

            mounts = []
            for m in c.get("Mounts", []):
                mounts.append(f"{m.get('Source', '?')} -> {m.get('Destination', '?')} ({m.get('Mode', 'rw')})")

            info = {
                "name": c.get("Name", "").lstrip("/"),
                "image": config.get("Image", ""),
                "state": {
                    "status": state.get("Status", ""),
                    "running": state.get("Running", False),
                    "started_at": state.get("StartedAt", ""),
                    "exit_code": state.get("ExitCode", 0),
                    "restart_count": host_config.get("RestartPolicy", {}).get("MaximumRetryCount", 0),
                },
                "networks": networks,
                "ports": network.get("Ports", {}),
                "mounts": mounts,
                "env": safe_env,
                "entrypoint": config.get("Entrypoint", []),
                "cmd": config.get("Cmd", []),
            }

            output = json.dumps(info, indent=2)
            return ToolResult(
                success=True,
                output=output,
                metadata={"container": container, "status": state.get("Status", "")},
            )

        except FileNotFoundError:
            return ToolResult(success=False, output="", error="docker CLI not found")
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"docker inspect timed out for {container}")
        except (json.JSONDecodeError, IndexError) as e:
            return ToolResult(success=False, output="", error=f"Failed to parse inspect output: {e}")

    def _health(self, container: str) -> ToolResult:
        try:
            result = self._run([
                "docker", "inspect",
                "--format", "{{json .State.Health}}",
                container,
            ])
            if err := self._docker_error(result):
                return err

            raw = result.stdout.strip()
            if not raw or raw == "<nil>" or raw == "null":
                return ToolResult(
                    success=True,
                    output=f"Container '{container}' has no health check configured.",
                    metadata={"container": container, "has_healthcheck": False},
                )

            health = json.loads(raw)
            status = health.get("Status", "unknown")
            failing = health.get("FailingStreak", 0)

            lines = [f"**Status:** {status}"]
            if failing:
                lines.append(f"**Failing streak:** {failing}")

            log = health.get("Log", [])
            if log:
                lines.append(f"\n**Recent checks** (last {min(len(log), 5)}):\n")
                for entry in log[-5:]:
                    exit_code = entry.get("ExitCode", "?")
                    ts = entry.get("Start", "")[:19]
                    out = entry.get("Output", "").strip()[:200]
                    icon = "pass" if exit_code == 0 else "FAIL"
                    lines.append(f"- [{icon}] {ts} (exit {exit_code}): {out}")

            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata={
                    "container": container,
                    "status": status,
                    "failing_streak": failing,
                    "has_healthcheck": True,
                },
            )

        except FileNotFoundError:
            return ToolResult(success=False, output="", error="docker CLI not found")
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"health check timed out for {container}")
        except json.JSONDecodeError as e:
            return ToolResult(success=False, output="", error=f"Failed to parse health data: {e}")

    def _stats(self, container: str) -> ToolResult:
        try:
            result = self._run([
                "docker", "stats", "--no-stream", "--format", "json", container,
            ])
            if err := self._docker_error(result):
                return err

            data = json.loads(result.stdout.strip())
            lines = [
                f"**Container:** {data.get('Name', container)}",
                f"**CPU:** {data.get('CPUPerc', '?')}",
                f"**Memory:** {data.get('MemUsage', '?')} ({data.get('MemPerc', '?')})",
                f"**Net I/O:** {data.get('NetIO', '?')}",
                f"**Block I/O:** {data.get('BlockIO', '?')}",
                f"**PIDs:** {data.get('PIDs', '?')}",
            ]

            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata={
                    "container": container,
                    "cpu": data.get("CPUPerc", ""),
                    "memory": data.get("MemUsage", ""),
                },
            )

        except FileNotFoundError:
            return ToolResult(success=False, output="", error="docker CLI not found")
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error=f"docker stats timed out for {container}")
        except json.JSONDecodeError as e:
            return ToolResult(success=False, output="", error=f"Failed to parse stats: {e}")
