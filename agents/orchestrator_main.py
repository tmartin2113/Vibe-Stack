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
from .metrics import start_health_server, set_scheduler_status_fn
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

    # Step 1: Start health server
    start_health_server(port=int(os.environ.get("VIBE_HEALTH_PORT", "8080")))

    # Step 2: Probe hardware
    profile = discover_system()

    # Step 3: Calculate concurrency budget
    sched_cfg = config.scheduler
    max_slots = calculate_concurrency_budget(
        profile,
        infra_reserve_gb=sched_cfg.infra_reserve_gb,
        slot_cost_gb=sched_cfg.slot_cost_gb,
        override_max=sched_cfg.max_concurrent_agents,
    )
    logger.info("[orchestrator] Detected: %dMB RAM, %d cores → budget: %d concurrent slots",
                 profile.total_ram_mb, profile.cpu_count, max_slots)

    # Step 4: Connect to Paperclip and resolve agent IDs
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

    # Step 5: Create and run scheduler
    _scheduler = Scheduler(
        config=sched_cfg,
        client=client,
        agent_map=agent_map,
        max_slots=max_slots,
    )

    # Wire scheduler status into health endpoint
    set_scheduler_status_fn(_scheduler.get_status)

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
