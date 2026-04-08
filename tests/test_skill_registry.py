"""
Tests for SkillRegistry — three-tier skill management with GitHub remote discovery.

Covers:
- Local skill search (official, local, temp tiers)
- Remote GitHub catalog fetch and skill download
- Match confidence calculation
- Skill registration and loading
- Index persistence
- Auto-promotion from temp -> local
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

# Set env to disable remote lookups by default in tests
os.environ["VIBE_DISABLE_REMOTE_SKILLS"] = "1"

from agents.skill_registry import SkillRegistry
from agents.skill_security import SkillSecurity
from agents.config import SkillSourceConfig


@pytest.fixture
def skills_dir(tmp_path):
    """Create a temporary skills directory with official/local/temp subdirs."""
    base = tmp_path / "vibe_skills"
    (base / "official").mkdir(parents=True)
    (base / "local").mkdir(parents=True)
    (base / "temp").mkdir(parents=True)
    return base


@pytest.fixture
def registry(skills_dir):
    """Create a SkillRegistry with remote lookups disabled.

    Uses require_promotion_approval=False to preserve original
    auto-promotion behavior for these core registry tests.
    """
    sec = SkillSecurity(require_promotion_approval=False)
    reg = SkillRegistry(str(skills_dir), security=sec)
    reg._enable_remote = False
    return reg


def _create_skill(skills_dir: Path, tier: str, name: str, description: str, task_types=None):
    """Helper to create a skill directory with SKILL.md and register it."""
    skill_dir = skills_dir / tier / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    task_types = task_types or ["general"]
    skill_md = f"""---
name: {name}
description: {description}
license: Apache-2.0
metadata:
  author: test
  version: "1.0"
allowed-tools: Read Write
---

# {name.replace('-', ' ').title()}

{description}

## When to Use
- Testing scenarios
"""
    (skill_dir / "SKILL.md").write_text(skill_md)
    return skill_dir, task_types


class TestSkillRegistryInit:
    """Test registry initialization."""

    def test_creates_directories(self, tmp_path):
        base = tmp_path / "skills"
        reg = SkillRegistry(str(base))
        assert (base / "official").is_dir()
        assert (base / "local").is_dir()
        assert (base / "temp").is_dir()

    def test_creates_empty_index(self, tmp_path):
        base = tmp_path / "skills"
        reg = SkillRegistry(str(base))
        assert reg.index["version"] == "1.0"
        assert len(reg._all_skills()) == 0

    def test_loads_existing_index(self, skills_dir):
        index = {
            "version": "1.0",
            "last_updated": "2026-01-01T00:00:00Z",
            "tiers": {
                "official": {"skills": {"test-skill": {"description": "A test", "task_types": ["general"], "path": str(skills_dir / "official" / "test-skill")}}},
                "local": {"skills": {}},
                "temp": {"skills": {}}
            }
        }
        (skills_dir / ".index.json").write_text(json.dumps(index))

        reg = SkillRegistry(str(skills_dir))
        assert len(reg._all_skills()) == 1
        assert "test-skill" in reg.index["tiers"]["official"]["skills"]


class TestSkillSearch:
    """Test skill matching and search."""

    def test_find_official_skill(self, skills_dir, registry):
        skill_dir, task_types = _create_skill(
            skills_dir, "official", "webapp-testing",
            "Toolkit for testing web applications using Playwright",
            ["test_generation", "frontend_development"]
        )
        registry.register_skill(
            "webapp-testing",
            "Toolkit for testing web applications using Playwright",
            "official",
            ["test_generation", "frontend_development"],
            skill_dir
        )

        tier, name, path = registry.find_skill("testing web applications")
        assert tier == "official"
        assert name == "webapp-testing"

    def test_find_local_skill(self, skills_dir, registry):
        skill_dir, _ = _create_skill(
            skills_dir, "local", "sql-optimizer",
            "Optimizes SQL queries for PostgreSQL databases",
            ["database_operations"]
        )
        registry.register_skill(
            "sql-optimizer",
            "Optimizes SQL queries for PostgreSQL databases",
            "local",
            ["database_operations"],
            skill_dir
        )

        tier, name, path = registry.find_skill("database sql optimize queries")
        assert tier == "local"
        assert name == "sql-optimizer"

    def test_no_match_returns_ephemeral(self, registry):
        tier, name, path = registry.find_skill("something completely unrelated xyz")
        assert tier == "ephemeral"
        assert name is None
        assert path is None

    def test_official_takes_priority_over_local(self, skills_dir, registry):
        # Create same-domain skill in both tiers
        off_dir, _ = _create_skill(
            skills_dir, "official", "pdf-tools",
            "PDF file operations including extraction merging splitting",
            ["pdf_processing"]
        )
        registry.register_skill("pdf-tools", "PDF file operations including extraction merging splitting", "official", ["pdf_processing"], off_dir)

        loc_dir, _ = _create_skill(
            skills_dir, "local", "my-pdf-util",
            "Local PDF utility for basic operations",
            ["pdf_processing"]
        )
        registry.register_skill("my-pdf-util", "Local PDF utility for basic operations", "local", ["pdf_processing"], loc_dir)

        tier, name, _ = registry.find_skill("pdf extraction merging splitting")
        assert tier == "official"
        assert name == "pdf-tools"


class TestMatchConfidence:
    """Test the confidence calculation logic."""

    def test_exact_overlap_high_confidence(self, registry):
        conf = registry._calculate_match_confidence(
            "testing web applications",
            "toolkit for testing web applications using playwright",
            ["test_generation"]
        )
        assert conf >= 0.5

    def test_no_overlap_low_confidence(self, registry):
        conf = registry._calculate_match_confidence(
            "database optimization",
            "creating animated gifs for slack",
            ["messaging"]
        )
        assert conf < 0.3

    def test_task_type_matching(self, registry):
        conf = registry._calculate_match_confidence(
            "test generation",
            "a skill for various tasks",
            ["test_generation", "code_review"]
        )
        assert conf > 0.0

    def test_substring_bonus(self, registry):
        conf = registry._calculate_match_confidence(
            "pdf",
            "handles all pdf file operations",
            ["pdf_processing"]
        )
        assert conf >= 0.3  # Substring bonus should kick in

    def test_empty_requirement_returns_zero(self, registry):
        """Bug #2: Empty strings should return 0.0, not 1.0."""
        assert registry._calculate_match_confidence("", "", []) == 0.0
        assert registry._calculate_match_confidence("", "some description", ["general"]) == 0.0
        assert registry._calculate_match_confidence("   ", "", []) == 0.0

    def test_empty_description_with_valid_requirement(self, registry):
        conf = registry._calculate_match_confidence("test", "", [])
        assert conf == 0.0


