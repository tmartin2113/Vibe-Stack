"""
Skill Registry - Three-tier skill management system.

Manages skill discovery, lifecycle, and promotion across three tiers:
- Tier 1 (official): Skills from vetted remote sources (anthropics, obra, vercel)
- Tier 2 (local): Custom persistent skills proven valuable over time
- Tier 3 (temp): Ephemeral skills generated for one-off requests
"""

import json
import math
import os
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, TypedDict
from dataclasses import dataclass
import logging

from .skill_security import SkillSecurity, SkillSecurityError
from .config import SkillsConfig, SkillSourceConfig, SkillTier
from .skill_registry_index import SkillRegistryIndexMixin
from .skill_registry_lifecycle import SkillRegistryLifecycleMixin
from .skill_registry_workspace import SkillRegistryWorkspaceMixin
from .skill_remote import SkillRegistryRemoteMixin
from .skill_search import SkillRegistrySearchMixin
from .skill_registry_utils import (
    parse_frontmatter as _parse_frontmatter_fn,
    infer_task_types_from_name as _infer_task_types_fn,
)

logger = logging.getLogger(__name__)


class TierStats(TypedDict):
    count: int
    total_usage: int
    avg_score: float


class RegistryStats(TypedDict):
    total_skills: int
    by_tier: Dict[str, TierStats]


@dataclass
class SkillMetadata:
    """Metadata for a skill."""
    name: str
    description: str
    tier: SkillTier  # "official", "local", or "temp"
    path: str
    task_types: List[str]
    usage_count: int = 0
    scores: Optional[List[int]] = None
    avg_score: float = 0.0
    created_at: Optional[str] = None
    last_used: Optional[str] = None
    promoted_at: Optional[str] = None

    def __post_init__(self):
        if self.scores is None:
            self.scores = []
        if self.created_at is None:
            self.created_at = datetime.utcnow().isoformat() + "Z"
        if self.last_used is None:
            self.last_used = self.created_at


