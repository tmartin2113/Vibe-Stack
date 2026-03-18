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


class SkillRegistry:
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

    def __init__(self, genesia_skills_dir: str = "genesia_skills",
                 security: Optional[SkillSecurity] = None,
                 skills_config: Optional[SkillsConfig] = None):
        """
        Initialize the skill registry.

        Args:
            genesia_skills_dir: Root directory for skill tiers.
            security: Optional SkillSecurity instance.  When provided,
                all skill registration, loading, downloading, and
                promotion operations are gated through security checks.
                Defaults to a SkillSecurity with promotion approval enabled.
            skills_config: Optional SkillsConfig with vetted remote sources.
                Defaults to the locked-down 3-source configuration.
        """
        # Bug #5 fix: Resolve to absolute path to handle working directory changes
        self.base_dir = Path(genesia_skills_dir).resolve()
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
            and os.environ.get("GENESIA_DISABLE_REMOTE_SKILLS", "").lower()
                not in ("1", "true", "yes")
        )

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

    def find_skill(self, requirement: str) -> Tuple[str, Optional[str], Optional[Path]]:
        """
        Find best matching skill across all tiers.

        Search order:
        1. Official (local cache) — fast, no network
        2. Local persistent skills — fast, no network
        3. Retained temp skills — ephemeral skills cached from prior sessions
        4. Remote sources in priority order — fetches on demand, caches result
        5. Ephemeral generation — fallback when nothing matches

        Args:
            requirement: Natural language description of what's needed

        Returns:
            Tuple of (tier, skill_name, skill_path)
            - If tier is "ephemeral", skill_name and skill_path are None
        """
        # Tier 1: Check official skills (already cached locally)
        official_match = self._search_tier(requirement, "official")
        if official_match and official_match["confidence"] >= self.OFFICIAL_CONFIDENCE_THRESHOLD:
            skill_name = official_match["name"]
            skill_path = self.official_dir / skill_name
            logger.info(f"📚 Found official skill: {skill_name} (confidence: {official_match['confidence']:.2f})")
            return ("official", skill_name, skill_path)

        # Tier 2: Check local persistent skills
        local_match = self._search_tier(requirement, "local")
        if local_match and local_match["confidence"] >= self.LOCAL_CONFIDENCE_THRESHOLD:
            skill_name = local_match["name"]
            skill_path = self.local_dir / skill_name
            logger.info(f"🏠 Found local skill: {skill_name} (confidence: {local_match['confidence']:.2f})")
            return ("local", skill_name, skill_path)

        # Tier 2.5: Check retained temp skills from previous sessions
        temp_match = self._search_tier(requirement, "temp")
        if temp_match and temp_match["confidence"] >= self.TEMP_CONFIDENCE_THRESHOLD:
            skill_name = temp_match["name"]
            skill_path = self.temp_dir / skill_name
            logger.info(f"♻️ Found retained temp skill: {skill_name} (confidence: {temp_match['confidence']:.2f})")
            return ("temp", skill_name, skill_path)

        # Tier 3: Probe remote sources in priority order
        if self._enable_remote:
            for source in self._skills_config.sources:
                if not source.enabled:
                    continue
                remote_result = self._find_remote_skill(requirement, source)
                if remote_result:
                    skill_name, skill_path = remote_result
                    logger.info(
                        f"🌐 Fetched skill from {source.name}: {skill_name}"
                    )
                    return ("official", skill_name, skill_path)

        # Tier 4: Need to generate ephemeral skill
        logger.info(f"🔧 No existing skill found, will generate ephemeral skill")
        return ("ephemeral", None, None)

    def _search_tier(self, requirement: str, tier: str) -> Optional[Dict[str, Any]]:
        """
        Search for matching skills in a specific tier.

        Uses quality-weighted ranking: keyword match confidence is boosted
        by the skill's historical avg_score and usage_count.  This means
        skills that have performed well in the reinforcement loop are
        preferred over untested or low-scoring alternatives.

        Args:
            requirement: Natural language description
            tier: "official", "local", or "temp"

        Returns:
            Dict with "name" and "confidence" if found, None otherwise
        """
        tier_skills = self.index["tiers"][tier]["skills"]

        if not tier_skills:
            return None

        requirement_lower = requirement.lower()
        best_match = None
        best_score = 0.0

        for skill_name, skill_data in tier_skills.items():
            match_confidence = self._calculate_match_confidence(
                requirement_lower,
                skill_data.get("description", "").lower(),
                skill_data.get("task_types", [])
            )

            # Quality-weighted ranking: factor in historical performance.
            # avg_score (0-100) is normalized to 0-1 and blended with
            # match confidence.  Skills with no usage history get a
            # neutral 0.5 quality factor (no penalty, no boost).
            # usage_count provides a small confidence bonus (log-scaled)
            # to prefer battle-tested skills over untested ones.
            avg_score = skill_data.get("avg_score", 0.0)
            usage_count = skill_data.get("usage_count", 0)

            if usage_count > 0 and avg_score > 0:
                quality_factor = avg_score / 100.0
                # Small usage bonus: log2(usage+1)/10, capped at 0.1
                usage_bonus = min(math.log2(usage_count + 1) / 10.0, 0.1)
            else:
                quality_factor = 0.5  # Neutral for untested skills
                usage_bonus = 0.0

            # Blend: 70% match relevance, 30% quality history
            weighted_score = (match_confidence * 0.7) + (quality_factor * 0.3) + usage_bonus

            if weighted_score > best_score:
                best_score = weighted_score
                best_match = {
                    "name": skill_name,
                    "confidence": min(weighted_score, 1.0),
                    "data": skill_data
                }

        return best_match

    def _calculate_match_confidence(
        self,
        requirement: str,
        description: str,
        task_types: List[str]
    ) -> float:
        """
        Calculate confidence score for a skill match.

        Uses keyword overlap, task type matching, and substring containment.
        Returns score between 0.0 and 1.0.
        """
        import re

        # Early exit: no meaningful input → no match
        if not requirement or not requirement.strip():
            return 0.0

        # Normalize: split on spaces, underscores, hyphens, drop empties
        def tokenize(text: str) -> set:
            return {t for t in re.split(r'[\s_\-/]+', text.lower()) if t}

        req_tokens = tokenize(requirement)
        if not req_tokens:
            return 0.0

        desc_tokens = tokenize(description)
        task_tokens = tokenize(' '.join(task_types))

        # Token overlap
        desc_overlap = len(req_tokens & desc_tokens) / len(req_tokens)
        task_overlap = len(req_tokens & task_tokens) / len(req_tokens)

        # Substring containment bonus: if the requirement appears in
        # the description or task types, boost confidence.
        requirement_clean = requirement.replace("_", " ").replace("-", " ").strip()
        description_clean = description.replace("_", " ").replace("-", " ")
        task_str_clean = " ".join(task_types).replace("_", " ").replace("-", " ").lower()

        substring_bonus = 0.0
        if requirement_clean in description_clean:
            substring_bonus = 0.3
        elif any(tok in description_clean for tok in req_tokens if len(tok) > 3):
            substring_bonus = 0.15
        if requirement_clean in task_str_clean:
            substring_bonus = max(substring_bonus, 0.25)

        # Weighted combination
        confidence = (desc_overlap * 0.5) + (task_overlap * 0.25) + substring_bonus

        return min(confidence, 1.0)

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
    # ------------------------------------------------------------------

    def _github_headers(self) -> Dict[str, str]:
        """Build HTTP headers for GitHub API requests, including auth token."""
        headers = {"User-Agent": "genesia-skill-registry/1.0"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    def _get_remote_catalog(
        self, source: SkillSourceConfig
    ) -> Dict[str, Dict]:
        """
        Fetch (or return cached) catalog of skills from a remote source.

        The catalog maps skill names to their metadata (description, inferred
        task types).  Results are cached per-source with independent TTLs;
        fetch failures are NOT cached so the next call retries.

        Args:
            source: The remote skill source to query.

        Returns:
            Dict mapping skill_name -> {"description": str, "task_types": [...]}
        """
        # Return cached catalog if still fresh
        cached = self._source_caches.get(source.name)
        if cached:
            age = (datetime.utcnow() - cached["fetched_at"]).total_seconds()
            if age < source.catalog_ttl_seconds:
                return cached["catalog"]

        catalog: Dict[str, Dict] = {}
        try:
            url = (
                f"https://api.github.com/repos/{source.repo}"
                f"/contents/{source.skills_path}"
            )
            headers = self._github_headers()

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                entries = json.loads(resp.read())

            # Skip skills we already have locally
            local_official = set(
                d.name for d in self.official_dir.iterdir() if d.is_dir()
            ) if self.official_dir.exists() else set()

            for entry in entries:
                if entry.get("type") == "dir":
                    name = entry["name"]
                    if name in local_official:
                        continue
                    # Security: validate remote skill names
                    try:
                        self.security.validate_skill_name(name)
                    except SkillSecurityError:
                        logger.warning(
                            f"Skipping remote skill with invalid name: {name!r}"
                        )
                        continue
                    catalog[name] = {
                        "description": name.replace("-", " "),
                        "task_types": self._infer_task_types_from_name(name),
                    }

            self._source_caches[source.name] = {
                "catalog": catalog,
                "fetched_at": datetime.utcnow(),
            }
            logger.info(
                f"🌐 Fetched {source.name} catalog: "
                f"{len(catalog)} uncached skills available"
            )

        except Exception as e:
            # Do NOT cache the failure — leave prior cache as-is so
            # the next call retries instead of permanently returning {}.
            logger.warning(
                f"Failed to fetch {source.name} skill catalog: {e}"
            )
            prev = self._source_caches.get(source.name)
            if prev is None:
                return {}
            return prev["catalog"]

        return catalog

    def _find_remote_skill(
        self, requirement: str, source: SkillSourceConfig
    ) -> Optional[Tuple[str, Path]]:
        """
        Search a remote source's catalog for a matching skill.

        If a match is found above the source's confidence threshold,
        downloads the skill's SKILL.md, caches it locally as an official
        skill, updates the index, and returns the result.

        Args:
            requirement: Natural language description of what's needed.
            source: The remote source to search.

        Returns:
            Tuple of (skill_name, skill_path) if found and downloaded,
            None otherwise.
        """
        catalog = self._get_remote_catalog(source)
        if not catalog:
            return None

        requirement_lower = requirement.lower()
        best_name = None
        best_confidence = 0.0

        for skill_name, meta in catalog.items():
            confidence = self._calculate_match_confidence(
                requirement_lower,
                meta["description"].lower(),
                meta["task_types"],
            )
            if confidence > best_confidence:
                best_confidence = confidence
                best_name = skill_name

        if best_name is None or best_confidence < source.confidence_threshold:
            return None

        logger.info(
            f"🌐 Remote match ({source.name}): {best_name} "
            f"(confidence: {best_confidence:.2f})"
        )

        # Download the skill
        return self._download_remote_skill(best_name, source)

    def _download_remote_skill(
        self, skill_name: str, source: SkillSourceConfig
    ) -> Optional[Tuple[str, Path]]:
        """
        Download a single skill from a remote source and cache it locally.

        Downloads SKILL.md and any small supporting files (< 500KB).
        Optionally downloads and scans scripts/ directory.
        Registers the skill in the index with source provenance.

        Args:
            skill_name: Name of the skill directory in the repo.
            source: The remote source configuration.

        Returns:
            Tuple of (skill_name, skill_path) on success, None on failure.
        """
        # Security: validate skill name before using in paths
        try:
            self.security.validate_skill_name(skill_name)
        except SkillSecurityError as e:
            logger.error(f"❌ Remote skill rejected: {e}")
            return None

        skill_dir = self.official_dir / skill_name

        # Security: verify path stays within official dir
        try:
            self.security.validate_skill_path(skill_dir, self.official_dir)
        except SkillSecurityError as e:
            logger.error(f"❌ Path validation failed: {e}")
            return None

        raw_base = (
            f"https://raw.githubusercontent.com/"
            f"{source.repo}/{source.branch}"
            f"/{source.skills_path}/{skill_name}"
        )

        try:
            # Always download SKILL.md first
            skill_md_url = f"{raw_base}/SKILL.md"
            headers = self._github_headers()
            req = urllib.request.Request(skill_md_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                skill_md_content = resp.read()

            # Security: validate downloaded content before writing to disk
            content_str = skill_md_content.decode("utf-8", errors="replace")
            try:
                warnings = self.security.validate_skill_content(
                    content_str, skill_name
                )
                if warnings:
                    logger.warning(
                        f"Remote skill {skill_name} from {source.name} has "
                        f"{len(warnings)} security warning(s) — proceeding "
                        f"with caution"
                    )
            except SkillSecurityError as e:
                logger.error(f"❌ Remote skill content rejected: {e}")
                return None

            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_bytes(skill_md_content)

            # Security: store integrity hash for future verification
            self.security.store_integrity_hash(skill_name, content_str)

            # Try to download supporting files (references/, templates/, etc.)
            self._download_skill_extras(skill_name, skill_dir, source)

            # Download scripts/ directory if enabled
            scripts_had_critical = False
            if self._skills_config.scan_scripts:
                scripts_had_critical = self._download_scripts(
                    skill_name, skill_dir, source
                )

            # Security: scan bundled scripts after downloading extras
            script_warnings = self.security.scan_bundled_scripts(
                skill_dir, skill_name
            )
            critical_scripts = [
                w for w in script_warnings if w.get("severity") == "critical"
            ]
            if critical_scripts or scripts_had_critical:
                # Clean up: remove downloaded directory and stale integrity hash
                shutil.rmtree(skill_dir, ignore_errors=True)
                self.security.remove_integrity_hash(skill_name)
                descriptions = [w["description"] for w in critical_scripts]
                logger.error(
                    f"❌ Remote skill {skill_name} rejected: "
                    f"{len(critical_scripts)} critical script violation(s): "
                    + "; ".join(descriptions)
                )
                return None

            # Parse frontmatter for description and task types
            meta = self._parse_frontmatter(content_str)
            description = meta.get("description", skill_name.replace("-", " "))
            # Prefer explicit task-types from frontmatter over name inference
            frontmatter_types = self.security.parse_task_types(content_str)
            task_types = frontmatter_types or self._infer_task_types_from_name(skill_name)

            # Register in the index with source provenance
            self.index["tiers"]["official"]["skills"][skill_name] = {
                "description": description,
                "task_types": task_types,
                "path": str(skill_dir),
                "usage_count": 0,
                "avg_score": 0.0,
                "created_at": datetime.utcnow().isoformat() + "Z",
                "source": f"https://github.com/{source.repo}",
                "source_name": source.name,
                "trust_level": source.trust_level,
                "synced_at": datetime.utcnow().isoformat() + "Z",
            }
            self._save_index()

            # Remove from source catalog cache (it's local now)
            cached = self._source_caches.get(source.name)
            if cached and skill_name in cached.get("catalog", {}):
                del cached["catalog"][skill_name]

            logger.info(f"✅ Cached {source.name} skill: {skill_name}")
            return (skill_name, skill_dir)

        except Exception as e:
            logger.warning(f"⚠️  Failed to download skill {skill_name}: {e}")
            # Clean up partial download
            if skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)
            return None

    def _download_skill_extras(
        self, skill_name: str, skill_dir: Path, source: SkillSourceConfig
    ):
        """
        Try to download extra files and subdirectories for a skill.

        Uses the GitHub Contents API to enumerate the skill directory,
        then downloads any non-SKILL.md files (including files inside
        subdirectories like references/ and templates/).

        Non-critical — failures are silently ignored.
        """
        headers = self._github_headers()
        self._download_directory_contents(
            skill_name, "", skill_dir, headers, depth=0, source=source
        )

    def _download_scripts(
        self, skill_name: str, skill_dir: Path, source: SkillSourceConfig
    ) -> bool:
        """
        Download scripts/ directory and security-scan all executables.

        Critical findings cause the entire scripts/ directory to be removed.

        Returns:
            True if critical findings were detected (scripts removed).
        """
        scripts_dir = skill_dir / "scripts"
        headers = self._github_headers()
        self._download_directory_contents(
            skill_name, "scripts", scripts_dir, headers,
            depth=0, source=source,
        )
        if scripts_dir.exists() and any(scripts_dir.iterdir()):
            findings = self.security.scan_bundled_scripts(
                scripts_dir, skill_name
            )
            critical = [
                f for f in findings if f.get("severity") == "critical"
            ]
            if critical:
                shutil.rmtree(scripts_dir, ignore_errors=True)
                logger.warning(
                    f"Removed scripts/ from {skill_name}: "
                    f"{len(critical)} critical finding(s)"
                )
                return True
        return False

    def _download_directory_contents(
        self,
        skill_name: str,
        subpath: str,
        local_dir: Path,
        headers: Dict[str, str],
        depth: int,
        source: Optional[SkillSourceConfig] = None,
    ):
        """
        Recursively download directory contents from GitHub.

        Args:
            skill_name: Skill name in the repo
            subpath: Relative path within the skill directory ('' for root)
            local_dir: Local directory to write files into
            headers: HTTP headers (including auth)
            depth: Recursion depth (capped at 3 to prevent runaway)
            source: Remote source config (for URL construction)
        """
        if depth > 3:
            return

        source_repo = source.repo if source else "anthropics/skills"
        source_branch = source.branch if source else "main"
        source_skills_path = source.skills_path if source else "skills"

        api_path = f"{source_skills_path}/{skill_name}"
        if subpath:
            api_path = f"{api_path}/{subpath}"
        url = f"https://api.github.com/repos/{source_repo}/contents/{api_path}"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                entries = json.loads(resp.read())
        except Exception:
            return

        raw_base = (
            f"https://raw.githubusercontent.com/"
            f"{source_repo}/{source_branch}/{api_path}"
        )

        for entry in entries:
            name = entry.get("name", "")
            entry_type = entry.get("type", "")

            # Skip SKILL.md (already downloaded) and large files (> 500KB)
            if name == "SKILL.md" and not subpath:
                continue
            size = entry.get("size", 0)
            if entry_type == "file" and size > 500_000:
                continue

            if entry_type == "file":
                try:
                    file_url = f"{raw_base}/{name}"
                    file_req = urllib.request.Request(file_url, headers=headers)
                    with urllib.request.urlopen(file_req, timeout=10) as file_resp:
                        local_dir.mkdir(parents=True, exist_ok=True)
                        (local_dir / name).write_bytes(file_resp.read())
                except Exception:
                    pass  # Non-critical

            elif entry_type == "dir":
                child_subpath = f"{subpath}/{name}" if subpath else name
                self._download_directory_contents(
                    skill_name, child_subpath,
                    local_dir / name, headers, depth + 1,
                    source=source,
                )

    @staticmethod
    def _parse_frontmatter(content: str) -> Dict[str, str]:
        """
        Extract key-value pairs from YAML frontmatter.

        Handles multiline values using YAML block scalar indicators
        (>-, >, |) by joining continuation lines (indented lines that
        follow a key).
        """
        if not content.startswith("---"):
            return {}
        try:
            end = content.index("---", 3)
        except ValueError:
            return {}

        metadata: Dict[str, str] = {}
        current_key: Optional[str] = None
        current_value_lines: list = []

        for line in content[3:end].strip().split("\n"):
            stripped = line.strip()

            # Top-level key: value (not indented)
            if ":" in line and not line[0].isspace():
                # Save previous key if any
                if current_key is not None:
                    metadata[current_key] = " ".join(current_value_lines).strip()

                key, _, value = stripped.partition(":")
                current_key = key.strip()
                value = value.strip()

                # Skip YAML block scalar indicators (>-, >, |, |-)
                if value in (">-", ">", "|", "|-"):
                    current_value_lines = []
                else:
                    current_value_lines = [value]

            elif current_key is not None and line[0:1].isspace() and stripped:
                # Continuation line for a multiline value
                current_value_lines.append(stripped)

        # Save the last key
        if current_key is not None:
            metadata[current_key] = " ".join(current_value_lines).strip()

        return metadata

    @staticmethod
    def _infer_task_types_from_name(skill_name: str) -> List[str]:
        """Infer task types from a skill name for matching."""
        text = skill_name.replace("-", " ").lower()

        # Keywords are ordered from most specific to least specific.
        # Use multi-word phrases first to avoid short-token collisions
        # (e.g. "doc" previously matched both documentation and document_processing).
        type_keywords = {
            "test_generation": ["testing", "playwright", "webapp testing",
                                "test driven", "tdd"],
            "security_audit": ["security", "audit", "pentest", "vulnerability",
                               "exploit", "recon"],
            "documentation": ["doc coauthoring", "writing", "internal comms"],
            "code_generation": ["code", "builder", "creator"],
            "code_review": ["code review", "receiving code review",
                            "requesting code review"],
            "debugging": ["debugging", "systematic debugging"],
            "planning": ["planning", "brainstorming", "writing plans",
                         "executing plans"],
            "frontend_development": ["frontend", "design", "canvas",
                                     "theme factory", "web artifact", "art",
                                     "brand", "react", "web design"],
            "data_processing": ["xlsx", "spreadsheet", "excel", "csv"],
            "pdf_processing": ["pdf"],
            "presentation": ["pptx", "slides", "presentation"],
            "document_processing": ["docx"],
            "mcp_development": ["mcp"],
            "messaging": ["slack", "gif"],
            "devops": ["git worktree", "worktree", "development branch"],
        }

        matched = []
        for task_type, keywords in type_keywords.items():
            if any(kw in text for kw in keywords):
                matched.append(task_type)
        return matched or ["general"]

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
