"""
Tests for the skill reinforcement pipeline.

Covers:
- SkillOutcomeStore: recording, retrieval, FIFO eviction, score bands
- SkillGeneratorNode: LLM-driven generation, template fallback, RAG
- SkillGeneratorNode: RAG-augmented generation, self-refinement
- SkillCleanupNode: outcome recording + refinement trigger integration
- Bug fixes: score=0 poisoning, feedback misattribution, empty content
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Disable remote lookups
os.environ["GENESIA_DISABLE_REMOTE_SKILLS"] = "1"

from agents.skill_outcome_store import SkillOutcomeStore
from agents.skill_generator import SkillGeneratorNode, REFINEMENT_THRESHOLD
from agents.skill_cleanup import SkillCleanupNode
from agents.skill_registry import SkillRegistry
from agents.skill_security import SkillSecurity


# ====================================================================
# Fixtures
# ====================================================================


@pytest.fixture
def tmp_store(tmp_path):
    """Create an outcome store backed by a temp JSONL file."""
    store_path = tmp_path / "outcome_store.jsonl"
    return SkillOutcomeStore(store_path=str(store_path), max_entries=50)


@pytest.fixture
def skills_dir(tmp_path):
    """Create a temporary skills directory."""
    base = tmp_path / "genesia_skills"
    (base / "official").mkdir(parents=True)
    (base / "local").mkdir(parents=True)
    (base / "temp").mkdir(parents=True)
    return base


@pytest.fixture
def registry(skills_dir):
    """Create a SkillRegistry with remote lookups disabled."""
    sec = SkillSecurity(require_promotion_approval=False)
    reg = SkillRegistry(str(skills_dir), security=sec)
    reg._enable_remote = False
    return reg


# ====================================================================
# SkillOutcomeStore tests
# ====================================================================


class TestOutcomeStoreRecording:
    """Test the write path of the outcome store."""

    def test_record_positive_example(self, tmp_store):
        """High-scoring outcomes are recorded as positive examples."""
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="Write unit tests for auth module",
            skill_content="# Test Generation\n## How It Works\n1. Analyze...",
            score=92,
            feedback="Excellent coverage",
        )

        entries = tmp_store._read_all()
        assert len(entries) == 1
        assert entries[0]["is_positive"] is True
        assert entries[0]["score"] == 92
        assert entries[0]["task_type"] == "test_generation"

    def test_record_negative_example(self, tmp_store):
        """Low-scoring outcomes are recorded as negative examples."""
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="Write unit tests",
            skill_content="# Test Generation",
            score=30,
            feedback="Missing edge cases",
        )

        entries = tmp_store._read_all()
        assert len(entries) == 1
        assert entries[0]["is_positive"] is False

    def test_skip_ambiguous_scores(self, tmp_store):
        """Mid-range scores (51-69) are skipped as ambiguous."""
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="Write tests",
            skill_content="# Test Gen",
            score=60,
        )

        entries = tmp_store._read_all()
        assert len(entries) == 0

    def test_fifo_eviction(self, tmp_path):
        """Entries beyond max_entries are evicted FIFO."""
        store = SkillOutcomeStore(
            store_path=str(tmp_path / "store.jsonl"),
            max_entries=3,
        )

        for i in range(5):
            store.record(
                skill_name=f"skill-{i}",
                task_type="test_generation",
                specification=f"spec-{i}",
                skill_content=f"content-{i}",
                score=90,
            )

        entries = store._read_all()
        assert len(entries) == 3
        # Oldest entries evicted
        assert entries[0]["skill_name"] == "skill-2"
        assert entries[2]["skill_name"] == "skill-4"

    def test_specification_truncated(self, tmp_store):
        """Long specifications are truncated to 300 chars."""
        long_spec = "x" * 1000
        tmp_store.record(
            skill_name="test",
            task_type="general",
            specification=long_spec,
            skill_content="content",
            score=85,
        )

        entries = tmp_store._read_all()
        assert len(entries[0]["specification_summary"]) == 300


class TestOutcomeStoreRetrieval:
    """Test the read path (RAG queries) of the outcome store."""

    def _seed_store(self, store):
        """Seed the store with mixed outcomes."""
        outcomes = [
            ("skill-a", "test_generation", 95, True),
            ("skill-b", "test_generation", 88, True),
            ("skill-c", "test_generation", 75, True),
            ("skill-d", "test_generation", 40, False),
            ("skill-e", "security_audit", 90, True),
            ("skill-f", "security_audit", 25, False),
        ]
        for name, task_type, score, _ in outcomes:
            store.record(
                skill_name=name,
                task_type=task_type,
                specification=f"spec for {name}",
                skill_content=f"content for {name}",
                score=score,
                feedback=f"feedback for {name}",
            )

    def test_retrieve_positive_examples(self, tmp_store):
        """Returns top-K positive examples sorted by score descending."""
        self._seed_store(tmp_store)

        results = tmp_store.retrieve_positive_examples("test_generation", top_k=2)

        assert len(results) == 2
        assert results[0]["score"] == 95
        assert results[1]["score"] == 88

    def test_retrieve_negative_examples(self, tmp_store):
        """Returns bottom-K negative examples sorted by score ascending."""
        self._seed_store(tmp_store)

        results = tmp_store.retrieve_negative_examples("test_generation", top_k=1)

        assert len(results) == 1
        assert results[0]["score"] == 40
        assert results[0]["is_positive"] is False

    def test_filter_by_task_type(self, tmp_store):
        """Only returns examples matching the requested task type."""
        self._seed_store(tmp_store)

        test_results = tmp_store.retrieve_positive_examples("test_generation")
        audit_results = tmp_store.retrieve_positive_examples("security_audit")

        assert all(r["task_type"] == "test_generation" for r in test_results)
        assert all(r["task_type"] == "security_audit" for r in audit_results)

    def test_empty_store_returns_empty(self, tmp_store):
        """Empty store returns empty lists (cold start)."""
        assert tmp_store.retrieve_positive_examples("test_generation") == []
        assert tmp_store.retrieve_negative_examples("test_generation") == []

    def test_get_stats(self, tmp_store):
        """Stats summarize outcomes by task type."""
        self._seed_store(tmp_store)

        stats = tmp_store.get_stats()
        assert stats["total"] == 6
        assert "test_generation" in stats["by_task_type"]
        assert stats["by_task_type"]["test_generation"]["count"] == 4
        assert stats["by_task_type"]["test_generation"]["positive"] == 3


# ====================================================================
# SkillGeneratorNode - RAG integration tests
# ====================================================================


class TestSkillGeneratorRAG:
    """Test that skill generation incorporates outcome store data."""

    def test_cold_start_no_learned_patterns(self, registry, tmp_store):
        """With empty outcome store, no 'Learned Patterns' section appears."""
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)
        content = gen._create_skill_content("test_generation", "Write tests")

        assert "Learned Patterns" not in content

    def test_positive_examples_injected(self, registry, tmp_store):
        """Positive examples appear in generated skill content."""
        # Seed the store
        tmp_store.record(
            skill_name="past-skill",
            task_type="test_generation",
            specification="Write tests for auth",
            skill_content="# Test Gen\n## How It Works\n1. Analyze code\n2. Write tests",
            score=95,
            feedback="Excellent edge case coverage",
        )

        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)
        content = gen._create_skill_content("test_generation", "Write tests")

        assert "Learned Patterns" in content
        assert "Positive Example 1" in content
        assert "score: 95/100" in content

    def test_negative_examples_injected(self, registry, tmp_store):
        """Negative examples appear as anti-patterns."""
        tmp_store.record(
            skill_name="bad-skill",
            task_type="test_generation",
            specification="Write tests",
            skill_content="# Bad Skill",
            score=30,
            feedback="Missing assertions, no edge cases",
        )

        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)
        content = gen._create_skill_content("test_generation", "Write tests")

        assert "Anti-Patterns" in content
        assert "Missing assertions" in content

    def test_no_outcome_store_graceful_fallback(self, registry):
        """Without outcome store, generation falls back to pure template."""
        gen = SkillGeneratorNode(registry, outcome_store=None)
        content = gen._create_skill_content("test_generation", "Write tests")

        # Should still produce valid skill content
        assert "# Test Generation" in content
        assert "## How It Works" in content
        assert "Learned Patterns" not in content

    def test_workflow_excerpt_extraction(self, registry, tmp_store):
        """Extracts workflow section from past skill content."""
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)

        skill_content = (
            "# My Skill\n"
            "## How It Works\n"
            "1. Step one\n"
            "2. Step two\n"
            "## Context\n"
            "Some context"
        )
        excerpt = gen._extract_workflow_excerpt(skill_content)

        assert "1. Step one" in excerpt
        assert "2. Step two" in excerpt
        assert "Some context" not in excerpt


# ====================================================================
# Self-Refinement tests
# ====================================================================


class TestSelfRefinement:
    """Test skill self-refinement triggered by low scores."""

    def test_refine_low_scoring_skill(self, registry, tmp_store):
        """Skills below threshold get refined with critic feedback."""
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)

        # First, generate a skill so it exists in the registry
        gen._generate_skill("test_generation", "Write tests")

        # Now refine it
        refined = gen.refine_skill(
            skill_name="ephemeral-test-generation",
            task_type="test_generation",
            original_content="# Old content",
            score=40,
            feedback="Missing edge cases and error handling",
            specification="Write tests",
        )

        assert refined is not None
        assert "Refinement Directives" in refined
        assert "Missing edge cases and error handling" in refined
        assert "40/100" in refined

    def test_skip_refinement_above_threshold(self, registry, tmp_store):
        """Skills above threshold are not refined."""
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)

        result = gen.refine_skill(
            skill_name="test-skill",
            task_type="test_generation",
            original_content="# Good skill",
            score=85,
            feedback="Minor improvements possible",
            specification="Write tests",
        )

        assert result is None

    def test_refinement_threshold_boundary(self, registry, tmp_store):
        """Score exactly at threshold is NOT refined."""
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)

        result = gen.refine_skill(
            skill_name="test-skill",
            task_type="test_generation",
            original_content="# OK skill",
            score=REFINEMENT_THRESHOLD,
            feedback="On the boundary",
            specification="Write tests",
        )

        assert result is None


# ====================================================================
# Integration: SkillCleanupNode with outcome recording
# ====================================================================


class TestCleanupOutcomeRecording:
    """Test that skill_cleanup records outcomes and triggers refinement."""

    def _make_state(
        self,
        score=90,
        skill_name="ephemeral-test-generation",
        feedback="Good coverage but missing edge cases",
        skill_content="# Test Generation\n## How It Works\n1. Analyze...",
    ):
        """Build a minimal state that looks like a completed workflow."""
        return {
            "skills_in_use": [skill_name],
            "discovered_skills": [
                {
                    "skill_name": skill_name,
                    "task_type": "test_generation",
                    "tier": "temp",
                    "skill_path": "/tmp/fake",
                }
            ],
            "loaded_skills": [
                {
                    "name": skill_name,
                    "tier": "temp",
                    "task_type": "test_generation",
                    "content": skill_content,
                    "path": "/tmp/fake",
                }
            ],
            "specification": "Write unit tests for auth module",
            "output_critic_score": score,
            "output_critic_feedback": feedback,
        }

    def test_outcome_recorded_after_cleanup(self, registry, tmp_store):
        """Cleanup records outcome in the store."""
        state = self._make_state(score=90)

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        entries = tmp_store._read_all()
        assert len(entries) == 1
        assert entries[0]["score"] == 90
        assert entries[0]["task_type"] == "test_generation"

    def test_no_outcome_store_no_crash(self, registry):
        """Without outcome store, cleanup works normally."""
        state = self._make_state(score=90)

        cleanup = SkillCleanupNode(registry, outcome_store=None)
        # Should not raise
        cleanup.execute(state)

    def test_low_score_triggers_refinement(self, registry, tmp_store):
        """Skills below REFINEMENT_THRESHOLD trigger self-refinement."""
        # Generate a skill first so it exists
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)
        skill_name, _ = gen._generate_skill("test_generation", "Write tests")

        state = self._make_state(score=40, skill_name=skill_name)

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        # Check outcome was recorded
        entries = tmp_store._read_all()
        assert len(entries) == 1

        # Check skill was refined (SKILL.md should contain refinement directives)
        skill_path = registry.temp_dir / skill_name / "SKILL.md"
        content = skill_path.read_text()
        assert "Refinement Directives" in content

    def test_high_score_no_refinement(self, registry, tmp_store):
        """Skills above threshold are not refined."""
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)
        skill_name, _ = gen._generate_skill("test_generation", "Write tests")

        state = self._make_state(score=90, skill_name=skill_name)

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        # Skill should NOT have refinement directives
        skill_path = registry.temp_dir / skill_name / "SKILL.md"
        content = skill_path.read_text()
        assert "Refinement Directives" not in content

    def test_full_loop_outcome_feeds_generation(self, registry, tmp_store):
        """
        End-to-end: outcome from session 1 appears as a learned pattern
        in session 2's skill generation.
        """
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)

        # Session 1: generate, "use", record high-scoring outcome
        tmp_store.record(
            skill_name="ephemeral-test-generation",
            task_type="test_generation",
            specification="Write auth tests",
            skill_content="# Test Gen\n## How It Works\n1. Analyze auth flows",
            score=95,
            feedback="Excellent auth coverage",
        )

        # Session 2: generate new skill — should include learned patterns
        content = gen._create_skill_content("test_generation", "Write new tests")

        assert "Learned Patterns" in content
        assert "Positive Example 1" in content
        assert "score: 95/100" in content


# ====================================================================
# LLM-driven skill generation tests
# ====================================================================


class FakeLLMBackend:
    """Fake LLM backend that returns a canned response."""

    def __init__(self, response: str = "", should_raise: bool = False):
        self.response = response
        self.should_raise = should_raise
        self.calls = []

    def generate(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.should_raise:
            raise RuntimeError("LLM backend unavailable")
        return self.response


class TestLLMDrivenGeneration:
    """Test LLM-driven workflow generation with template fallback."""

    VALID_LLM_RESPONSE = (
        "## How It Works\n\n"
        "1. **Read the auth module**: Identify all authentication entry points\n"
        "2. **Map code paths**: Trace login, logout, and token refresh flows\n"
        "3. **Write unit tests**: Cover happy paths, invalid credentials, expired tokens\n"
        "4. **Write integration tests**: Test end-to-end auth flows with mocked DB\n"
        "5. **Run and validate**: Execute tests, verify coverage exceeds 80%\n\n"
        "## Best Practices\n\n"
        "1. **Test both success and failure paths**: Every auth endpoint should have\n"
        "   tests for valid and invalid credentials\n"
        "2. **Mock external dependencies**: Use fixtures for database and token services\n"
        "3. **Check security boundaries**: Verify that unauthenticated requests are rejected\n"
    )

    def test_llm_generates_tailored_workflow(self, registry, tmp_store):
        """When LLM is available, it generates the workflow and practices."""
        fake_llm = FakeLLMBackend(response=self.VALID_LLM_RESPONSE)
        gen = SkillGeneratorNode(
            registry, outcome_store=tmp_store, base_model=fake_llm
        )

        content = gen._create_skill_content(
            "test_generation", "Write tests for the auth module"
        )

        # LLM-generated workflow should appear
        assert "Read the auth module" in content
        assert "Map code paths" in content
        # Deterministic sections should also appear
        assert "## When to Use" in content
        assert "## Context" in content
        assert "## Notes" in content
        # The static template workflow should NOT appear
        assert "Analyze Code" not in content

    def test_llm_receives_rag_examples(self, registry, tmp_store):
        """RAG examples are passed to the LLM prompt, not injected into output."""
        # Seed the store
        tmp_store.record(
            skill_name="past-skill",
            task_type="test_generation",
            specification="Write tests for auth",
            skill_content="# Past\n## How It Works\n1. Old workflow step",
            score=95,
            feedback="Excellent coverage",
        )

        fake_llm = FakeLLMBackend(response=self.VALID_LLM_RESPONSE)
        gen = SkillGeneratorNode(
            registry, outcome_store=tmp_store, base_model=fake_llm
        )

        gen._create_skill_content("test_generation", "Write new tests")

        # LLM should have been called with examples in the prompt
        assert len(fake_llm.calls) == 1
        user_prompt = fake_llm.calls[0]["messages"][1]["content"]
        assert "HIGH-SCORING EXAMPLES" in user_prompt
        assert "score: 95/100" in user_prompt

        # The "Learned Patterns" section should NOT appear in the output
        # (RAG feeds the LLM, not the output)

    def test_llm_failure_falls_back_to_template(self, registry, tmp_store):
        """LLM exception falls back to template generation."""
        fake_llm = FakeLLMBackend(should_raise=True)
        gen = SkillGeneratorNode(
            registry, outcome_store=tmp_store, base_model=fake_llm
        )

        content = gen._create_skill_content("test_generation", "Write tests")

        # Should produce valid template content
        assert "## How It Works" in content
        assert "Analyze Code" in content  # Template workflow
        assert "## Best Practices" in content

    def test_invalid_llm_output_falls_back(self, registry, tmp_store):
        """LLM output missing required sections falls back to template."""
        # Missing "## Best Practices"
        bad_response = "## How It Works\n1. Do something\n"
        fake_llm = FakeLLMBackend(response=bad_response)
        gen = SkillGeneratorNode(
            registry, outcome_store=tmp_store, base_model=fake_llm
        )

        content = gen._create_skill_content("test_generation", "Write tests")

        # Should fall back to template
        assert "Analyze Code" in content  # Template workflow

    def test_no_base_model_uses_template(self, registry, tmp_store):
        """Without base_model, always uses template (same as before)."""
        gen = SkillGeneratorNode(
            registry, outcome_store=tmp_store, base_model=None
        )

        content = gen._create_skill_content("test_generation", "Write tests")

        assert "## How It Works" in content
        assert "Analyze Code" in content  # Template workflow

    def test_llm_system_prompt(self, registry, tmp_store):
        """System prompt instructs LLM to produce correct format."""
        fake_llm = FakeLLMBackend(response=self.VALID_LLM_RESPONSE)
        gen = SkillGeneratorNode(
            registry, outcome_store=tmp_store, base_model=fake_llm
        )

        gen._create_skill_content("test_generation", "Write tests")

        system_msg = fake_llm.calls[0]["messages"][0]
        assert system_msg["role"] == "system"
        assert "skill designer" in system_msg["content"]
        assert "How It Works" in system_msg["content"]
        assert "Best Practices" in system_msg["content"]

    def test_validation_requires_numbered_steps(self, registry):
        """Validation rejects output without numbered steps."""
        gen = SkillGeneratorNode(registry)

        # Has headings but no numbered steps
        assert not gen._validate_llm_output(
            "## How It Works\nSome text\n## Best Practices\nMore text"
        )
        # Has everything
        assert gen._validate_llm_output(
            "## How It Works\n1. Step one\n## Best Practices\n1. Practice one"
        )
        # Empty
        assert not gen._validate_llm_output("")
        # Too short
        assert not gen._validate_llm_output("## How It Works\n1.\n## Best Practices")

    def test_validation_rejects_version_numbers_as_steps(self, registry):
        """Version numbers like '1.0' must NOT pass as numbered steps."""
        gen = SkillGeneratorNode(registry)

        # Contains "1." only inside version numbers — no real steps
        assert not gen._validate_llm_output(
            "## How It Works\n"
            "Follow section 1.1 of the methodology guide.\n"
            "## Best Practices\n"
            "Based on v1.0 best practices, ensure quality throughout."
        )
        # Score "21.5" contains "1." as substring — must also be rejected
        assert not gen._validate_llm_output(
            "## How It Works\n"
            "The baseline score was 21.5 which is too low.\n"
            "## Best Practices\n"
            "Aim for scores above 81.0 in all cases."
        )
        # Real numbered step at start of line — should pass
        assert gen._validate_llm_output(
            "## How It Works\n"
            "1. Analyze the code for version 1.0 compatibility\n"
            "2. Write tests\n"
            "## Best Practices\n"
            "1. Always test edge cases"
        )

    def test_template_path_with_rag_still_works(self, registry, tmp_store):
        """Template fallback path still includes RAG as 'Learned Patterns'."""
        # Seed the store
        tmp_store.record(
            skill_name="past-skill",
            task_type="test_generation",
            specification="Write tests",
            skill_content="# Past\n## How It Works\n1. Old step",
            score=90,
            feedback="Great coverage",
        )

        # No base_model -> template path
        gen = SkillGeneratorNode(
            registry, outcome_store=tmp_store, base_model=None
        )

        content = gen._create_skill_content("test_generation", "Write tests")

        # Template workflow
        assert "Analyze Code" in content
        # RAG section appended
        assert "Learned Patterns" in content
        assert "score: 90/100" in content


# ====================================================================
# Bug fix regression tests
# ====================================================================


class TestBugFixScoreZeroPoisoning:
    """Bug #1: score=0 means 'unevaluated', not 'terrible'. Must not record."""

    def _make_state(self, score, skill_name="ephemeral-test-generation"):
        return {
            "skills_in_use": [skill_name],
            "discovered_skills": [
                {
                    "skill_name": skill_name,
                    "task_type": "test_generation",
                    "tier": "temp",
                    "skill_path": "/tmp/fake",
                }
            ],
            "loaded_skills": [
                {
                    "name": skill_name,
                    "tier": "temp",
                    "task_type": "test_generation",
                    "content": "# Skill\n## How It Works\n1. Do stuff",
                    "path": "/tmp/fake",
                }
            ],
            "specification": "Write tests",
            "output_critic_score": score,
            "output_critic_feedback": "Some feedback",
        }

    def test_score_zero_not_recorded(self, registry, tmp_store):
        """Score=0 (unevaluated) must NOT be recorded in outcome store."""
        state = self._make_state(score=0)

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        entries = tmp_store._read_all()
        assert len(entries) == 0

    def test_score_zero_no_refinement(self, registry, tmp_store):
        """Score=0 must NOT trigger refinement."""
        gen = SkillGeneratorNode(registry, outcome_store=tmp_store)
        skill_name, _ = gen._generate_skill("test_generation", "Write tests")

        state = self._make_state(score=0, skill_name=skill_name)

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        # Should NOT be refined
        skill_path = registry.temp_dir / skill_name / "SKILL.md"
        content = skill_path.read_text()
        assert "Refinement Directives" not in content

    def test_score_one_is_recorded(self, registry, tmp_store):
        """Score=1 (genuinely scored, albeit poorly) IS recorded."""
        state = self._make_state(score=1)

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        entries = tmp_store._read_all()
        assert len(entries) == 1
        assert entries[0]["score"] == 1

    def test_no_critic_score_at_all(self, registry, tmp_store):
        """No critic score fields in state -> fallback=0 -> not recorded."""
        state = {
            "skills_in_use": ["ephemeral-test-generation"],
            "discovered_skills": [
                {
                    "skill_name": "ephemeral-test-generation",
                    "task_type": "test_generation",
                    "tier": "temp",
                    "skill_path": "/tmp/fake",
                }
            ],
            "loaded_skills": [
                {
                    "name": "ephemeral-test-generation",
                    "tier": "temp",
                    "task_type": "test_generation",
                    "content": "# Skill content",
                    "path": "/tmp/fake",
                }
            ],
            "specification": "Write tests",
            # No output_critic_score, no critic_score
        }

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        entries = tmp_store._read_all()
        assert len(entries) == 0


