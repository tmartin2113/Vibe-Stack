"""
Skill Remote Source Operations

Mixin providing GitHub-based remote skill discovery, downloading,
and caching for the SkillRegistry.
"""

import json
import os
import shutil
import urllib.request
import urllib.error
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple, TYPE_CHECKING

from .skill_security import SkillSecurityError

if TYPE_CHECKING:
    from .config import SkillSourceConfig

logger = logging.getLogger(__name__)


class SkillRegistryRemoteMixin:
    """
    Mixin providing remote skill catalog fetching and downloading.

    Expects the composing class to provide:
    - self.security: SkillSecurity instance
    - self.official_dir: Path to official skills directory
    - self.index: dict with tiers structure
    - self._source_caches: dict for catalog caching
    - self._skills_config: SkillsConfig instance
    - self._save_index(): method to persist index
    - self._calculate_match_confidence(): method for matching
    - self._parse_frontmatter(): staticmethod for frontmatter parsing
    - self._infer_task_types_from_name(): staticmethod for type inference
    """

    def _github_headers(self) -> Dict[str, str]:
        """Build HTTP headers for GitHub API requests, including auth token."""
        headers = {"User-Agent": "vibe-skill-registry/1.0"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    def _get_remote_catalog(
        self, source: "SkillSourceConfig"
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
        self, requirement: str, source: "SkillSourceConfig"
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
        self, skill_name: str, source: "SkillSourceConfig"
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
        self, skill_name: str, skill_dir: Path, source: "SkillSourceConfig"
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
        self, skill_name: str, skill_dir: Path, source: "SkillSourceConfig"
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
        source: "Optional[SkillSourceConfig]" = None,
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
