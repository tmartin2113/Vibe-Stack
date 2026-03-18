"""
Resource allocator for Vibe.

Consumes a SystemProfile from resource_discovery and produces a
ResourcePlan with concrete CPU/memory/GPU budgets for every service.

The allocator runs once at startup. Its output feeds into SandboxConfig,
docker-compose env vars, and the /status diagnostic display.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import List

from .resource_discovery import SystemProfile

logger = logging.getLogger(__name__)


# ── Data models ─────────────────────────────────────────────────────

@dataclass
class ServiceBudget:
    """Resource budget for a single service."""

    cpu_cores: float              # Fractional cores (e.g. 1.5)
    memory_mb: int                # RAM in MiB
    gpu_device_ids: List[int] = field(default_factory=list)
    gpu_memory_fraction: float = 0.0  # 0.0-1.0, fraction of VRAM to claim


@dataclass
class SandboxPoolPlan:
    """Resource plan for the warm sandbox pool."""

    pool_size: int                # Number of warm sandboxes
    per_sandbox_cpu: str          # K8s format: "500m", "1000m"
    per_sandbox_memory: str       # K8s format: "256Mi", "512Mi"
    gpu_enabled: bool             # Whether sandboxes get GPU access
    gpu_device_ids: List[int] = field(default_factory=list)
    sandbox_image: str = "opensandbox/code-interpreter:v1.0.1"


@dataclass
class ResourcePlan:
    """Complete resource allocation for all Vibe services."""

    vllm: ServiceBudget
    vibe: ServiceBudget
    opensandbox_server: ServiceBudget
    sandbox_pool: SandboxPoolPlan

    # Metadata
    profile: SystemProfile
    strategy: str                 # "cpu_only", "single_gpu", "multi_gpu"
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary for logging and diagnostics."""
        lines = [
            f"Strategy: {self.strategy}",
            f"  vLLM:      {self.vllm.cpu_cores} cores, "
            f"{self.vllm.memory_mb}MB RAM"
            + (f", GPU {self.vllm.gpu_device_ids}" if self.vllm.gpu_device_ids else ", CPU-only"),
            f"  Vibe:     {self.vibe.cpu_cores} cores, "
            f"{self.vibe.memory_mb}MB RAM",
            f"  OpenSandbox: {self.opensandbox_server.cpu_cores} cores, "
            f"{self.opensandbox_server.memory_mb}MB RAM",
            f"  Sandbox pool: {self.sandbox_pool.pool_size} warm, "
            f"{self.sandbox_pool.per_sandbox_cpu} CPU + "
            f"{self.sandbox_pool.per_sandbox_memory} RAM each"
            + (f", GPU {self.sandbox_pool.gpu_device_ids}" if self.sandbox_pool.gpu_enabled else ", CPU-only"),
        ]
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        return "\n".join(lines)


# ── Allocation strategies ───────────────────────────────────────────

def _plan_cpu_only(profile: SystemProfile) -> ResourcePlan:
    """Allocation when no GPUs are detected."""
    warnings = ["No GPU detected — LLM inference will be slow"]

    threads = profile.cpu_threads
    ram = profile.total_ram_mb

    # vLLM gets the lion's share (CPU inference is hungry)
    vllm_cores = max(2.0, threads - 3)
    vllm_ram = int(ram * 0.70)

    # Pool sizing based on available RAM
    pool_size, sandbox_cpu, sandbox_mem = _size_pool(profile)

    return ResourcePlan(
        vllm=ServiceBudget(cpu_cores=vllm_cores, memory_mb=vllm_ram),
        vibe=ServiceBudget(cpu_cores=1.0, memory_mb=512),
        opensandbox_server=ServiceBudget(cpu_cores=0.5, memory_mb=256),
        sandbox_pool=SandboxPoolPlan(
            pool_size=pool_size,
            per_sandbox_cpu=sandbox_cpu,
            per_sandbox_memory=sandbox_mem,
            gpu_enabled=False,
        ),
        profile=profile,
        strategy="cpu_only",
        warnings=warnings,
    )


def _plan_single_gpu(profile: SystemProfile) -> ResourcePlan:
    """Allocation for a single GPU system."""
    gpu = profile.gpus[0]
    threads = profile.cpu_threads
    warnings: List[str] = []

    # vLLM gets the GPU + half the CPU budget
    vllm_cores = max(2.0, threads / 2)
    vllm_ram = int(profile.total_ram_mb * 0.40)

    # Can sandboxes share the GPU?
    # Below 16GB VRAM: too tight, LLM models need it all
    # 16-23GB: possible but risky, disable by default
    # 24GB+: enough headroom to share
    sandbox_gpu = gpu.vram_mb >= 24000  # ~24 GB (accounts for driver overhead)
    sandbox_gpu_ids = [gpu.index] if sandbox_gpu else []

    if gpu.vram_mb < 16384:
        warnings.append(
            f"Low VRAM ({gpu.vram_mb}MB): sandbox GPU disabled "
            f"to protect LLM inference"
        )
    elif not sandbox_gpu:
        warnings.append(
            f"Moderate VRAM ({gpu.vram_mb}MB): sandbox GPU disabled. "
            f"Set VIBE_SANDBOX_GPU=true to override"
        )

    sandbox_image = (
        "vibe/sandbox-gpu:latest"
        if sandbox_gpu
        else "opensandbox/code-interpreter:v1.0.1"
    )

    pool_size, sandbox_cpu, sandbox_mem = _size_pool(profile)

    return ResourcePlan(
        vllm=ServiceBudget(
            cpu_cores=vllm_cores,
            memory_mb=vllm_ram,
            gpu_device_ids=[gpu.index],
            gpu_memory_fraction=1.0 if not sandbox_gpu else 0.8,
        ),
        vibe=ServiceBudget(cpu_cores=1.0, memory_mb=512),
        opensandbox_server=ServiceBudget(cpu_cores=0.5, memory_mb=256),
        sandbox_pool=SandboxPoolPlan(
            pool_size=pool_size,
            per_sandbox_cpu=sandbox_cpu,
            per_sandbox_memory=sandbox_mem,
            gpu_enabled=sandbox_gpu,
            gpu_device_ids=sandbox_gpu_ids,
            sandbox_image=sandbox_image,
        ),
        profile=profile,
        strategy="single_gpu",
        warnings=warnings,
    )