class TestBugFixFeedbackAttribution:
    """Bug #2: multi-specialist must use per-sub-task feedback, not top-level."""

    def test_per_subtask_feedback_used(self, registry, tmp_store):
        """Each skill gets its own sub-task's feedback, not the aggregated one."""
        state = {
            "skills_in_use": ["skill-a", "skill-b"],
            "discovered_skills": [
                {"skill_name": "skill-a", "task_type": "test_generation",
                 "tier": "temp", "skill_path": "/tmp/fake-a"},
                {"skill_name": "skill-b", "task_type": "security_audit",
                 "tier": "temp", "skill_path": "/tmp/fake-b"},
            ],
            "loaded_skills": [
                {"name": "skill-a", "tier": "temp", "task_type": "test_generation",
                 "content": "# Skill A content", "path": "/tmp/fake-a"},
                {"name": "skill-b", "tier": "temp", "task_type": "security_audit",
                 "content": "# Skill B content", "path": "/tmp/fake-b"},
            ],
            "sub_tasks": [
                {"task_type": "test_generation", "output_score": 92,
                 "output_feedback": "Great test coverage"},
                {"task_type": "security_audit", "output_score": 78,
                 "output_feedback": "Missed XSS vectors"},
            ],
            "specification": "Full audit",
            # Top-level feedback is from the aggregated-output critic —
            # it should NOT be used for individual skills
            "output_critic_score": 85,
            "output_critic_feedback": "Aggregated output looks good overall",
        }

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        entries = tmp_store._read_all()
        assert len(entries) == 2

        entry_a = next(e for e in entries if e["skill_name"] == "skill-a")
        entry_b = next(e for e in entries if e["skill_name"] == "skill-b")

        # Each skill gets its OWN sub-task feedback
        assert entry_a["feedback"] == "Great test coverage"
        assert entry_b["feedback"] == "Missed XSS vectors"

        # NOT the aggregated feedback
        assert "Aggregated" not in entry_a["feedback"]
        assert "Aggregated" not in entry_b["feedback"]

    def test_single_specialist_uses_toplevel_feedback(self, registry, tmp_store):
        """In single-specialist mode (no sub-tasks), top-level feedback is used."""
        state = {
            "skills_in_use": ["skill-a"],
            "discovered_skills": [
                {"skill_name": "skill-a", "task_type": "test_generation",
                 "tier": "temp", "skill_path": "/tmp/fake-a"},
            ],
            "loaded_skills": [
                {"name": "skill-a", "tier": "temp", "task_type": "test_generation",
                 "content": "# Skill A content", "path": "/tmp/fake-a"},
            ],
            # No sub_tasks — single specialist
            "specification": "Write tests",
            "output_critic_score": 88,
            "output_critic_feedback": "Good single-specialist feedback",
        }

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        entries = tmp_store._read_all()
        assert len(entries) == 1
        assert entries[0]["feedback"] == "Good single-specialist feedback"


