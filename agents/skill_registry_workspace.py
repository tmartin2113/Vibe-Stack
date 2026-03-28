"""
Skill Registry Workspace Tier — Per-Task In-Memory Skills

Mixin providing workspace skill scanning from project directories.
Workspace skills are held in memory only and cleared after each task.
Not intended for standalone use.
"""

import logging
from pathlib import Path
from typing import Any, Dict

from .skill_security import SkillSecurityError

logger = logging.getLogger(__name__)

__all__ = ["SkillRegistryWorkspaceMixin"]


class SkillRegistryWorkspaceMixin:
    """Workspace tier methods for SkillRegistry.

    Expects the host class to have:
        self._workspace_skills: Dict[str, Dict[str, Any]]
        self.security: SkillSecurity
        self._parse_frontmatter(content: str) -> Dict[str, str]
    """

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
        except SkillSecurityError:
            logger.warning(f"Workspace skill rejected (invalid name): {skill_file}")
            return 0

        try:
            self.security.validate_skill_content(content, skill_name)
        except SkillSecurityError as exc:
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