class TestSkillRegistration:
    """Test skill registration and index updates."""

    def test_register_official_skill(self, skills_dir, registry):
        skill_dir, _ = _create_skill(skills_dir, "official", "my-skill", "A test skill")
        meta = registry.register_skill("my-skill", "A test skill", "official", ["general"], skill_dir)

        assert meta.name == "my-skill"
        assert "my-skill" in registry.index["tiers"]["official"]["skills"]

    def test_register_requires_skill_md(self, skills_dir, registry):
        skill_dir = skills_dir / "official" / "empty-skill"
        skill_dir.mkdir(parents=True)
        # No SKILL.md

        with pytest.raises(ValueError, match="SKILL.md not found"):
            registry.register_skill("empty-skill", "No content", "official", ["general"], skill_dir)

    def test_load_skill_content(self, skills_dir, registry):
        skill_dir, _ = _create_skill(skills_dir, "official", "loadable", "Test skill for loading")
        registry.register_skill("loadable", "Test skill for loading", "official", ["general"], skill_dir)

        content = registry.load_skill("loadable")
        assert content is not None
        assert "Test skill for loading" in content

    def test_load_nonexistent_skill(self, registry):
        content = registry.load_skill("does-not-exist")
        assert content is None


class TestUsageTracking:
    """Test usage tracking and auto-promotion."""

    def test_track_usage_increments(self, skills_dir, registry):
        skill_dir, _ = _create_skill(skills_dir, "temp", "ephemeral-skill", "Temp skill")
        registry.register_skill("ephemeral-skill", "Temp skill", "temp", ["general"], skill_dir)

        registry.track_usage("ephemeral-skill", 80)
        data = registry.index["tiers"]["temp"]["skills"]["ephemeral-skill"]
        assert data["usage_count"] == 1
        assert data["avg_score"] == 80.0

    def test_auto_promotion_on_criteria_met(self, skills_dir, registry):
        skill_dir, _ = _create_skill(skills_dir, "temp", "promote-me", "A promotable skill")
        registry.register_skill("promote-me", "A promotable skill", "temp", ["general"], skill_dir)

        # Track 3 usages with high scores (meets MIN_USAGE=3, MIN_AVG=85)
        registry.track_usage("promote-me", 90)
        registry.track_usage("promote-me", 88)
        registry.track_usage("promote-me", 92)

        # Should have been auto-promoted to local
        assert "promote-me" not in registry.index["tiers"]["temp"]["skills"]
        assert "promote-me" in registry.index["tiers"]["local"]["skills"]

    def test_no_promotion_below_threshold(self, skills_dir, registry):
        skill_dir, _ = _create_skill(skills_dir, "temp", "low-score", "Low scoring skill")
        registry.register_skill("low-score", "Low scoring skill", "temp", ["general"], skill_dir)

        registry.track_usage("low-score", 50)
        registry.track_usage("low-score", 60)
        registry.track_usage("low-score", 55)

        # Should still be in temp (avg 55 < 85 threshold)
        assert "low-score" in registry.index["tiers"]["temp"]["skills"]


