"""
Simulation Budget — Hardware-Aware VRAM Gating for Simulation Module

Environment knobs, SimulationBudget dataclass, and budget assessment
functions extracted from simulation.py for clarity and reuse.

All simulation integration points (sidecar, clarification, skill
vetting) import their hardware gating from this module.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Environment knobs ──────────────────────────────────────────────

# Minimum free VRAM (MB) required to run the **sidecar** simulation
# (which competes with specialists for KV cache).  Set high because
# on constrained cards (<=24GB) the sidecar steals KV slots from
# specialists and adds latency rather than hiding behind it.
# Clarification simulation is NOT gated by this — it runs when the
# specialist is paused, so there's no KV contention.
_MIN_FREE_VRAM_MB_SIDECAR = int(os.getenv("VIBE_SIM_MIN_FREE_VRAM_MB", "6144"))

# Maximum persona rounds per simulation invocation (sequential).
_MAX_PERSONA_ROUNDS = int(os.getenv("VIBE_SIM_MAX_PERSONA_ROUNDS", "3"))

# Token budget for simulation adapter calls (keeps KV cache small).
_SIM_MAX_TOKENS = int(os.getenv("VIBE_SIM_MAX_TOKENS", "600"))

# Confidence threshold: simulated clarification answers below this
# are discarded and the questions go to the human as before.
_CLARIFICATION_CONFIDENCE_THRESHOLD = float(
    os.getenv("VIBE_SIM_CLARIFICATION_CONFIDENCE", "0.6")
)

# Master kill-switch: set VIBE_SIM_ENABLED=false to disable all simulation.
_SIM_ENABLED = os.getenv("VIBE_SIM_ENABLED", "true").lower() not in (
    "false", "0", "no",
)

# Skill vetting: set VIBE_SIM_VET_SKILLS=false to skip offline skill vetting.
_VET_SKILLS_ENABLED = os.getenv("VIBE_SIM_VET_SKILLS", "true").lower() not in (
    "false", "0", "no",
)


# ── Hardware gating ────────────────────────────────────────────────

@dataclass
class SimulationBudget:
    """Hardware-aware budget for a simulation run."""
    enabled: bool = True
    max_rounds: int = _MAX_PERSONA_ROUNDS
    max_tokens: int = _SIM_MAX_TOKENS
    reason: str = ""  # Why disabled (for logging)


def assess_simulation_budget(
    system_profile: Optional[Any] = None,
    mode: str = "sidecar",
) -> SimulationBudget:
    """Determine whether simulation can run and with what constraints.

    Two modes with very different VRAM requirements:

    **sidecar** — runs concurrently with specialists.  Competes for KV
    cache, so it needs substantial free VRAM (>=6GB) to avoid slowing
    down the specialists.  On 22GB cards with a 9B model this will
    typically be disabled, which is correct — the sidecar would hurt
    more than help at that VRAM level.

    **clarification** — runs when the specialist is paused waiting for
    human input.  The GPU is idle, so there is zero KV contention.
    This mode is always enabled (unless the master kill-switch is off)
    because even on 22GB the LLM has full VRAM to itself.

    Checks (in order):
        1. Master kill-switch (VIBE_SIM_ENABLED)
        2. Mode-specific VRAM gating
        3. Available headroom → rounds/token scaling
    """
    if not _SIM_ENABLED:
        return SimulationBudget(
            enabled=False, reason="Disabled via VIBE_SIM_ENABLED=false"
        )

    # ── Clarification mode: no KV contention, always run ──
    if mode == "clarification":
        # No VRAM gating — the specialist is paused, GPU is idle.
        # Scale rounds based on whether we have a GPU at all (affects
        # throughput but not correctness).
        free_vram_mb = _probe_free_vram(system_profile, mode="clarification")
        if free_vram_mb is None:
            # CPU-only: fewer rounds for throughput reasons
            return SimulationBudget(
                enabled=True,
                max_rounds=min(2, _MAX_PERSONA_ROUNDS),
                max_tokens=min(400, _SIM_MAX_TOKENS),
                reason="Clarification mode, CPU-only (no contention)",
            )
        return SimulationBudget(
            enabled=True,
            max_rounds=_MAX_PERSONA_ROUNDS,
            max_tokens=_SIM_MAX_TOKENS,
            reason=f"Clarification mode, GPU idle ({free_vram_mb}MB free)",
        )

    # ── Sidecar mode: competing with specialists for KV cache ──
    free_vram_mb = _probe_free_vram(system_profile, mode="sidecar")

    if free_vram_mb is not None:
        if free_vram_mb < _MIN_FREE_VRAM_MB_SIDECAR:
            return SimulationBudget(
                enabled=False,
                reason=(
                    f"Sidecar disabled: {free_vram_mb}MB free VRAM < "
                    f"{_MIN_FREE_VRAM_MB_SIDECAR}MB threshold "
                    f"(would compete with specialists for KV cache)"
                ),
            )

        # Scale rounds based on available headroom above the floor
        if free_vram_mb < 8192:
            # Just above threshold — minimal sidecar
            return SimulationBudget(
                enabled=True,
                max_rounds=min(2, _MAX_PERSONA_ROUNDS),
                max_tokens=min(400, _SIM_MAX_TOKENS),
                reason=f"Sidecar constrained ({free_vram_mb}MB free)",
            )
        elif free_vram_mb < 16384:
            # Comfortable headroom
            return SimulationBudget(
                enabled=True,
                max_rounds=min(3, _MAX_PERSONA_ROUNDS),
                max_tokens=min(600, _SIM_MAX_TOKENS),
                reason=f"Sidecar moderate ({free_vram_mb}MB free)",
            )
        else:
            # Plenty of headroom (multi-GPU or large card)
            return SimulationBudget(
                enabled=True,
                max_rounds=_MAX_PERSONA_ROUNDS,
                max_tokens=_SIM_MAX_TOKENS,
                reason=f"Sidecar ample ({free_vram_mb}MB free)",
            )

    # No GPU detected — CPU-only.  Sidecar still competes for inference
    # throughput, so keep it minimal.
    return SimulationBudget(
        enabled=True,
        max_rounds=min(2, _MAX_PERSONA_ROUNDS),
        max_tokens=min(400, _SIM_MAX_TOKENS),
        reason="Sidecar on CPU-only (throughput-limited)",
    )


def _probe_free_vram(
    system_profile: Optional[Any] = None,
    mode: str = "sidecar",
) -> Optional[int]:
    """Return free VRAM in MB, or None if no GPU available.

    For **sidecar** mode, uses real-time nvidia-smi data via
    resource_discovery for accurate free memory (since specialists are
    actively consuming KV cache and the static SystemProfile total is
    unreliable).  Falls back to a conservative heuristic if the
    real-time probe fails.

    For **clarification** mode, the GPU is idle so the static estimate
    is fine — uses 35% of total as the free estimate.
    """
    # Try real-time probe first for sidecar mode via resource_discovery
    # (avoids duplicating nvidia-smi logic)
    if mode == "sidecar":
        try:
            from .resource_discovery import get_free_vram_mb
            realtime = get_free_vram_mb()
        except ImportError:
            realtime = _nvidia_smi_free_mb()
        if realtime is not None:
            return realtime

    if system_profile is not None:
        if not getattr(system_profile, "has_gpu", False):
            return None
        total = getattr(system_profile, "total_vram_mb", 0)
        if total > 0:
            if mode == "sidecar":
                # Conservative: during active specialist work, model weights
                # + KV cache for 4 concurrent requests eat most VRAM.
                # A 9B fp16 model takes ~18GB on a 24GB card, leaving ~6GB
                # total, of which KV cache for 4 requests uses ~4GB.
                # Estimate 15% free as a worst-case-ish proxy.
                estimated_free = int(total * 0.15)
            else:
                # Clarification: GPU is idle, model weights are the only
                # consumer.  35% free is a reasonable estimate.
                estimated_free = int(total * 0.35)
            return estimated_free
        return None

    # No SystemProfile — fall back to real-time probe
    return _nvidia_smi_free_mb()


def _nvidia_smi_free_mb() -> Optional[int]:
    """Query real-time free GPU memory in MB via resource_discovery.

    Delegates to resource_discovery.get_free_vram_mb() to avoid
    duplicating nvidia-smi invocation logic.

    Returns None if no GPU or nvidia-smi unavailable.
    """
    # Import here to avoid circular imports (simulation <-> resource_discovery)
    from .resource_discovery import get_free_vram_mb
    return get_free_vram_mb()
