"""Tests for agents.resource_allocator — auto-tuning resource budgets."""

import pytest

from agents.resource_discovery import GpuInfo, SystemProfile
from agents.resource_allocator import (
    ResourcePlan,
    ServiceBudget,
    SandboxPoolPlan,
    compute_resource_plan,
    _plan_cpu_only,
    _plan_single_gpu,
    _plan_multi_gpu,
    _size_pool,
)


# ── Helpers ─────────────────────────────────────────────────────────

def _make_profile(
    cpu_count=8, cpu_threads=16, cpu_model="Test CPU",
    total_ram_mb=32768, available_ram_mb=20000,
    gpus=None, nvidia_driver="535.0", cuda_version="12.2",
    docker_available=True, docker_gpu_runtime=True,
):
    return SystemProfile(
        cpu_count=cpu_count,
        cpu_threads=cpu_threads,
        cpu_model=cpu_model,
        total_ram_mb=total_ram_mb,
        available_ram_mb=available_ram_mb,
        gpus=gpus or [],
        nvidia_driver=nvidia_driver if gpus else "",
        cuda_version=cuda_version if gpus else "",
        docker_available=docker_available,
        docker_gpu_runtime=docker_gpu_runtime if gpus else False,
    )


RTX_4090 = GpuInfo(0, "RTX 4090", 24564, "8.9", 10, 50)
RTX_3090_0 = GpuInfo(0, "RTX 3090", 24576, "8.6", 5, 48)
RTX_3090_1 = GpuInfo(1, "RTX 3090", 24576, "8.6", 0, 35)
RTX_3070 = GpuInfo(0, "RTX 3070", 8192, "8.6", 0, 40)
RTX_3080 = GpuInfo(0, "RTX 3080", 10240, "8.6", 10, 45)
A100_0 = GpuInfo(0, "A100", 81920, "8.0", 0, 30)
A100_1 = GpuInfo(1, "A100", 81920, "8.0", 0, 30)
A100_2 = GpuInfo(2, "A100", 81920, "8.0", 0, 30)
A100_3 = GpuInfo(3, "A100", 81920, "8.0", 0, 30)


# ── Strategy selection ──────────────────────────────────────────────

class TestStrategySelection:
    def test_no_gpu_selects_cpu_only(self):
        plan = compute_resource_plan(_make_profile(gpus=[]))
        assert plan.strategy == "cpu_only"

    def test_single_gpu_selects_single(self):
        plan = compute_resource_plan(_make_profile(gpus=[RTX_4090]))
        assert plan.strategy == "single_gpu"

    def test_multi_gpu_selects_multi(self):
        plan = compute_resource_plan(_make_profile(gpus=[RTX_3090_0, RTX_3090_1]))
        assert plan.strategy == "multi_gpu"


# ── CPU-only strategy ──────────────────────────────────────────────

class TestCpuOnlyStrategy:
    def test_warns_no_gpu(self):
        plan = _plan_cpu_only(_make_profile(gpus=[]))
        assert any("No GPU" in w for w in plan.warnings)

    def test_vllm_gets_most_cpu(self):
        profile = _make_profile(cpu_threads=16, gpus=[])
        plan = _plan_cpu_only(profile)
        assert plan.vllm.cpu_cores >= 10  # 16 - 3 = 13
        assert plan.vibe.cpu_cores == 1.0
        assert plan.opensandbox_server.cpu_cores == 0.5

    def test_vllm_gets_most_ram(self):
        profile = _make_profile(total_ram_mb=32768, gpus=[])
        plan = _plan_cpu_only(profile)
        assert plan.vllm.memory_mb == int(32768 * 0.70)

    def test_no_sandbox_gpu(self):
        plan = _plan_cpu_only(_make_profile(gpus=[]))
        assert plan.sandbox_pool.gpu_enabled is False
        assert plan.sandbox_pool.gpu_device_ids == []


