"""
Tests for agents/scheduler.py

Tests the priority queue, agent health tracker, subprocess pool,
and the main scheduler loop.

TDD: tests written first, run to confirm import failure, then implementation.
"""

import os
import time
import subprocess
from dataclasses import dataclass
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest

from agents.config import SchedulerConfig
from agents.scheduler import (
    AgentPriority,
    PriorityQueue,
    AgentHealth,
    SubprocessPool,
    ActiveProcess,
    Scheduler,
    get_memory_pressure_pct,
)


# ---------------------------------------------------------------------------
# TestAgentPriority
# ---------------------------------------------------------------------------

class TestAgentPriority:
    """AgentPriority maps role names to static integer priority levels."""

    def test_cto_is_priority_0(self):
        assert AgentPriority.for_role("cto") == 0

    def test_senior_engineers_are_priority_1(self):
        for role in ("backend-engineer", "frontend-engineer", "devops-engineer", "qa-engineer"):
            assert AgentPriority.for_role(role) == 1, f"expected 1 for {role}"

    def test_assistants_are_priority_2(self):
        for role in (
            "cto-assistant",
            "backend-assistant",
            "frontend-assistant",
            "devops-assistant",
            "qa-assistant",
        ):
            assert AgentPriority.for_role(role) == 2, f"expected 2 for {role}"

    def test_unknown_role_defaults_to_2(self):
        assert AgentPriority.for_role("wizard") == 2
        assert AgentPriority.for_role("") == 2


# ---------------------------------------------------------------------------
# TestPriorityQueue
# ---------------------------------------------------------------------------

class TestPriorityQueue:
    """Min-heap priority queue for (priority, created_at, role, agent_id)."""

    def test_empty_pop_returns_none(self):
        q = PriorityQueue()
        assert q.pop() is None

    def test_higher_priority_pops_first(self):
        q = PriorityQueue()
        t = time.time()
        q.push("backend-engineer", "agent-b", t)
        q.push("cto", "agent-c", t + 1)
        role, agent_id = q.pop()
        assert role == "cto"
        assert agent_id == "agent-c"

    def test_same_priority_fifo_by_created_at(self):
        q = PriorityQueue()
        t = time.time()
        q.push("backend-engineer", "agent-first", t)
        q.push("frontend-engineer", "agent-second", t + 5)
        role, agent_id = q.pop()
        assert agent_id == "agent-first"

    def test_duplicate_role_push_is_skipped(self):
        q = PriorityQueue()
        t = time.time()
        q.push("cto", "agent-1", t)
        q.push("cto", "agent-2", t + 1)  # duplicate role — should be skipped
        assert len(q) == 1
        role, agent_id = q.pop()
        assert agent_id == "agent-1"

    def test_len_works(self):
        q = PriorityQueue()
        assert len(q) == 0
        t = time.time()
        q.push("cto", "a1", t)
        assert len(q) == 1
        q.push("backend-engineer", "a2", t + 1)
        assert len(q) == 2
        q.pop()
        assert len(q) == 1

    def test_contains_works(self):
        q = PriorityQueue()
        t = time.time()
        q.push("cto", "a1", t)
        assert "cto" in q
        assert "backend-engineer" not in q
        q.pop()
        assert "cto" not in q


# ---------------------------------------------------------------------------
# TestAgentHealth
# ---------------------------------------------------------------------------

