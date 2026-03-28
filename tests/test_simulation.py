"""
Tests for MiroFish-Inspired Simulation Module.

Covers:
- Hardware gating (assess_simulation_budget) — sidecar vs clarification modes
- VRAM probing (_probe_free_vram) — heuristic + nvidia-smi fallback
- Integration simulation (run_integration_simulation)
- Clarification simulation (simulate_clarification)
- Skill vetting (vet_skill_with_simulation)
- Parsing helpers (_parse_simulation_report, _parse_mediator_response, _match_question)
- Formatting helpers (format_simulation_for_aggregator, format_clarification_for_spec)
- Adapter registration (register_simulation_adapters)
"""

import importlib
import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ── Helpers ──────────────────────────────────────────────────────

def _reload_sim(**env_overrides):
    """Reload the simulation module with env var overrides."""
    with patch.dict(os.environ, env_overrides):
        import agents.simulation as sim
        importlib.reload(sim)
        return sim


class MockProfile:
    """Mock SystemProfile for VRAM gating tests."""

    def __init__(self, has_gpu: bool = True, total_vram_mb: int = 22528):
        self.has_gpu = has_gpu
        self.total_vram_mb = total_vram_mb


class MockAdapter:
    """Mock PromptAdapter that returns canned responses."""

    def __init__(self, name: str = "mock", response: str = "mock output"):
        self.name = name
        self._response = response

    def generate(self, prompt: str, **kwargs) -> str:
        return self._response


class MockAdapterRegistry:
    """Mock AdapterRegistry that returns MockAdapters."""

    def __init__(self, adapters: Optional[Dict[str, MockAdapter]] = None):
        self._adapters = adapters or {}

    def get(self, name: str):
        return self._adapters.get(name)

    def register(self, adapter):
        self._adapters[adapter.name] = adapter

    def list_adapters(self):
        return list(self._adapters.keys())


# ===== assess_simulation_budget =====