# ── Single GPU strategy ────────────────────────────────────────────

class TestSingleGpuStrategy:
    def test_vllm_gets_gpu(self):
        plan = _plan_single_gpu(_make_profile(gpus=[RTX_4090]))
        assert plan.vllm.gpu_device_ids == [0]

    def test_24gb_enables_sandbox_gpu(self):
        plan = _plan_single_gpu(_make_profile(gpus=[RTX_4090]))
        assert plan.sandbox_pool.gpu_enabled is True
        assert plan.sandbox_pool.gpu_device_ids == [0]
        assert "vibe/sandbox-gpu" in plan.sandbox_pool.sandbox_image

    def test_10gb_disables_sandbox_gpu(self):
        plan = _plan_single_gpu(_make_profile(gpus=[RTX_3080]))
        assert plan.sandbox_pool.gpu_enabled is False
        assert plan.sandbox_pool.gpu_device_ids == []
        assert "code-interpreter" in plan.sandbox_pool.sandbox_image

    def test_8gb_warns_low_vram(self):
        plan = _plan_single_gpu(_make_profile(gpus=[RTX_3070]))
        assert any("Low VRAM" in w for w in plan.warnings)
        assert plan.sandbox_pool.gpu_enabled is False

    def test_vllm_vram_fraction_with_sharing(self):
        plan = _plan_single_gpu(_make_profile(gpus=[RTX_4090]))
        assert plan.vllm.gpu_memory_fraction == 0.8  # Shared

    def test_vllm_vram_fraction_without_sharing(self):
        plan = _plan_single_gpu(_make_profile(gpus=[RTX_3070]))
        assert plan.vllm.gpu_memory_fraction == 1.0  # Exclusive


# ── Multi GPU strategy ─────────────────────────────────────────────

class TestMultiGpuStrategy:
    def test_vllm_gets_largest_gpu(self):
        gpus = [RTX_3090_0, RTX_3090_1]
        plan = _plan_multi_gpu(_make_profile(gpus=gpus))
        assert 0 in plan.vllm.gpu_device_ids or 1 in plan.vllm.gpu_device_ids

    def test_sandboxes_get_remaining_gpus(self):
        gpus = [RTX_3090_0, RTX_3090_1]
        plan = _plan_multi_gpu(_make_profile(gpus=gpus))
        # One GPU to vLLM, one to sandboxes
        assert len(plan.vllm.gpu_device_ids) == 1
        assert len(plan.sandbox_pool.gpu_device_ids) == 1
        assert plan.sandbox_pool.gpu_enabled is True

    def test_four_gpus(self):
        gpus = [A100_0, A100_1, A100_2, A100_3]
        plan = _plan_multi_gpu(_make_profile(gpus=gpus, cpu_threads=64,
                                             total_ram_mb=131072))
        # A100 has 80GB — single GPU is enough for vLLM
        assert len(plan.vllm.gpu_device_ids) == 1
        assert len(plan.sandbox_pool.gpu_device_ids) == 3

    def test_small_gpus_use_tensor_parallel(self):
        small_0 = GpuInfo(0, "RTX 3060", 12288, "8.6", 0, 40)
        small_1 = GpuInfo(1, "RTX 3060", 12288, "8.6", 0, 38)
        plan = _plan_multi_gpu(_make_profile(gpus=[small_0, small_1]))
        # Both < 24GB → tensor parallel across both
        assert len(plan.vllm.gpu_device_ids) == 2
        # No remaining GPUs for sandboxes
        assert plan.sandbox_pool.gpu_enabled is False

    def test_pool_size_bumped(self):
        gpus = [RTX_3090_0, RTX_3090_1]
        plan = _plan_multi_gpu(_make_profile(gpus=gpus, total_ram_mb=65536))
        # Multi-GPU gets pool_size + 1
        cpu_only_plan = _plan_cpu_only(_make_profile(total_ram_mb=65536))
        assert plan.sandbox_pool.pool_size >= cpu_only_plan.sandbox_pool.pool_size


