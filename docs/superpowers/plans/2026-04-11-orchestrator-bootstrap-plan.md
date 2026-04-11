# Resource-Aware Orchestrator & Agent Bootstrap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a resource-aware orchestrator mode that auto-discovers agents from Paperclip, probes hardware to set concurrency limits, and schedules heartbeat subprocesses within a priority queue — enabling fully autonomous multi-agent execution from `docker compose up`.

**Architecture:** A new `--orchestrator` execution mode in `agents/main.py` boots a long-running scheduler loop. At startup it resolves all agent IDs from the Paperclip API and calculates a concurrency budget from system RAM/CPU. The scheduler polls Paperclip for pending tasks every 30s, spawns heartbeat subprocesses (up to the concurrency budget), reaps finished processes, enforces timeouts, and monitors memory pressure. Agent instructions are baked into the Docker image at build time and passed to subprocesses via `--instructions` CLI arg.

**Tech Stack:** Python 3.13 stdlib (`subprocess`, `os`, `signal`, `threading`), existing `resource_discovery.py` for hardware probing, existing `paperclip_client.py` for API calls, existing `metrics.py` for health endpoint expansion.

**Spec:** `docs/superpowers/specs/2026-04-11-orchestrator-bootstrap-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `agents/concurrency_budget.py` | Pure function: `SystemProfile` → `max_slots` integer |
| `agents/agent_registry.py` | Resolve all agent IDs from Paperclip API, build `{role: uuid}` map |
| `agents/scheduler.py` | Priority queue, subprocess pool, spawn/reap loop, memory pressure, health tracking |
| `agents/orchestrator_main.py` | Entry point for `--orchestrator` mode — wires registry + budget + scheduler |
| `tests/test_concurrency_budget.py` | Tests for concurrency budget calculation |
| `tests/test_agent_registry.py` | Tests for agent registry resolution |
| `tests/test_scheduler.py` | Tests for scheduler loop, priority queue, subprocess pool, health tracking |
| `tests/test_orchestrator_main.py` | Integration tests for orchestrator entry point |
| `agents/instructions/cto/AGENTS.md` | Placeholder CTO instructions (+ 9 other role dirs) |

### Modified Files

| File | Lines | Change |
|------|-------|--------|
| `agents/main.py` | ~402 | Add `--orchestrator` arg + `--agent-id` + `--instructions` args; dispatch to orchestrator |
| `agents/config.py` | ~309 | Add `SchedulerConfig` dataclass; wire into `SystemConfig` |
| `agents/metrics.py` | ~221 | Expand `/healthz` to include scheduler state when available |
| `Dockerfile` | ~39 | Add `COPY` for instructions directory; change default CMD |
| `docker-compose.yml` | ~148 | Add secret mounts, env vars, raise resource limits for orchestrator |
| `.env.example` | EOF | Add `VIBE_MAX_CONCURRENT_AGENTS` and related env vars |

---

## Task 1: Concurrency Budget Calculator

**Files:**
- Create: `agents/concurrency_budget.py`
- Create: `tests/test_concurrency_budget.py`

This is a pure function with no external dependencies — takes a `SystemProfile` and returns an integer.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_concurrency_budget.py`:

```python
"""Tests for concurrency budget calculation."""

import pytest
from agents.resource_discovery import SystemProfile
from agents.concurrency_budget import calculate_concurrency_budget


def _profile(total_ram_mb: int, cpu_count: int) -> SystemProfile:
    """Build a minimal SystemProfile for budget testing."""
    return SystemProfile(
        cpu_count=cpu_count,
        cpu_threads=cpu_count * 2,
        cpu_model="test",
        total_ram_mb=total_ram_mb,
        available_ram_mb=total_ram_mb,
    )


class TestConcurrencyBudget:
    """Spec table (Section 2, lines 148-155):
    16GB → 2, 32GB → 4, 64GB → 10, 128GB → 10
    """

    def test_16gb_2_slots(self):
        # 16GB = 16384MB, reserve 10GB → 6GB available → 6/1.5=4 from RAM
        # cpu_count=4 → 4-2=2 from CPU → min(4,2,10)=2
        result = calculate_concurrency_budget(_profile(16384, 4))
        assert result == 2

    def test_32gb_4_slots(self):
        # 32GB = 32768MB, reserve 10GB → ~22GB → 22/1.5=14 from RAM
        # cpu_count=12 → 12-2=10 from CPU → min(14,10,10)=10
        # But spec says 4 for 32GB, so with 6 cores: min(14,4,10)=4
        result = calculate_concurrency_budget(_profile(32768, 6))
        assert result == 4

    def test_64gb_10_slots(self):
        # 64GB, 16 cores → min(36, 14, 10) = 10
        result = calculate_concurrency_budget(_profile(65536, 16))
        assert result == 10

    def test_128gb_capped_at_10(self):
        # 128GB, 32 cores → min(78, 30, 10) = 10
        result = calculate_concurrency_budget(_profile(131072, 32))
        assert result == 10

    def test_minimum_is_1(self):
        # Tiny system: 2GB, 2 cores → would compute 0, clamped to 1
        result = calculate_concurrency_budget(_profile(2048, 2))
        assert result == 1

    def test_custom_reserve(self):
        # 32GB with 4GB reserve instead of 10GB → more headroom
        result = calculate_concurrency_budget(
            _profile(32768, 16), infra_reserve_gb=4
        )
        assert result == 10  # min(18, 14, 10)

    def test_custom_slot_cost(self):
        # 32GB, default reserve, but 3GB per slot instead of 1.5
        result = calculate_concurrency_budget(
            _profile(32768, 16), slot_cost_gb=3.0
        )
        assert result == 7  # (22/3)=7, min(7, 14, 10)=7

    def test_override_max(self):
        # Explicit override bypasses auto-detection
        result = calculate_concurrency_budget(_profile(16384, 4), override_max=6)
        assert result == 6

    def test_override_zero_means_auto(self):
        # override_max=0 means "auto-detect" (not "zero agents")
        result = calculate_concurrency_budget(_profile(16384, 4), override_max=0)
        assert result == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_concurrency_budget.py -v`
Expected: `ModuleNotFoundError: No module named 'agents.concurrency_budget'`

- [ ] **Step 3: Write the implementation**

Create `agents/concurrency_budget.py`:

```python
"""
Concurrency budget calculator.

Determines the maximum number of agent subprocesses that can run
concurrently based on available system resources.

See spec: docs/superpowers/specs/2026-04-11-orchestrator-bootstrap-design.md
Section 2, lines 143-155.
"""

import logging
import math

from .resource_discovery import SystemProfile

logger = logging.getLogger(__name__)

# Defaults from spec
DEFAULT_INFRA_RESERVE_GB = 10
DEFAULT_SLOT_COST_GB = 1.5
MAX_ORG_SIZE = 10


def calculate_concurrency_budget(
    profile: SystemProfile,
    infra_reserve_gb: int = DEFAULT_INFRA_RESERVE_GB,
    slot_cost_gb: float = DEFAULT_SLOT_COST_GB,
    override_max: int = 0,
) -> int:
    """Calculate max concurrent agent slots from system resources.

    Args:
        profile: Hardware snapshot from ``discover_system()``.
        infra_reserve_gb: RAM reserved for non-agent containers (default 10GB).
        slot_cost_gb: Estimated peak RAM per agent subprocess (default 1.5GB).
        override_max: If >0, bypass auto-detection and return this value directly.

    Returns:
        Integer ≥ 1 — the number of concurrent agent slots.
    """
    if override_max > 0:
        logger.info("Concurrency budget: override_max=%d", override_max)
        return override_max

    total_ram_gb = profile.total_ram_mb / 1024
    available_ram_gb = total_ram_gb - infra_reserve_gb
    max_from_ram = max(1, math.floor(available_ram_gb / slot_cost_gb))
    max_from_cpu = max(1, profile.cpu_count - 2)
    max_slots = min(max_from_ram, max_from_cpu, MAX_ORG_SIZE)
    max_slots = max(1, max_slots)  # absolute floor

    logger.info(
        "Concurrency budget: %.1fGB RAM (%.1fGB available after %dGB reserve), "
        "%d cores → %d slots (ram=%d, cpu=%d, cap=%d)",
        total_ram_gb, available_ram_gb, infra_reserve_gb,
        profile.cpu_count, max_slots, max_from_ram, max_from_cpu, MAX_ORG_SIZE,
    )
    return max_slots
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_concurrency_budget.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/concurrency_budget.py tests/test_concurrency_budget.py
git commit -m "feat: add concurrency budget calculator for orchestrator scheduling"
```

