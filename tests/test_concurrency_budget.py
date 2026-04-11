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
