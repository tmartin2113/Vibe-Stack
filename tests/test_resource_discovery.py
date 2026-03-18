"""Tests for agents.resource_discovery — hardware auto-discovery."""

import os
from unittest.mock import patch, mock_open

import pytest

from agents.resource_discovery import (
    GpuInfo,
    SystemProfile,
    _discover_cpu,
    _discover_ram,
    _discover_gpus,
    _discover_docker,
    _run_cmd,
    discover_system,
)


# ── SystemProfile tests ────────────────────────────────────────────

class TestSystemProfile:
    """Test SystemProfile derived properties."""

    def test_no_gpus(self):
        p = SystemProfile(cpu_count=4, cpu_threads=8, cpu_model="test",
                          total_ram_mb=16384, available_ram_mb=8192)
        assert p.gpu_count == 0
        assert p.total_vram_mb == 0
        assert p.has_gpu is False
        assert p.is_multi_gpu is False

    def test_single_gpu(self):
        gpu = GpuInfo(0, "RTX 4090", 24564, "8.9", 0, 45)
        p = SystemProfile(cpu_count=8, cpu_threads=16, cpu_model="test",
                          total_ram_mb=32768, available_ram_mb=16384,
                          gpus=[gpu])
        assert p.gpu_count == 1
        assert p.total_vram_mb == 24564
        assert p.has_gpu is True
        assert p.is_multi_gpu is False

    def test_multi_gpu(self):
        gpus = [
            GpuInfo(0, "RTX 3090", 24576, "8.6", 10, 50),
            GpuInfo(1, "RTX 3090", 24576, "8.6", 5, 48),
        ]
        p = SystemProfile(cpu_count=16, cpu_threads=32, cpu_model="test",
                          total_ram_mb=65536, available_ram_mb=40000,
                          gpus=gpus)
        assert p.gpu_count == 2
        assert p.total_vram_mb == 49152
        assert p.has_gpu is True
        assert p.is_multi_gpu is True

    def test_frozen(self):
        p = SystemProfile(cpu_count=4, cpu_threads=8, cpu_model="test",
                          total_ram_mb=16384, available_ram_mb=8192)
        with pytest.raises(AttributeError):
            p.cpu_count = 99  # type: ignore[misc]


# ── GpuInfo tests ──────────────────────────────────────────────────

class TestGpuInfo:
    def test_creation(self):
        gpu = GpuInfo(0, "RTX 4090", 24564, "8.9", 50, 65)
        assert gpu.index == 0
        assert gpu.name == "RTX 4090"
        assert gpu.vram_mb == 24564
        assert gpu.compute_capability == "8.9"
        assert gpu.utilization_pct == 50
        assert gpu.temperature_c == 65


# ── _run_cmd tests ─────────────────────────────────────────────────

class TestRunCmd:
    def test_success(self):
        result = _run_cmd(["echo", "hello"])
        assert result == "hello"

    def test_command_not_found(self):
        result = _run_cmd(["nonexistent_binary_xyz_123"])
        assert result is None

    def test_command_failure(self):
        result = _run_cmd(["false"])
        assert result is None

    @patch("agents.resource_discovery.subprocess.run", side_effect=OSError("boom"))
    def test_oserror(self, mock_run):
        result = _run_cmd(["anything"])
        assert result is None


# ── _discover_cpu tests ────────────────────────────────────────────

