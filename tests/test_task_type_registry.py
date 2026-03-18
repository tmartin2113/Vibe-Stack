"""
Tests for the unified TaskTypeRegistry.
"""

import os
from unittest.mock import MagicMock

import pytest

os.environ["GENESIA_DISABLE_REMOTE_SKILLS"] = "1"

from agents.task_type_registry import (
    BUILTIN_TYPES,
    TaskTypeEntry,
    TaskTypeRegistry,
    create_default_registry,
    populate_from_skill_registry,
)


class TestTaskTypeRegistry:
    """Core registry operations."""

    def test_empty_registry(self):
        reg = TaskTypeRegistry()
        assert len(reg) == 0
        assert reg.type_names() == []

    def test_register_and_get(self):
        reg = TaskTypeRegistry()
        entry = TaskTypeEntry(
            name="my_type", description="desc", adapter="genesia",
            label="my type", source="builtin",
        )
        reg.register(entry)
        assert "my_type" in reg
        assert reg.get("my_type") is entry
        assert len(reg) == 1

    def test_get_missing_returns_none(self):
        reg = TaskTypeRegistry()
        assert reg.get("nonexistent") is None

    def test_skill_does_not_overwrite_builtin(self):
        reg = TaskTypeRegistry()
        builtin = TaskTypeEntry(
            name="code_generation", description="builtin desc",
            adapter="genesia", label="code gen", source="builtin",
        )
        skill = TaskTypeEntry(
            name="code_generation", description="skill desc",
            adapter="custom_adapter", label="code gen", source="skill",
        )
        reg.register(builtin)
        reg.register(skill)
        assert reg.get("code_generation").adapter == "genesia"
        assert reg.get("code_generation").description == "builtin desc"

    def test_skill_can_register_new_type(self):
        reg = TaskTypeRegistry()
        skill = TaskTypeEntry(
            name="ml_pipeline", description="ML workflows",
            adapter="genesia", label="ML pipeline", source="skill",
        )
        reg.register(skill)
        assert "ml_pipeline" in reg

    def test_builtin_overwrites_skill(self):
        reg = TaskTypeRegistry()
        skill = TaskTypeEntry(
            name="code_generation", description="skill desc",
            adapter="custom", label="code gen", source="skill",
        )
        builtin = TaskTypeEntry(
            name="code_generation", description="builtin desc",
            adapter="genesia", label="code gen", source="builtin",
        )
        reg.register(skill)
        reg.register(builtin)  # builtin should overwrite skill
        assert reg.get("code_generation").adapter == "genesia"


class TestRegistryProjections:
    """Test the dict-projection methods."""

    @pytest.fixture
    def registry(self):
        return create_default_registry()

    def test_adapter_mapping_returns_all(self, registry):
        mapping = registry.adapter_mapping()
        assert mapping["test_generation"] == "test_generator"
        assert mapping["code_generation"] == "genesia"
        assert mapping["general"] == "genesia"

    def test_task_descriptions_returns_all(self, registry):
        descs = registry.task_descriptions()
        assert "test_generation" in descs
        assert "pytest" in descs["test_generation"].lower()

    def test_task_labels_returns_all(self, registry):
        labels = registry.task_labels()
        assert labels["api_development"] == "API development"

    def test_task_patterns_returns_lists(self, registry):
        patterns = registry.task_patterns()
        assert isinstance(patterns["test_generation"], list)
        assert len(patterns["test_generation"]) > 0
        assert patterns["general"] == []

    def test_pattern_weights_excludes_empty(self, registry):
        weights = registry.pattern_weights()
        # "general" has no weights
        assert "general" not in weights
        assert r"\bpytest" in weights["test_generation"]

    def test_hybrid_thresholds_returns_all(self, registry):
        thresholds = registry.hybrid_thresholds()
        assert thresholds["test_generation"] == 0.5
        assert thresholds["general"] == 0.7


class TestDefaultRegistry:
    """Tests for the pre-populated default registry."""

    def test_has_12_builtin_types(self):
        reg = create_default_registry()
        assert len(reg) == 12

    def test_all_builtins_are_builtin_source(self):
        reg = create_default_registry()
        for entry in reg.all_types().values():
            assert entry.source == "builtin"

    def test_builtin_list_matches_registry(self):
        reg = create_default_registry()
        builtin_names = {e.name for e in BUILTIN_TYPES}
        registry_names = set(reg.all_types().keys())
        assert builtin_names == registry_names


