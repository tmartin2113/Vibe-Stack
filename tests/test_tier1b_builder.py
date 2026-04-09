"""Tests for agents/self_upgrade/tier1b_builder.py — Tier 1b prompt overrides."""

from dataclasses import is_dataclass
from unittest.mock import MagicMock

import pytest

from agents.self_upgrade.tier1b_builder import (
    APPEND_MAX_LEN,
    MIN_FIXTURES_PER_ADAPTER,
    SAFETY_CLAUSE_BLOCKLIST,
    SMOKE_MAX_DROP_PCT,
    Tier1bBuilder,
    Tier1bResult,
)
from agents.self_upgrade_trigger import UpgradeSignal


def _make_signal(task_type="code_generation", detail="use explicit response_model"):
    return UpgradeSignal(
        category="critic_pattern",
        task_type=task_type,
        detail=detail,
        score=60,
        source_node="critic",
    )


class TestTier1bResultShape:
    def test_override_committed_is_dataclass(self):
        assert is_dataclass(Tier1bResult.OverrideCommitted)

    def test_low_confidence_is_dataclass(self):
        assert is_dataclass(Tier1bResult.LowConfidence)

    def test_gate_failed_is_dataclass(self):
        assert is_dataclass(Tier1bResult.GateFailed)

    def test_override_committed_has_expected_fields(self):
        r = Tier1bResult.OverrideCommitted(
            override_id="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X",
            task_type="code_generation",
            branch="vibe/self-upgrade/tier1b-ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X",
            commit="abc123",
            pr_url="https://github.com/tmartin2113/Vibe-Stack/pull/99",
            issue_id="iss_1",
            signal_refs=["sig_a", "sig_b"],
        )
        assert r.override_id == "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X"
        assert r.task_type == "code_generation"

    def test_gate_failed_has_gate_and_detail(self):
        r = Tier1bResult.GateFailed(
            gate="smoke_test",
            detail="fixture can_01 dropped from 91 to 78",
            signal_refs=["sig_a"],
        )
        assert r.gate == "smoke_test"
        assert "fixture can_01" in r.detail


class TestModuleConstants:
    def test_append_max_len_is_500(self):
        assert APPEND_MAX_LEN == 500

    def test_min_fixtures_per_adapter_is_3(self):
        assert MIN_FIXTURES_PER_ADAPTER == 3

    def test_smoke_max_drop_is_5(self):
        assert SMOKE_MAX_DROP_PCT == 5

    def test_safety_blocklist_is_nonempty_tuple(self):
        assert isinstance(SAFETY_CLAUSE_BLOCKLIST, tuple)
        assert len(SAFETY_CLAUSE_BLOCKLIST) > 0


class TestTier1bBuilderInit:
    def test_builder_accepts_required_dependencies(self, tmp_path):
        builder = Tier1bBuilder(
            task_type_registry=MagicMock(),
            smoke_scorer=MagicMock(),
            git_runner=MagicMock(),
            paperclip_client=MagicMock(),
            fixtures_root=tmp_path / "canonical",
            overrides_root=tmp_path / "overrides",
            human_triage_user_id="human_1",
        )
        assert builder is not None


class TestTier1bBuilderStub:
    def test_build_stub_returns_low_confidence(self, tmp_path):
        """Until gates are wired, build() returns LowConfidence("stub")."""
        builder = Tier1bBuilder(
            task_type_registry=MagicMock(),
            smoke_scorer=MagicMock(),
            git_runner=MagicMock(),
            paperclip_client=MagicMock(),
            fixtures_root=tmp_path / "canonical",
            overrides_root=tmp_path / "overrides",
            allow_publish=False,
        )
        result = builder.build(
            [_make_signal()],
            author_agent_id="backend-engineer",
            author_run_id="run_1",
        )
        assert isinstance(result, Tier1bResult.LowConfidence)
