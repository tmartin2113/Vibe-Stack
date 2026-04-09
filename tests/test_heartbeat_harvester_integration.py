"""Light integration test: the heartbeat calls the canonical harvester after success.

Does not actually spin up the full heartbeat — just patches the call site
and asserts it runs with the right state.
"""

from unittest.mock import MagicMock

import pytest


class TestHeartbeatHarvesterHook:
    def test_harvester_called_on_successful_state(self, monkeypatch):
        # We import the harvester module and spy on maybe_capture_canonical.
        import agents.canonical_harvester as harvester_mod

        recorded_states = []

        def spy(*, state, task_type_registry, **kwargs):
            recorded_states.append(dict(state))
            return None

        monkeypatch.setattr(harvester_mod, "maybe_capture_canonical", spy)

        from agents.heartbeat import _run_canonical_harvester_hook

        fake_state = {
            "routed_task_type": "code_generation",
            "critic_score": 95,
            "user_prompt": "hello",
            "final_output": "world",
            "model_id": "vllm-local",
        }
        fake_registry = MagicMock()
        fake_registry.adapter_mapping.return_value = {"code_generation": "vibe"}

        _run_canonical_harvester_hook(fake_state, fake_registry)

        assert len(recorded_states) == 1
        assert recorded_states[0]["routed_task_type"] == "code_generation"

    def test_hook_swallows_harvester_exception(self, monkeypatch):
        import agents.canonical_harvester as harvester_mod

        def boom(**kwargs):
            raise RuntimeError("harvester blew up")

        monkeypatch.setattr(harvester_mod, "maybe_capture_canonical", boom)

        from agents.heartbeat import _run_canonical_harvester_hook

        # Must not raise
        _run_canonical_harvester_hook({}, MagicMock())
