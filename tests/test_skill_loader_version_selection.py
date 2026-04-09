"""Tests for version-aware skill loading in agents/skill_loader.py.

Verifies that when a __v{N} sibling exists alongside a discovered skill,
the loader deterministically buckets the run to one version via
sha256(session_id) % 2 and mutates skill_info in place so discovered_skills
and loaded_skills agree on the versioned name.
"""

import hashlib

import pytest


def _bucket_for(session_id: str) -> int:
    """Independent re-computation of the bucket formula for test inputs."""
    return hashlib.sha256(session_id.encode("utf-8")).digest()[0] % 2


def _session_id_for_bucket(target: int) -> str:
    """Find a session_id whose bucket assignment matches ``target``."""
    for i in range(1000):
        sid = f"session_{i}"
        if _bucket_for(sid) == target:
            return sid
    raise RuntimeError("unreachable")


@pytest.fixture
def registry_with_skill(tmp_path):
    """Create a real SkillRegistry with one skill registered in the temp tier.

    Uses kebab-case names because validate_skill_name enforces
    ``^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`` via register_skill.
    """
    from agents.skill_registry import SkillRegistry

    registry = SkillRegistry(str(tmp_path))
    skill_dir = registry.temp_dir / "my-code-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-code-skill\n---\n\n# v1 content"
    )
    registry.register_skill(
        name="my-code-skill",
        description="test skill",
        tier="temp",
        task_types=["code_generation"],
        skill_path=skill_dir,
    )
    return registry, skill_dir


def _load_state(discovered_name, skill_path, session_id):
    """Build a minimal AgentState dict for the loader."""
    return {
        "session_id": session_id,
        "discovered_skills": [{
            "skill_name": discovered_name,
            "skill_path": str(skill_path),
            "task_type": "code_generation",
            "tier": "temp",
        }],
        "loaded_skills": [],
    }


class TestSkillLoaderVersionSelection:
    def test_single_version_loads_base(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        loader = SkillLoaderNode(skill_registry=registry)
        state = _load_state("my-code-skill", skill_dir, "any-session")

        result = loader.execute(state)
        assert len(result["loaded_skills"]) == 1
        assert result["loaded_skills"][0]["name"] == "my-code-skill"

    def test_picks_v1_when_bucket_zero(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        # Create v2 sibling and register it via the registry
        v2_dir = skill_dir.parent / "my-code-skill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text(
            "---\nname: my-code-skill__v2\n---\n\n# v2 content"
        )
        registry.register_skill(
            name="my-code-skill__v2",
            description="v2 refined",
            tier="temp",
            task_types=["code_generation"],
            skill_path=v2_dir,
        )

        loader = SkillLoaderNode(skill_registry=registry)
        state = _load_state(
            "my-code-skill", skill_dir, _session_id_for_bucket(0)
        )

        result = loader.execute(state)
        assert result["loaded_skills"][0]["name"] == "my-code-skill"
        assert "v1 content" in result["loaded_skills"][0]["content"]

    def test_picks_v2_when_bucket_one(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        v2_dir = skill_dir.parent / "my-code-skill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text(
            "---\nname: my-code-skill__v2\n---\n\n# v2 content"
        )
        registry.register_skill(
            name="my-code-skill__v2",
            description="v2 refined",
            tier="temp",
            task_types=["code_generation"],
            skill_path=v2_dir,
        )

        loader = SkillLoaderNode(skill_registry=registry)
        state = _load_state(
            "my-code-skill", skill_dir, _session_id_for_bucket(1)
        )

        result = loader.execute(state)
        assert result["loaded_skills"][0]["name"] == "my-code-skill__v2"
        assert "v2 content" in result["loaded_skills"][0]["content"]

    def test_version_choice_deterministic(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        v2_dir = skill_dir.parent / "my-code-skill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text(
            "---\nname: my-code-skill__v2\n---\n\n# v2"
        )
        registry.register_skill(
            name="my-code-skill__v2",
            description="v2",
            tier="temp",
            task_types=["code_generation"],
            skill_path=v2_dir,
        )

        loader = SkillLoaderNode(skill_registry=registry)
        session_id = "session_42"

        names = []
        for _ in range(3):
            state = _load_state("my-code-skill", skill_dir, session_id)
            result = loader.execute(state)
            names.append(result["loaded_skills"][0]["name"])

        assert len(set(names)) == 1

    def test_discovered_skills_updated_with_versioned_name(self, registry_with_skill):
        from agents.skill_loader import SkillLoaderNode
        registry, skill_dir = registry_with_skill

        v2_dir = skill_dir.parent / "my-code-skill__v2"
        v2_dir.mkdir()
        (v2_dir / "SKILL.md").write_text(
            "---\nname: my-code-skill__v2\n---\n\n# v2"
        )
        registry.register_skill(
            name="my-code-skill__v2",
            description="v2",
            tier="temp",
            task_types=["code_generation"],
            skill_path=v2_dir,
        )

        loader = SkillLoaderNode(skill_registry=registry)
        state = _load_state(
            "my-code-skill", skill_dir, _session_id_for_bucket(1)
        )
        result = loader.execute(state)

        # Invariant: discovered_skills entry mutated in place to the
        # versioned name so cleanup's task_type map lookup succeeds.
        assert result["discovered_skills"][0]["skill_name"] == "my-code-skill__v2"