class TestDiscoverCpu:
    SAMPLE_CPUINFO = """\
processor	: 0
vendor_id	: GenuineIntel
model name	: Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz
physical id	: 0
core id		: 0

processor	: 1
vendor_id	: GenuineIntel
model name	: Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz
physical id	: 0
core id		: 1

processor	: 2
vendor_id	: GenuineIntel
model name	: Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz
physical id	: 0
core id		: 2

processor	: 3
vendor_id	: GenuineIntel
model name	: Intel(R) Core(TM) i7-9700K CPU @ 3.60GHz
physical id	: 0
core id		: 3
"""

    def test_parse_cpuinfo(self):
        with patch("builtins.open", mock_open(read_data=self.SAMPLE_CPUINFO)):
            physical, logical, model = _discover_cpu()
        assert physical == 4
        assert logical == 4
        assert "i7-9700K" in model

    SAMPLE_HT_CPUINFO = """\
processor	: 0
model name	: AMD Ryzen 9 7950X 16-Core Processor
physical id	: 0
core id		: 0

processor	: 1
model name	: AMD Ryzen 9 7950X 16-Core Processor
physical id	: 0
core id		: 0

processor	: 2
model name	: AMD Ryzen 9 7950X 16-Core Processor
physical id	: 0
core id		: 1

processor	: 3
model name	: AMD Ryzen 9 7950X 16-Core Processor
physical id	: 0
core id		: 1
"""

    def test_hyperthreading(self):
        with patch("builtins.open", mock_open(read_data=self.SAMPLE_HT_CPUINFO)):
            physical, logical, model = _discover_cpu()
        assert physical == 2  # 2 unique core IDs
        assert logical == 4   # 4 processor entries
        assert "7950X" in model

    def test_file_not_found(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            physical, logical, model = _discover_cpu()
        assert physical >= 1
        assert model == "unknown"


# ── _discover_ram tests ────────────────────────────────────────────

class TestDiscoverRam:
    SAMPLE_MEMINFO = """\
MemTotal:       32878096 kB
MemFree:         1234567 kB
MemAvailable:   20000000 kB
Buffers:          567890 kB
Cached:         12345678 kB
"""

    def test_parse_meminfo(self):
        with patch("builtins.open", mock_open(read_data=self.SAMPLE_MEMINFO)):
            total, available = _discover_ram()
        assert total == 32878096 // 1024  # ~32 GB
        assert available == 20000000 // 1024  # ~19 GB

    def test_file_not_found_fallback(self):
        # /proc/meminfo open fails → psutil fallback fails → hardcoded defaults
        with patch("builtins.open", side_effect=FileNotFoundError):
            with patch.dict("sys.modules", {"psutil": None}):
                total, available = _discover_ram()
        # Should return hardcoded defaults (4096, 2048)
        assert total > 0
        assert available > 0


# ── _discover_gpus tests ──────────────────────────────────────────

class TestDiscoverGpus:
    NVIDIA_CSV = """\
0, NVIDIA GeForce RTX 4090, 24564, 8.9, 15, 52
1, NVIDIA GeForce RTX 3080, 10240, 8.6, 0, 35
"""
    DRIVER_OUTPUT = "535.129.03"
    SMI_HEADER = """\
+-------------------------------------------------------------------+
| NVIDIA-SMI 535.129.03   Driver Version: 535.129.03   CUDA Version: 12.2 |
"""

    @patch("agents.resource_discovery._run_cmd")
    def test_parse_nvidia_smi(self, mock_cmd):
        def side_effect(cmd, timeout=10):
            if "--query-gpu=index,name" in " ".join(cmd):
                return self.NVIDIA_CSV
            if "--query-gpu=driver_version" in " ".join(cmd):
                return self.DRIVER_OUTPUT
            if cmd == ["nvidia-smi"]:
                return self.SMI_HEADER
            return None

        mock_cmd.side_effect = side_effect
        gpus, driver, cuda = _discover_gpus()

        assert len(gpus) == 2
        assert gpus[0].name == "NVIDIA GeForce RTX 4090"
        assert gpus[0].vram_mb == 24564
        assert gpus[1].vram_mb == 10240
        assert driver == "535.129.03"
        assert cuda == "12.2"

    @patch("agents.resource_discovery._run_cmd", return_value=None)
    def test_no_nvidia(self, mock_cmd):
        gpus, driver, cuda = _discover_gpus()
        assert gpus == []
        assert driver == ""
        assert cuda == ""

    @patch("agents.resource_discovery._run_cmd")
    def test_malformed_csv_line(self, mock_cmd):
        def side_effect(cmd, timeout=10):
            if "--query-gpu=index,name" in " ".join(cmd):
                return "bad line\n0, GPU, 1000, 8.0, 5, 40"
            return None
        mock_cmd.side_effect = side_effect
        gpus, _, _ = _discover_gpus()
        assert len(gpus) == 1


# ── _discover_docker tests ────────────────────────────────────────

class TestDiscoverDocker:
    @patch("agents.resource_discovery._run_cmd")
    def test_docker_with_nvidia(self, mock_cmd):
        mock_cmd.return_value = "Runtimes: io.containerd.runc.v2 nvidia"
        avail, gpu_rt = _discover_docker()
        assert avail is True
        assert gpu_rt is True

    @patch("agents.resource_discovery._run_cmd")
    def test_docker_without_nvidia(self, mock_cmd):
        mock_cmd.return_value = "Runtimes: io.containerd.runc.v2"
        avail, gpu_rt = _discover_docker()
        assert avail is True
        assert gpu_rt is False

    @patch("agents.resource_discovery._run_cmd", return_value=None)
    def test_no_docker(self, mock_cmd):
        avail, gpu_rt = _discover_docker()
        assert avail is False
        assert gpu_rt is False


# ── discover_system integration ───────────────────────────────────

class TestDiscoverSystem:
    @patch("agents.resource_discovery._discover_docker", return_value=(True, True))
    @patch("agents.resource_discovery._discover_gpus", return_value=(
        [GpuInfo(0, "RTX 4090", 24564, "8.9", 10, 50)], "535.0", "12.2"
    ))
    @patch("agents.resource_discovery._discover_ram", return_value=(32768, 20000))
    @patch("agents.resource_discovery._discover_cpu", return_value=(8, 16, "AMD Ryzen 9"))
    def test_full_discovery(self, mock_cpu, mock_ram, mock_gpu, mock_docker):
        profile = discover_system()
        assert profile.cpu_count == 8
        assert profile.cpu_threads == 16
        assert profile.total_ram_mb == 32768
        assert profile.has_gpu is True
        assert profile.docker_available is True
        assert profile.docker_gpu_runtime is True

    @patch("agents.resource_discovery._discover_docker", return_value=(False, False))
    @patch("agents.resource_discovery._discover_gpus", return_value=([], "", ""))
    @patch("agents.resource_discovery._discover_ram", return_value=(8192, 4096))
    @patch("agents.resource_discovery._discover_cpu", return_value=(2, 4, "unknown"))
    def test_minimal_system(self, mock_cpu, mock_ram, mock_gpu, mock_docker):
        profile = discover_system()
        assert profile.has_gpu is False
        assert profile.docker_available is False
        assert profile.cpu_threads == 4