class TestRemoteDiscovery:
    """Test GitHub remote skill catalog and download."""

    def test_remote_disabled_by_env(self, registry):
        """When VIBE_DISABLE_REMOTE_SKILLS is set, remote is off."""
        assert registry._enable_remote is False

    def test_remote_catalog_returns_empty_on_failure(self, skills_dir):
        """Network failure returns empty catalog, no crash."""
        reg = SkillRegistry(str(skills_dir))
        reg._enable_remote = True
        source = SkillSourceConfig(name="anthropics", repo="anthropics/skills", trust_level="high")

        with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            catalog = reg._get_remote_catalog(source)

        assert catalog == {}

    def test_remote_catalog_caches_result(self, skills_dir):
        """Catalog is fetched once and cached."""
        reg = SkillRegistry(str(skills_dir))
        reg._enable_remote = True
        source = SkillSourceConfig(name="anthropics", repo="anthropics/skills", trust_level="high")

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([
            {"name": "pdf", "type": "dir"},
            {"name": "xlsx", "type": "dir"},
            {"name": "README.md", "type": "file"},
        ]).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_open:
            catalog1 = reg._get_remote_catalog(source)
            catalog2 = reg._get_remote_catalog(source)

        # urlopen should be called only once (cached)
        assert mock_open.call_count == 1
        assert "pdf" in catalog1
        assert "xlsx" in catalog1
        assert catalog1 is catalog2  # Same cached object

    def test_find_remote_skill_downloads_on_match(self, skills_dir):
        """When a remote skill matches, it's downloaded and cached locally."""
        from datetime import datetime
        reg = SkillRegistry(str(skills_dir))
        reg._enable_remote = True

        # Mock the remote catalog via source cache
        reg._source_caches["anthropics"] = {
            "catalog": {
                "webapp-testing": {
                    "description": "webapp testing toolkit",
                    "task_types": ["test_generation"]
                }
            },
            "fetched_at": datetime.utcnow(),
        }

        # Mock the SKILL.md download
        skill_md = b"""---
name: webapp-testing
description: Toolkit for testing web applications
---

# Webapp Testing

Test your web apps with Playwright.
"""
        mock_response = MagicMock()
        mock_response.read.return_value = skill_md
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            tier, name, path = reg.find_skill("testing web applications")

        assert tier == "official"
        assert name == "webapp-testing"
        assert (skills_dir / "official" / "webapp-testing" / "SKILL.md").exists()
        # Should be in the index now
        assert "webapp-testing" in reg.index["tiers"]["official"]["skills"]

    def test_find_remote_no_match_returns_ephemeral(self, skills_dir):
        """When no remote skill matches, fall through to ephemeral."""
        from datetime import datetime
        reg = SkillRegistry(str(skills_dir))
        reg._enable_remote = True

        # Populate source cache for the default anthropics source
        reg._source_caches["anthropics"] = {
            "catalog": {
                "webapp-testing": {
                    "description": "webapp testing toolkit",
                    "task_types": ["test_generation"]
                }
            },
            "fetched_at": datetime.utcnow(),
        }

        tier, name, path = reg.find_skill("quantum computing optimization")
        assert tier == "ephemeral"
        assert name is None