class TestAgentHealth:
    """AgentHealth tracks consecutive failures and computes backoff."""

    def _health(self, threshold=3, backoff_base=5, backoff_max=60):
        cfg = SchedulerConfig(
            crash_threshold=threshold,
            backoff_base_minutes=backoff_base,
            backoff_max_minutes=backoff_max,
        )
        return AgentHealth(cfg)

    def test_initial_state_is_healthy(self):
        h = self._health()
        assert h.is_healthy("cto") is True
        assert h.consecutive_failures("cto") == 0

    def test_record_success_resets_failures(self):
        h = self._health()
        h.record_failure("cto")
        h.record_failure("cto")
        h.record_success("cto")
        assert h.consecutive_failures("cto") == 0
        assert h.is_healthy("cto") is True

    def test_threshold_failures_marks_unhealthy(self):
        h = self._health(threshold=3)
        h.record_failure("cto")
        h.record_failure("cto")
        assert h.is_healthy("cto") is True   # 2 < threshold
        h.record_failure("cto")
        assert h.is_healthy("cto") is False  # 3 == threshold

    def test_backoff_increases_exponentially(self):
        """backoff_base * 3^(failures - threshold) minutes → seconds."""
        h = self._health(threshold=3, backoff_base=5, backoff_max=120)
        for _ in range(3):
            h.record_failure("cto")
        # failures == threshold → 5 * 3^(0) = 5 min
        assert h.backoff_seconds("cto") == pytest.approx(5 * 60, abs=1)

        h.record_failure("cto")
        # failures == threshold+1 → 5 * 3^(1) = 15 min
        assert h.backoff_seconds("cto") == pytest.approx(15 * 60, abs=1)

        h.record_failure("cto")
        # failures == threshold+2 → 5 * 3^(2) = 45 min
        assert h.backoff_seconds("cto") == pytest.approx(45 * 60, abs=1)

    def test_backoff_capped_at_max(self):
        h = self._health(threshold=3, backoff_base=5, backoff_max=60)
        for _ in range(10):
            h.record_failure("cto")
        assert h.backoff_seconds("cto") == pytest.approx(60 * 60, abs=1)

    def test_is_ready_false_during_backoff(self):
        h = self._health(threshold=3)
        for _ in range(3):
            h.record_failure("cto")
        # Just became unhealthy — should not be ready (within backoff window)
        assert h.is_ready("cto") is False

    def test_is_ready_true_after_backoff_elapses(self):
        h = self._health(threshold=3, backoff_base=5)
        for _ in range(3):
            h.record_failure("cto")
        # Backdate the last failure timestamp so the backoff window has passed
        h._last_failure_time["cto"] = time.monotonic() - 301
        assert h.is_ready("cto") is True


# ---------------------------------------------------------------------------
# TestSubprocessPool
# ---------------------------------------------------------------------------

class TestSubprocessPool:
    """SubprocessPool manages running agent subprocesses."""

    def test_spawn_creates_process(self, tmp_path):
        pool = SubprocessPool(agent_timeout=60)
        role = "cto"
        agent_id = "agent-test-1"
        # Patch Popen so the test doesn't require the real agents.main module
        with patch("agents.scheduler.subprocess.Popen") as mock_popen:
            fake_process = MagicMock()
            fake_process.pid = 9999
            fake_process.poll.return_value = None  # still running
            mock_popen.return_value = fake_process

            proc = pool.spawn(role, agent_id, str(tmp_path), env=os.environ.copy())
            assert proc is not None
            assert pool.is_running(role)
            assert pool.active_count == 1
            # Verify the spawn command includes expected args
            call_args = mock_popen.call_args[0][0]
            assert "--heartbeat" in call_args
            assert "--agent-id" in call_args
            assert agent_id in call_args

    def test_reap_detects_finished_process(self, tmp_path):
        pool = SubprocessPool(agent_timeout=60)
        # Use a process that exits immediately
        role = "backend-engineer"
        agent_id = "agent-test-2"
        env = os.environ.copy()
        env["PAPERCLIP_AGENT_ID"] = agent_id

        # Manually insert a fast-exiting process into the pool
        fast_proc = subprocess.Popen(["python3", "-c", "import sys; sys.exit(0)"])
        fast_proc.wait()  # ensure it's actually done

        ap = ActiveProcess(
            role=role,
            agent_id=agent_id,
            process=fast_proc,
            started_at=time.monotonic(),
        )
        pool._active[role] = ap

        reaped = pool.reap()
        assert len(reaped) == 1
        assert reaped[0][0] == role
        assert reaped[0][1] == 0  # exit code

    def test_kill_timed_out_kills_old_processes(self, tmp_path):
        pool = SubprocessPool(agent_timeout=60)
        role = "devops-engineer"
        agent_id = "agent-test-3"

        # Start a sleeping process
        sleepy = subprocess.Popen(["python3", "-c", "import time; time.sleep(999)"])
        ap = ActiveProcess(
            role=role,
            agent_id=agent_id,
            process=sleepy,
            started_at=time.monotonic() - 700,  # way past 60s timeout
        )
        pool._active[role] = ap

        killed = pool.kill_timed_out(timeout=60)
        assert role in killed
        sleepy.wait()

    def test_active_roles_property(self, tmp_path):
        pool = SubprocessPool(agent_timeout=60)
        assert pool.active_roles == set()

        # Add a fake process directly
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None  # still running
        pool._active["cto"] = ActiveProcess(
            role="cto",
            agent_id="x",
            process=fake_proc,
            started_at=time.monotonic(),
        )
        assert "cto" in pool.active_roles


