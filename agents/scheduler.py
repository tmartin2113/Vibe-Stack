"""
Orchestrator Scheduler

Manages concurrent agent heartbeat subprocesses within a resource budget.

Components:
- AgentPriority  — static role → priority-level mapping
- PriorityQueue  — min-heap queue, dedup by role
- AgentHealth    — consecutive-failure tracking with exponential backoff
- SubprocessPool — spawn/reap/timeout for agent subprocesses
- Scheduler      — main scheduling loop (poll → enqueue → spawn → reap)
"""

import heapq
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .config import SchedulerConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AgentPriority
# ---------------------------------------------------------------------------

class AgentPriority:
    """Static priority levels by role.

    Lower integer = higher priority (min-heap convention).
    """

    _PRIORITY_MAP: Dict[str, int] = {
        # Priority 0 — executive
        "cto": 0,
        # Priority 1 — senior engineers
        "backend-engineer": 1,
        "frontend-engineer": 1,
        "devops-engineer": 1,
        "qa-engineer": 1,
        # Priority 2 — assistants
        "cto-assistant": 2,
        "backend-assistant": 2,
        "frontend-assistant": 2,
        "devops-assistant": 2,
        "qa-assistant": 2,
    }

    @classmethod
    def for_role(cls, role: str) -> int:
        """Return numeric priority for *role*; unknown roles default to 2."""
        return cls._PRIORITY_MAP.get(role, 2)


# ---------------------------------------------------------------------------
# PriorityQueue
# ---------------------------------------------------------------------------

class PriorityQueue:
    """Min-heap priority queue.

    Entries stored as ``(priority, created_at, role, agent_id)``.
    Duplicate roles are silently discarded — only the first enqueue wins.
    """

    def __init__(self) -> None:
        self._heap: List[Tuple[int, float, str, str]] = []
        self._queued_roles: Set[str] = set()

    def push(self, role: str, agent_id: str, created_at: float) -> None:
        """Enqueue *role*/*agent_id* unless *role* is already queued."""
        if role in self._queued_roles:
            return
        priority = AgentPriority.for_role(role)
        heapq.heappush(self._heap, (priority, created_at, role, agent_id))
        self._queued_roles.add(role)

    def pop(self) -> Optional[Tuple[str, str]]:
        """Pop and return ``(role, agent_id)`` for the highest-priority entry.

        Returns *None* when the queue is empty.
        """
        while self._heap:
            _priority, _created_at, role, agent_id = heapq.heappop(self._heap)
            if role in self._queued_roles:
                self._queued_roles.discard(role)
                return role, agent_id
        return None

    def __len__(self) -> int:
        return len(self._queued_roles)

    def __contains__(self, role: str) -> bool:
        return role in self._queued_roles


# ---------------------------------------------------------------------------
# AgentHealth
# ---------------------------------------------------------------------------

class AgentHealth:
    """Tracks consecutive failures per role and computes exponential backoff.

    Backoff formula: ``backoff_base_minutes * 3^(failures - threshold)``
    capped at ``backoff_max_minutes``.
    """

    def __init__(self, config: SchedulerConfig) -> None:
        self._config = config
        self._consecutive_failures: Dict[str, int] = {}
        self._last_failure_time: Dict[str, float] = {}

    def record_success(self, role: str) -> None:
        """Reset failure counter for *role*."""
        self._consecutive_failures.pop(role, None)
        self._last_failure_time.pop(role, None)

    def record_failure(self, role: str) -> None:
        """Increment failure counter for *role* and note the time."""
        self._consecutive_failures[role] = self._consecutive_failures.get(role, 0) + 1
        self._last_failure_time[role] = time.monotonic()

    def consecutive_failures(self, role: str) -> int:
        """Return current consecutive failure count for *role*."""
        return self._consecutive_failures.get(role, 0)

    def is_healthy(self, role: str) -> bool:
        """Return True if *role* has fewer consecutive failures than the threshold."""
        return self.consecutive_failures(role) < self._config.crash_threshold

    def backoff_seconds(self, role: str) -> float:
        """Compute current backoff duration in seconds.

        Returns 0 if *role* is healthy.  Caps at ``backoff_max_minutes``.
        """
        failures = self.consecutive_failures(role)
        threshold = self._config.crash_threshold
        if failures < threshold:
            return 0.0
        exponent = failures - threshold
        base = self._config.backoff_base_minutes
        maximum = self._config.backoff_max_minutes
        minutes = min(base * (3 ** exponent), maximum)
        return minutes * 60.0

    def is_ready(self, role: str) -> bool:
        """Return True if *role* is either healthy or has served its backoff."""
        if self.is_healthy(role):
            return True
        last = self._last_failure_time.get(role)
        if last is None:
            return True
        return (time.monotonic() - last) >= self.backoff_seconds(role)