class TestInferTaskTypes:
    """Test task type inference from skill names."""

    def test_testing_skill(self):
        types = SkillRegistry._infer_task_types_from_name("webapp-testing")
        assert "test_generation" in types

    def test_pdf_skill(self):
        types = SkillRegistry._infer_task_types_from_name("pdf")
        assert "pdf_processing" in types

    def test_frontend_skill(self):
        types = SkillRegistry._infer_task_types_from_name("frontend-design")
        assert "frontend_development" in types

    def test_mcp_skill(self):
        types = SkillRegistry._infer_task_types_from_name("mcp-builder")
        assert "mcp_development" in types

    def test_unknown_returns_general(self):
        types = SkillRegistry._infer_task_types_from_name("xyz-unknown")
        assert types == ["general"]

    def test_doc_coauthoring_no_double_match(self):
        """Bug #5: doc-coauthoring should not match both documentation AND document_processing."""
        types = SkillRegistry._infer_task_types_from_name("doc-coauthoring")
        assert "documentation" in types
        assert "document_processing" not in types

    def test_docx_matches_document_processing(self):
        """docx should match document_processing, not documentation."""
        types = SkillRegistry._infer_task_types_from_name("docx")
        assert "document_processing" in types

    def test_internal_comms_no_double_match(self):
        """Bug #5: internal-comms should not match both documentation AND messaging."""
        types = SkillRegistry._infer_task_types_from_name("internal-comms")
        # Should match documentation (via "internal comms" keyword), not messaging
        assert "documentation" in types
        assert "messaging" not in types

    def test_slack_gif_matches_messaging(self):
        types = SkillRegistry._infer_task_types_from_name("slack-gif-creator")
        assert "messaging" in types


class TestParseFrontmatter:
    """Test YAML frontmatter parsing."""

    def test_valid_frontmatter(self):
        content = """---
name: test-skill
description: A test skill for testing
license: Apache-2.0
---

# Test Skill
"""
        meta = SkillRegistry._parse_frontmatter(content)
        assert meta["name"] == "test-skill"
        assert meta["description"] == "A test skill for testing"

    def test_multiline_description_block_scalar(self):
        """Bug #3: Multiline YAML descriptions with >- should be joined."""
        content = """---
name: webapp-testing
description: >-
  Toolkit for interacting with and testing
  local web applications using Playwright
license: Apache-2.0
---

# Content
"""
        meta = SkillRegistry._parse_frontmatter(content)
        assert meta["name"] == "webapp-testing"
        assert "Toolkit for interacting" in meta["description"]
        assert "Playwright" in meta["description"]
        assert ">-" not in meta["description"]

    def test_multiline_description_pipe(self):
        """Bug #3: Multiline YAML descriptions with | should be joined."""
        content = """---
name: test-skill
description: |
  First line of description
  Second line of description
license: MIT
---
"""
        meta = SkillRegistry._parse_frontmatter(content)
        assert "First line" in meta["description"]
        assert "Second line" in meta["description"]

    def test_nested_yaml_metadata_ignored(self):
        """Indented keys under metadata: should not pollute top-level keys."""
        content = """---
name: test
description: A simple skill
metadata:
  author: anthropic
  version: "1.0"
---
"""
        meta = SkillRegistry._parse_frontmatter(content)
        assert meta["name"] == "test"
        assert meta["description"] == "A simple skill"
        # metadata's sub-keys should not appear as top-level
        assert "author" not in meta

    def test_no_frontmatter(self):
        content = "# No Frontmatter\nJust content."
        meta = SkillRegistry._parse_frontmatter(content)
        assert meta == {}

    def test_malformed_frontmatter(self):
        content = "---\nname: broken\nno closing marker here"
        meta = SkillRegistry._parse_frontmatter(content)
        assert meta == {}


class TestCleanup:
    """Test temp skill cleanup."""

    def test_cleanup_temp(self, skills_dir, registry):
        skill_dir, _ = _create_skill(skills_dir, "temp", "cleanup-me", "Ephemeral")
        registry.register_skill("cleanup-me", "Ephemeral", "temp", ["general"], skill_dir)

        assert "cleanup-me" in registry.index["tiers"]["temp"]["skills"]

        # Backdate last_used beyond TTL (7 days) so cleanup_temp evicts it
        from datetime import datetime, timedelta
        stale_date = (datetime.utcnow() - timedelta(days=8)).isoformat()
        registry.index["tiers"]["temp"]["skills"]["cleanup-me"]["last_used"] = stale_date
        registry.index["tiers"]["temp"]["skills"]["cleanup-me"]["created_at"] = stale_date

        registry.cleanup_temp()

        assert "cleanup-me" not in registry.index["tiers"]["temp"]["skills"]
        assert not (skills_dir / "temp" / "cleanup-me").exists()


