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
