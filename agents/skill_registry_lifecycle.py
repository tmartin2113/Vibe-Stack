"""
Skill Registry Lifecycle — Registration, Promotion & Cleanup

Mixin providing skill registration, usage tracking, tier promotion,
and TTL-based eviction. Not intended for standalone use.
"""

import json
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging

from .skill_security import SkillSecurity, SkillSecurityError
from .config import SkillTier

logger = logging.getLogger(__name__)

__all__ = ["SkillRegistryLifecycleMixin"]


class SkillRegistryLifecycleMixin:
    """Lifecycle management methods for SkillRegistry.

    Expects the host class to have:
        self.index: Dict[str, Any]
        self.security: SkillSecurity
        self.official_dir: Path
        self.local_dir: Path
        self.temp_dir: Path
        self._save_index(): method (from SkillRegistryIndexMixin)
        self.MIN_USAGE_FOR_PROMOTION: int
        self.MIN_AVG_SCORE_FOR_PROMOTION: int
        self.TEMP_SKILL_TTL_DAYS: int
        self.OFFICIAL_SKILL_TTL_DAYS: int
    """

    def register_skill(
        self,
        name: str,
        description: str,
        tier: SkillTier,
        task_types: List[str],
        skill_path: Path
    ):
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
        from .skill_registry import SkillMetadata

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

    def approve_promotion(self, skill_name: str) -> bool:
        """
        Approve a pending skill promotion and execute it.

        Returns True if the skill was promoted.
        """
        if self.security.approve_promotion(skill_name):
            self.promote_skill(skill_name)
            return True
        return False

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