class TestStats:
    """Test registry statistics."""

    def test_empty_stats(self, registry):
        stats = registry.get_stats()
        assert stats["total_skills"] == 0
        assert stats["by_tier"]["official"]["count"] == 0

    def test_stats_with_skills(self, skills_dir, registry):
        skill_dir, _ = _create_skill(skills_dir, "official", "stat-skill", "For stats")
        registry.register_skill("stat-skill", "For stats", "official", ["general"], skill_dir)

        stats = registry.get_stats()
        assert stats["total_skills"] == 1
        assert stats["by_tier"]["official"]["count"] == 1


class TestQualityWeightedRanking:
    """Test that skills with higher quality scores are preferred."""

    def test_high_score_skill_preferred(self, skills_dir, registry):
        """A skill with high avg_score should rank above one with low avg_score."""
        # Create two skills for the same task type
        skill_dir_a, _ = _create_skill(
            skills_dir, "official", "test-skill-a", "test generation skill alpha"
        )
        registry.register_skill(
            "test-skill-a", "test generation skill alpha",
            "official", ["test_generation"], skill_dir_a
        )

        skill_dir_b, _ = _create_skill(
            skills_dir, "official", "test-skill-b", "test generation skill beta"
        )
        registry.register_skill(
            "test-skill-b", "test generation skill beta",
            "official", ["test_generation"], skill_dir_b
        )

        # Simulate usage history: skill-b has better scores
        registry.index["tiers"]["official"]["skills"]["test-skill-a"]["avg_score"] = 45.0
        registry.index["tiers"]["official"]["skills"]["test-skill-a"]["usage_count"] = 5
        registry.index["tiers"]["official"]["skills"]["test-skill-b"]["avg_score"] = 92.0
        registry.index["tiers"]["official"]["skills"]["test-skill-b"]["usage_count"] = 5

        # Search should prefer skill-b
        match = registry._search_tier("test_generation", "official")
        assert match is not None
        assert match["name"] == "test-skill-b"

    def test_no_usage_gets_neutral_score(self, skills_dir, registry):
        """Skills with no usage history get a neutral quality factor."""
        skill_dir, _ = _create_skill(
            skills_dir, "official", "new-skill", "new untested skill"
        )
        registry.register_skill(
            "new-skill", "new untested skill",
            "official", ["general"], skill_dir
        )

        match = registry._search_tier("new untested skill", "official")
        assert match is not None
        # Confidence should be reasonable (not penalized for no history)
        assert match["confidence"] > 0.0

    def test_usage_count_provides_tiebreaker(self, skills_dir, registry):
        """Between equal-quality skills, higher usage gives a small boost."""
        skill_dir_a, _ = _create_skill(
            skills_dir, "official", "used-skill", "data processing pipeline"
        )
        registry.register_skill(
            "used-skill", "data processing pipeline",
            "official", ["data_processing"], skill_dir_a
        )

        skill_dir_b, _ = _create_skill(
            skills_dir, "official", "unused-skill", "data processing pipeline"
        )
        registry.register_skill(
            "unused-skill", "data processing pipeline",
            "official", ["data_processing"], skill_dir_b
        )

        # Same quality, but different usage counts
        registry.index["tiers"]["official"]["skills"]["used-skill"]["avg_score"] = 80.0
        registry.index["tiers"]["official"]["skills"]["used-skill"]["usage_count"] = 20
        registry.index["tiers"]["official"]["skills"]["unused-skill"]["avg_score"] = 80.0
        registry.index["tiers"]["official"]["skills"]["unused-skill"]["usage_count"] = 1

        match = registry._search_tier("data_processing", "official")
        assert match is not None
        assert match["name"] == "used-skill"


class TestFrontmatterTaskTypes:
    """Test that skills can declare custom task-types in SKILL.md frontmatter."""

    def test_frontmatter_task_types_in_backfill(self, skills_dir, registry):
        """Skills with task-types in frontmatter get those types in the index."""
        skill_dir = skills_dir / "official" / "ml-pipeline-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("""---
name: ml-pipeline-skill
description: ML pipeline orchestration
task-types: ml_pipeline data_science
allowed-tools: Read Write
---

# ML Pipeline Skill
""")
        registry.register_skill(
            "ml-pipeline-skill", "ML pipeline orchestration",
            "official", ["general"], skill_dir
        )

        # Re-run backfill (simulates restart)
        registry._backfill_frontmatter_task_types()

        skill_data = registry.index["tiers"]["official"]["skills"]["ml-pipeline-skill"]
        assert "ml_pipeline" in skill_data["task_types"]
        assert "data_science" in skill_data["task_types"]

    def test_get_all_custom_task_types(self, skills_dir, registry):
        """get_all_custom_task_types collects types from all tiers."""
        skill_dir = skills_dir / "local" / "custom-type-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("""---