class TestAssessSimulationBudget:
    """Hardware-aware budget gating."""

    def test_kill_switch_disables_all(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="false")
        budget = sim.assess_simulation_budget(mode="sidecar")
        assert not budget.enabled
        assert "VIBE_SIM_ENABLED" in budget.reason

        budget = sim.assess_simulation_budget(mode="clarification")
        assert not budget.enabled

    def test_sidecar_disabled_on_22gb(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        profile = MockProfile(has_gpu=True, total_vram_mb=22528)
        budget = sim.assess_simulation_budget(profile, mode="sidecar")
        # 22GB * 0.15 = ~3.4GB < 6GB threshold → disabled
        assert not budget.enabled
        assert "Sidecar disabled" in budget.reason

    def test_sidecar_enabled_on_48gb(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        profile = MockProfile(has_gpu=True, total_vram_mb=49152)
        budget = sim.assess_simulation_budget(profile, mode="sidecar")
        # 48GB * 0.15 = ~7.4GB > 6GB threshold → enabled
        assert budget.enabled

    def test_sidecar_scales_rounds_with_vram(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        # ~7.4GB free → constrained (max 2 rounds)
        profile = MockProfile(has_gpu=True, total_vram_mb=49152)
        budget = sim.assess_simulation_budget(profile, mode="sidecar")
        assert budget.enabled
        assert budget.max_rounds <= 2

        # ~16GB free → ample (max 3 rounds)
        profile_large = MockProfile(has_gpu=True, total_vram_mb=110000)
        budget_large = sim.assess_simulation_budget(profile_large, mode="sidecar")
        assert budget_large.enabled
        assert budget_large.max_rounds >= 3

    def test_clarification_always_enabled_on_22gb(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        profile = MockProfile(has_gpu=True, total_vram_mb=22528)
        budget = sim.assess_simulation_budget(profile, mode="clarification")
        assert budget.enabled
        assert "Clarification" in budget.reason

    def test_clarification_full_rounds_with_gpu(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        profile = MockProfile(has_gpu=True, total_vram_mb=22528)
        budget = sim.assess_simulation_budget(profile, mode="clarification")
        assert budget.max_rounds == 3

    def test_cpu_only_sidecar(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        budget = sim.assess_simulation_budget(None, mode="sidecar")
        assert budget.enabled
        assert budget.max_rounds <= 2
        assert "CPU-only" in budget.reason or "throughput" in budget.reason

    def test_cpu_only_clarification(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        budget = sim.assess_simulation_budget(None, mode="clarification")
        assert budget.enabled
        assert budget.max_rounds <= 2

    def test_no_gpu_in_profile(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        profile = MockProfile(has_gpu=False)
        budget = sim.assess_simulation_budget(profile, mode="clarification")
        assert budget.enabled
        assert "CPU-only" in budget.reason


# ===== _probe_free_vram =====


class TestProbeeFreeVram:
    """VRAM probing with heuristics and nvidia-smi fallback."""

    def test_sidecar_uses_15_percent(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        profile = MockProfile(has_gpu=True, total_vram_mb=24000)
        free = sim._probe_free_vram(profile, mode="sidecar")
        # 15% of 24000 = 3600
        assert free == int(24000 * 0.15)

    def test_clarification_uses_35_percent(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        profile = MockProfile(has_gpu=True, total_vram_mb=24000)
        free = sim._probe_free_vram(profile, mode="clarification")
        # 35% of 24000 = 8400
        assert free == int(24000 * 0.35)

    def test_no_gpu_returns_none(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        profile = MockProfile(has_gpu=False)
        free = sim._probe_free_vram(profile, mode="sidecar")
        assert free is None

    def test_nvidia_smi_fallback(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")

        with patch("agents.resource_discovery.get_free_vram_mb", return_value=4096):
            free = sim._probe_free_vram(None, mode="clarification")
            assert free == 4096

    def test_nvidia_smi_unavailable(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        with patch("agents.resource_discovery.get_free_vram_mb", return_value=None):
            free = sim._probe_free_vram(None, mode="sidecar")
            assert free is None


# ===== run_integration_simulation =====


class TestRunIntegrationSimulation:
    """Integration simulation sidecar."""

    def setup_method(self):
        self.sim = _reload_sim(VIBE_SIM_ENABLED="true")

    def test_skipped_when_budget_disabled(self):
        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=False, reason="test"),
        ):
            result = self.sim.run_integration_simulation(
                specification="test spec",
                sub_tasks=[],
                adapter_registry=MockAdapterRegistry(),
            )
            assert result.skipped
            assert "test" in result.skip_reason

    def test_runs_persona_rounds(self):
        persona_response = "- [RISK_LEVEL: MEDIUM] naming mismatch between modules"
        synthesis_response = """## Integration Risks
- [RISK_LEVEL: MEDIUM] naming mismatch between modules

## Recommended Mitigations
- Harmonize naming conventions"""

        adapters = {
            "sim_maintainer": MockAdapter("sim_maintainer", persona_response),
            "sim_consumer": MockAdapter("sim_consumer", persona_response),
            "sim_qa": MockAdapter("sim_qa", persona_response),
            "sim_synthesis": MockAdapter("sim_synthesis", synthesis_response),
        }
        registry = MockAdapterRegistry(adapters)

        sub_tasks = [
            {"task_type": "code_generation", "specialist_adapter": "code", "specification": "build API"},
            {"task_type": "test_generation", "specialist_adapter": "test", "specification": "test API"},
        ]

        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=True, max_rounds=3, max_tokens=600),
        ):
            result = self.sim.run_integration_simulation(
                specification="Build and test an API",
                sub_tasks=sub_tasks,
                adapter_registry=registry,
                delay_seconds=0,
            )

        assert not result.skipped
        assert result.rounds_completed >= 1
        assert result.report
        assert result.risk_level in ("low", "medium", "high")

    def test_partial_persona_failure(self):
        def fail_adapter(name):
            a = MockAdapter(name)
            a.generate = MagicMock(side_effect=Exception("LLM down"))
            return a

        adapters = {
            "sim_maintainer": fail_adapter("sim_maintainer"),
            "sim_consumer": MockAdapter("sim_consumer", "- [RISK_LEVEL: LOW] minor gap"),
            "sim_qa": fail_adapter("sim_qa"),
            "sim_synthesis": MockAdapter("sim_synthesis", "## Integration Risks\n- [RISK_LEVEL: LOW] minor gap"),
        }

        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=True, max_rounds=3, max_tokens=600),
        ):
            result = self.sim.run_integration_simulation(
                specification="test",
                sub_tasks=[{"task_type": "code", "specialist_adapter": "code", "specification": "x"}],
                adapter_registry=MockAdapterRegistry(adapters),
                delay_seconds=0,
            )

        assert not result.skipped
        assert result.rounds_completed >= 1

    def test_all_personas_fail(self):
        def fail_adapter(name):
            a = MockAdapter(name)
            a.generate = MagicMock(side_effect=Exception("fail"))
            return a

        adapters = {
            "sim_maintainer": fail_adapter("sim_maintainer"),
            "sim_consumer": fail_adapter("sim_consumer"),
            "sim_qa": fail_adapter("sim_qa"),
        }

        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=True, max_rounds=3, max_tokens=600),
        ):
            result = self.sim.run_integration_simulation(
                specification="test",
                sub_tasks=[],
                adapter_registry=MockAdapterRegistry(adapters),
                delay_seconds=0,
            )

        assert result.skipped
        assert "All persona rounds failed" in result.skip_reason


# ===== simulate_clarification =====


class TestSimulateClarification:
    """Clarification simulation."""

    def setup_method(self):
        self.sim = _reload_sim(VIBE_SIM_ENABLED="true")

    def test_skipped_when_no_questions(self):
        result = self.sim.simulate_clarification(
            questions=[],
            specification="spec",
            user_request="request",
            adapter_registry=MockAdapterRegistry(),
        )
        assert result.skipped
        assert "No questions" in result.skip_reason

    def test_resolved_with_high_confidence(self):
        stakeholder_response = (
            "1. PostgreSQL is the standard choice for this type of application.\n"
            "Confidence: HIGH\n"
            "2. The expected volume is around 1000 requests per minute.\n"
            "Confidence: HIGH"
        )

        mediator_response = """## Resolved
- Q: What database engine are you using?
  A: PostgreSQL is the standard choice.
  Confidence: HIGH
- Q: What is the expected request volume?
  A: Around 1000 requests per minute.
  Confidence: HIGH

## Unresolved

Overall confidence: 0.9"""

        adapters = {
            "sim_stakeholder_product_owner": MockAdapter("po", stakeholder_response),
            "sim_stakeholder_end_user": MockAdapter("eu", stakeholder_response),
            "sim_stakeholder_domain_expert": MockAdapter("de", stakeholder_response),
            "sim_mediator": MockAdapter("mediator", mediator_response),
        }

        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=True, max_rounds=3, max_tokens=600),
        ):
            result = self.sim.simulate_clarification(
                questions=[
                    "What database engine are you using?",
                    "What is the expected request volume?",
                ],
                specification="Build a web API",
                user_request="Build a scalable API",
                adapter_registry=MockAdapterRegistry(adapters),
            )

        assert result.resolved
        assert result.confidence >= 0.6
        assert len(result.answers) >= 1

    def test_unresolved_low_confidence(self):
        mediator_response = """## Resolved

## Unresolved
- Q: What framework should we use?
  Reason: All stakeholders uncertain

Overall confidence: 0.2"""

        adapters = {
            "sim_stakeholder_product_owner": MockAdapter("po", "UNCERTAIN\nConfidence: LOW"),
            "sim_stakeholder_end_user": MockAdapter("eu", "UNCERTAIN\nConfidence: LOW"),
            "sim_stakeholder_domain_expert": MockAdapter("de", "UNCERTAIN\nConfidence: LOW"),
            "sim_mediator": MockAdapter("mediator", mediator_response),
        }

        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=True, max_rounds=3, max_tokens=600),
        ):
            result = self.sim.simulate_clarification(
                questions=["What framework should we use?"],
                specification="spec",
                user_request="request",
                adapter_registry=MockAdapterRegistry(adapters),
            )

        assert not result.resolved
        assert len(result.unresolved) >= 1

    def test_partial_stakeholder_failure(self):
        def fail_adapter(name):
            a = MockAdapter(name)
            a.generate = MagicMock(side_effect=Exception("fail"))
            return a

        mediator_response = """## Resolved
- Q: What language?
  A: Python
  Confidence: MEDIUM

Overall confidence: 0.7"""

        adapters = {
            "sim_stakeholder_product_owner": fail_adapter("po"),
            "sim_stakeholder_end_user": MockAdapter("eu", "Python\nConfidence: MEDIUM"),
            "sim_stakeholder_domain_expert": fail_adapter("de"),
            "sim_mediator": MockAdapter("mediator", mediator_response),
        }

        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=True, max_rounds=3, max_tokens=600),
        ):
            result = self.sim.simulate_clarification(
                questions=["What language?"],
                specification="spec",
                user_request="request",
                adapter_registry=MockAdapterRegistry(adapters),
            )

        # Should still produce a result (not skipped) if at least one stakeholder worked
        assert not result.skipped