class TestBugFixEmptyContent:
    """Bug #3: skills with empty content (failed load) must not be recorded."""

    def test_empty_content_not_recorded(self, registry, tmp_store):
        """Skill that failed to load (not in loaded_skills) is not recorded."""
        state = {
            "skills_in_use": ["skill-missing"],
            "discovered_skills": [
                {"skill_name": "skill-missing", "task_type": "test_generation",
                 "tier": "temp", "skill_path": "/tmp/fake"},
            ],
            # NOT in loaded_skills — simulates a failed load
            "loaded_skills": [],
            "specification": "Write tests",
            "output_critic_score": 90,
            "output_critic_feedback": "Good feedback",
        }

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        entries = tmp_store._read_all()
        assert len(entries) == 0

    def test_present_content_is_recorded(self, registry, tmp_store):
        """Skill that loaded successfully IS recorded."""
        state = {
            "skills_in_use": ["skill-ok"],
            "discovered_skills": [
                {"skill_name": "skill-ok", "task_type": "test_generation",
                 "tier": "temp", "skill_path": "/tmp/fake"},
            ],
            "loaded_skills": [
                {"name": "skill-ok", "tier": "temp", "task_type": "test_generation",
                 "content": "# Real content here", "path": "/tmp/fake"},
            ],
            "specification": "Write tests",
            "output_critic_score": 90,
            "output_critic_feedback": "Good feedback",
        }

        cleanup = SkillCleanupNode(registry, outcome_store=tmp_store)
        cleanup.execute(state)

        entries = tmp_store._read_all()
        assert len(entries) == 1
        assert entries[0]["skill_name"] == "skill-ok"