def _plan_multi_gpu(profile: SystemProfile) -> ResourcePlan:
    """Allocation for multi-GPU systems (2+)."""
    gpus = profile.gpus
    threads = profile.cpu_threads
    warnings: List[str] = []

    # Sort GPUs by VRAM descending — give the biggest to vLLM
    sorted_gpus = sorted(gpus, key=lambda g: g.vram_mb, reverse=True)

    # vLLM gets the first GPU (largest VRAM)
    # If largest GPU < 24GB, consider tensor parallel across top 2
    primary_gpu = sorted_gpus[0]
    if primary_gpu.vram_mb < 24576 and len(sorted_gpus) >= 2:
        # Tensor parallel across the 2 largest GPUs
        vllm_gpu_ids = [sorted_gpus[0].index, sorted_gpus[1].index]
        remaining_gpus = sorted_gpus[2:]
    else:
        vllm_gpu_ids = [primary_gpu.index]
        remaining_gpus = sorted_gpus[1:]

    vllm_cores = max(2.0, threads * 0.4)
    vllm_ram = int(profile.total_ram_mb * 0.35)

    # Sandboxes get the remaining GPUs
    sandbox_gpu_ids = [g.index for g in remaining_gpus]
    sandbox_gpu = len(sandbox_gpu_ids) > 0

    if not sandbox_gpu:
        warnings.append(
            "All GPUs assigned to vLLM for tensor parallel — "
            "sandboxes will be CPU-only"
        )

    sandbox_image = (
        "vibe/sandbox-gpu:latest"
        if sandbox_gpu
        else "opensandbox/code-interpreter:v1.0.1"
    )

    pool_size, sandbox_cpu, sandbox_mem = _size_pool(profile)
    # Multi-GPU systems are beefy — bump pool size
    pool_size = min(pool_size + 1, 6)

    return ResourcePlan(
        vllm=ServiceBudget(
            cpu_cores=vllm_cores,
            memory_mb=vllm_ram,
            gpu_device_ids=vllm_gpu_ids,
            gpu_memory_fraction=0.9,
        ),
        vibe=ServiceBudget(cpu_cores=2.0, memory_mb=1024),
        opensandbox_server=ServiceBudget(cpu_cores=1.0, memory_mb=512),
        sandbox_pool=SandboxPoolPlan(
            pool_size=pool_size,
            per_sandbox_cpu=sandbox_cpu,
            per_sandbox_memory=sandbox_mem,
            gpu_enabled=sandbox_gpu,
            gpu_device_ids=sandbox_gpu_ids,
            sandbox_image=sandbox_image,
        ),
        profile=profile,
        strategy="multi_gpu",
        warnings=warnings,
    )


def _size_pool(profile: SystemProfile) -> tuple:
    """Compute sandbox pool size and per-sandbox resources based on RAM and CPU.

    Returns (pool_size, per_sandbox_cpu, per_sandbox_memory).
    """
    ram = profile.total_ram_mb
    threads = profile.cpu_threads

    # Base pool size from RAM
    if ram < 16384:        # < 16 GB
        pool_size = 1
        mem = "256Mi"
        cpu = "500m"
    elif ram < 32768:      # < 32 GB
        pool_size = 2
        mem = "512Mi"
        cpu = "500m"
    elif ram < 65536:      # < 64 GB
        pool_size = 3
        mem = "512Mi"
        cpu = "1000m"
    else:                  # >= 64 GB
        pool_size = 4
        mem = "1Gi"
        cpu = "1000m"

    # CPU-thread adjustments
    if threads < 4:
        pool_size = max(1, pool_size - 1)
    elif threads >= 16:
        pool_size = min(pool_size + 1, 6)

    return pool_size, cpu, mem


# ── Public API ──────────────────────────────────────────────────────

def compute_resource_plan(profile: SystemProfile) -> ResourcePlan:
    """Produce optimal resource allocation for a single-machine deployment.

    Selects strategy based on GPU count, then tunes budgets from
    RAM and CPU thread count. Returns a ResourcePlan that can be
    consumed by SandboxConfig, docker-compose generation, and diagnostics.
    """
    if not profile.has_gpu:
        plan = _plan_cpu_only(profile)
    elif profile.is_multi_gpu:
        plan = _plan_multi_gpu(profile)
    else:
        plan = _plan_single_gpu(profile)

    # Docker runtime check — OpenSandbox requires Docker
    if not profile.docker_available:
        raise RuntimeError(
            "Docker is required for OpenSandbox execution. "
            "Install Docker and ensure the daemon is running."
        )
    elif not profile.docker_gpu_runtime and profile.has_gpu:
        plan.warnings.append(
            "nvidia-container-toolkit not detected — "
            "GPU passthrough to containers may fail"
        )

    logger.info("Resource plan computed:\n%s", plan.summary())
    return plan