# ---------------------------------------------------------------------------
# SubprocessPool
# ---------------------------------------------------------------------------

@dataclass
class ActiveProcess:
    """Metadata for a running agent subprocess."""
    role: str
    agent_id: str
    process: subprocess.Popen
    started_at: float


class SubprocessPool:
    """Manages running agent subprocesses.

    Spawn command::

        python -m agents.main --heartbeat --agent-id <id> --instructions <path>

    ``PAPERCLIP_AGENT_ID`` is injected into the subprocess environment.
    """

    def __init__(self, agent_timeout: int = 600) -> None:
        self._active: Dict[str, ActiveProcess] = {}
        self._agent_timeout = agent_timeout

    # -- spawn -----------------------------------------------------------------

    def spawn(
        self,
        role: str,
        agent_id: str,
        instructions_path: str,
        env: Optional[Dict[str, str]] = None,
    ) -> ActiveProcess:
        """Spawn a heartbeat subprocess for *role*/*agent_id*.

        Returns the ``ActiveProcess`` record and registers it in the pool.
        """
        proc_env = dict(env) if env else dict(os.environ)
        proc_env["PAPERCLIP_AGENT_ID"] = agent_id

        cmd = [
            "python", "-m", "agents.main",
            "--heartbeat",
            "--agent-id", agent_id,
            "--instructions", instructions_path,
        ]
        logger.info("Spawning %s (agent_id=%s)", role, agent_id)
        process = subprocess.Popen(
            cmd,
            env=proc_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ap = ActiveProcess(
            role=role,
            agent_id=agent_id,
            process=process,
            started_at=time.monotonic(),
        )
        self._active[role] = ap
        return ap

    # -- reap ------------------------------------------------------------------

    def reap(self) -> List[Tuple[str, int]]:
        """Poll all active processes; collect and remove finished ones.

        Returns list of ``(role, exit_code)`` for finished processes.
        """
        finished = []
        for role, ap in list(self._active.items()):
            rc = ap.process.poll()
            if rc is not None:
                finished.append((role, rc))
                del self._active[role]
                logger.info("Reaped %s (exit_code=%d)", role, rc)
        return finished

    # -- timeout ---------------------------------------------------------------

    def kill_timed_out(self, timeout: Optional[int] = None) -> List[str]:
        """SIGKILL processes that have exceeded *timeout* seconds.

        Returns list of roles that were killed.
        """
        if timeout is None:
            timeout = self._agent_timeout
        now = time.monotonic()
        killed = []
        for role, ap in list(self._active.items()):
            elapsed = now - ap.started_at
            if elapsed > timeout:
                logger.warning(
                    "Killing timed-out process %s (elapsed=%.0fs, timeout=%ds)",
                    role, elapsed, timeout,
                )
                ap.process.kill()
                del self._active[role]
                killed.append(role)
        return killed

    # -- shutdown --------------------------------------------------------------

    def terminate_all(self, grace_period: int = 30) -> None:
        """SIGTERM all active processes, then SIGKILL after *grace_period* seconds."""
        for role, ap in list(self._active.items()):
            logger.info("Terminating %s (pid=%d)", role, ap.process.pid)
            ap.process.terminate()

        deadline = time.monotonic() + grace_period
        for role, ap in list(self._active.items()):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                ap.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.warning("Force-killing %s after grace period", role)
                ap.process.kill()
                ap.process.wait()
        self._active.clear()

    # -- properties ------------------------------------------------------------

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def active_roles(self) -> Set[str]:
        return set(self._active.keys())

    def is_running(self, role: str) -> bool:
        return role in self._active


# ---------------------------------------------------------------------------
# Memory pressure helper
# ---------------------------------------------------------------------------

def get_memory_pressure_pct() -> int:
    """Read /proc/meminfo and return memory usage as int 0-100.

    Falls back to 0 on read failure.
    """
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        info[key] = int(parts[1])
                    except ValueError:
                        pass
        total = info.get("MemTotal", 0)
        if total == 0:
            return 0
        available = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - available
        return int(used * 100 / total)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """Main orchestrator scheduling loop.

    Args:
        config:            SchedulerConfig instance.
        client:            PaperclipClient (or compatible mock).
        agent_map:         Dict mapping role → agent_id.
        max_slots:         Maximum concurrent agent subprocesses.
        memory_pressure_fn: Callable returning current memory usage 0-100.
    """

    def __init__(
        self,
        config: SchedulerConfig,
        client,
        agent_map: Dict[str, str],
        max_slots: int,
        memory_pressure_fn: Callable[[], int] = get_memory_pressure_pct,
    ) -> None:
        self._config = config
        self._client = client
        self._agent_map = agent_map  # role → agent_id
        self._max_slots = max_slots
        self._memory_pressure_fn = memory_pressure_fn

        self._queue: PriorityQueue = PriorityQueue()
        self._health: AgentHealth = AgentHealth(config)
        self._pool: SubprocessPool = SubprocessPool(agent_timeout=config.agent_timeout)
        self._memory_paused: bool = False
        self._running: bool = False

    # -- instruction resolution ------------------------------------------------

    def _resolve_instructions_path(self, role: str) -> str:
        """Return the instructions directory for *role*.

        Checks the overrides directory first; falls back to the baked dir.
        """
        overrides_dir = Path(self._config.overrides_path)
        for suffix in (f"{role}.md", f"{role}.txt", role):
            candidate = overrides_dir / suffix
            if candidate.exists():
                return str(overrides_dir)
        return self._config.instructions_path

    # -- poll ------------------------------------------------------------------

    def _poll_pending_tasks(self) -> Dict[str, float]:
        """Query Paperclip for each agent's pending (todo) tasks.

        Returns a dict of ``{agent_id: created_at_timestamp}``.
        """
        pending: Dict[str, float] = {}
        for role, agent_id in self._agent_map.items():
            if role in self._config.disabled_agents:
                continue
            try:
                tasks = self._client.get_assignments_for(agent_id, statuses=["todo"])
                if tasks:
                    # Use earliest task's created_at (or current time as fallback)
                    earliest = min(
                        (getattr(t, "created_at", None) or time.time() for t in tasks),
                        default=time.time(),
                    )
                    # Normalize to float timestamp
                    if isinstance(earliest, str):
                        try:
                            from datetime import datetime
                            dt = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                            earliest = dt.timestamp()
                        except Exception:
                            earliest = time.time()
                    pending[agent_id] = float(earliest)
            except Exception as exc:
                logger.warning("Failed to poll tasks for %s (%s): %s", role, agent_id, exc)
        return pending

    # -- enqueue ---------------------------------------------------------------

    def _enqueue_pending(self, pending: Dict[str, float]) -> None:
        """Add agents with pending tasks to the priority queue.

        Skips agents that are:
        - already running in the pool
        - already in the queue
        - in an unhealthy backoff period
        """
        # Build reverse map: agent_id → role
        agent_id_to_role = {v: k for k, v in self._agent_map.items()}

        for agent_id, created_at in pending.items():
            role = agent_id_to_role.get(agent_id)
            if role is None:
                continue
            if role in self._config.disabled_agents:
                continue
            if self._pool.is_running(role):
                continue
            if role in self._queue:
                continue
            if not self._health.is_ready(role):
                logger.debug("Skipping %s — in backoff", role)
                continue
            self._queue.push(role, agent_id, created_at)

    # -- spawn -----------------------------------------------------------------

    def _spawn_from_queue(self) -> None:
        """Pop from the priority queue and spawn up to the available slot count."""
        available = self._max_slots - self._pool.active_count
        while available > 0 and len(self._queue) > 0:
            result = self._queue.pop()
            if result is None:
                break
            role, agent_id = result
            instructions_path = self._resolve_instructions_path(role)
            try:
                self._pool.spawn(role, agent_id, instructions_path)
                available -= 1
            except Exception as exc:
                logger.error("Failed to spawn %s: %s", role, exc)

    # -- reap / timeout --------------------------------------------------------

    def _handle_finished(self) -> None:
        """Reap finished processes and record health outcomes."""
        for role, exit_code in self._pool.reap():
            if exit_code == 0:
                self._health.record_success(role)
                logger.info("%s completed successfully", role)
            else:
                self._health.record_failure(role)
                logger.warning("%s exited with code %d", role, exit_code)

    def _handle_timeouts(self) -> None:
        """Kill timed-out processes and record failures."""
        killed = self._pool.kill_timed_out(timeout=self._config.agent_timeout)
        for role in killed:
            self._health.record_failure(role)

    # -- memory pressure -------------------------------------------------------

    def _check_memory_pressure(self) -> bool:
        """Return True (paused) when memory exceeds threshold.

        Transitions:
        - pressure >= threshold  → set _memory_paused = True
        - pressure <  resume     → set _memory_paused = False
        """
        pct = self._memory_pressure_fn()
        if pct >= self._config.memory_pressure_threshold:
            if not self._memory_paused:
                logger.warning("Memory pressure %d%% >= %d%%; pausing spawning", pct, self._config.memory_pressure_threshold)
            self._memory_paused = True
        elif self._memory_paused and pct < self._config.memory_pressure_resume:
            logger.info("Memory pressure %d%% < %d%%; resuming spawning", pct, self._config.memory_pressure_resume)
            self._memory_paused = False
        return self._memory_paused

    # -- tick ------------------------------------------------------------------

    def tick(self) -> None:
        """One scheduler cycle: reap → timeout → pressure check → poll → enqueue → spawn."""
        self._handle_finished()
        self._handle_timeouts()

        if self._check_memory_pressure():
            logger.debug("Memory pressure pause — skipping poll+spawn")
            return

        pending = self._poll_pending_tasks()
        self._enqueue_pending(pending)
        self._spawn_from_queue()

    # -- run -------------------------------------------------------------------

    def run(self) -> None:
        """Blocking scheduler loop. Calls tick() every scheduler_interval seconds."""
        self._running = True
        logger.info(
            "Scheduler starting (max_slots=%d, interval=%ds)",
            self._max_slots,
            self._config.scheduler_interval,
        )
        while self._running:
            try:
                self.tick()
            except Exception as exc:
                logger.error("Scheduler tick error: %s", exc, exc_info=True)
            time.sleep(self._config.scheduler_interval)

    def stop(self) -> None:
        """Signal the scheduler to stop and terminate all child processes."""
        logger.info("Scheduler stopping")
        self._running = False
        self._pool.terminate_all()

    # -- status ----------------------------------------------------------------

    def get_status(self) -> Dict:
        """Return current scheduler state for /healthz or admin endpoints."""
        return {
            "running": self._running,
            "active_count": self._pool.active_count,
            "active_roles": sorted(self._pool.active_roles),
            "queue_size": len(self._queue),
            "memory_paused": self._memory_paused,
            "max_slots": self._max_slots,
        }