# ====================================================================
# Dedup-on-write tests
# ====================================================================


class TestDedupOnWrite:
    """Dedup: at most one positive + one negative per skill_name."""

    def test_positive_band_keeps_higher_score(self, tmp_store):
        """In the positive band, the higher-scoring entry wins."""
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="spec v1",
            skill_content="content v1",
            score=85,
            feedback="Good first run",
        )
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="spec v2",
            skill_content="content v2",
            score=92,
            feedback="Even better second run",
        )

        entries = tmp_store._read_all()
        assert len(entries) == 1
        # Higher score (92) replaced lower (85)
        assert entries[0]["score"] == 92
        assert entries[0]["feedback"] == "Even better second run"
        assert entries[0]["specification_summary"] == "spec v2"

    def test_positive_band_keeps_best_not_latest(self, tmp_store):
        """A worse positive score does NOT replace a better one."""
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="excellent run",
            skill_content="content-excellent",
            score=95,
            feedback="Excellent",
        )
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="mediocre run",
            skill_content="content-mediocre",
            score=72,
            feedback="Mediocre",
        )

        entries = tmp_store._read_all()
        assert len(entries) == 1
        # Best score (95) retained, not replaced by worse (72)
        assert entries[0]["score"] == 95
        assert entries[0]["feedback"] == "Excellent"

    def test_negative_band_keeps_lowest_score(self, tmp_store):
        """In the negative band, the lowest score wins (worst anti-pattern)."""
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="bad run",
            skill_content="content-bad",
            score=40,
            feedback="Bad",
        )
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="worse run",
            skill_content="content-worse",
            score=15,
            feedback="Terrible",
        )

        entries = tmp_store._read_all()
        assert len(entries) == 1
        # Lowest score (15) replaced higher negative (40)
        assert entries[0]["score"] == 15
        assert entries[0]["feedback"] == "Terrible"

    def test_negative_band_keeps_worst_not_latest(self, tmp_store):
        """A less-bad negative score does NOT replace a worse one."""
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="terrible run",
            skill_content="content-terrible",
            score=10,
            feedback="Terrible",
        )
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="merely bad run",
            skill_content="content-bad",
            score=45,
            feedback="Just bad",
        )

        entries = tmp_store._read_all()
        assert len(entries) == 1
        # Worst score (10) retained, not replaced by less-bad (45)
        assert entries[0]["score"] == 10
        assert entries[0]["feedback"] == "Terrible"

    def test_same_skill_different_band_keeps_both(self, tmp_store):
        """Same skill can have one positive AND one negative entry."""
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="good run",
            skill_content="content",
            score=90,
            feedback="Great",
        )
        tmp_store.record(
            skill_name="ephemeral-test-gen",
            task_type="test_generation",
            specification="bad run",
            skill_content="content",
            score=30,
            feedback="Terrible",
        )

        entries = tmp_store._read_all()
        assert len(entries) == 2
        bands = {e["is_positive"] for e in entries}
        assert bands == {True, False}

    def test_different_skills_same_band_kept(self, tmp_store):
        """Different skill names in the same band are NOT deduped."""
        for name in ["skill-a", "skill-b", "skill-c"]:
            tmp_store.record(
                skill_name=name,
                task_type="test_generation",
                specification=f"spec for {name}",
                skill_content=f"content for {name}",
                score=88,
                feedback=f"feedback for {name}",
            )

        entries = tmp_store._read_all()
        assert len(entries) == 3
        names = [e["skill_name"] for e in entries]
        assert names == ["skill-a", "skill-b", "skill-c"]

    def test_dedup_preserves_entry_position(self, tmp_store):
        """Replacement happens in-place, preserving ordering."""
        # Create three different skills
        for name, score in [("skill-a", 80), ("skill-b", 85), ("skill-c", 90)]:
            tmp_store.record(
                skill_name=name,
                task_type="test_generation",
                specification=f"spec for {name}",
                skill_content=f"content for {name}",
                score=score,
            )

        # Replace skill-b (middle entry)
        tmp_store.record(
            skill_name="skill-b",
            task_type="test_generation",
            specification="updated spec",
            skill_content="updated content",
            score=95,
        )

        entries = tmp_store._read_all()
        assert len(entries) == 3
        # Order preserved: a, b (replaced), c
        assert entries[0]["skill_name"] == "skill-a"
        assert entries[1]["skill_name"] == "skill-b"
        assert entries[1]["score"] == 95
        assert entries[2]["skill_name"] == "skill-c"

    def test_dedup_improves_rag_diversity(self, tmp_store):
        """After 50 runs of same skill, top-K returns diverse results."""
        # Record 50 outcomes for the same skill
        for i in range(50):
            tmp_store.record(
                skill_name="ephemeral-test-gen",
                task_type="test_generation",
                specification=f"spec run {i}",
                skill_content=f"content run {i}",
                score=80 + (i % 10),
                feedback=f"feedback run {i}",
            )

        # Also record a different skill
        tmp_store.record(
            skill_name="ephemeral-security-audit",
            task_type="test_generation",
            specification="security spec",
            skill_content="security content",
            score=95,
            feedback="top security skill",
        )

        entries = tmp_store._read_all()
        # Should be exactly 2 positive entries (one per skill), not 50+1
        positive = [e for e in entries if e["is_positive"]]
        assert len(positive) == 2

        # top-K=3 retrieval returns both skills (diverse)
        results = tmp_store.retrieve_positive_examples("test_generation", top_k=3)
        names = {r["skill_name"] for r in results}
        assert len(names) == 2
        assert "ephemeral-test-gen" in names
        assert "ephemeral-security-audit" in names

    def test_fifo_still_works_with_dedup(self, tmp_path):
        """FIFO eviction applies after dedup replacement."""
        store = SkillOutcomeStore(
            store_path=str(tmp_path / "store.jsonl"),
            max_entries=3,
        )

        # Fill to capacity with 3 different skills
        for name in ["skill-a", "skill-b", "skill-c"]:
            store.record(
                skill_name=name,
                task_type="test_generation",
                specification=f"spec for {name}",
                skill_content=f"content for {name}",
                score=85,
            )

        assert len(store._read_all()) == 3

        # Add a 4th skill — should evict skill-a via FIFO
        store.record(
            skill_name="skill-d",
            task_type="test_generation",
            specification="spec for skill-d",
            skill_content="content for skill-d",
            score=85,
        )

        entries = store._read_all()
        assert len(entries) == 3
        names = [e["skill_name"] for e in entries]
        assert "skill-a" not in names
        assert "skill-d" in names