# ===== Parsing Helpers =====


class TestParseSimulationReport:
    """_parse_simulation_report parsing."""

    def setup_method(self):
        self.sim = _reload_sim(VIBE_SIM_ENABLED="true")

    def test_structured_risks(self):
        report = """## Integration Risks
- [RISK_LEVEL: HIGH] API signatures don't match
- [RISK_LEVEL: MEDIUM] inconsistent error handling
- [RISK_LEVEL: LOW] minor style differences"""

        conflicts, risk_level = self.sim._parse_simulation_report(report)
        assert len(conflicts) == 3
        assert risk_level == "high"
        assert conflicts[0]["level"] == "high"
        assert conflicts[1]["level"] == "medium"

    def test_no_structured_risks_keyword_fallback(self):
        report = "There is a critical mismatch between the API module and the test module."
        _, risk_level = self.sim._parse_simulation_report(report)
        assert risk_level == "high"

    def test_empty_report(self):
        conflicts, risk_level = self.sim._parse_simulation_report("")
        assert len(conflicts) == 0
        assert risk_level == "low"

    def test_warning_keyword(self):
        report = "Warning: there may be inconsistencies in naming."
        _, risk_level = self.sim._parse_simulation_report(report)
        assert risk_level == "medium"


class TestParseMediatorResponse:
    """_parse_mediator_response parsing."""

    def setup_method(self):
        self.sim = _reload_sim(VIBE_SIM_ENABLED="true")

    def test_resolved_qa_pairs(self):
        response = """## Resolved
- Q: What database?
  A: PostgreSQL
  Confidence: HIGH

Overall confidence: 0.85"""

        answers, unresolved, confidence = self.sim._parse_mediator_response(
            response, ["What database?"]
        )
        assert "What database?" in answers
        assert confidence == 0.85
        assert len(unresolved) == 0

    def test_unresolved_questions(self):
        response = """## Resolved

## Unresolved
- Q: What cloud provider?
  Reason: No consensus

Overall confidence: 0.3"""

        answers, unresolved, confidence = self.sim._parse_mediator_response(
            response, ["What cloud provider?"]
        )
        assert len(answers) == 0
        assert "What cloud provider?" in unresolved
        assert confidence == 0.3

    def test_missing_confidence_estimated(self):
        response = """## Resolved
- Q: What language?
  A: Python
  Confidence: HIGH"""

        answers, unresolved, confidence = self.sim._parse_mediator_response(
            response, ["What language?"]
        )
        # No "Overall confidence" line, so estimated from resolution ratio
        assert confidence > 0