# ---------------------------------------------------------------------------
# TestScheduler
# ---------------------------------------------------------------------------

class TestScheduler:
    """Main scheduler loop tests."""

    def _make_scheduler(self, cfg=None, memory_pressure_fn=None, agent_map=None):
        if cfg is None:
            cfg = SchedulerConfig(
                scheduler_interval=30,
                agent_timeout=600,
                crash_threshold=3,
                backoff_base_minutes=5,
                backoff_max_minutes=60,
            )
        client = MagicMock()
        client.get_assignments_for = MagicMock(return_value=[])
        if agent_map is None:
            agent_map = {"cto": "agent-cto-1"}
        if memory_pressure_fn is None:
            memory_pressure_fn = lambda: 0
        scheduler = Scheduler(
            config=cfg,
            client=client,
            agent_map=agent_map,
            max_slots=3,
            memory_pressure_fn=memory_pressure_fn,
        )
        return scheduler

    def test_tick_with_no_pending_tasks_zero_active(self):
        s = self._make_scheduler()
        s.tick()
        assert s._pool.active_count == 0

    def test_tick_pauses_when_memory_pressure_high(self):
        s = self._make_scheduler(memory_pressure_fn=lambda: 95)
        # tick should detect high pressure and skip spawn
        s.tick()
        # Even with agents in the queue, nothing should be spawned
        assert s._pool.active_count == 0

    def test_memory_pressure_resumes_below_threshold(self):
        """Pressure flag resets once pressure drops below resume level."""
        s = self._make_scheduler(memory_pressure_fn=lambda: 75)
        # Manually trip the pressure flag
        s._memory_paused = True
        result = s._check_memory_pressure()
        # 75% < 80% resume threshold → should resume
        assert s._memory_paused is False

    def test_get_status_returns_correct_fields(self):
        s = self._make_scheduler()
        status = s.get_status()
        assert "active_count" in status
        assert "queue_size" in status
        assert "memory_paused" in status
        assert "running" in status

    def test_resolve_instructions_prefers_override_dir(self, tmp_path):
        baked = tmp_path / "baked"
        overrides = tmp_path / "overrides"
        baked.mkdir()
        overrides.mkdir()
        # Write an override for cto
        (overrides / "cto.md").write_text("override instructions")

        cfg = SchedulerConfig(
            instructions_path=str(baked),
            overrides_path=str(overrides),
        )
        s = self._make_scheduler(cfg=cfg)
        resolved = s._resolve_instructions_path("cto")
        assert str(overrides) in resolved

    def test_resolve_instructions_falls_back_to_baked(self, tmp_path):
        baked = tmp_path / "baked"
        overrides = tmp_path / "overrides"
        baked.mkdir()
        overrides.mkdir()
        # No override file for cto

        cfg = SchedulerConfig(
            instructions_path=str(baked),
            overrides_path=str(overrides),
        )
        s = self._make_scheduler(cfg=cfg)
        resolved = s._resolve_instructions_path("cto")
        assert str(baked) in resolved

    def test_stop_sets_running_false(self):
        s = self._make_scheduler()
        s._running = True
        # Patch terminate_all so we don't actually spawn/kill anything
        s._pool.terminate_all = MagicMock()
        s.stop()
        assert s._running is False

    def test_enqueue_skips_already_running_agents(self):
        s = self._make_scheduler(agent_map={"cto": "agent-cto-1"})
        # Mark cto as already running
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        s._pool._active["cto"] = ActiveProcess(
            role="cto",
            agent_id="agent-cto-1",
            process=fake_proc,
            started_at=time.monotonic(),
        )
        # Try to enqueue cto
        pending = {"agent-cto-1": time.time()}
        s._enqueue_pending(pending)
        # Queue should remain empty — cto is already running
        assert len(s._queue) == 0