class SkillRegistry(
    SkillRegistryIndexMixin,
    SkillRegistryLifecycleMixin,
    SkillRegistryWorkspaceMixin,
    SkillRegistryRemoteMixin,
    SkillRegistrySearchMixin,
):
    """
    Manages three-tier skill discovery and lifecycle.

    Tier 1 (official): Check first for battle-tested community skills
    Tier 2 (local): Check for proven custom skills with high usage/quality
    Tier 3 (temp): Generate ephemeral skills for one-off requests

    When no local skill matches, probes vetted remote skill sources
    (anthropics/skills, obra/superpowers, vercel-labs/agent-skills) in
    priority order, downloads on demand, and caches in the official tier.
    """

    # Promotion criteria
    MIN_USAGE_FOR_PROMOTION = int(os.getenv("VIBE_SKILL_MIN_USAGE_FOR_PROMOTION", "3"))
    MIN_AVG_SCORE_FOR_PROMOTION = int(os.getenv("VIBE_SKILL_MIN_AVG_SCORE_FOR_PROMOTION", "85"))

    if MIN_USAGE_FOR_PROMOTION < 1:
        raise ValueError(f"VIBE_SKILL_MIN_USAGE_FOR_PROMOTION must be >= 1, got {MIN_USAGE_FOR_PROMOTION}")
    if MIN_AVG_SCORE_FOR_PROMOTION < 0 or MIN_AVG_SCORE_FOR_PROMOTION > 100:
        raise ValueError(f"VIBE_SKILL_MIN_AVG_SCORE_FOR_PROMOTION must be 0-100, got {MIN_AVG_SCORE_FOR_PROMOTION}")

    # Confidence thresholds for tier selection.
    # find_skill() now blends keyword scoring with embedding similarity
    # (see agents/skill_embeddings.py) but these thresholds remain tuned
    # for the keyword path — they will be revisited once telemetry on the
    # blended distribution is collected.
    OFFICIAL_CONFIDENCE_THRESHOLD = float(os.getenv("VIBE_SKILL_OFFICIAL_CONFIDENCE", "0.4"))
    LOCAL_CONFIDENCE_THRESHOLD = float(os.getenv("VIBE_SKILL_LOCAL_CONFIDENCE", "0.35"))
    TEMP_CONFIDENCE_THRESHOLD = float(os.getenv("VIBE_SKILL_TEMP_CONFIDENCE", "0.35"))  # Same as local — retained temp skills are proven

    # TTL-based retention (days since last use before eviction)
    TEMP_SKILL_TTL_DAYS = int(os.getenv("VIBE_SKILL_TEMP_TTL_DAYS", "7"))        # Ephemeral skills kept 7 days after last use
    OFFICIAL_SKILL_TTL_DAYS = int(os.getenv("VIBE_SKILL_OFFICIAL_TTL_DAYS", "30"))   # Cached GitHub skills evicted after 30 days unused

    def __init__(self, vibe_skills_dir: str = "vibe_skills",
                 security: Optional[SkillSecurity] = None,
                 skills_config: Optional[SkillsConfig] = None):
        """
        Initialize the skill registry.

        Args:
            vibe_skills_dir: Root directory for skill tiers.
            security: Optional SkillSecurity instance.  When provided,
                all skill registration, loading, downloading, and
                promotion operations are gated through security checks.
                Defaults to a SkillSecurity with promotion approval enabled.
            skills_config: Optional SkillsConfig with vetted remote sources.
                Defaults to the locked-down 3-source configuration.
        """
        # Bug #5 fix: Resolve to absolute path to handle working directory changes
        self.base_dir = Path(vibe_skills_dir).resolve()
        self.official_dir = self.base_dir / "official"
        self.local_dir = self.base_dir / "local"
        self.temp_dir = self.base_dir / "temp"
        self.index_path = self.base_dir / ".index.json"

        # Security layer — auto-promotion enabled by default so ephemeral
        # skills that consistently score well (3+ uses, avg >= 85) get
        # promoted to local/ without manual approval.  Downloaded skills
        # still go through content validation at registration time.
        self.security = security if security is not None else SkillSecurity(
            require_promotion_approval=False
        )

        # Multi-source configuration (locked to 3 vetted sources)
        self._skills_config = skills_config if skills_config is not None else SkillsConfig()

        # Ensure directories exist
        self.official_dir.mkdir(parents=True, exist_ok=True)
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Load index
        self.index = self._load_index()

        # Bug #1/#2 fix: Restore persisted security state (hashes + pending promotions)
        self.security.import_state(self.index.get("security", {}))

        # Bug #5 fix: Compute integrity hashes for pre-existing skills that lack one
        self._backfill_integrity_hashes()

        # Backfill frontmatter task-types for skills indexed before this feature
        self._backfill_frontmatter_task_types()

        # Per-source catalog caches: {source_name: {"catalog": Dict, "fetched_at": datetime}}
        self._source_caches: Dict[str, Dict] = {}
        # Allow disabling remote lookups (e.g. in tests or offline mode)
        self._enable_remote = (
            self._skills_config.enable_remote
            and os.environ.get("VIBE_DISABLE_REMOTE_SKILLS", "").lower()
                not in ("1", "true", "yes")
        )

        # Workspace tier: in-memory, per-task. Populated by scan_workspace(),
        # cleared by clear_workspace() at end of each task. Never written to disk.
        self._workspace_skills: Dict[str, Dict[str, Any]] = {}

        # Local indexed sources: large local skill repos (e.g. openclaw) that are
        # too big to bulk-register. Each entry is:
        #   {"name": str, "index_path": Path, "skills_path": Path, "trust_level": str}
        # Their index.json is searched on demand; matching skills are copied to temp/.
        self._local_indexed_sources: List[Dict[str, Any]] = []
        self._local_index_caches: Dict[str, Dict[str, Any]] = {}  # source_name -> index dict
        self._init_local_indexed_sources()

        logger.info(f"✅ SkillRegistry initialized with {len(self._all_skills())} skills")

    def _all_skills(self) -> Dict[str, SkillMetadata]:
        """Get all skills across all tiers."""
        all_skills = {}
        for tier in ["official", "local", "temp"]:
            all_skills.update(self.index["tiers"][tier]["skills"])
        return all_skills

    def _backfill_integrity_hashes(self):
        """
        Compute and store integrity hashes for skills that lack one.

        This handles skills that were cached/registered before the security
        module existed, ensuring they get baseline hashes for future
        tamper detection.
        """
        backfilled = 0
        for tier in ["official", "local", "temp"]:
            for skill_name, skill_data in self.index["tiers"][tier]["skills"].items():
                if self.security.get_integrity_hash(skill_name) is not None:
                    continue  # Already has a hash
                skill_path = Path(skill_data.get("path", ""))
                skill_file = skill_path / "SKILL.md"
                if skill_file.exists():
                    try:
                        content = skill_file.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        self.security.store_integrity_hash(skill_name, content)
                        backfilled += 1
                    except Exception as e:
                        logger.debug(
                            f"Could not backfill hash for {skill_name}: {e}"
                        )
        if backfilled > 0:
            self._save_index()
            logger.info(
                f"🔒 Backfilled integrity hashes for {backfilled} "
                f"pre-existing skill(s)"
            )

    def _backfill_frontmatter_task_types(self):
        """
        Backfill task-types from SKILL.md frontmatter for indexed skills.

        Skills indexed before the task-types frontmatter feature used
        name-based inference only.  This reads each skill's SKILL.md
        and updates the index if explicit task-types are declared.
        """
        updated = 0
        for tier in ["official", "local", "temp"]:
            for skill_name, skill_data in self.index["tiers"][tier]["skills"].items():
                # Skip if already has a _frontmatter_task_types marker
                if skill_data.get("_task_types_from_frontmatter"):
                    continue
                skill_path = Path(skill_data.get("path", ""))
                skill_file = skill_path / "SKILL.md"
                if not skill_file.exists():
                    continue
                try:
                    content = skill_file.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    frontmatter_types = self.security.parse_task_types(content)
                    if frontmatter_types:
                        skill_data["task_types"] = frontmatter_types
                        skill_data["_task_types_from_frontmatter"] = True
                        updated += 1
                except Exception as e:
                    logger.debug(
                        f"Could not backfill task-types for {skill_name}: {e}"
                    )
        if updated > 0:
            self._save_index()
            logger.info(
                f"Backfilled frontmatter task-types for {updated} skill(s)"
            )

    def get_all_custom_task_types(self) -> Dict[str, str]:
        """
        Collect all custom task types declared by registered skills.

        Returns a dict mapping task_type -> description, suitable for
        injecting into the router's LLM classifier prompt.  Only includes
        types that are NOT in the router's hardcoded set.
        """
        custom_types: Dict[str, str] = {}
        for tier in ["official", "local", "temp"]:
            for skill_name, skill_data in self.index["tiers"][tier]["skills"].items():
                for task_type in skill_data.get("task_types", []):
                    if task_type not in custom_types:
                        desc = skill_data.get("description", skill_name.replace("-", " "))
                        custom_types[task_type] = desc
        return custom_types

    # ------------------------------------------------------------------
    # Local indexed sources (e.g. openclaw)
    # ------------------------------------------------------------------

    def _init_local_indexed_sources(self) -> None:
        """
        Auto-register local indexed skill repos from environment variables.

        VIBE_OPENCLAW_PATH — path to openclaw-skills repo root.
            If set and the directory exists, openclaw is added as a
            local indexed source searched after the three main tiers.

        Disabled when VIBE_DISABLE_REMOTE_SKILLS=1 (same flag used in tests)
        so the test suite is not affected by the local environment.

        Additional sources can be added at runtime via
        add_local_indexed_source().
        """
        if not self._enable_remote:
            return

        openclaw_path = os.environ.get("VIBE_OPENCLAW_PATH", "").strip()
        if openclaw_path:
            self.add_local_indexed_source(
                name="openclaw",
                source_dir=Path(openclaw_path),
                trust_level="standard",
            )

    def add_local_indexed_source(
        self,
        name: str,
        source_dir: Path,
        trust_level: str = "standard",
        skills_subdir: str = "skills",
        index_filename: str = "index.json",
    ) -> bool:
        """
        Register a large local skill repo for on-demand targeted search.

        The repo must have:
          {source_dir}/{index_filename}     — flat index: {skill_name: {tags, description}}
          {source_dir}/{skills_subdir}/{skill_name}/SKILL.md

        When a match is found, the skill is copied to temp/ so subsequent
        tasks find it without re-scanning the source.

        Args:
            name:          Logical source name (used for deduplication).
            source_dir:    Root directory of the skill repo.
            trust_level:   "high", "standard", or "restricted".
            skills_subdir: Subdirectory containing skill dirs (default: "skills").
            index_filename: Name of the flat index file (default: "index.json").

        Returns:
            True if the source was registered, False if path invalid.
        """
        source_dir = Path(source_dir)
        index_path = source_dir / index_filename
        skills_path = source_dir / skills_subdir

        if not index_path.exists() or not skills_path.is_dir():
            logger.warning(
                f"Local indexed source '{name}' skipped: "
                f"index or skills dir not found at {source_dir}"
            )
            return False

        # Deduplicate
        if any(s["name"] == name for s in self._local_indexed_sources):
            return True

        self._local_indexed_sources.append({
            "name": name,
            "index_path": index_path,
            "skills_path": skills_path,
            "trust_level": trust_level,
        })
        logger.info(f"📚 Local indexed source registered: {name} ({source_dir})")
        return True

    def _load_local_index(self, source: Dict[str, Any]) -> Dict[str, Any]:
        """Lazy-load and cache the index.json for a local indexed source."""
        name = source["name"]
        if name not in self._local_index_caches:
            try:
                raw = Path(source["index_path"]).read_text(encoding="utf-8", errors="replace")
                self._local_index_caches[name] = json.loads(raw)
            except Exception as exc:
                logger.warning(f"Failed to load index for '{name}': {exc}")
                self._local_index_caches[name] = {}
        return self._local_index_caches[name]

    # ------------------------------------------------------------------
    # Search and matching
    # Implemented in SkillRegistrySearchMixin (agents/skill_search.py)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Lifecycle: register, track_usage, promote_skill, approve_promotion,
    # cleanup_temp, _evict_stale_from_tier, _sweep_orphaned_dirs
    # Implemented in SkillRegistryLifecycleMixin (agents/skill_registry_lifecycle.py)
    # ------------------------------------------------------------------

    def load_skill(self, skill_name: str) -> Optional[str]:
        """
        Load skill content from SKILL.md file.

        Args:
            skill_name: Name of the skill

        Returns:
            Skill content as string, or None if not found
        """
        # Workspace skills are cached in memory — return immediately (no file I/O)
        if skill_name in self._workspace_skills:
            return self._workspace_skills[skill_name]["content"]

        # Find which tier the skill is in
        tier = None
        for t in ["official", "local", "temp"]:
            if skill_name in self.index["tiers"][t]["skills"]:
                tier = t
                break

        if not tier:
            logger.error(f"❌ Skill not found: {skill_name}")
            return None

        skill_path = Path(self.index["tiers"][tier]["skills"][skill_name]["path"])
        skill_file = skill_path / "SKILL.md"

        if not skill_file.exists():
            logger.error(f"❌ SKILL.md not found: {skill_file}")
            return None

        # Bug #4 fix: Use explicit UTF-8 encoding to match register_skill()
        with open(skill_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # Security: verify content integrity before returning
        if not self.security.verify_integrity(skill_name, content):
            logger.error(
                f"❌ Integrity check failed for {skill_name}. "
                f"Skill content may have been tampered with."
            )
            return None

        return content

    # ------------------------------------------------------------------
    # Remote skill discovery (multi-source)
    # Implemented in SkillRegistryRemoteMixin (agents/skill_remote.py)
    # ------------------------------------------------------------------

    # Remote methods (_get_remote_catalog, _find_remote_skill,
    # _download_remote_skill, _download_skill_extras, _download_scripts,
    # _download_directory_contents) are inherited from SkillRegistryRemoteMixin.

    @staticmethod
    def _parse_frontmatter(content: str) -> Dict[str, str]:
        """Delegate to standalone utility."""
        return _parse_frontmatter_fn(content)

    @staticmethod
    def _infer_task_types_from_name(skill_name: str) -> List[str]:
        """Delegate to standalone utility."""
        return _infer_task_types_fn(skill_name)

    def get_pending_promotions(self) -> Dict[str, Any]:
        """Get all skills pending promotion approval."""
        return self.security.get_pending_promotions()

    def get_skill_allowed_tools(self, skill_name: str) -> set:
        """
        Get the set of tools a skill is allowed to use.

        Parses the allowed-tools field from the skill's SKILL.md.
        Returns DEFAULT_ALLOWED_TOOLS if the skill can't be loaded.
        """
        from .skill_security import DEFAULT_ALLOWED_TOOLS

        content = self.load_skill(skill_name)
        if content is None:
            return set(DEFAULT_ALLOWED_TOOLS)
        return self.security.parse_allowed_tools(content)

    def get_stats(self) -> RegistryStats:
        """Get statistics about the skill registry."""
        stats: Dict[str, Any] = {
            "total_skills": 0,
            "by_tier": {}
        }

        # Workspace tier (in-memory, task-scoped)
        ws_count = len(self._workspace_skills)
        stats["by_tier"]["workspace"] = {"count": ws_count, "total_usage": 0, "avg_score": 0.0}
        stats["total_skills"] += ws_count

        for tier in ["official", "local", "temp"]:
            tier_skills = self.index["tiers"][tier]["skills"]
            count = len(tier_skills)
            stats["total_skills"] += count

            # Calculate tier-specific stats
            if count > 0:
                total_usage = sum(s.get("usage_count", 0) for s in tier_skills.values())
                avg_scores = [s.get("avg_score", 0) for s in tier_skills.values() if s.get("avg_score", 0) > 0]

                stats["by_tier"][tier] = {
                    "count": count,
                    "total_usage": total_usage,
                    "avg_score": sum(avg_scores) / len(avg_scores) if avg_scores else 0.0
                }
            else:
                stats["by_tier"][tier] = {
                    "count": 0,
                    "total_usage": 0,
                    "avg_score": 0.0
                }

        return stats
