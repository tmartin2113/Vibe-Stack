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
import shutil
import tempfile
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
import logging

from .skill_security import SkillSecurity, SkillSecurityError
from .config import SkillsConfig, SkillSourceConfig
from .skill_remote import SkillRegistryRemoteMixin
from .skill_search import SkillRegistrySearchMixin
from .skill_registry_utils import (
    parse_frontmatter as _parse_frontmatter_fn,
    infer_task_types_from_name as _infer_task_types_fn,
)

# Platform-specific file locking imports
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

logger = logging.getLogger(__name__)


@dataclass
class SkillMetadata:
    """Metadata for a skill."""
    name: str
    description: str
    tier: str  # "official", "local", or "temp"
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


class SkillRegistry(SkillRegistryRemoteMixin, SkillRegistrySearchMixin):
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
    MIN_USAGE_FOR_PROMOTION = 3
    MIN_AVG_SCORE_FOR_PROMOTION = 85

    # Confidence thresholds for tier selection
    # These are tuned for keyword-based matching; raise when semantic
    # (embedding) matching is implemented.
    OFFICIAL_CONFIDENCE_THRESHOLD = 0.4
    LOCAL_CONFIDENCE_THRESHOLD = 0.35
    TEMP_CONFIDENCE_THRESHOLD = 0.35  # Same as local — retained temp skills are proven

    # TTL-based retention (days since last use before eviction)
    TEMP_SKILL_TTL_DAYS = 7        # Ephemeral skills kept 7 days after last use
    OFFICIAL_SKILL_TTL_DAYS = 30   # Cached GitHub skills evicted after 30 days unused

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

    def _load_index(self) -> Dict[str, Any]:
        """
        Load the skill index from disk with file locking.

        Uses shared lock to allow concurrent reads but prevent reads during writes.
        """
        if not self.index_path.exists():
            return {
                "version": "1.0",
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "tiers": {
                    "official": {"skills": {}},
                    "local": {"skills": {}},
                    "temp": {"skills": {}}
                }
            }

        if HAS_FCNTL:
            # Unix/Linux/Mac: Use shared lock for reading
            with open(self.index_path, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    return json.load(f)  # type: ignore[no-any-return]
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        elif HAS_MSVCRT:
            # Windows: Use shared lock for reading
            with open(self.index_path, 'r') as f:
                # Note: msvcrt doesn't have shared locks, use exclusive briefly
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                try:
                    return json.load(f)  # type: ignore[no-any-return]
                finally:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            # Fallback: No locking
            with open(self.index_path, 'r') as f:
                return json.load(f)  # type: ignore[no-any-return]

    def _save_index(self):
        """
        Save the skill index to disk with file locking.

        Uses platform-specific file locking to prevent race conditions:
        - Unix/Linux: fcntl (exclusive lock)
        - Windows: msvcrt (file locking)
        - Fallback: Atomic write via temp file + rename

        This fixes Bug #7: Race condition on concurrent .index.json writes.
        """
        self.index["last_updated"] = datetime.utcnow().isoformat() + "Z"
        # Bug #1/#2 fix: Persist security state (hashes + pending promotions)
        self.index["security"] = self.security.export_state()

        if HAS_FCNTL:
            # Unix/Linux/Mac: Use fcntl for file locking
            self._save_index_with_fcntl()
        elif HAS_MSVCRT:
            # Windows: Use msvcrt for file locking
            self._save_index_with_msvcrt()
        else:
            # Fallback: Atomic write (no locking, but safer than direct write)
            self._save_index_atomic()

    def _save_index_with_fcntl(self):
        """Save index with fcntl file locking (Unix/Linux/Mac)."""
        # Open with 'r+' (or create with 'w' if missing) to avoid truncating
        # before the lock is acquired.  'w' truncates immediately on open(),
        # which races with concurrent readers/writers.
        try:
            f = open(self.index_path, 'r+')
        except FileNotFoundError:
            f = open(self.index_path, 'w')

        with f:
            # Acquire exclusive lock (blocks until available)
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.truncate()
                json.dump(self.index, f, indent=2)
                f.flush()  # Ensure data written to disk
            finally:
                # Release lock
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _save_index_with_msvcrt(self):
        """Save index with msvcrt file locking (Windows)."""
        # Use atomic write for Windows instead of in-place locking.
        # msvcrt.locking() only locks N bytes starting at the current
        # file position, which is fragile with 'w' mode (truncation
        # before lock) and doesn't cover the full file.
        self._save_index_atomic()

    def _save_index_atomic(self):
        """
        Save index atomically without file locking (fallback).

        Writes to a temporary file first, then renames it to the target.
        This prevents partial writes but doesn't prevent race conditions.
        """
        # Write to temp file in same directory (ensures same filesystem)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=self.base_dir,
            prefix=".index-",
            suffix=".json.tmp"
        )

        try:
            with open(temp_fd, 'w') as f:
                json.dump(self.index, f, indent=2)
                f.flush()

            # Atomic rename (replaces existing file)
            Path(temp_path).replace(self.index_path)

        except Exception as e:
            # Clean up temp file on error
            try:
                Path(temp_path).unlink()
            except Exception:
                pass
            raise e

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

    # ── Workspace Tier (per-task, in-memory) ───────────────────────────────────

    def scan_workspace(self, workspace_dir: Path) -> int:
        """
        Scan a project directory for task-scoped skills.

        Looks in these subdirectories (all optional):
          {workspace_dir}/skills/
          {workspace_dir}/.claude/skills/
          {workspace_dir}/docs/skills/
          {workspace_dir}/.skills/

        Each subdirectory containing a SKILL.md is loaded as a workspace
        skill. Standalone *.md files with valid frontmatter are also accepted.

        Workspace skills are held in memory only — never written to
        vibe_skills/ — and cleared at the end of each task via
        clear_workspace().

        Args:
            workspace_dir: Root of the project repo being worked on.

        Returns:
            Number of skills successfully loaded.
        """
        workspace_dir = Path(workspace_dir)
        if not workspace_dir.is_dir():
            return 0

        search_dirs = [
            workspace_dir / "skills",
            workspace_dir / ".claude" / "skills",
            workspace_dir / "docs" / "skills",
            workspace_dir / ".skills",
        ]

        loaded = 0
        for search_dir in search_dirs:
            if not search_dir.is_dir():
                continue

            # Convention 1: subdirectory with SKILL.md (matches tier layout)
            for entry in sorted(search_dir.iterdir()):
                if entry.is_dir():
                    skill_file = entry / "SKILL.md"
                    if skill_file.exists():
                        loaded += self._register_workspace_skill(skill_file)

            # Convention 2: standalone *.md files with frontmatter
            for skill_file in sorted(search_dir.glob("*.md")):
                loaded += self._register_workspace_skill(skill_file)

        if loaded:
            logger.info(f"🗂️  Loaded {loaded} workspace skill(s) from {workspace_dir}")
        return loaded

    def _register_workspace_skill(self, skill_file: Path) -> int:
        """Parse and register one workspace skill file. Returns 1 on success."""
        try:
            content = skill_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0

        meta = self._parse_frontmatter(content)
        skill_name = meta.get("name", "").strip()
        description = meta.get("description", "").strip()
        if not skill_name or not description:
            return 0

        try:
            self.security.validate_skill_name(skill_name)
        except Exception:
            logger.warning(f"Workspace skill rejected (invalid name): {skill_file}")
            return 0

        try:
            self.security.validate_skill_content(content, skill_name)
        except Exception as exc:
            logger.warning(f"Workspace skill rejected (security): {skill_file}: {exc}")
            return 0

        raw_task_types = meta.get("task-types", meta.get("task_types", "")).strip()
        task_types = [t.strip() for t in raw_task_types.split(",") if t.strip()]

        self._workspace_skills[skill_name] = {
            "description": description,
            "task_types": task_types,
            "content": content,
            "path": str(skill_file),
            "usage_count": 0,
            "avg_score": 0.0,
        }
        logger.debug(f"  🗂️  Workspace skill registered: {skill_name}")
        return 1

    def clear_workspace(self) -> int:
        """
        Remove all workspace skills from memory.

        Called at the end of each task by SkillCleanupNode. Workspace
        skills are never persisted to disk so this is a pure memory op.

        Returns:
            Number of skills removed.
        """
        count = len(self._workspace_skills)
        self._workspace_skills.clear()
        if count:
            logger.info(f"🗂️  Cleared {count} workspace skill(s)")
        return count

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

    def register_skill(
        self,
        name: str,
        description: str,
        tier: str,
        task_types: List[str],
        skill_path: Path
    ) -> SkillMetadata:
        """
        Register a new skill in the index.

        Args:
            name: Skill name (kebab-case)
            description: What the skill does
            tier: "official", "local", or "temp"
            task_types: List of task types this skill handles
            skill_path: Path to skill directory

        Returns:
            SkillMetadata object

        Raises:
            ValueError: If SKILL.md not found at skill_path
        """
        # Bug #7 fix: Validate tier parameter
        valid_tiers = {"official": self.official_dir, "local": self.local_dir,
                       "temp": self.temp_dir}
        if tier not in valid_tiers:
            raise ValueError(
                f"Invalid tier: {tier!r}. Must be one of: "
                f"{', '.join(sorted(valid_tiers))}"
            )

        # Security: validate skill name and path
        self.security.validate_skill_name(name)
        tier_dir = valid_tiers[tier]
        self.security.validate_skill_path(skill_path, tier_dir)

        # Bug #6 fix: Validate that SKILL.md exists
        skill_file = skill_path / "SKILL.md"
        if not skill_file.exists():
            raise ValueError(
                f"SKILL.md not found at {skill_file}. "
                f"Cannot register skill without SKILL.md file."
            )

        # Security: validate content, scan bundled scripts, store integrity hash
        content = skill_file.read_text(encoding="utf-8", errors="replace")
        self.security.validate_skill_content(content, name)
        script_warnings = self.security.scan_bundled_scripts(skill_path, name)
        if script_warnings:
            critical_scripts = [
                w for w in script_warnings if w.get("severity") == "critical"
            ]
            if critical_scripts:
                descriptions = [w["description"] for w in critical_scripts]
                raise SkillSecurityError(
                    f"Skill {name}: {len(critical_scripts)} critical security "
                    f"violation(s) in bundled scripts. Skill rejected.\n"
                    + "\n".join(f"  - {d}" for d in descriptions)
                )
            logger.warning(
                f"Skill {name}: {len(script_warnings)} bundled script "
                f"warning(s) detected during registration"
            )
        self.security.store_integrity_hash(name, content)

        metadata = SkillMetadata(
            name=name,
            description=description,
            tier=tier,
            path=str(skill_path),
            task_types=task_types
        )

        # Add to index
        self.index["tiers"][tier]["skills"][name] = {
            "description": description,
            "task_types": task_types,
            "path": str(skill_path),
            "usage_count": 0,
            "avg_score": 0.0,
            "created_at": metadata.created_at
        }

        # Save metadata file for local/temp skills
        if tier in ["local", "temp"]:
            metadata_path = skill_path / "metadata.json"
            with open(metadata_path, 'w') as f:
                json.dump(asdict(metadata), f, indent=2)

        self._save_index()
        logger.info(f"✅ Registered {tier} skill: {name}")

        return metadata

    def track_usage(self, skill_name: str, quality_score: int):
        """
        Track skill usage and update metrics.

        Automatically promotes temp skills to local if they meet criteria:
        - Used 3+ times
        - Average score >= 85

        Args:
            skill_name: Name of the skill
            quality_score: Quality score (0-100) from this usage
        """
        # Find which tier the skill is in
        tier = None
        for t in ["official", "local", "temp"]:
            if skill_name in self.index["tiers"][t]["skills"]:
                tier = t
                break

        if not tier:
            logger.warning(f"⚠️  Skill not found in index: {skill_name}")
            return

        # Update usage metrics in index
        skill_data = self.index["tiers"][tier]["skills"][skill_name]
        skill_data["usage_count"] = skill_data.get("usage_count", 0) + 1

        if "scores" not in skill_data:
            skill_data["scores"] = []
        skill_data["scores"].append(quality_score)
        skill_data["avg_score"] = sum(skill_data["scores"]) / len(skill_data["scores"])
        skill_data["last_used"] = datetime.utcnow().isoformat() + "Z"

        # Update metadata file for local/temp skills
        if tier in ["local", "temp"]:
            skill_path = Path(skill_data["path"])
            metadata_path = skill_path / "metadata.json"
            if metadata_path.exists():
                with open(metadata_path, 'r') as f:
                    metadata = json.load(f)
                metadata["usage_count"] = skill_data["usage_count"]
                metadata["scores"] = skill_data["scores"]
                metadata["avg_score"] = skill_data["avg_score"]
                metadata["last_used"] = skill_data["last_used"]
                with open(metadata_path, 'w') as f:
                    json.dump(metadata, f, indent=2)
            else:
                # Bug #4 fix: Warn when metadata file is missing
                logger.warning(
                    f"⚠️  Metadata file missing for {skill_name}, "
                    f"index-only tracking. Path: {metadata_path}"
                )

        self._save_index()

        # Check for auto-promotion (temp -> local) with security gate
        if tier == "temp":
            if (skill_data["usage_count"] >= self.MIN_USAGE_FOR_PROMOTION and
                skill_data["avg_score"] >= self.MIN_AVG_SCORE_FOR_PROMOTION):
                approved, reason = self.security.gate_promotion(
                    skill_name,
                    skill_data["usage_count"],
                    skill_data["avg_score"]
                )
                if approved:
                    self.promote_skill(skill_name)
                else:
                    logger.info(
                        f"⏳ Skill {skill_name} meets promotion criteria "
                        f"but is gated: {reason}"
                    )

        logger.info(
            f"📊 Tracked usage for {skill_name}: "
            f"uses={skill_data['usage_count']}, "
            f"avg_score={skill_data['avg_score']:.1f}"
        )

    def promote_skill(self, skill_name: str):
        """
        Promote a skill from temp to local tier.

        Args:
            skill_name: Name of the skill to promote
        """
        # Check if skill exists in temp
        if skill_name not in self.index["tiers"]["temp"]["skills"]:
            logger.warning(f"⚠️  Cannot promote: {skill_name} not in temp tier")
            return

        # Move directory
        source = self.temp_dir / skill_name
        dest = self.local_dir / skill_name

        if not source.exists():
            logger.error(f"❌ Source directory not found: {source}")
            # Bug #3 fix: Clean up orphaned index entry
            self.index["tiers"]["temp"]["skills"].pop(skill_name, None)
            self._save_index()
            return

        # Security: re-verify content integrity before promotion
        skill_file = source / "SKILL.md"
        if skill_file.exists():
            content = skill_file.read_text(encoding="utf-8", errors="replace")
            if not self.security.verify_integrity(skill_name, content):
                logger.error(
                    f"❌ Integrity check failed for {skill_name} during "
                    f"promotion. Content may have been tampered with."
                )
                return

        if dest.exists():
            logger.warning(f"⚠️  Destination already exists: {dest}, skipping promotion")
            return

        shutil.move(str(source), str(dest))

        # Update index
        skill_data = self.index["tiers"]["temp"]["skills"].pop(skill_name, None)
        if skill_data is None:
            logger.warning(f"Skill {skill_name} disappeared from temp index during promotion")
            return
        skill_data["tier"] = "local"
        skill_data["path"] = str(dest)
        skill_data["promoted_at"] = datetime.utcnow().isoformat() + "Z"
        self.index["tiers"]["local"]["skills"][skill_name] = skill_data

        # Update metadata file
        metadata_path = dest / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
            metadata["tier"] = "local"
            metadata["path"] = str(dest)
            metadata["promoted_at"] = skill_data["promoted_at"]
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)

        self._save_index()

        logger.info(
            f"🎉 Promoted {skill_name} to local tier! "
            f"(uses={skill_data['usage_count']}, avg_score={skill_data['avg_score']:.1f})"
        )

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

    def cleanup_temp(self):
        """
        TTL-based cleanup for temp and official skill caches.

        Ephemeral (temp) skills are retained across sessions and evicted
        only after TEMP_SKILL_TTL_DAYS of inactivity.  This preserves
        novel generated skills so they can be reused without regeneration.

        Official (GitHub-cached) skills are evicted after
        OFFICIAL_SKILL_TTL_DAYS of inactivity since they can always be
        re-fetched from the remote repository.

        Called at the end of each session by SkillCleanupNode.
        """
        now = datetime.utcnow()

        temp_evicted = self._evict_stale_from_tier(
            "temp", self.temp_dir, self.TEMP_SKILL_TTL_DAYS, now
        )
        official_evicted = self._evict_stale_from_tier(
            "official", self.official_dir, self.OFFICIAL_SKILL_TTL_DAYS, now
        )

        # Sweep orphaned directories (on disk but not in index) left by crashes
        orphans_cleaned = self._sweep_orphaned_dirs(
            self.temp_dir, self.index["tiers"]["temp"]["skills"]
        )

        if temp_evicted or official_evicted:
            self._save_index()

        if temp_evicted:
            logger.info(f"🧹 Evicted {temp_evicted} stale temp skill(s)")
        if official_evicted:
            logger.info(f"🧹 Evicted {official_evicted} stale official skill(s)")
        if orphans_cleaned:
            logger.info(f"🧹 Removed {orphans_cleaned} orphaned temp director(ies)")

        # Log what was retained
        temp_remaining = len(self.index["tiers"]["temp"]["skills"])
        if temp_remaining:
            logger.info(f"♻️ Retained {temp_remaining} temp skill(s) (within TTL)")

    def _evict_stale_from_tier(
        self, tier: str, tier_dir: Path, ttl_days: int, now: datetime
    ) -> int:
        """
        Evict skills from a tier that haven't been used within ttl_days.

        Uses last_used timestamp, falling back to created_at for skills
        that were cached but never used in a session.

        Args:
            tier: Index tier key ("temp", "official", etc.)
            tier_dir: Filesystem directory for this tier
            ttl_days: Maximum days since last use before eviction
            now: Current UTC timestamp

        Returns:
            Number of skills evicted.
        """
        tier_skills = self.index["tiers"][tier]["skills"]
        to_evict: list = []

        for skill_name, skill_data in tier_skills.items():
            last_used_str = (
                skill_data.get("last_used")
                or skill_data.get("synced_at")
                or skill_data.get("created_at", "")
            )
            if not last_used_str:
                to_evict.append(skill_name)
                continue

            try:
                last_used = datetime.fromisoformat(last_used_str.rstrip("Z"))
                age_days = (now - last_used).days
                if age_days > ttl_days:
                    to_evict.append(skill_name)
            except (ValueError, TypeError):
                # Unparseable timestamp — evict to be safe
                to_evict.append(skill_name)

        for skill_name in to_evict:
            skill_dir = tier_dir / skill_name
            if skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)
            tier_skills.pop(skill_name, None)
            # Clean up orphaned integrity hash for this skill
            self.security.remove_integrity_hash(skill_name)
            logger.debug(f"Evicted stale {tier} skill: {skill_name}")

        return len(to_evict)

    @staticmethod
    def _sweep_orphaned_dirs(tier_dir: Path, index_skills: Dict) -> int:
        """
        Remove directories on disk that have no corresponding index entry.

        Catches orphans left by crashes between disk write and index save.

        Args:
            tier_dir: Filesystem directory for the tier (e.g. temp/)
            index_skills: The index dict for that tier's skills

        Returns:
            Number of orphaned directories removed.
        """
        if not tier_dir.exists():
            return 0

        count = 0
        for entry in tier_dir.iterdir():
            if entry.is_dir() and entry.name not in index_skills:
                shutil.rmtree(entry, ignore_errors=True)
                logger.debug(f"Removed orphaned directory: {entry}")
                count += 1
        return count

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

    def approve_promotion(self, skill_name: str) -> bool:
        """
        Approve a pending skill promotion and execute it.

        Returns True if the skill was promoted.
        """
        if self.security.approve_promotion(skill_name):
            self.promote_skill(skill_name)
            return True
        return False

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

    def get_stats(self) -> Dict[str, Any]:
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
