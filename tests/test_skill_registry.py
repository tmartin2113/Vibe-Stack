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