# ── Pool sizing ────────────────────────────────────────────────────

class TestPoolSizing:
    def test_low_ram(self):
        profile = _make_profile(total_ram_mb=12288, cpu_threads=8)  # 12 GB
        pool_size, cpu, mem = _size_pool(profile)
        assert pool_size == 1
        assert mem == "256Mi"

    def test_medium_ram(self):
        profile = _make_profile(total_ram_mb=32768, cpu_threads=8)  # 32 GB
        pool_size, cpu, mem = _size_pool(profile)
        assert pool_size == 2 or pool_size == 3  # Depends on thread adjustment

    def test_high_ram(self):
        profile = _make_profile(total_ram_mb=65536, cpu_threads=16)  # 64 GB
        pool_size, cpu, mem = _size_pool(profile)
        assert pool_size >= 4  # 4 base + thread bonus
        assert mem == "1Gi"

    def test_low_threads_reduces_pool(self):
        low = _make_profile(total_ram_mb=32768, cpu_threads=2)
        high = _make_profile(total_ram_mb=32768, cpu_threads=8)
        low_size, _, _ = _size_pool(low)
        high_size, _, _ = _size_pool(high)
        assert low_size <= high_size

    def test_high_threads_increases_pool(self):
        mid = _make_profile(total_ram_mb=32768, cpu_threads=8)
        high = _make_profile(total_ram_mb=32768, cpu_threads=32)
        mid_size, _, _ = _size_pool(mid)
        high_size, _, _ = _size_pool(high)
        assert high_size >= mid_size

    def test_pool_size_capped(self):
        huge = _make_profile(total_ram_mb=131072, cpu_threads=128)
        size, _, _ = _size_pool(huge)
        assert size <= 6


# ── Warnings ───────────────────────────────────────────────────────

class TestWarnings:
    def test_no_docker_raises(self):
        with pytest.raises(RuntimeError, match="Docker is required"):
            compute_resource_plan(
                _make_profile(gpus=[], docker_available=False)
            )

    def test_no_gpu_runtime_warns(self):
        plan = compute_resource_plan(
            _make_profile(gpus=[RTX_4090], docker_gpu_runtime=False)
        )
        assert any("nvidia-container-toolkit" in w for w in plan.warnings)

    def test_no_warning_when_all_good(self):
        plan = compute_resource_plan(
            _make_profile(gpus=[RTX_4090])
        )
        # Should only have resource-level warnings, not docker warnings
        docker_warnings = [w for w in plan.warnings if "Docker" in w or "nvidia-container" in w]
        assert docker_warnings == []


# ── ResourcePlan summary ──────────────────────────────────────────

class TestResourcePlanSummary:
    def test_summary_contains_strategy(self):
        plan = compute_resource_plan(_make_profile(gpus=[RTX_4090]))
        summary = plan.summary()
        assert "single_gpu" in summary
        assert "vLLM" in summary
        assert "Vibe" in summary

    def test_summary_contains_warnings(self):
        plan = compute_resource_plan(
            _make_profile(gpus=[RTX_4090], docker_gpu_runtime=False)
        )
        summary = plan.summary()
        assert "WARNING" in summary


# ── ServiceBudget / SandboxPoolPlan ───────────────────────────────

class TestDataclasses:
    def test_service_budget_defaults(self):
        b = ServiceBudget(cpu_cores=2.0, memory_mb=1024)
        assert b.gpu_device_ids == []
        assert b.gpu_memory_fraction == 0.0

    def test_sandbox_pool_plan_defaults(self):
        p = SandboxPoolPlan(pool_size=2, per_sandbox_cpu="500m",
                            per_sandbox_memory="512Mi", gpu_enabled=False)
        assert p.gpu_device_ids == []
        assert "code-interpreter" in p.sandbox_image