class TestMatchQuestion:
    """_match_question fuzzy matching."""

    def setup_method(self):
        self.sim = _reload_sim(VIBE_SIM_ENABLED="true")

    def test_exact_match(self):
        result = self.sim._match_question(
            "What database?",
            ["What database?", "What language?"],
        )
        assert result == "What database?"

    def test_prefix_match(self):
        result = self.sim._match_question(
            "What database engine should we use for this project?",
            ["What database engine should we use for production?"],
        )
        assert result is not None

    def test_no_match(self):
        result = self.sim._match_question(
            "completely different question",
            ["What database?", "What language?"],
        )
        assert result is None


# ===== Formatting Helpers =====


class TestFormatSimulationForAggregator:
    """format_simulation_for_aggregator."""

    def setup_method(self):
        self.sim = _reload_sim(VIBE_SIM_ENABLED="true")

    def test_with_conflicts(self):
        report = self.sim.SimulationReport(
            report="Integration risks identified.",
            conflicts=[
                {"level": "high", "description": "API mismatch"},
                {"level": "low", "description": "style diff"},
            ],
            risk_level="high",
            rounds_completed=3,
        )
        output = self.sim.format_simulation_for_aggregator(report)
        assert "risk: high" in output
        assert "API mismatch" in output

    def test_skipped_returns_empty(self):
        report = self.sim.SimulationReport(skipped=True, skip_reason="test")
        output = self.sim.format_simulation_for_aggregator(report)
        assert output == ""

    def test_no_report_text_returns_empty(self):
        report = self.sim.SimulationReport(report="")
        output = self.sim.format_simulation_for_aggregator(report)
        assert output == ""