---

## Task 2: Agent Registry

**Files:**
- Create: `agents/agent_registry.py`
- Create: `tests/test_agent_registry.py`

Wraps `PaperclipClient.list_agents()` to build a `{role: agent_id}` map. Handles disabled agents and missing roles.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_registry.py`:

```python
"""Tests for agent registry — resolves agent IDs from Paperclip API."""

import pytest
from unittest.mock import MagicMock
from agents.paperclip_client import AgentInfo, PaperclipAPIError
from agents.agent_registry import AgentRegistry


def _agent(role: str, agent_id: str, status: str = "active") -> AgentInfo:
    return AgentInfo(
        id=agent_id,
        company_id="company-1",
        name=role.replace("-", " ").title(),
        role=role,
        title=role,
        status=status,
    )


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.company_id = "company-1"
    return client


class TestAgentRegistry:

    def test_resolve_all_builds_role_map(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
            _agent("backend-engineer", "uuid-2"),
            _agent("frontend-engineer", "uuid-3"),
        ]
        registry = AgentRegistry(mock_client)
        result = registry.resolve_all()
        assert result == {
            "cto": "uuid-1",
            "backend-engineer": "uuid-2",
            "frontend-engineer": "uuid-3",
        }

    def test_resolve_all_skips_disabled_agents(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
            _agent("frontend-engineer", "uuid-2"),
        ]
        registry = AgentRegistry(mock_client, disabled_roles=["frontend-engineer"])
        result = registry.resolve_all()
        assert result == {"cto": "uuid-1"}

    def test_resolve_all_skips_inactive_agents(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1", status="active"),
            _agent("qa-engineer", "uuid-2", status="inactive"),
        ]
        registry = AgentRegistry(mock_client)
        result = registry.resolve_all()
        assert result == {"cto": "uuid-1"}

    def test_resolve_all_empty_org(self, mock_client):
        mock_client.list_agents.return_value = []
        registry = AgentRegistry(mock_client)
        result = registry.resolve_all()
        assert result == {}

    def test_resolve_all_api_error_raises(self, mock_client):
        mock_client.list_agents.side_effect = PaperclipAPIError("Server down")
        registry = AgentRegistry(mock_client)
        with pytest.raises(PaperclipAPIError):
            registry.resolve_all()

    def test_get_agent_id_returns_uuid(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
        ]
        registry = AgentRegistry(mock_client)
        registry.resolve_all()
        assert registry.get_agent_id("cto") == "uuid-1"

    def test_get_agent_id_unknown_role_returns_none(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
        ]
        registry = AgentRegistry(mock_client)
        registry.resolve_all()
        assert registry.get_agent_id("unknown-role") is None

    def test_roles_property(self, mock_client):
        mock_client.list_agents.return_value = [
            _agent("cto", "uuid-1"),
            _agent("backend-engineer", "uuid-2"),
        ]
        registry = AgentRegistry(mock_client)
        registry.resolve_all()
        assert set(registry.roles) == {"cto", "backend-engineer"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_agent_registry.py -v`
Expected: `ModuleNotFoundError: No module named 'agents.agent_registry'`

- [ ] **Step 3: Write the implementation**

Create `agents/agent_registry.py`:

```python
"""
Agent registry — resolves agent IDs from the Paperclip API at startup.

On boot the orchestrator calls ``resolve_all()`` once to build an internal
{role: agent_id} map.  No UUIDs are hardcoded — if ``bootstrap-org.cjs``
recreates agents with new UUIDs, the orchestrator picks them up on restart.
"""

import logging
from typing import Dict, List, Optional, Set

from .paperclip_client import PaperclipClient

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Resolve and cache agent identities from Paperclip."""

    def __init__(
        self,
        client: PaperclipClient,
        disabled_roles: Optional[List[str]] = None,
    ):
        self._client = client
        self._disabled: Set[str] = set(disabled_roles or [])
        self._role_map: Dict[str, str] = {}

    def resolve_all(self) -> Dict[str, str]:
        """Fetch all agents from Paperclip and build {role: agent_id} map.

        Skips agents whose role is in ``disabled_roles`` or whose status
        is not ``active``.

        Returns:
            Dict mapping role name → agent UUID.

        Raises:
            PaperclipAPIError: If the API call fails.
        """
        agents = self._client.list_agents()
        self._role_map = {}

        for agent in agents:
            if agent.role in self._disabled:
                logger.info("Skipping disabled agent: %s (%s)", agent.role, agent.id)
                continue
            if agent.status != "active":
                logger.info("Skipping inactive agent: %s (%s, status=%s)",
                            agent.role, agent.id, agent.status)
                continue
            self._role_map[agent.role] = agent.id
            logger.debug("Registered agent: %s → %s", agent.role, agent.id)

        logger.info("Resolved %d agents from Paperclip (skipped %d disabled, %d inactive)",
                     len(self._role_map),
                     sum(1 for a in agents if a.role in self._disabled),
                     sum(1 for a in agents if a.status != "active" and a.role not in self._disabled))
        return dict(self._role_map)

    def get_agent_id(self, role: str) -> Optional[str]:
        """Look up an agent ID by role.  Returns None if not found."""
        return self._role_map.get(role)

    @property
    def roles(self) -> List[str]:
        """List of all resolved (active, non-disabled) role names."""
        return list(self._role_map.keys())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_agent_registry.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/agent_registry.py tests/test_agent_registry.py
git commit -m "feat: add agent registry for dynamic ID resolution from Paperclip"
```

---

## Task 3: Scheduler Config

**Files:**
- Modify: `agents/config.py:309` — add `SchedulerConfig` dataclass after `PaperclipConfig`
- Modify: `agents/config.py:344` — add `scheduler` field to `SystemConfig`

- [ ] **Step 1: Add SchedulerConfig dataclass**

In `agents/config.py`, insert after line 309 (after `PaperclipConfig`'s `orchestrator_poll_timeout` field):

```python
@dataclass
class SchedulerConfig:
    """Resource-aware orchestrator scheduler configuration.

    Controls how the ``--orchestrator`` mode spawns and manages agent
    subprocesses.  All fields can be overridden via VIBE_* env vars.
    """
    max_concurrent_agents: int = 0          # 0 = auto-detect from hardware
    scheduler_interval: int = 30            # Poll interval in seconds
    agent_timeout: int = 600                # Per-heartbeat timeout in seconds
    disabled_agents: List[str] = field(default_factory=list)  # Roles to skip
    memory_pressure_threshold: int = 90     # Pause spawning above this %
    memory_pressure_resume: int = 80        # Resume spawning below this %
    infra_reserve_gb: int = 10              # RAM reserved for non-agent containers
    slot_cost_gb: float = 1.5               # Estimated peak RAM per subprocess
    crash_threshold: int = 3                # Consecutive crashes before unhealthy
    backoff_base_minutes: int = 5           # Initial backoff after unhealthy
    backoff_max_minutes: int = 60           # Maximum backoff cap
    instructions_path: str = "/opt/vibe/instructions"  # Baked instruction dir
    overrides_path: str = "/opt/vibe/overrides"         # Optional override dir
```

- [ ] **Step 2: Wire SchedulerConfig into SystemConfig**

In `agents/config.py`, add to the `SystemConfig` dataclass fields (after `self_upgrade` on line 345):

```python
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
```

- [ ] **Step 3: Add List import if not present**

Check line 1-10 of `config.py` for existing imports. Add `List` to the typing import if not already there.

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/ -x -m "not e2e" --no-header -q -k "config" 2>&1 | tail -5`
Expected: All existing tests still pass

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/config.py
git commit -m "feat: add SchedulerConfig for orchestrator concurrency and scheduling settings"
```

---

## Task 4: Scheduler — Priority Queue & Agent Health

**Files:**
- Create: `agents/scheduler.py`
- Create: `tests/test_scheduler.py`

This is the largest task. Split into sub-steps: priority queue, agent health tracker, subprocess pool, and the main loop. All in one file to avoid premature abstraction.

- [ ] **Step 1: Write priority queue and health tracker tests**

Create `tests/test_scheduler.py` with the first batch of tests:

```python
"""Tests for the orchestrator scheduler."""

import os
import signal
import subprocess
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from agents.scheduler import (
    AgentPriority,
    AgentHealth,
    PriorityQueue,
    SubprocessPool,
    Scheduler,
)


# ── Priority Queue ──


class TestAgentPriority:

    def test_cto_is_priority_0(self):
        assert AgentPriority.for_role("cto") == 0

    def test_senior_engineers_are_priority_1(self):
        for role in ["backend-engineer", "frontend-engineer", "devops-engineer", "qa-engineer"]:
            assert AgentPriority.for_role(role) == 1

    def test_assistants_are_priority_2(self):
        for role in ["cto-assistant", "backend-assistant", "frontend-assistant",
                      "devops-assistant", "qa-assistant"]:
            assert AgentPriority.for_role(role) == 2

    def test_unknown_role_is_lowest_priority(self):
        assert AgentPriority.for_role("unknown") == 2


class TestPriorityQueue:

    def test_empty_queue(self):
        q = PriorityQueue()
        assert q.pop() is None
        assert len(q) == 0

    def test_higher_priority_first(self):
        q = PriorityQueue()
        q.push("backend-engineer", "uuid-2", created_at=1.0)
        q.push("cto", "uuid-1", created_at=1.0)
        role, agent_id = q.pop()
        assert role == "cto"

    def test_same_priority_fifo(self):
        q = PriorityQueue()
        q.push("backend-engineer", "uuid-2", created_at=1.0)
        q.push("frontend-engineer", "uuid-3", created_at=2.0)
        role1, _ = q.pop()
        role2, _ = q.pop()
        assert role1 == "backend-engineer"
        assert role2 == "frontend-engineer"

    def test_skip_if_already_queued(self):
        q = PriorityQueue()
        q.push("cto", "uuid-1", created_at=1.0)
        q.push("cto", "uuid-1", created_at=2.0)  # duplicate
        assert len(q) == 1

    def test_len(self):
        q = PriorityQueue()
        q.push("cto", "uuid-1", created_at=1.0)
        q.push("backend-engineer", "uuid-2", created_at=1.0)
        assert len(q) == 2
        q.pop()
        assert len(q) == 1

    def test_contains(self):
        q = PriorityQueue()
        q.push("cto", "uuid-1", created_at=1.0)
        assert "cto" in q
        assert "backend-engineer" not in q


# ── Agent Health ──


class TestAgentHealth:

    def test_initial_state_is_healthy(self):
        h = AgentHealth()
        assert h.is_healthy("cto")
        assert h.consecutive_failures("cto") == 0

    def test_record_success_resets_failures(self):
        h = AgentHealth()
        h.record_failure("cto")
        h.record_failure("cto")
        h.record_success("cto")
        assert h.consecutive_failures("cto") == 0
        assert h.is_healthy("cto")

    def test_three_failures_marks_unhealthy(self):
        h = AgentHealth(crash_threshold=3)
        h.record_failure("cto")
        h.record_failure("cto")
        assert h.is_healthy("cto")  # only 2
        h.record_failure("cto")
        assert not h.is_healthy("cto")  # 3 = threshold

    def test_backoff_increases_exponentially(self):
        h = AgentHealth(crash_threshold=1, backoff_base_minutes=5, backoff_max_minutes=60)
        h.record_failure("cto")  # 1st fail → unhealthy, backoff=5m
        assert h.backoff_seconds("cto") == 300
        h.record_failure("cto")  # 2nd → backoff=15m
        assert h.backoff_seconds("cto") == 900
        h.record_failure("cto")  # 3rd → backoff=45m
        assert h.backoff_seconds("cto") == 2700

    def test_backoff_capped_at_max(self):
        h = AgentHealth(crash_threshold=1, backoff_base_minutes=5, backoff_max_minutes=60)
        for _ in range(10):
            h.record_failure("cto")
        assert h.backoff_seconds("cto") <= 3600

    def test_is_ready_respects_backoff(self):
        h = AgentHealth(crash_threshold=1, backoff_base_minutes=5)
        h.record_failure("cto")
        # Just failed — not ready yet (backoff hasn't elapsed)
        assert not h.is_ready("cto")

    def test_is_ready_after_backoff_elapses(self):
        h = AgentHealth(crash_threshold=1, backoff_base_minutes=5)
        h.record_failure("cto")
        # Simulate time passing by backdating the failure timestamp
        h._last_failure_time["cto"] = time.monotonic() - 301
        assert h.is_ready("cto")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_scheduler.py -v -k "TestAgentPriority or TestPriorityQueue or TestAgentHealth"`
Expected: `ModuleNotFoundError: No module named 'agents.scheduler'`

- [ ] **Step 3: Write priority queue, health tracker, and priority constants**

Create `agents/scheduler.py` with the first portion:

```python
"""
Orchestrator scheduler — priority queue, subprocess pool, and scheduling loop.

Manages concurrent agent heartbeat subprocesses within a resource budget.
See spec: docs/superpowers/specs/2026-04-11-orchestrator-bootstrap-design.md
"""

import heapq
import logging
import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .config import SchedulerConfig

logger = logging.getLogger(__name__)


# ── Priority Constants ──
# Spec Section 2, lines 158-167


class AgentPriority:
    """Static priority levels by agent role."""

    _PRIORITIES = {
        "cto": 0,
        "backend-engineer": 1,
        "frontend-engineer": 1,
        "devops-engineer": 1,
        "qa-engineer": 1,
        "cto-assistant": 2,
        "backend-assistant": 2,
        "frontend-assistant": 2,
        "devops-assistant": 2,
        "qa-assistant": 2,
    }

    @classmethod
    def for_role(cls, role: str) -> int:
        return cls._PRIORITIES.get(role, 2)


# ── Priority Queue ──


class PriorityQueue:
    """Min-heap priority queue for agent scheduling.

    Entries: (priority, created_at, role, agent_id).
    Tracks queued roles to prevent duplicate entries.
    """

    def __init__(self):
        self._heap: List[Tuple[int, float, str, str]] = []
        self._queued: Set[str] = set()

    def push(self, role: str, agent_id: str, created_at: float) -> None:
        if role in self._queued:
            return
        priority = AgentPriority.for_role(role)
        heapq.heappush(self._heap, (priority, created_at, role, agent_id))
        self._queued.add(role)

    def pop(self) -> Optional[Tuple[str, str]]:
        while self._heap:
            _, _, role, agent_id = heapq.heappop(self._heap)
            if role in self._queued:
                self._queued.discard(role)
                return (role, agent_id)
        return None

    def __len__(self) -> int:
        return len(self._queued)

    def __contains__(self, role: str) -> bool:
        return role in self._queued


# ── Agent Health Tracker ──


class AgentHealth:
    """Track consecutive failures and enforce backoff for unhealthy agents."""

    def __init__(
        self,
        crash_threshold: int = 3,
        backoff_base_minutes: int = 5,
        backoff_max_minutes: int = 60,
    ):
        self._threshold = crash_threshold
        self._backoff_base = backoff_base_minutes
        self._backoff_max = backoff_max_minutes
        self._failures: Dict[str, int] = {}
        self._last_failure_time: Dict[str, float] = {}

    def record_success(self, role: str) -> None:
        self._failures.pop(role, None)
        self._last_failure_time.pop(role, None)

    def record_failure(self, role: str) -> None:
        self._failures[role] = self._failures.get(role, 0) + 1
        self._last_failure_time[role] = time.monotonic()

    def consecutive_failures(self, role: str) -> int:
        return self._failures.get(role, 0)

    def is_healthy(self, role: str) -> bool:
        return self._failures.get(role, 0) < self._threshold

    def backoff_seconds(self, role: str) -> int:
        failures = self._failures.get(role, 0)
        if failures < self._threshold:
            return 0
        # Exponential: base * 3^(failures - threshold)
        exponent = failures - self._threshold
        seconds = self._backoff_base * 60 * (3 ** exponent)
        return min(seconds, self._backoff_max * 60)

    def is_ready(self, role: str) -> bool:
        if self.is_healthy(role):
            return True
        last = self._last_failure_time.get(role)
        if last is None:
            return True
        elapsed = time.monotonic() - last
        return elapsed >= self.backoff_seconds(role)


# ── Subprocess Pool ──


@dataclass
class ActiveProcess:
    """A running agent subprocess."""
    role: str
    agent_id: str
    process: subprocess.Popen
    started_at: float


class SubprocessPool:
    """Manages active agent subprocesses."""

    def __init__(self):
        self._active: Dict[str, ActiveProcess] = {}  # role → ActiveProcess

    def spawn(
        self,
        role: str,
        agent_id: str,
        instructions_path: str,
        env: Optional[Dict[str, str]] = None,
    ) -> ActiveProcess:
        cmd = [
            "python", "-m", "agents.main",
            "--heartbeat",
            "--agent-id", agent_id,
            "--instructions", instructions_path,
        ]
        proc_env = dict(os.environ)
        if env:
            proc_env.update(env)
        proc_env["PAPERCLIP_AGENT_ID"] = agent_id

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proc_env,
        )

        entry = ActiveProcess(
            role=role,
            agent_id=agent_id,
            process=process,
            started_at=time.monotonic(),
        )
        self._active[role] = entry
        logger.info("[scheduler] Spawning %s (priority %d) → PID %d",
                     role, AgentPriority.for_role(role), process.pid)
        return entry

    def reap(self) -> List[Tuple[str, int]]:
        """Check for finished subprocesses. Returns list of (role, returncode)."""
        finished = []
        for role, entry in list(self._active.items()):
            retcode = entry.process.poll()
            if retcode is not None:
                duration = time.monotonic() - entry.started_at
                logger.info("[scheduler] %s exited (%d) in %.0fs — %d slots available",
                             role, retcode, duration, len(self._active) - 1)
                finished.append((role, retcode))
                del self._active[role]
        return finished

    def kill_timed_out(self, timeout_seconds: int) -> List[str]:
        """Kill subprocesses exceeding the timeout. Returns roles killed."""
        killed = []
        now = time.monotonic()
        for role, entry in list(self._active.items()):
            if now - entry.started_at > timeout_seconds:
                logger.warning("[scheduler] %s exceeded timeout (%ds) — killed",
                                role, timeout_seconds)
                entry.process.kill()
                killed.append(role)
                del self._active[role]
        return killed

    def terminate_all(self, grace_period: float = 30.0) -> None:
        """Send SIGTERM to all children, wait, then SIGKILL stragglers."""
        for entry in self._active.values():
            try:
                entry.process.terminate()
            except OSError:
                pass

        deadline = time.monotonic() + grace_period
        while self._active and time.monotonic() < deadline:
            self.reap()
            if self._active:
                time.sleep(0.5)

        for entry in self._active.values():
            try:
                entry.process.kill()
            except OSError:
                pass
        self._active.clear()

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def active_roles(self) -> Set[str]:
        return set(self._active.keys())

    def is_running(self, role: str) -> bool:
        return role in self._active


# ── Memory Pressure Monitor ──


def get_memory_pressure_pct() -> int:
    """Read current memory usage percentage from /proc/meminfo.

    Returns usage as integer 0-100.  Falls back to 0 on read failure.
    """
    try:
        with open("/proc/meminfo", "r") as f:
            meminfo = f.read()
        total = available = 0
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available = int(line.split()[1])
        if total > 0:
            return int((1 - available / total) * 100)
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return 0


# ── Scheduler ──


class Scheduler:
    """Main orchestrator scheduling loop.

    Polls Paperclip for pending tasks, manages a priority queue,
    spawns heartbeat subprocesses within the concurrency budget,
    and handles timeouts, crashes, and memory pressure.
    """

    def __init__(
        self,
        config: SchedulerConfig,
        client: Any,  # PaperclipClient
        agent_map: Dict[str, str],
        max_slots: int,
        memory_pressure_fn: Callable[[], int] = get_memory_pressure_pct,
    ):
        self._config = config
        self._client = client
        self._agent_map = agent_map
        self._max_slots = max_slots
        self._memory_pressure_fn = memory_pressure_fn

        self._queue = PriorityQueue()
        self._pool = SubprocessPool()
        self._health = AgentHealth(
            crash_threshold=config.crash_threshold,
            backoff_base_minutes=config.backoff_base_minutes,
            backoff_max_minutes=config.backoff_max_minutes,
        )

        self._running = False
        self._paused_for_memory = False

    def _resolve_instructions_path(self, role: str) -> str:
        """Return the instruction file path for a role.

        Checks overrides dir first, falls back to baked instructions.
        """
        override = os.path.join(self._config.overrides_path, role, "AGENTS.md")
        if os.path.isfile(override):
            return override
        return os.path.join(self._config.instructions_path, role, "AGENTS.md")

    def _poll_pending_tasks(self) -> Dict[str, float]:
        """Query Paperclip for agents with pending work.

        Returns {role: earliest_task_timestamp} for roles that have
        assigned, unstarted tasks.
        """
        pending: Dict[str, float] = {}
        for role, agent_id in self._agent_map.items():
            try:
                # Create a temporary client with this agent's identity
                issues = self._client.get_assignments_for(agent_id, statuses=["todo"])
                if issues:
                    # Use earliest task creation as FIFO tiebreaker
                    pending[role] = min(
                        float(getattr(issue, "created_at", 0) or 0)
                        for issue in issues
                    )
            except Exception as e:
                logger.warning("[scheduler] Failed to poll tasks for %s: %s", role, e)
        return pending

    def _enqueue_pending(self, pending: Dict[str, float]) -> None:
        """Add agents with pending work to the priority queue."""
        for role, created_at in pending.items():
            if self._pool.is_running(role):
                continue
            if role in self._queue:
                continue
            if not self._health.is_ready(role):
                logger.debug("[scheduler] %s is in backoff — skipping", role)
                continue
            self._queue.push(role, self._agent_map[role], created_at)

    def _spawn_from_queue(self) -> None:
        """Spawn subprocesses from the queue up to available slots."""
        available = self._max_slots - self._pool.active_count
        while available > 0:
            entry = self._queue.pop()
            if entry is None:
                break
            role, agent_id = entry
            instructions = self._resolve_instructions_path(role)
            self._pool.spawn(role, agent_id, instructions)
            available -= 1

    def _handle_finished(self) -> None:
        """Reap finished subprocesses and update health tracking."""
        for role, retcode in self._pool.reap():
            if retcode == 0:
                self._health.record_success(role)
            else:
                self._health.record_failure(role)
                failures = self._health.consecutive_failures(role)
                if not self._health.is_healthy(role):
                    logger.warning(
                        "[scheduler] %s marked unhealthy (attempt %d/%d)",
                        role, failures, self._config.crash_threshold,
                    )

    def _handle_timeouts(self) -> None:
        """Kill subprocesses exceeding the configured timeout."""
        killed = self._pool.kill_timed_out(self._config.agent_timeout)
        for role in killed:
            self._health.record_failure(role)

    def _check_memory_pressure(self) -> bool:
        """Check memory pressure. Returns True if spawning should be paused."""
        pressure = self._memory_pressure_fn()
        if pressure >= self._config.memory_pressure_threshold:
            if not self._paused_for_memory:
                logger.warning("[scheduler] Memory pressure %d%% ≥ %d%% — pausing spawning",
                                pressure, self._config.memory_pressure_threshold)
                self._paused_for_memory = True
            return True
        if self._paused_for_memory and pressure < self._config.memory_pressure_resume:
            logger.info("[scheduler] Memory pressure %d%% < %d%% — resuming spawning",
                         pressure, self._config.memory_pressure_resume)
            self._paused_for_memory = False
        return self._paused_for_memory

    def tick(self) -> None:
        """Execute one scheduler cycle (called every interval)."""
        # 1. Reap finished subprocesses
        self._handle_finished()

        # 2. Kill timed-out subprocesses
        self._handle_timeouts()

        # 3. Check memory pressure
        if self._check_memory_pressure():
            return

        # 4. Poll Paperclip for pending tasks
        pending = self._poll_pending_tasks()

        # 5. Enqueue agents with pending work
        self._enqueue_pending(pending)

        # 6. Spawn from queue up to available slots
        self._spawn_from_queue()

    def run(self) -> None:
        """Run the scheduler loop. Blocks until stopped."""
        self._running = True
        logger.info("[scheduler] Starting — %d slots, %d agents, polling every %ds",
                     self._max_slots, len(self._agent_map), self._config.scheduler_interval)

        while self._running:
            try:
                self.tick()
            except Exception:
                logger.exception("[scheduler] Unexpected error in tick")
            time.sleep(self._config.scheduler_interval)

    def stop(self) -> None:
        """Signal the scheduler to stop and terminate all children."""
        logger.info("[scheduler] Stopping — terminating %d active subprocesses",
                     self._pool.active_count)
        self._running = False
        self._pool.terminate_all()

    def get_status(self) -> Dict[str, Any]:
        """Return scheduler state for /healthz endpoint."""
        return {
            "slots_total": self._max_slots,
            "slots_active": self._pool.active_count,
            "slots_available": self._max_slots - self._pool.active_count,
            "queue_depth": len(self._queue),
            "agents_healthy": sum(1 for r in self._agent_map if self._health.is_healthy(r)),
            "agents_unhealthy": sum(1 for r in self._agent_map if not self._health.is_healthy(r)),
            "memory_pressure_pct": self._memory_pressure_fn(),
            "paused_for_memory": self._paused_for_memory,
            "active_roles": sorted(self._pool.active_roles),
        }
```

- [ ] **Step 4: Run the priority queue and health tests**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_scheduler.py -v -k "TestAgentPriority or TestPriorityQueue or TestAgentHealth"`
Expected: All 19 tests PASS

- [ ] **Step 5: Write subprocess pool tests**

Append to `tests/test_scheduler.py`:

```python
# ── Subprocess Pool ──


class TestSubprocessPool:

    def test_spawn_creates_process(self):
        pool = SubprocessPool()
        entry = pool.spawn("cto", "uuid-1", "/tmp/test-instructions",
                           env={"PAPERCLIP_API_URL": "http://localhost:3100"})
        assert entry.role == "cto"
        assert entry.process.pid > 0
        assert pool.active_count == 1
        assert pool.is_running("cto")
        entry.process.kill()
        entry.process.wait()

    def test_reap_finished_process(self):
        pool = SubprocessPool()
        # Spawn a process that exits immediately
        entry = pool.spawn.__wrapped__ if hasattr(pool.spawn, '__wrapped__') else None
        # Use a direct Popen for testability
        proc = subprocess.Popen(["python", "-c", "import sys; sys.exit(0)"])
        active = ActiveProcess(role="cto", agent_id="uuid-1", process=proc,
                               started_at=time.monotonic())
        pool._active["cto"] = active
        proc.wait()  # ensure it finishes
        finished = pool.reap()
        assert finished == [("cto", 0)]
        assert pool.active_count == 0

    def test_kill_timed_out(self):
        pool = SubprocessPool()
        # Spawn a long-running process
        proc = subprocess.Popen(["python", "-c", "import time; time.sleep(300)"])
        active = ActiveProcess(role="cto", agent_id="uuid-1", process=proc,
                               started_at=time.monotonic() - 700)  # 700s ago
        pool._active["cto"] = active
        killed = pool.kill_timed_out(600)
        assert killed == ["cto"]
        assert pool.active_count == 0
        proc.wait()  # cleanup

    def test_active_roles(self):
        pool = SubprocessPool()
        proc = subprocess.Popen(["python", "-c", "import time; time.sleep(300)"])
        pool._active["cto"] = ActiveProcess("cto", "uuid-1", proc, time.monotonic())
        assert pool.active_roles == {"cto"}
        proc.kill()
        proc.wait()
```

- [ ] **Step 6: Run subprocess pool tests**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_scheduler.py::TestSubprocessPool -v`
Expected: All 4 tests PASS

- [ ] **Step 7: Write scheduler integration tests**

Append to `tests/test_scheduler.py`:

```python
# ── Scheduler ──


@pytest.fixture
def scheduler_config():
    return SchedulerConfig(
        max_concurrent_agents=0,
        scheduler_interval=1,
        agent_timeout=600,
        crash_threshold=3,
        backoff_base_minutes=5,
        backoff_max_minutes=60,
        instructions_path="/tmp/test-instructions",
        overrides_path="/tmp/test-overrides",
    )


@pytest.fixture
def mock_paperclip():
    client = MagicMock()
    client.company_id = "company-1"
    return client


@pytest.fixture
def agent_map():
    return {
        "cto": "uuid-1",
        "backend-engineer": "uuid-2",
        "frontend-engineer": "uuid-3",
    }


class TestScheduler:

    def test_tick_with_no_pending_tasks(self, scheduler_config, mock_paperclip, agent_map):
        mock_paperclip.get_assignments_for.return_value = []
        sched = Scheduler(scheduler_config, mock_paperclip, agent_map, max_slots=2,
                          memory_pressure_fn=lambda: 50)
        sched.tick()
        assert sched._pool.active_count == 0

    def test_tick_does_not_spawn_when_memory_pressure_high(
        self, scheduler_config, mock_paperclip, agent_map
    ):
        sched = Scheduler(scheduler_config, mock_paperclip, agent_map, max_slots=2,
                          memory_pressure_fn=lambda: 95)
        sched.tick()
        assert sched._pool.active_count == 0
        assert sched._paused_for_memory is True

    def test_memory_pressure_resumes_below_threshold(
        self, scheduler_config, mock_paperclip, agent_map
    ):
        pressure = [95]
        sched = Scheduler(scheduler_config, mock_paperclip, agent_map, max_slots=2,
                          memory_pressure_fn=lambda: pressure[0])
        sched.tick()
        assert sched._paused_for_memory is True
        pressure[0] = 75
        sched.tick()
        assert sched._paused_for_memory is False

    def test_get_status(self, scheduler_config, mock_paperclip, agent_map):
        sched = Scheduler(scheduler_config, mock_paperclip, agent_map, max_slots=4,
                          memory_pressure_fn=lambda: 67)
        status = sched.get_status()
        assert status["slots_total"] == 4
        assert status["slots_active"] == 0
        assert status["slots_available"] == 4
        assert status["queue_depth"] == 0
        assert status["agents_healthy"] == 3
        assert status["agents_unhealthy"] == 0
        assert status["memory_pressure_pct"] == 67

    def test_resolve_instructions_prefers_override(self, scheduler_config, tmp_path):
        scheduler_config.instructions_path = str(tmp_path / "baked")
        scheduler_config.overrides_path = str(tmp_path / "overrides")

        # Create override
        override_dir = tmp_path / "overrides" / "cto"
        override_dir.mkdir(parents=True)
        (override_dir / "AGENTS.md").write_text("# Override")

        sched = Scheduler(scheduler_config, MagicMock(), {"cto": "uuid-1"}, max_slots=1)
        path = sched._resolve_instructions_path("cto")
        assert path == str(override_dir / "AGENTS.md")

    def test_resolve_instructions_falls_back_to_baked(self, scheduler_config, tmp_path):
        scheduler_config.instructions_path = str(tmp_path / "baked")
        scheduler_config.overrides_path = str(tmp_path / "overrides")

        sched = Scheduler(scheduler_config, MagicMock(), {"cto": "uuid-1"}, max_slots=1)
        path = sched._resolve_instructions_path("cto")
        assert path == str(tmp_path / "baked" / "cto" / "AGENTS.md")

    def test_stop_sets_running_false(self, scheduler_config, mock_paperclip, agent_map):
        sched = Scheduler(scheduler_config, mock_paperclip, agent_map, max_slots=2)
        sched._running = True
        sched.stop()
        assert sched._running is False

    def test_enqueue_skips_running_agent(self, scheduler_config, mock_paperclip, agent_map):
        sched = Scheduler(scheduler_config, mock_paperclip, agent_map, max_slots=2,
                          memory_pressure_fn=lambda: 50)
        # Simulate cto already running
        proc = subprocess.Popen(["python", "-c", "import time; time.sleep(300)"])
        sched._pool._active["cto"] = ActiveProcess("cto", "uuid-1", proc, time.monotonic())
        try:
            sched._enqueue_pending({"cto": 1.0, "backend-engineer": 2.0})
            assert "cto" not in sched._queue
            assert "backend-engineer" in sched._queue
        finally:
            proc.kill()
            proc.wait()
```

- [ ] **Step 8: Run all scheduler tests**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_scheduler.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/scheduler.py tests/test_scheduler.py
git commit -m "feat: add orchestrator scheduler with priority queue, subprocess pool, and health tracking"
```

---

## Task 5: Orchestrator Main Entry Point

**Files:**
- Create: `agents/orchestrator_main.py`
- Create: `tests/test_orchestrator_main.py`

Wires together registry + budget + scheduler. Handles SIGTERM for graceful shutdown.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orchestrator_main.py`:

```python
"""Tests for the orchestrator main entry point."""

import signal
from unittest.mock import MagicMock, patch, call

import pytest
from agents.orchestrator_main import run_orchestrator
from agents.config import SystemConfig, SchedulerConfig, PaperclipConfig
from agents.paperclip_client import AgentInfo


def _agent(role: str, agent_id: str) -> AgentInfo:
    return AgentInfo(id=agent_id, company_id="c1", name=role, role=role)


@pytest.fixture
def config():
    cfg = SystemConfig()
    cfg.paperclip = PaperclipConfig(enabled=True, api_url="http://localhost:3100")
    cfg.scheduler = SchedulerConfig(scheduler_interval=1, max_concurrent_agents=2)
    return cfg


class TestRunOrchestrator:

    @patch("agents.orchestrator_main.Scheduler")
    @patch("agents.orchestrator_main.AgentRegistry")
    @patch("agents.orchestrator_main.calculate_concurrency_budget", return_value=4)
    @patch("agents.orchestrator_main.discover_system")
    @patch("agents.orchestrator_main.PaperclipClient")
    @patch("agents.orchestrator_main.start_health_server")
    def test_wiring(self, mock_health, mock_client_cls, mock_discover,
                    mock_budget, mock_registry_cls, mock_scheduler_cls, config):
        mock_client = mock_client_cls.return_value
        mock_registry = mock_registry_cls.return_value
        mock_registry.resolve_all.return_value = {"cto": "uuid-1", "backend-engineer": "uuid-2"}

        mock_scheduler = mock_scheduler_cls.return_value
        mock_scheduler.run.side_effect = KeyboardInterrupt  # exit immediately

        with pytest.raises(SystemExit):
            run_orchestrator(config)

        # Verify wiring
        mock_discover.assert_called_once()
        mock_registry.resolve_all.assert_called_once()
        mock_scheduler_cls.assert_called_once()
        mock_scheduler.run.assert_called_once()

    @patch("agents.orchestrator_main.Scheduler")
    @patch("agents.orchestrator_main.AgentRegistry")
    @patch("agents.orchestrator_main.calculate_concurrency_budget", return_value=4)
    @patch("agents.orchestrator_main.discover_system")
    @patch("agents.orchestrator_main.PaperclipClient")
    @patch("agents.orchestrator_main.start_health_server")
    def test_override_max_concurrent(self, mock_health, mock_client_cls, mock_discover,
                                      mock_budget, mock_registry_cls, mock_scheduler_cls, config):
        config.scheduler.max_concurrent_agents = 3
        mock_registry = mock_registry_cls.return_value
        mock_registry.resolve_all.return_value = {"cto": "uuid-1"}

        mock_scheduler = mock_scheduler_cls.return_value
        mock_scheduler.run.side_effect = KeyboardInterrupt

        with pytest.raises(SystemExit):
            run_orchestrator(config)

        # Budget should receive override
        mock_budget.assert_called_once()
        _, kwargs = mock_budget.call_args
        assert kwargs.get("override_max") == 3 or mock_budget.call_args[0][-1] == 3

    @patch("agents.orchestrator_main.Scheduler")
    @patch("agents.orchestrator_main.AgentRegistry")
    @patch("agents.orchestrator_main.calculate_concurrency_budget", return_value=2)
    @patch("agents.orchestrator_main.discover_system")
    @patch("agents.orchestrator_main.PaperclipClient")
    @patch("agents.orchestrator_main.start_health_server")
    def test_graceful_shutdown_on_keyboard_interrupt(
        self, mock_health, mock_client_cls, mock_discover,
        mock_budget, mock_registry_cls, mock_scheduler_cls, config
    ):
        mock_registry = mock_registry_cls.return_value
        mock_registry.resolve_all.return_value = {"cto": "uuid-1"}
        mock_scheduler = mock_scheduler_cls.return_value
        mock_scheduler.run.side_effect = KeyboardInterrupt

        with pytest.raises(SystemExit) as exc_info:
            run_orchestrator(config)

        mock_scheduler.stop.assert_called_once()
        assert exc_info.value.code == 0

    @patch("agents.orchestrator_main.PaperclipClient")
    @patch("agents.orchestrator_main.start_health_server")
    def test_exits_if_no_agents_resolved(self, mock_health, mock_client_cls, config):
        mock_client = mock_client_cls.return_value
        mock_client.list_agents.return_value = []

        with patch("agents.orchestrator_main.AgentRegistry") as mock_reg_cls:
            mock_reg = mock_reg_cls.return_value
            mock_reg.resolve_all.return_value = {}

            with pytest.raises(SystemExit) as exc_info:
                run_orchestrator(config)
            assert exc_info.value.code == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_orchestrator_main.py -v`
Expected: `ModuleNotFoundError: No module named 'agents.orchestrator_main'`

- [ ] **Step 3: Write the implementation**

Create `agents/orchestrator_main.py`:

```python
"""
Orchestrator main entry point.

Launched via ``python -m agents.main --orchestrator``.  Resolves agent IDs
from Paperclip, calculates a concurrency budget from system resources, and
runs the scheduler loop until terminated.
"""

import logging
import os
import signal
import sys

from .agent_registry import AgentRegistry
from .concurrency_budget import calculate_concurrency_budget
from .config import SystemConfig
from .metrics import start_health_server
from .paperclip_client import PaperclipClient
from .resource_discovery import discover_system
from .scheduler import Scheduler

logger = logging.getLogger(__name__)

# Module-level reference so the signal handler can reach it
_scheduler: Scheduler | None = None


def run_orchestrator(config: SystemConfig) -> None:
    """Boot the orchestrator: resolve agents, calculate budget, run scheduler.

    This function blocks until SIGTERM/SIGINT, then gracefully shuts down.
    """
    global _scheduler

    logger.info("[orchestrator] Starting resource-aware orchestrator")

    # ── Step 1: Start health server ──
    start_health_server(port=int(os.environ.get("VIBE_HEALTH_PORT", "8080")))

    # ── Step 2: Probe hardware ──
    profile = discover_system()

    # ── Step 3: Calculate concurrency budget ──
    sched_cfg = config.scheduler
    max_slots = calculate_concurrency_budget(
        profile,
        infra_reserve_gb=sched_cfg.infra_reserve_gb,
        slot_cost_gb=sched_cfg.slot_cost_gb,
        override_max=sched_cfg.max_concurrent_agents,
    )
    logger.info("[orchestrator] Detected: %dMB RAM, %d cores → budget: %d concurrent slots",
                 profile.total_ram_mb, profile.cpu_count, max_slots)

    # ── Step 4: Connect to Paperclip and resolve agent IDs ──
    client = PaperclipClient(
        api_url=config.paperclip.api_url or None,
        api_key=config.paperclip.api_key or None,
    )
    registry = AgentRegistry(client, disabled_roles=sched_cfg.disabled_agents)
    agent_map = registry.resolve_all()

    if not agent_map:
        logger.error("[orchestrator] No agents resolved from Paperclip — exiting")
        sys.exit(1)

    logger.info("[orchestrator] Resolved %d agents from Paperclip", len(agent_map))

    # ── Step 5: Create and run scheduler ──
    _scheduler = Scheduler(
        config=sched_cfg,
        client=client,
        agent_map=agent_map,
        max_slots=max_slots,
    )

    # Install signal handlers for graceful shutdown
    def _shutdown(signum, frame):
        logger.info("[orchestrator] Received signal %d — shutting down", signum)
        if _scheduler:
            _scheduler.stop()

    signal.signal(signal.SIGTERM, _shutdown)

    try:
        _scheduler.run()
    except KeyboardInterrupt:
        logger.info("[orchestrator] Interrupted — shutting down")
        _scheduler.stop()
        sys.exit(0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_orchestrator_main.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/orchestrator_main.py tests/test_orchestrator_main.py
git commit -m "feat: add orchestrator main entry point wiring registry, budget, and scheduler"
```

---

## Task 6: CLI Integration — `--orchestrator`, `--agent-id`, `--instructions`

**Files:**
- Modify: `agents/main.py:400-465` — add three new CLI args and orchestrator dispatch

- [ ] **Step 1: Add CLI arguments to argparse**

In `agents/main.py`, after line 404 (`--spending-reset`), add:

```python
    parser.add_argument("--orchestrator", action="store_true",
                        help="Run orchestrator scheduler (long-running, manages agent subprocesses)")
    parser.add_argument("--agent-id", type=str, default=None,
                        help="Override PAPERCLIP_AGENT_ID (used by orchestrator subprocesses)")
    parser.add_argument("--instructions", type=str, default=None,
                        help="Path to agent instruction file (used by orchestrator subprocesses)")
```

- [ ] **Step 2: Add orchestrator dispatch block**

In `agents/main.py`, after the heartbeat dispatch block (after line 465), add:

```python
    # Orchestrator mode — long-running scheduler
    if args.orchestrator:
        from .orchestrator_main import run_orchestrator
        setup_logging(config)
        config.paperclip.enabled = True
        run_orchestrator(config)
        return
```

- [ ] **Step 3: Wire `--agent-id` and `--instructions` into heartbeat dispatch**

In `agents/main.py`, modify the heartbeat block (lines 455-465). Before `result = run_heartbeat(config)`, add:

```python
        if args.agent_id:
            os.environ["PAPERCLIP_AGENT_ID"] = args.agent_id
        if args.instructions:
            os.environ["VIBE_INSTRUCTIONS_PATH"] = args.instructions
```

Add `import os` to the top of the file if not already present.

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/ -x -m "not e2e" --no-header -q 2>&1 | tail -5`
Expected: All existing tests still pass

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/main.py
git commit -m "feat: add --orchestrator, --agent-id, and --instructions CLI args"
```

---

## Task 7: Health Endpoint Expansion

**Files:**
- Modify: `agents/metrics.py:221-290` — expand `/healthz` to include scheduler state

The scheduler exposes its state via `Scheduler.get_status()`. The health handler needs a way to access it. We use a module-level setter so `orchestrator_main.py` can register the scheduler after creation.

- [ ] **Step 1: Add scheduler status hook to health handler**

In `agents/metrics.py`, add a module-level variable near the top (after line 32):

```python
# Optional scheduler status provider — set by orchestrator_main
_scheduler_status_fn: Optional[Callable[[], Dict[str, Any]]] = None


def set_scheduler_status_fn(fn: Callable[[], Dict[str, Any]]) -> None:
    """Register a scheduler status provider for the /healthz endpoint."""
    global _scheduler_status_fn
    _scheduler_status_fn = fn
```

- [ ] **Step 2: Expand `_handle_healthz` to include scheduler state**

In `agents/metrics.py`, at the end of `_handle_healthz` (before the response is sent, around line 290), add:

```python
        # Check 5: Scheduler state (when orchestrator mode is active)
        if _scheduler_status_fn is not None:
            try:
                checks["scheduler"] = _scheduler_status_fn()
            except Exception:
                checks["scheduler"] = "probe_failed"
```

- [ ] **Step 3: Wire the scheduler status in orchestrator_main.py**

In `agents/orchestrator_main.py`, after the scheduler is created (after `_scheduler = Scheduler(...)`) add:

```python
    from .metrics import set_scheduler_status_fn
    set_scheduler_status_fn(_scheduler.get_status)
```

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/ -x -m "not e2e" --no-header -q 2>&1 | tail -5`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/metrics.py agents/orchestrator_main.py
git commit -m "feat: expand /healthz endpoint to include scheduler state in orchestrator mode"
```

---

## Task 8: Instruction File Scaffolding

**Files:**
- Create: `agents/instructions/cto/AGENTS.md` (and 9 other role directories)
- Create: `agents/instructions/shared/README.md`

These are placeholder files so the Dockerfile COPY works and the instruction path resolves. Real instruction content will be authored separately.

- [ ] **Step 1: Create directory structure and placeholder files**

```bash
cd ~/Repos/Vibe-Stack

for role in cto cto-assistant backend-engineer backend-assistant \
            frontend-engineer frontend-assistant devops-engineer \
            devops-assistant qa-engineer qa-assistant; do
    mkdir -p "agents/instructions/${role}"
    cat > "agents/instructions/${role}/AGENTS.md" << EOF
# ${role} Agent Instructions

You are the **${role}** agent in the Vibe Stack organization.

<!-- TODO: Add role-specific instructions, constraints, and workflows -->
EOF
done

mkdir -p agents/instructions/shared
cat > agents/instructions/shared/README.md << 'EOF'
# Shared Instruction Resources

Files in this directory are shared across all agent roles.
Place common reference documents, coding standards, and
organizational policies here.
EOF
```

- [ ] **Step 2: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/instructions/
git commit -m "feat: scaffold agent instruction directories for orchestrator bootstrap"
```

---

## Task 9: Dockerfile Changes

**Files:**
- Modify: `Dockerfile:39-66` — add instruction COPY, add overrides directory, change default CMD

- [ ] **Step 1: Add instruction COPY to Dockerfile**

After line 41 (`COPY --chown=vibe:vibe scripts/ scripts/`), add:

```dockerfile
COPY --chown=vibe:vibe agents/instructions/ /opt/vibe/instructions/
```

- [ ] **Step 2: Create overrides directory**

After line 47 (`RUN mkdir -p /home/vibe/.vibe/skills && chown -R vibe:vibe /home/vibe/.vibe`), add:

```dockerfile
# Create empty overrides directory (mounted as volume for dev-time instruction editing)
RUN mkdir -p /opt/vibe/overrides && chown -R vibe:vibe /opt/vibe/overrides
```

- [ ] **Step 3: Change default CMD to orchestrator mode**

Change line 66 from:
```dockerfile
CMD ["--heartbeat"]
```
to:
```dockerfile
CMD ["--orchestrator"]
```

- [ ] **Step 4: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add Dockerfile
git commit -m "feat: bake agent instructions into image, default to orchestrator mode"
```

---

## Task 10: Docker Compose Changes

**Files:**
- Modify: `docker-compose.yml:148-193` — add secret mounts, env vars, raise resource limits

- [ ] **Step 1: Add orchestrator env vars**

In `docker-compose.yml`, in the `vibe` service `environment:` block (after line 180), add:

```yaml
      - VIBE_MAX_CONCURRENT_AGENTS=${VIBE_MAX_CONCURRENT_AGENTS:-0}
      - VIBE_SCHEDULER_INTERVAL=${VIBE_SCHEDULER_INTERVAL:-30}
      - VIBE_AGENT_TIMEOUT=${VIBE_AGENT_TIMEOUT:-600}
      - VIBE_DISABLED_AGENTS=${VIBE_DISABLED_AGENTS:-}
      - VIBE_MEMORY_PRESSURE_THRESHOLD=${VIBE_MEMORY_PRESSURE_THRESHOLD:-90}
      - VIBE_INFRA_RESERVE_GB=${VIBE_INFRA_RESERVE_GB:-10}
```

- [ ] **Step 2: Add overrides volume mount**

In the `volumes:` block of the vibe service (after line 186), add:

```yaml
      - ${VIBE_INSTRUCTIONS_OVERRIDE_PATH:-/dev/null}:/opt/vibe/overrides:ro
```

- [ ] **Step 3: Raise resource limits for orchestrator**

Change the `deploy.resources.limits` (lines 189-191) to allow headroom for orchestrator + subprocesses:

```yaml
        limits:
          cpus: "${VIBE_ORCHESTRATOR_CPUS:-4.0}"
          memory: ${VIBE_ORCHESTRATOR_MEMORY_LIMIT:-4G}
```

- [ ] **Step 4: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add docker-compose.yml
git commit -m "feat: add orchestrator env vars, override mount, and raised resource limits"
```

---

## Task 11: Environment Variable Documentation

**Files:**
- Modify: `.env.example` — add orchestrator section at the end

- [ ] **Step 1: Append orchestrator section to .env.example**

Add to the end of `.env.example`:

```bash

# ── Orchestrator (resource-aware scheduler) ────────────────────────────
# These control the --orchestrator mode that manages agent subprocesses.
# All are optional — auto-detection works out of the box.
VIBE_MAX_CONCURRENT_AGENTS=           # 0 or empty = auto-detect from hardware
VIBE_SCHEDULER_INTERVAL=30            # Poll interval in seconds
VIBE_AGENT_TIMEOUT=600                # Per-heartbeat timeout in seconds
VIBE_DISABLED_AGENTS=                 # Comma-separated roles to skip (e.g. frontend-engineer,frontend-assistant)
VIBE_MEMORY_PRESSURE_THRESHOLD=90     # Pause spawning above this % memory usage
VIBE_INFRA_RESERVE_GB=10              # RAM (GB) reserved for non-agent containers
VIBE_ORCHESTRATOR_CPUS=4.0            # CPU limit for orchestrator container
VIBE_ORCHESTRATOR_MEMORY_LIMIT=4G     # Memory limit for orchestrator container
VIBE_INSTRUCTIONS_OVERRIDE_PATH=      # Host path for instruction overrides (dev only)
```

- [ ] **Step 2: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add .env.example
git commit -m "docs: add orchestrator env vars to .env.example"
```

---

## Task 12: Config Env Var Loading

**Files:**
- Modify: `agents/config.py` — add env var loading for `SchedulerConfig`

The existing config dataclasses don't auto-read env vars — they're set by `main.py` or the adapter. The orchestrator needs to read `VIBE_*` env vars into `SchedulerConfig` before passing it to the scheduler.

- [ ] **Step 1: Add `from_env()` classmethod to SchedulerConfig**

In `agents/config.py`, add a classmethod to `SchedulerConfig`:

```python
    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        """Create SchedulerConfig by reading VIBE_* environment variables."""
        disabled_str = os.environ.get("VIBE_DISABLED_AGENTS", "")
        disabled = [r.strip() for r in disabled_str.split(",") if r.strip()]
        return cls(
            max_concurrent_agents=int(os.environ.get("VIBE_MAX_CONCURRENT_AGENTS", "0")),
            scheduler_interval=int(os.environ.get("VIBE_SCHEDULER_INTERVAL", "30")),
            agent_timeout=int(os.environ.get("VIBE_AGENT_TIMEOUT", "600")),
            disabled_agents=disabled,
            memory_pressure_threshold=int(os.environ.get("VIBE_MEMORY_PRESSURE_THRESHOLD", "90")),
            memory_pressure_resume=int(os.environ.get("VIBE_MEMORY_PRESSURE_RESUME", "80")),
            infra_reserve_gb=int(os.environ.get("VIBE_INFRA_RESERVE_GB", "10")),
            slot_cost_gb=float(os.environ.get("VIBE_SLOT_COST_GB", "1.5")),
        )
```

- [ ] **Step 2: Use `from_env()` in orchestrator dispatch**

In `agents/main.py`, in the orchestrator dispatch block, add before `run_orchestrator(config)`:

```python
        config.scheduler = SchedulerConfig.from_env()
```

And add the import at the top of the orchestrator block:

```python
        from .config import SchedulerConfig
```

- [ ] **Step 3: Write a quick test for from_env**

Add to an existing test file or create inline:

```python
# In tests/test_concurrency_budget.py or a new test_scheduler_config.py

def test_scheduler_config_from_env():
    import os
    from agents.config import SchedulerConfig

    os.environ["VIBE_MAX_CONCURRENT_AGENTS"] = "5"
    os.environ["VIBE_DISABLED_AGENTS"] = "frontend-engineer, frontend-assistant"
    try:
        cfg = SchedulerConfig.from_env()
        assert cfg.max_concurrent_agents == 5
        assert cfg.disabled_agents == ["frontend-engineer", "frontend-assistant"]
    finally:
        os.environ.pop("VIBE_MAX_CONCURRENT_AGENTS", None)
        os.environ.pop("VIBE_DISABLED_AGENTS", None)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/ -x -m "not e2e" --no-header -q 2>&1 | tail -5`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/config.py agents/main.py
git commit -m "feat: add SchedulerConfig.from_env() for orchestrator env var loading"
```

---

## Task 13: Polling Implementation — `get_assignments_for`

**Files:**
- Modify: `agents/paperclip_client.py:337-355` — add `get_assignments_for()` method

The scheduler needs to poll assignments for arbitrary agent IDs (not just `self.agent_id`). The existing `get_assignments()` only queries for the client's own agent.

- [ ] **Step 1: Add `get_assignments_for()` method**

In `agents/paperclip_client.py`, after the `get_assignments()` method (after line 355), add:

```python
    def get_assignments_for(
        self,
        agent_id: str,
        statuses: Optional[List[str]] = None,
    ) -> List[Issue]:
        """GET /api/companies/{companyId}/issues — fetch tasks assigned to a specific agent."""
        if statuses is None:
            statuses = ["todo"]

        params: Dict[str, str] = {
            "assigneeAgentId": agent_id,
            "status": ",".join(statuses),
        }
        data = self._request(
            "GET",
            f"/api/companies/{self.company_id}/issues",
            params=params,
        )
        issues_list = data if isinstance(data, list) else data.get("issues", data.get("data", []))
        return [_parse_issue(item) for item in issues_list]
```

- [ ] **Step 2: Run existing Paperclip client tests**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_paperclip_client.py -v 2>&1 | tail -10`
Expected: All existing tests still pass

- [ ] **Step 3: Commit**

```bash
cd ~/Repos/Vibe-Stack
git add agents/paperclip_client.py
git commit -m "feat: add get_assignments_for() to PaperclipClient for orchestrator polling"
```

---

## Task 14: Full Integration Smoke Test

**Files:**
- No new files — verify everything compiles and wires together

- [ ] **Step 1: Run the full test suite**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/ -x -m "not e2e" --no-header -q 2>&1 | tail -10`
Expected: All ~2970+ tests pass (including new tests from tasks 1-5)

- [ ] **Step 2: Verify the CLI arg registers**

Run: `cd ~/Repos/Vibe-Stack && python -m agents.main --help 2>&1 | grep -E "orchestrator|agent-id|instructions"`
Expected:
```
  --orchestrator        Run orchestrator scheduler (long-running, manages agent subprocesses)
  --agent-id AGENT_ID   Override PAPERCLIP_AGENT_ID (used by orchestrator subprocesses)
  --instructions INSTRUCTIONS
                        Path to agent instruction file (used by orchestrator subprocesses)
```

- [ ] **Step 3: Verify import chain**

Run: `cd ~/Repos/Vibe-Stack && python -c "from agents.orchestrator_main import run_orchestrator; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Verify Docker build succeeds**

Run: `cd ~/Repos/Vibe-Stack && docker build -t vibe-test --target runtime . 2>&1 | tail -5`
Expected: Build completes successfully

- [ ] **Step 5: Commit any fixups if needed, then tag**

```bash
cd ~/Repos/Vibe-Stack
git log --oneline -15  # Review all commits from this plan
```

---

## Summary

| Task | Description | New Tests | Files |
|------|------------|-----------|-------|
| 1 | Concurrency budget calculator | 9 | 2 new |
| 2 | Agent registry | 8 | 2 new |
| 3 | Scheduler config | 0 (config-only) | 1 modified |
| 4 | Scheduler (queue, pool, health, loop) | ~27 | 2 new |
| 5 | Orchestrator main entry point | 4 | 2 new |
| 6 | CLI integration | 0 (manual verify) | 1 modified |
| 7 | Health endpoint expansion | 0 (wiring-only) | 2 modified |
| 8 | Instruction file scaffolding | 0 | 11 new |
| 9 | Dockerfile changes | 0 | 1 modified |
| 10 | Docker Compose changes | 0 | 1 modified |
| 11 | Env var documentation | 0 | 1 modified |
| 12 | Config env var loading | 1 | 2 modified |
| 13 | Polling implementation | 0 | 1 modified |
| 14 | Integration smoke test | 0 | 0 |

**Total: ~49 new tests, 8 new files, 9 modified files, 14 tasks**
