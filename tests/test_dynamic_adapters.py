"""
Tests for dynamic adapter resolution — spec-driven agent types.

Covers:
- SKILL.md frontmatter parsing: adapter-prompt, generation-config, tools-enabled
- AdapterRegistry.get_or_create: dynamic adapter creation from skill prompts
- SpecialistNodesMixin: skill-provided prompt/config resolution
- Router: respecting pre-set task types from orchestrator
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ["GENESIA_DISABLE_REMOTE_SKILLS"] = "1"

from agents.skill_security import SkillSecurity
from agents.adapters import AdapterRegistry, PromptAdapter


# ====================================================================
# Frontmatter parsing: adapter-prompt
# ====================================================================


class TestParseAdapterPrompt:
    """Test adapter-prompt parsing from SKILL.md frontmatter."""

    @pytest.fixture
    def security(self):
        return SkillSecurity()

    def test_absent_returns_none(self, security):
        content = "---\nname: test-skill\n---\n# Body"
        assert security.parse_adapter_prompt(content) is None

    def test_inline_value(self, security):
        content = "---\nadapter-prompt: You are an ML pipeline specialist.\n---\n# Body"
        result = security.parse_adapter_prompt(content)
        assert result == "You are an ML pipeline specialist."

    def test_block_value(self, security):
        content = (
            "---\n"
            "adapter-prompt: >\n"
            "  You are an expert at building\n"
            "  data pipelines with Apache Beam.\n"
            "---\n"
            "# Body"
        )
        result = security.parse_adapter_prompt(content)
        assert "data pipelines" in result
        assert "Apache Beam" in result

    def test_empty_value_returns_empty_string(self, security):
        content = "---\nadapter-prompt:\n---\n# Body"
        result = security.parse_adapter_prompt(content)
        assert result == ""

    def test_no_frontmatter_returns_none(self, security):
        content = "# Just a body, no frontmatter"
        assert security.parse_adapter_prompt(content) is None

    def test_multiline_block_scalar(self, security):
        content = (
            "---\n"
            "name: ml-agent\n"
            "adapter-prompt: |-\n"
            "  You are a machine learning engineer.\n"
            "  You specialize in PyTorch and TensorFlow.\n"
            "allowed-tools: Read Glob\n"
            "---\n"
            "# Body"
        )
        result = security.parse_adapter_prompt(content)
        assert "machine learning engineer" in result
        assert "PyTorch" in result

    def test_does_not_bleed_into_next_key(self, security):
        content = (
            "---\n"
            "adapter-prompt: You are a specialist.\n"
            "allowed-tools: Read Glob\n"
            "---\n"
        )
        result = security.parse_adapter_prompt(content)
        assert result == "You are a specialist."
        assert "Read" not in result


# ====================================================================
# Frontmatter parsing: generation-config
# ====================================================================


class TestParseGenerationConfig:
    """Test generation-config parsing from SKILL.md frontmatter."""

    @pytest.fixture
    def security(self):
        return SkillSecurity()

    def test_absent_returns_none(self, security):
        content = "---\nname: test\n---\n# Body"
        assert security.parse_generation_config(content) is None

    def test_parses_key_value_pairs(self, security):
        content = "---\ngeneration-config: temperature=0.2 max_tokens=2000\n---\n"
        result = security.parse_generation_config(content)
        assert result == {"temperature": 0.2, "max_tokens": 2000.0}

    def test_ignores_unknown_keys(self, security):
        content = "---\ngeneration-config: temperature=0.5 evil_param=999\n---\n"
        result = security.parse_generation_config(content)
        assert result == {"temperature": 0.5}

    def test_ignores_non_numeric_values(self, security):
        content = "---\ngeneration-config: temperature=hot max_tokens=1000\n---\n"
        result = security.parse_generation_config(content)
        assert result == {"max_tokens": 1000.0}

    def test_empty_returns_none(self, security):
        content = "---\ngeneration-config:\n---\n"
        result = security.parse_generation_config(content)
        assert result is None

    def test_all_known_keys(self, security):
        content = "---\ngeneration-config: temperature=0.3 max_tokens=1500 top_p=0.9 top_k=40\n---\n"
        result = security.parse_generation_config(content)
        assert result == {
            "temperature": 0.3,
            "max_tokens": 1500.0,
            "top_p": 0.9,
            "top_k": 40.0,
        }


# ====================================================================
# Frontmatter parsing: tools-enabled
# ====================================================================


class TestParseToolsEnabled:
    """Test tools-enabled parsing from SKILL.md frontmatter."""

    @pytest.fixture
    def security(self):
        return SkillSecurity()

    def test_absent_returns_none(self, security):
        content = "---\nname: test\n---\n"
        assert security.parse_tools_enabled(content) is None

    def test_true_values(self, security):
        for val in ("true", "True", "yes", "1"):
            content = f"---\ntools-enabled: {val}\n---\n"
            assert security.parse_tools_enabled(content) is True

    def test_false_values(self, security):
        for val in ("false", "False", "no", "0"):
            content = f"---\ntools-enabled: {val}\n---\n"
            assert security.parse_tools_enabled(content) is False

    def test_empty_is_false(self, security):
        content = "---\ntools-enabled:\n---\n"
        assert security.parse_tools_enabled(content) is False


# ====================================================================
# AdapterRegistry.get_or_create
# ====================================================================


class TestGetOrCreate:
    """Test dynamic adapter creation via get_or_create."""

    @pytest.fixture
    def registry(self):
        reg = AdapterRegistry()
        model = MagicMock()
        model.generate.return_value = "test output"
        # Register a base adapter
        reg.register(PromptAdapter("genesia", "Base prompt", model))
        reg.register(PromptAdapter("test_generator", "Test prompt", model))
        return reg

    def test_returns_existing_adapter_when_no_skill_prompt(self, registry):
        adapter = registry.get_or_create("test_generator")
        assert adapter.name == "test_generator"
        assert adapter.system_prompt == "Test prompt"

    def test_creates_dynamic_adapter_with_skill_prompt(self, registry):
        adapter = registry.get_or_create(
            "ml_pipeline", "You are an ML pipeline specialist."
        )
        assert adapter.name == "ml_pipeline__skill"
        assert adapter.system_prompt == "You are an ML pipeline specialist."

    def test_caches_dynamic_adapter(self, registry):
        prompt = "You are a data engineer."
        a1 = registry.get_or_create("data_eng", prompt)
        a2 = registry.get_or_create("data_eng", prompt)
        assert a1 is a2

    def test_recreates_on_prompt_change(self, registry):
        a1 = registry.get_or_create("custom", "Prompt v1")
        a2 = registry.get_or_create("custom", "Prompt v2")
        assert a2.system_prompt == "Prompt v2"
        assert a1 is not a2

    def test_falls_back_to_genesia_for_unknown(self, registry):
        adapter = registry.get_or_create("totally_unknown")
        assert adapter.name == "genesia"

    def test_skill_prompt_overrides_existing_adapter(self, registry):
        """Even if test_generator is registered, skill prompt takes priority."""
        adapter = registry.get_or_create(
            "test_generator", "Custom test specialist prompt"
        )
        assert adapter.name == "test_generator__skill"
        assert adapter.system_prompt == "Custom test specialist prompt"

    def test_dynamic_adapter_uses_base_model(self, registry):
        """Dynamic adapters borrow the base_model from existing adapters."""
        adapter = registry.get_or_create("new_type", "New prompt")
        assert adapter.base_model is registry.adapters["genesia"].base_model


# ====================================================================
# SpecialistNodesMixin: skill resolution helpers
# ====================================================================


class TestSkillResolutionHelpers:
    """Test the static resolution methods on SpecialistNodesMixin."""

    def _get_mixin_class(self):
        from agents.specialist_nodes import SpecialistNodesMixin
        return SpecialistNodesMixin

    def test_resolve_adapter_prompt_from_skills(self):
        cls = self._get_mixin_class()
        skills = [
            {"name": "s1", "adapter_prompt": None},
            {"name": "s2", "adapter_prompt": "You are a DB expert."},
        ]
        assert cls._resolve_skill_adapter_prompt(skills) == "You are a DB expert."

    def test_resolve_adapter_prompt_empty_when_none(self):
        cls = self._get_mixin_class()
        skills = [{"name": "s1", "adapter_prompt": None}]
        assert cls._resolve_skill_adapter_prompt(skills) == ""

    def test_resolve_adapter_prompt_first_wins(self):
        cls = self._get_mixin_class()
        skills = [
            {"name": "s1", "adapter_prompt": "First prompt"},
            {"name": "s2", "adapter_prompt": "Second prompt"},
        ]
        assert cls._resolve_skill_adapter_prompt(skills) == "First prompt"

    def test_resolve_generation_config_merges(self):
        cls = self._get_mixin_class()
        skills = [
            {"name": "s1", "generation_config": {"temperature": 0.2}},
            {"name": "s2", "generation_config": {"max_tokens": 2000}},
        ]
        result = cls._resolve_skill_generation_config(skills)
        assert result == {"temperature": 0.2, "max_tokens": 2000}

    def test_resolve_generation_config_first_wins_conflicts(self):
        cls = self._get_mixin_class()
        skills = [
            {"name": "s1", "generation_config": {"temperature": 0.2}},
            {"name": "s2", "generation_config": {"temperature": 0.8}},
        ]
        result = cls._resolve_skill_generation_config(skills)
        assert result["temperature"] == 0.2

    def test_resolve_generation_config_empty(self):
        cls = self._get_mixin_class()
        skills = [{"name": "s1", "generation_config": None}]
        assert cls._resolve_skill_generation_config(skills) == {}

    def test_resolve_tools_enabled_true(self):
        cls = self._get_mixin_class()
        skills = [{"name": "s1", "tools_enabled": True}]
        assert cls._resolve_skill_tools_enabled(skills) is True

    def test_resolve_tools_enabled_false(self):
        cls = self._get_mixin_class()
        skills = [{"name": "s1", "tools_enabled": False}]
        assert cls._resolve_skill_tools_enabled(skills) is False

    def test_resolve_tools_enabled_none_when_absent(self):
        cls = self._get_mixin_class()
        skills = [{"name": "s1", "tools_enabled": None}]
        assert cls._resolve_skill_tools_enabled(skills) is None

    def test_resolve_tools_enabled_first_wins(self):
        cls = self._get_mixin_class()
        skills = [
            {"name": "s1", "tools_enabled": None},
            {"name": "s2", "tools_enabled": True},
        ]
        assert cls._resolve_skill_tools_enabled(skills) is True


# ====================================================================
# Router: pre-set task type
# ====================================================================


class TestRouterPreSetTaskType:
    """Test that the router respects pre-set task types from the orchestrator."""

    @pytest.fixture
    def router(self):
        from agents.router import RouterNode
        model = MagicMock()
        model.generate.return_value = '{"task_type": "code_generation"}'
        skill_registry = MagicMock()
        skill_registry.discover_skills.return_value = []
        skill_registry.find_skill.return_value = (None, None, None)
        return RouterNode(skill_registry=skill_registry, base_model=model)

    def test_pre_set_task_type_skips_classification(self, router):
        state = {
            "specification": "Build an ML pipeline",
            "routed_task_type": "ml_pipeline",
            "debug_info": {},
        }
        result = router.execute(state)
        assert result["routed_task_type"] == "ml_pipeline"
        assert result["routing_confidence"] == 1.0
        assert result["debug_info"]["router_decision"]["classification_mode"] == "pre_set"

    def test_pre_set_unknown_type_defaults_to_genesia(self, router):
        state = {
            "specification": "Do something custom",
            "routed_task_type": "custom_agent_type",
            "debug_info": {},
        }
        result = router.execute(state)
        assert result["specialist_adapter"] == "genesia"
        assert result["routed_task_type"] == "custom_agent_type"

    def test_pre_set_known_type_uses_mapping(self, router):
        state = {
            "specification": "Write tests",
            "routed_task_type": "test_generation",
            "debug_info": {},
        }
        result = router.execute(state)
        assert result["specialist_adapter"] == "test_generator"

    def test_no_pre_set_falls_through_to_classification(self, router):
        state = {
            "specification": "Write comprehensive unit tests for the auth module",
            "debug_info": {},
        }
        result = router.execute(state)
        # Should classify via regex/llm, not use "pre_set" mode
        assert result["debug_info"]["router_decision"]["classification_mode"] != "pre_set"
        assert result["routed_task_type"] != ""


# ====================================================================
# End-to-end: SKILL.md with adapter-prompt flows through to specialist
# ====================================================================


class TestSkillLoaderAdapterFields:
    """Test that the skill loader extracts adapter fields from SKILL.md."""

    def test_loader_extracts_adapter_prompt(self, tmp_path):
        from agents.skill_registry import SkillRegistry

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        sec = SkillSecurity()
        reg = SkillRegistry(str(skills_dir), security=sec)

        # Create a skill with adapter-prompt
        skill_dir = skills_dir / "local" / "ml-pipeline"
        skill_dir.mkdir(parents=True)
        content = (
            "---\n"
            "name: ml-pipeline\n"
            "description: ML pipeline specialist\n"
            "adapter-prompt: You are an ML pipeline engineer.\n"
            "generation-config: temperature=0.2 max_tokens=3000\n"
            "tools-enabled: true\n"
            "---\n"
            "# ML Pipeline Skill\n"
            "Build data pipelines.\n"
        )
        (skill_dir / "SKILL.md").write_text(content)

        # Register the skill
        reg.register_skill("ml-pipeline", "ML", "local", ["ml_pipeline"], skill_dir)

        # Load via SkillLoaderNode
        from agents.skill_loader import SkillLoaderNode
        loader = SkillLoaderNode(reg)
        state = {
            "discovered_skills": [{
                "skill_name": "ml-pipeline",
                "tier": "local",
                "task_type": "ml_pipeline",
                "skill_path": str(skill_dir),
            }],
            "debug_info": {},
        }
        result = loader.execute(state)

        loaded = result["loaded_skills"]
        assert len(loaded) == 1
        skill = loaded[0]
        assert skill["adapter_prompt"] == "You are an ML pipeline engineer."
        assert skill["generation_config"] == {"temperature": 0.2, "max_tokens": 3000.0}
        assert skill["tools_enabled"] is True

    def test_loader_handles_missing_adapter_fields(self, tmp_path):
        from agents.skill_registry import SkillRegistry

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        sec = SkillSecurity()
        reg = SkillRegistry(str(skills_dir), security=sec)

        skill_dir = skills_dir / "local" / "basic"
        skill_dir.mkdir(parents=True)
        content = (
            "---\n"
            "name: basic\n"
            "description: A basic skill\n"
            "---\n"
            "# Basic\n"
        )
        (skill_dir / "SKILL.md").write_text(content)
        reg.register_skill("basic", "Basic", "local", ["general"], skill_dir)

        from agents.skill_loader import SkillLoaderNode
        loader = SkillLoaderNode(reg)
        state = {
            "discovered_skills": [{
                "skill_name": "basic",
                "tier": "local",
                "task_type": "general",
                "skill_path": str(skill_dir),
            }],
            "debug_info": {},
        }
        result = loader.execute(state)

        loaded = result["loaded_skills"]
        assert len(loaded) == 1
        skill = loaded[0]
        assert skill["adapter_prompt"] is None
        assert skill["generation_config"] is None
        assert skill["tools_enabled"] is None