name: custom-type-skill
description: A skill with custom types
task-types: infrastructure_as_code
allowed-tools: Read
---

Body
""")
        registry.register_skill(
            "custom-type-skill", "A skill with custom types",
            "local", ["infrastructure_as_code"], skill_dir
        )

        custom_types = registry.get_all_custom_task_types()
        assert "infrastructure_as_code" in custom_types


# ── Workspace Tier Tests ────────────────────────────────────────────────────

def _make_workspace_skill(directory: Path, name: str, description: str, task_types: str = "general") -> Path:
    """Write a SKILL.md file into a skills/ subdirectory under directory."""
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntask-types: {task_types}\nallowed-tools: Read\n---\n\nBody.\n"
    )
    return skill_dir


class TestWorkspaceTier:
    """Tests for the workspace (project-specific, in-memory) skill tier."""

    def test_scan_workspace_loads_skills_from_skills_subdir(self, registry, tmp_path):
        """Skills in {workspace}/skills/ are discovered and loaded."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _make_workspace_skill(skills_root, "django-patterns", "Django REST framework patterns", "api_development")

        count = registry.scan_workspace(tmp_path)
        assert count == 1
        assert "django-patterns" in registry._workspace_skills

    def test_scan_workspace_loads_from_claude_skills_subdir(self, registry, tmp_path):
        """Skills in {workspace}/.claude/skills/ are discovered."""
        skills_root = tmp_path / ".claude" / "skills"
        skills_root.mkdir(parents=True)
        _make_workspace_skill(skills_root, "react-query-skill", "React Query data fetching patterns", "frontend")

        count = registry.scan_workspace(tmp_path)
        assert count == 1
        assert "react-query-skill" in registry._workspace_skills

    def test_scan_workspace_nonexistent_dir_returns_zero(self, registry, tmp_path):
        """scan_workspace on a missing path returns 0 without error."""
        assert registry.scan_workspace(tmp_path / "does-not-exist") == 0

    def test_scan_workspace_skips_invalid_skill_files(self, registry, tmp_path):
        """Files without required frontmatter are silently skipped."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        # File with no frontmatter
        (skills_root / "not-a-skill.md").write_text("just some text\n")
        # Subdirectory with no SKILL.md
        (skills_root / "empty-dir").mkdir()

        assert registry.scan_workspace(tmp_path) == 0

    def test_find_skill_returns_workspace_first(self, registry, tmp_path, skills_dir):
        """Workspace tier takes priority over official/local/temp tiers."""
        # Register a persistent skill
        _create_skill(skills_dir, "local", "generic-api-skill", "API development helper", ["api_development"])
        registry.register_skill("generic-api-skill", "API development helper", "local", ["api_development"],
                                 skills_dir / "local" / "generic-api-skill")

        # Register a workspace skill with the same task type
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _make_workspace_skill(skills_root, "project-api-patterns", "Project-specific API patterns", "api_development")
        registry.scan_workspace(tmp_path)

        tier, name, _path = registry.find_skill("api development patterns")
        assert tier == "workspace"
        assert name == "project-api-patterns"

    def test_load_skill_returns_workspace_content(self, registry, tmp_path):
        """load_skill returns in-memory content for workspace skills."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _make_workspace_skill(skills_root, "ws-skill", "Workspace skill for testing", "general")
        registry.scan_workspace(tmp_path)

        content = registry.load_skill("ws-skill")
        assert content is not None
        assert "Workspace skill for testing" in content

    def test_clear_workspace_removes_all_workspace_skills(self, registry, tmp_path):
        """clear_workspace empties the workspace tier."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _make_workspace_skill(skills_root, "temp-skill-a", "Temp skill A", "general")
        _make_workspace_skill(skills_root, "temp-skill-b", "Temp skill B", "general")
        registry.scan_workspace(tmp_path)
        assert len(registry._workspace_skills) == 2

        cleared = registry.clear_workspace()
        assert cleared == 2
        assert len(registry._workspace_skills) == 0

    def test_cleared_workspace_skill_not_found(self, registry, tmp_path):
        """After clear_workspace, skills from that workspace are no longer discoverable."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _make_workspace_skill(skills_root, "gone-skill", "Will be cleared", "general")
        registry.scan_workspace(tmp_path)
        registry.clear_workspace()

        assert registry.load_skill("gone-skill") is None

    def test_find_skills_includes_workspace(self, registry, tmp_path):
        """find_skills includes workspace matches when available."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _make_workspace_skill(skills_root, "ws-multi-skill", "Multi skill workspace", "code_generation")
        registry.scan_workspace(tmp_path)

        results = registry.find_skills("code generation workspace", max_skills=3)
        tiers = [r[0] for r in results]
        assert "workspace" in tiers

    def test_scan_multiple_workspace_dirs_accumulates_skills(self, registry, tmp_path):
        """Scanning multiple directories accumulates skills from all."""
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        (repo_a / "skills").mkdir(parents=True)
        (repo_b / "skills").mkdir(parents=True)
        _make_workspace_skill(repo_a / "skills", "skill-from-a", "Skill from repo A", "backend")
        _make_workspace_skill(repo_b / "skills", "skill-from-b", "Skill from repo B", "frontend")

        registry.scan_workspace(repo_a)
        registry.scan_workspace(repo_b)

        assert "skill-from-a" in registry._workspace_skills
        assert "skill-from-b" in registry._workspace_skills
        assert len(registry._workspace_skills) == 2

    def test_get_stats_includes_workspace_count(self, registry, tmp_path):
        """get_stats reports workspace skill count."""
        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        _make_workspace_skill(skills_root, "stat-skill", "Stat skill", "general")
        registry.scan_workspace(tmp_path)

        stats = registry.get_stats()
        assert "workspace" in stats["by_tier"]
        assert stats["by_tier"]["workspace"]["count"] == 1


class TestSemanticMatching:
    """find_skill() blends embedding similarity into the keyword score.

    The registry is given a mock embedder via its SkillEmbeddingCache so
    these tests run with no network. Each test exercises a case where
    keyword scoring alone would fail — the skill's description shares
    zero tokens with the query — and asserts that semantic similarity
    rescues the match.
    """

    def _inject_mock_embedder(self, registry, vectors):
        """Wire a mock embedder into the registry's embedding cache.

        ``vectors`` maps arbitrary text keys → vector. The mock returns
        the vector for the first key found in the embed input text,
        matching how MessageStore/MemoryStore tests set up their mocks
        (tests/test_memory_store.py:1060-1119).
        """
        from unittest.mock import MagicMock
        from agents.embedder import VLLMEmbedder
        from agents.skill_embeddings import SkillEmbeddingCache

        def fake_embed(text):
            for key, vec in vectors.items():
                if key.lower() in text.lower():
                    return vec
            return [0.0, 0.0, 0.0, 1.0]  # orthogonal "no match" vector

        embedder = MagicMock(spec=VLLMEmbedder)
        embedder.model = "test-model"
        embedder.is_available.return_value = True
        embedder.embed.side_effect = fake_embed

        # Force the lazy attribute so _get_embedding_cache returns our instance.
        registry._embedding_cache = SkillEmbeddingCache(
            registry.base_dir, embedder=embedder
        )
        return embedder

    def test_semantic_match_beats_zero_keyword_overlap(self, skills_dir, registry):
        """A query with no shared tokens still finds the semantically close skill."""
        # Two skills with deliberately non-overlapping keywords.
        ml_dir, _ = _create_skill(
            skills_dir, "local", "ml-pipeline",
            "Machine learning training workflows for tabular datasets",
            ["data_analysis"],
        )
        web_dir, _ = _create_skill(
            skills_dir, "local", "web-scraper",
            "HTTP crawling and HTML extraction",
            ["research"],
        )

        # Inject BEFORE register_skill so the warm step uses our mock.
        self._inject_mock_embedder(
            registry,
            {
                "machine learning": [1.0, 0.0, 0.0, 0.0],
                "HTTP crawling": [0.0, 1.0, 0.0, 0.0],
                # Query: "train a model on tabular data" semantically
                # matches the ML skill's vector.
                "train a model": [0.95, 0.05, 0.0, 0.0],
            },
        )

        registry.register_skill(
            "ml-pipeline",
            "Machine learning training workflows for tabular datasets",
            "local",
            ["data_analysis"],
            ml_dir,
        )
        registry.register_skill(
            "web-scraper",
            "HTTP crawling and HTML extraction",
            "local",
            ["research"],
            web_dir,
        )

        # Zero keyword overlap with either description — only embeddings
        # can save this match.
        tier, name, _ = registry.find_skill("train a model on tabular data")
        assert tier == "local"
        assert name == "ml-pipeline"

    def test_embedder_unavailable_falls_back_to_keyword(self, skills_dir, registry):
        """When the embedder is down, behavior is byte-identical to the keyword path."""
        skill_dir, _ = _create_skill(
            skills_dir, "official", "pdf-tools",
            "Toolkit for PDF extraction and parsing",
            ["documentation"],
        )
        registry.register_skill(
            "pdf-tools",
            "Toolkit for PDF extraction and parsing",
            "official",
            ["documentation"],
            skill_dir,
        )

        # Force the cache into an "embedder unavailable" state.
        from agents.skill_embeddings import SkillEmbeddingCache
        registry._embedding_cache = SkillEmbeddingCache(registry.base_dir)
        registry._embedding_cache._embedder_checked = True
        registry._embedding_cache._embedder = None
        registry._last_query_vec = None

        # Exact keyword match — the pure-keyword path should still find it.
        tier, name, _ = registry.find_skill("PDF extraction")
        assert tier == "official"
        assert name == "pdf-tools"

    def test_semantic_does_not_break_keyword_priority(self, skills_dir, registry):
        """With both a strong keyword hit and a weak semantic hit, keyword still wins."""
        exact_dir, _ = _create_skill(
            skills_dir, "official", "pdf-extractor",
            "PDF extraction toolkit",
            ["documentation"],
        )
        weak_dir, _ = _create_skill(
            skills_dir, "local", "doc-parser",
            "Document parsing utilities",
            ["documentation"],
        )

        self._inject_mock_embedder(
            registry,
            {
                # Weak semantic signal for both skills
                "PDF extraction": [0.3, 0.3, 0.3, 0.0],
                "Document parsing": [0.3, 0.3, 0.3, 0.0],
                "PDF extraction toolkit": [0.3, 0.3, 0.3, 0.0],
            },
        )

        registry.register_skill(
            "pdf-extractor", "PDF extraction toolkit", "official",
            ["documentation"], exact_dir,
        )
        registry.register_skill(
            "doc-parser", "Document parsing utilities", "local",
            ["documentation"], weak_dir,
        )

        # Exact keyword match + official tier — pdf-extractor must win.
        tier, name, _ = registry.find_skill("PDF extraction")
        assert tier == "official"
        assert name == "pdf-extractor"


class TestUnregisterSkill:
    def test_unregister_removes_temp_skill_from_index(self, tmp_path):
        registry = SkillRegistry(str(tmp_path))
        skill_dir = registry.temp_dir / "my-temp-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-temp-skill\n---\n\n# Test"
        )

        registry.register_skill(
            name="my-temp-skill",
            description="temp test",
            tier="temp",
            task_types=["general"],
            skill_path=skill_dir,
        )
        assert "my-temp-skill" in registry.index["tiers"]["temp"]["skills"]

        registry.unregister_skill("my-temp-skill")
        assert "my-temp-skill" not in registry.index["tiers"]["temp"]["skills"]

    def test_unregister_noop_for_unknown_skill(self, tmp_path):
        registry = SkillRegistry(str(tmp_path))
        # No exception should be raised
        registry.unregister_skill("never-existed")

    def test_unregister_persists_to_index_file(self, tmp_path):
        registry = SkillRegistry(str(tmp_path))
        skill_dir = registry.temp_dir / "my-temp-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-temp-skill\n---\n\n# Test"
        )
        registry.register_skill(
            name="my-temp-skill",
            description="test",
            tier="temp",
            task_types=["general"],
            skill_path=skill_dir,
        )
        registry.unregister_skill("my-temp-skill")

        # New registry instance reads from disk — should not see the skill
        fresh = SkillRegistry(str(tmp_path))
        assert "my-temp-skill" not in fresh.index["tiers"]["temp"]["skills"]

    def test_unregister_clears_integrity_hash_and_cache(self, tmp_path):
        from agents.skill_registry import SkillRegistry

        registry = SkillRegistry(str(tmp_path))
        skill_dir = registry.temp_dir / "my-temp-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-temp-skill\n---\n\n# Test"
        )
        registry.register_skill(
            name="my-temp-skill",
            description="test",
            tier="temp",
            task_types=["general"],
            skill_path=skill_dir,
        )

        # Mock both cleanup paths so we can verify they were called
        registry.security.remove_integrity_hash = MagicMock()
        mock_cache = MagicMock()
        registry._get_embedding_cache = MagicMock(return_value=mock_cache)

        registry.unregister_skill("my-temp-skill")

        registry.security.remove_integrity_hash.assert_called_once_with(
            "my-temp-skill"
        )
        mock_cache.invalidate.assert_called_once_with("my-temp-skill")