class TestFormatClarificationForSpec:
    """format_clarification_for_spec."""

    def setup_method(self):
        self.sim = _reload_sim(VIBE_SIM_ENABLED="true")

    def test_resolved_answers(self):
        result = self.sim.ClarificationResult(
            resolved=True,
            answers={"What DB?": "PostgreSQL"},
            confidence=0.85,
        )
        output = self.sim.format_clarification_for_spec(result)
        assert "PostgreSQL" in output
        assert "85%" in output

    def test_unresolved_returns_empty(self):
        result = self.sim.ClarificationResult(resolved=False)
        output = self.sim.format_clarification_for_spec(result)
        assert output == ""


# ===== register_simulation_adapters =====


class TestRegisterSimulationAdapters:
    """Adapter registration."""

    def test_registers_correct_count(self):
        sim = _reload_sim(VIBE_SIM_ENABLED="true")
        registry = MockAdapterRegistry()
        base_model = MagicMock()

        sim.register_simulation_adapters(registry, base_model)

        # 5 sim adapters + 3 stakeholder adapters + 3 skill vet adapters = 11
        registered = registry.list_adapters()
        assert len(registered) >= 8  # At minimum the original 8


# ===== vet_skill_with_simulation =====


class TestVetSkillWithSimulation:
    """Offline skill vetting."""

    def setup_method(self):
        self.sim = _reload_sim(VIBE_SIM_ENABLED="true", VIBE_SIM_VET_SKILLS="true")

    def test_skipped_when_disabled(self):
        sim = _reload_sim(VIBE_SIM_VET_SKILLS="false", VIBE_SIM_ENABLED="true")
        result = sim.vet_skill_with_simulation(
            skill_content="# Test Skill",
            task_type="code_generation",
            adapter_registry=MockAdapterRegistry(),
        )
        assert result.skipped

    def test_runs_and_scores(self):
        task_gen_response = "TASK: Build a REST API with Flask\nTASK: Create a CLI tool\nTASK: Write a data parser"
        specialist_response = "Here is the implementation..."
        critic_response = "SCORE: 75\nFEEDBACK: Good but missing error handling"

        adapters = {
            "sim_vet_task_gen": MockAdapter("sim_vet_task_gen", task_gen_response),
            "sim_vet_specialist": MockAdapter("sim_vet_specialist", specialist_response),
            "sim_vet_critic": MockAdapter("sim_vet_critic", critic_response),
        }

        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=True, max_rounds=3, max_tokens=600),
        ):
            result = self.sim.vet_skill_with_simulation(
                skill_content="# Code Gen Skill\n## How It Works\n1. Write code",
                task_type="code_generation",
                adapter_registry=MockAdapterRegistry(adapters),
            )

        assert not result.skipped
        assert result.tasks_evaluated >= 1
        assert result.avg_score >= 0

    def test_skipped_when_budget_disabled(self):
        with patch.object(
            self.sim, "assess_simulation_budget",
            return_value=self.sim.SimulationBudget(enabled=False, reason="test"),
        ):
            result = self.sim.vet_skill_with_simulation(
                skill_content="# Skill",
                task_type="code",
                adapter_registry=MockAdapterRegistry(),
            )
        assert result.skipped