class TestPopulateFromSkillRegistry:
    """Tests for skill-registry injection."""

    def test_injects_new_types(self):
        reg = create_default_registry()
        skill_reg = MagicMock()
        skill_reg.get_all_custom_task_types.return_value = {
            "ml_pipeline": "Machine learning pipeline tasks",
            "infrastructure": "Infrastructure management",
        }
        injected = populate_from_skill_registry(reg, skill_reg)
        assert injected == 2
        assert "ml_pipeline" in reg
        assert "infrastructure" in reg
        assert reg.get("ml_pipeline").source == "skill"
        assert reg.get("ml_pipeline").adapter == "genesia"

    def test_skips_existing_builtin_types(self):
        reg = create_default_registry()
        skill_reg = MagicMock()
        skill_reg.get_all_custom_task_types.return_value = {
            "code_generation": "Custom code gen",  # already builtin
            "ml_pipeline": "ML tasks",
        }
        injected = populate_from_skill_registry(reg, skill_reg)
        assert injected == 1  # only ml_pipeline
        assert reg.get("code_generation").source == "builtin"

    def test_empty_skill_registry(self):
        reg = create_default_registry()
        skill_reg = MagicMock()
        skill_reg.get_all_custom_task_types.return_value = {}
        injected = populate_from_skill_registry(reg, skill_reg)
        assert injected == 0
        assert len(reg) == 12


class TestRouterUsesRegistry:
    """Verify the router reads from the registry instead of hardcoding."""

    def test_router_has_all_builtin_types(self):
        from agents.router import RouterNode
        router = RouterNode(classification_mode="regex")
        assert "test_generation" in router.task_patterns
        assert "code_generation" in router.adapter_mapping
        assert router.adapter_mapping["test_generation"] == "test_generator"

    def test_router_uses_custom_registry(self):
        from agents.router import RouterNode
        reg = create_default_registry()
        reg.register(TaskTypeEntry(
            name="custom_type", description="Custom",
            adapter="genesia", label="custom",
            patterns=[r"\bcustom"], pattern_weights={r"\bcustom": 2.0},
            source="skill",
        ))
        router = RouterNode(
            classification_mode="regex",
            task_type_registry=reg,
        )
        assert "custom_type" in router.task_patterns
        assert "custom_type" in router.adapter_mapping
        assert router.task_patterns["custom_type"] == [r"\bcustom"]

    def test_router_classifies_custom_type_via_regex(self):
        from agents.router import RouterNode
        reg = create_default_registry()
        reg.register(TaskTypeEntry(
            name="ml_pipeline", description="ML workflows",
            adapter="genesia", label="ML pipeline",
            patterns=[r"\bml.*pipeline", r"\btrain.*model", r"\bfeature.*engineer"],
            pattern_weights={
                r"\bml.*pipeline": 3.0,
                r"\btrain.*model": 2.5,
                r"\bfeature.*engineer": 2.0,
            },
            source="skill",
        ))
        router = RouterNode(
            classification_mode="regex",
            task_type_registry=reg,
        )
        task_type, confidence = router._classify_task_regex(
            "Build an ML pipeline to train the model with feature engineering"
        )
        assert task_type == "ml_pipeline"
        assert confidence > 0.0

    def test_skill_types_visible_to_orchestrator_decomposition(self):
        """Custom types from registry are available for decomposition."""
        from agents.router import RouterNode
        reg = create_default_registry()
        reg.register(TaskTypeEntry(
            name="ml_pipeline", description="ML workflows",
            adapter="genesia", label="ML pipeline",
            patterns=[r"\bml.*pipeline", r"\btrain.*model"],
            pattern_weights={r"\bml.*pipeline": 3.0, r"\btrain.*model": 2.5},
            source="skill",
        ))
        router = RouterNode(
            classification_mode="regex",
            task_type_registry=reg,
        )
        # ml_pipeline patterns are in task_patterns used by _requires_decomposition
        assert "ml_pipeline" in router.task_patterns
        assert len(router.task_patterns["ml_pipeline"]) > 0
