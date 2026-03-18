"""
Hardware auto-discovery for Vibe.

Probes the host system at startup and produces a frozen SystemProfile
that other modules (resource_allocator, doctor, sandbox) consume.

Safe to call in any environment — returns sensible defaults when
probing tools (nvidia-smi, docker) are unavailable.
"""

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── Data models ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class GpuInfo:
    """Information about a single GPU device."""

    index: int
    name: str                  # "NVIDIA GeForce RTX 4090"
    vram_mb: int               # Total VRAM in MiB
    compute_capability: str    # "8.9"
    utilization_pct: int       # Current GPU utilization 0-100
    temperature_c: int         # Current temp in Celsius


@dataclass(frozen=True)
class SystemProfile:
    """Immutable snapshot of host hardware capabilities."""

    # CPU
    cpu_count: int             # Physical cores
    cpu_threads: int           # Logical threads (with hyperthreading)
    cpu_model: str             # e.g. "AMD Ryzen 9 7950X 16-Core Processor"

    # Memory
    total_ram_mb: int          # Total system RAM in MiB
    available_ram_mb: int      # Currently available RAM in MiB

    # GPU
    gpus: List[GpuInfo] = field(default_factory=list)
    nvidia_driver: str = ""    # e.g. "535.129.03"
    cuda_version: str = ""     # e.g. "12.2"

    # Docker
    docker_available: bool = False
    docker_gpu_runtime: bool = False  # nvidia-container-toolkit present?

    # ── Derived properties ──

    @property
    def gpu_count(self) -> int:
        return len(self.gpus)

    @property
    def total_vram_mb(self) -> int:
        return sum(g.vram_mb for g in self.gpus)

    @property
    def has_gpu(self) -> bool:
        return len(self.gpus) > 0

    @property
    def is_multi_gpu(self) -> bool:
        return len(self.gpus) > 1


# ── Probing helpers ─────────────────────────────────────────────────

def _run_cmd(cmd: List[str], timeout: int = 10) -> Optional[str]:
    """Run a command and return stdout, or None on any failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _discover_cpu() -> tuple:
    """Return (physical_cores, logical_threads, model_name)."""
    model = "unknown"
    physical = os.cpu_count() or 1
    logical = physical

    # Try /proc/cpuinfo (Linux)
    try:
        with open("/proc/cpuinfo", "r") as f:
            cpuinfo = f.read()

        # Model name from first entry
        m = re.search(r"model name\s*:\s*(.+)", cpuinfo)
        if m:
            model = m.group(1).strip()

        # Count physical cores (unique core IDs per physical package)
        core_ids = set()
        current_physical_id = "0"
        for line in cpuinfo.splitlines():
            pid_match = re.match(r"physical id\s*:\s*(\d+)", line)
            if pid_match:
                current_physical_id = pid_match.group(1)
            cid_match = re.match(r"core id\s*:\s*(\d+)", line)
            if cid_match:
                core_ids.add((current_physical_id, cid_match.group(1)))

        if core_ids:
            physical = len(core_ids)

        # Logical = number of "processor" entries
        processors = re.findall(r"^processor\s*:", cpuinfo, re.MULTILINE)
        if processors:
            logical = len(processors)

    except (FileNotFoundError, PermissionError):
        pass

    return physical, logical, model


def _discover_ram() -> tuple:
    """Return (total_mb, available_mb)."""
    total_mb = 0
    available_mb = 0

    try:
        with open("/proc/meminfo", "r") as f:
            meminfo = f.read()

        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                kb = int(re.search(r"(\d+)", line).group(1))
                total_mb = kb // 1024
            elif line.startswith("MemAvailable:"):
                kb = int(re.search(r"(\d+)", line).group(1))
                available_mb = kb // 1024

    except (FileNotFoundError, PermissionError):
        # Fallback: use os module
        try:
            import psutil
            mem = psutil.virtual_memory()
            total_mb = mem.total // (1024 * 1024)
            available_mb = mem.available // (1024 * 1024)
        except ImportError:
            total_mb = 4096  # Conservative default
            available_mb = 2048

    return total_mb, available_mb


def _discover_gpus() -> tuple:
    """Return (list[GpuInfo], driver_version, cuda_version)."""
    gpus: List[GpuInfo] = []
    driver = ""
    cuda = ""

    # Query nvidia-smi for GPU details (CSV format)
    output = _run_cmd([
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,compute_cap,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ])

    if output is None:
        return gpus, driver, cuda

    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append(GpuInfo(
                index=int(parts[0]),
                name=parts[1],
                vram_mb=int(parts[2]),
                compute_capability=parts[3],
                utilization_pct=int(parts[4]),
                temperature_c=int(parts[5]),
            ))
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse nvidia-smi line: {line!r}: {e}")

    # Driver version
    driver_out = _run_cmd([
        "nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader",
    ])
    if driver_out:
        driver = driver_out.splitlines()[0].strip()

    # CUDA version (from nvidia-smi header)
    smi_out = _run_cmd(["nvidia-smi"])
    if smi_out:
        m = re.search(r"CUDA Version:\s*([\d.]+)", smi_out)
        if m:
            cuda = m.group(1)

    return gpus, driver, cuda


def _discover_docker() -> tuple:
    """Return (docker_available, docker_gpu_runtime)."""
    docker_available = False
    gpu_runtime = False

    info = _run_cmd(["docker", "info"])
    if info is not None:
        docker_available = True
        # Check for nvidia runtime
        gpu_runtime = "nvidia" in info.lower()

    return docker_available, gpu_runtime


# ── Public API ──────────────────────────────────────────────────────

def discover_system() -> SystemProfile:
    """Probe host hardware and return an immutable SystemProfile.

    Safe to call anywhere — never raises, returns defaults on failure.
    Logs discovery results at INFO level.
    """
    logger.info("Discovering system hardware...")

    cpu_count, cpu_threads, cpu_model = _discover_cpu()
    total_ram, available_ram = _discover_ram()
    gpus, nvidia_driver, cuda_version = _discover_gpus()
    docker_available, docker_gpu_runtime = _discover_docker()

    profile = SystemProfile(
        cpu_count=cpu_count,
        cpu_threads=cpu_threads,
        cpu_model=cpu_model,
        total_ram_mb=total_ram,
        available_ram_mb=available_ram,
        gpus=gpus,
        nvidia_driver=nvidia_driver,
        cuda_version=cuda_version,
        docker_available=docker_available,
        docker_gpu_runtime=docker_gpu_runtime,
    )

    # Log summary
    gpu_desc = (
        f"{profile.gpu_count} GPU(s): "
        + ", ".join(f"{g.name} ({g.vram_mb}MB)" for g in gpus)
        if gpus
        else "No GPU detected"
    )
    logger.info(
        f"System: {cpu_model} ({cpu_threads} threads) | "
        f"{total_ram}MB RAM ({available_ram}MB free) | {gpu_desc}"
    )
    if docker_available:
        logger.info(f"Docker: available, GPU runtime: {docker_gpu_runtime}")
    else:
        logger.info("Docker: not available")

    return profile
