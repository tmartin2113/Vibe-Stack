"""Tests for PromptAdapter + override loader integration."""

import pytest

from agents.adapters import PromptAdapter


class _FakeBackend:
    def __init__(self):
        self.last_messages = None
        self.return_text = "ok"

    def generate(self, messages, **kwargs):
        self.last_messages = messages
        return self.return_text


class _StubLoader:
    def __init__(self, mapping):
        self._mapping = mapping

    def get_appends_for(self, task_type):
        return list(self._mapping.get(task_type, []))


class TestPromptAdapterTaskType:
    def test_no_task_type_kwarg_is_backward_compatible(self):
        backend = _FakeBackend()
        loader = _StubLoader({"code_generation": ["EXTRA INSTRUCTION"]})
        adapter = PromptAdapter(
            name="test", system_prompt="BASE", base_model=backend,
            override_loader=loader,
        )
        adapter.generate("do a thing")
        system = backend.last_messages[0]["content"]
        assert system == "BASE"  # no task_type → no append

    def test_task_type_kwarg_appends_override(self):
        backend = _FakeBackend()
        loader = _StubLoader({"code_generation": ["EXTRA INSTRUCTION"]})
        adapter = PromptAdapter(
            name="test", system_prompt="BASE", base_model=backend,
            override_loader=loader,
        )
        adapter.generate("do a thing", task_type="code_generation")
        system = backend.last_messages[0]["content"]
        assert system.startswith("BASE")
        assert "EXTRA INSTRUCTION" in system

    def test_multiple_appends_all_present(self):
        backend = _FakeBackend()
        loader = _StubLoader({
            "code_generation": ["FIRST RULE", "SECOND RULE"]
        })
        adapter = PromptAdapter(
            name="test", system_prompt="BASE", base_model=backend,
            override_loader=loader,
        )
        adapter.generate("do a thing", task_type="code_generation")
        system = backend.last_messages[0]["content"]
        assert "FIRST RULE" in system
        assert "SECOND RULE" in system

    def test_static_system_prompt_never_mutated(self):
        backend = _FakeBackend()
        loader = _StubLoader({"code_generation": ["EXTRA"]})
        adapter = PromptAdapter(
            name="test", system_prompt="BASE", base_model=backend,
            override_loader=loader,
        )
        adapter.generate("do a thing", task_type="code_generation")
        # A second call without task_type should produce the clean base.
        adapter.generate("another thing")
        system = backend.last_messages[0]["content"]
        assert system == "BASE"
        assert adapter.system_prompt == "BASE"

    def test_task_type_with_no_matching_overrides(self):
        backend = _FakeBackend()
        loader = _StubLoader({})
        adapter = PromptAdapter(
            name="test", system_prompt="BASE", base_model=backend,
            override_loader=loader,
        )
        adapter.generate("do a thing", task_type="unknown_type")
        system = backend.last_messages[0]["content"]
        assert system == "BASE"

    def test_loader_none_with_task_type_is_no_op(self):
        backend = _FakeBackend()
        adapter = PromptAdapter(
            name="test", system_prompt="BASE", base_model=backend,
            override_loader=None,
        )
        adapter.generate("do a thing", task_type="code_generation")
        system = backend.last_messages[0]["content"]
        assert system == "BASE"

    def test_explicit_system_prompt_override_kwarg_still_works(self):
        """Existing 'system_prompt' kwarg override still composes with task_type appends."""
        backend = _FakeBackend()
        loader = _StubLoader({"code_generation": ["EXTRA"]})
        adapter = PromptAdapter(
            name="test", system_prompt="BASE", base_model=backend,
            override_loader=loader,
        )
        adapter.generate(
            "do a thing",
            system_prompt="CALLER_OVERRIDE",
            task_type="code_generation",
        )
        system = backend.last_messages[0]["content"]
        assert system.startswith("CALLER_OVERRIDE")
        assert "EXTRA" in system
