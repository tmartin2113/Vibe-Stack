"""
Skill Loader Node - Loads discovered skills for use by specialists.

This node loads the SKILL.md content for all discovered skills and
makes it available in the agent state for specialists to use.

Supports progressive disclosure: when multiple skills are loaded,
the primary skill gets full content while secondary skills are
summarized as name+description to conserve context budget.
"""

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Optional
import logging
from .state import AgentState
from .skill_registry import SkillRegistry

logger = logging.getLogger(__name__)


class SkillLoaderNode:
    """
    Loads skill content for use by specialist nodes.

    After skills are discovered (and possibly generated), this node
    loads the SKILL.md content and adds it to the state so specialists
    can use the skill instructions.
    """

    def __init__(self, skill_registry: SkillRegistry):
        """
        Initialize skill loader.

        Args:
            skill_registry: Shared SkillRegistry instance
        """
        self.name = "skill_loader"
        self.skill_registry = skill_registry

    def execute(self, state: AgentState) -> AgentState:
        """
        Load skill content for all discovered skills.

        Args:
            state: Current agent state with discovered_skills

        Returns:
            Updated state with loaded_skills containing skill content
        """
        # Scan workspace and any connected skill repos before loading.
        # This populates the workspace tier so find_skill() can match against
        # project-specific skills on this run.
        self._scan_skill_sources(state)

        discovered_skills = state.get("discovered_skills", [])

        if not discovered_skills:
            logger.info("No skills to load")
            return state

        loaded_skills = []
        failed_loads = []

        for skill_info in discovered_skills:
            skill_name = skill_info.get("skill_name")

            # Skip ephemeral skills that haven't been generated yet
            if not skill_name:
                continue

            # Load skill content
            skill_content = self.skill_registry.load_skill(skill_name)

            if skill_content:
                # Compute allowed tools from the already-loaded content
                # (avoids re-reading SKILL.md and re-verifying integrity).
                # Pass source trust_level and default_tools from index.
                index_data = self._get_skill_index_data(skill_name)
                trust_level = index_data.get("trust_level", "standard")
                default_tools = index_data.get("default_allowed_tools", "")
                allowed_tools = self.skill_registry.security.parse_allowed_tools(
                    skill_content,
                    trust_level=trust_level,
                    default_tools_override=default_tools,
                )
                # Parse optional quality criteria for the critic
                quality_criteria = self.skill_registry.security.parse_quality_criteria(
                    skill_content
                )

                # Parse optional adapter overrides from SKILL.md frontmatter.
                # These allow skills to define the agent's persona and
                # generation parameters, making agent type spec-driven
                # rather than hardcoded.
                adapter_prompt = self.skill_registry.security.parse_adapter_prompt(
                    skill_content
                )
                generation_config = self.skill_registry.security.parse_generation_config(
                    skill_content
                )
                tools_enabled = self.skill_registry.security.parse_tools_enabled(
                    skill_content
                )

                # Parse task-types from frontmatter (allows skills to
                # declare custom types beyond the router's defaults)
                frontmatter_task_types = self.skill_registry.security.parse_task_types(
                    skill_content
                )

                loaded_skills.append({
                    "name": skill_name,
                    "tier": skill_info.get("tier", "unknown"),
                    "task_type": skill_info.get("task_type", "general"),
                    "task_types": frontmatter_task_types,
                    "content": skill_content,
                    "path": skill_info.get("skill_path", ""),
                    "allowed_tools": allowed_tools,
                    "quality_criteria": quality_criteria,
                    "adapter_prompt": adapter_prompt,
                    "generation_config": generation_config,
                    "tools_enabled": tools_enabled,
                })
                logger.info(f"📚 Loaded skill: {skill_name} ({skill_info.get('tier', 'unknown')} tier)")
            else:
                failed_loads.append(skill_name)
                logger.warning(f"⚠️  Failed to load skill: {skill_name}")

        # Add loaded skills to state
        state["loaded_skills"] = loaded_skills

        # Log loading results
        debug_info = state.get("debug_info", {})
        debug_info["skill_loading"] = {
            "attempted": len(discovered_skills),
            "loaded": len(loaded_skills),
            "failed": len(failed_loads),
            "failed_skills": failed_loads
        }
        state["debug_info"] = debug_info

        logger.info(
            f"✅ Loaded {len(loaded_skills)}/{len(discovered_skills)} skills"
        )

        return state

    def get_skills_for_task(
        self,
        state: AgentState,
        task_type: str
    ) -> List[Dict[str, Any]]:
        """
        Get loaded skills relevant to a specific task type.

        Args:
            state: Current agent state
            task_type: Task type to filter by

        Returns:
            List of loaded skills matching the task type
        """
        loaded_skills = state.get("loaded_skills", [])

        return [
            skill for skill in loaded_skills
            if skill["task_type"] == task_type
        ]

    def format_skills_for_context(
        self,
        skills: List[Dict[str, Any]],
        max_length: int = 2000
    ) -> str:
        """
        Format skills for inclusion in specialist context.

        Uses progressive disclosure: when multiple skills are loaded,
        the primary (first) skill gets 70% of the context budget with
        full content, while secondary skills are listed as summaries.

        Args:
            skills: List of loaded skills
            max_length: Maximum length of formatted output

        Returns:
            Formatted skill content for context
        """
        if not skills:
            return ""

        if len(skills) == 1:
            # Single skill - include full content (up to max_length)
            skill = skills[0]
            content = skill["content"][:max_length]
            if len(skill["content"]) > max_length:
                content += "\n\n[...content truncated...]"

            return f"""
# Skill: {skill['name']}

{content}
"""

        # Multiple skills — progressive disclosure:
        # Primary skill gets 70% budget with full content,
        # secondary skills get name+description summaries.
        primary = skills[0]
        primary_budget = int(max_length * 0.7)
        primary_content = primary["content"][:primary_budget]
        if len(primary["content"]) > primary_budget:
            primary_content += "\n\n[...content truncated...]"

        formatted = f"# Primary Skill: {primary['name']}\n\n{primary_content}"

        if len(skills) > 1:
            formatted += "\n\n# Additional Skills (available on request)\n\n"
            for skill in skills[1:]:
                desc = self._extract_description(skill["content"])
                formatted += (
                    f"- **{skill['name']}** ({skill['tier']}): {desc}\n"
                )

        return formatted

    def _scan_skill_sources(self, state: AgentState) -> None:
        """
        Scan all workspace and connected skill repo directories.

        Sources (in priority order):
          1. state["workspace_dir"]   — the project the agent is working on
          2. state["skill_repo_dirs"] — explicitly configured skill repos
          3. VIBE_SKILL_REPOS env var — colon-separated list of extra paths
        """
        scanned: List[str] = []

        dirs_to_scan: List[str] = []

        # 1. Current task's project directory
        workspace = state.get("workspace_dir", "")
        if workspace:
            dirs_to_scan.append(workspace)

        # 2. Explicitly configured skill repos (set by orchestrator or task)
        for repo_dir in state.get("skill_repo_dirs", []):
            if repo_dir and repo_dir not in dirs_to_scan:
                dirs_to_scan.append(repo_dir)

        # 3. Environment-variable overrides (useful for always-on shared repos)
        env_repos = os.environ.get("VIBE_SKILL_REPOS", "")
        for path in env_repos.split(":"):
            path = path.strip()
            if path and path not in dirs_to_scan:
                dirs_to_scan.append(path)

        for dir_path in dirs_to_scan:
            count = self.skill_registry.scan_workspace(Path(dir_path))
            if count:
                scanned.append(f"{dir_path} ({count})")

        if scanned:
            logger.info(f"🗂️  Workspace skills loaded from: {', '.join(scanned)}")

    def _get_skill_index_data(self, skill_name: str) -> Dict[str, Any]:
        """Get index data for a skill, including source provenance."""
        # Workspace tier — use standard defaults (validated at registration time)
        if skill_name in self.skill_registry._workspace_skills:
            return {"trust_level": "standard", "default_allowed_tools": ""}
        for tier in ("official", "local", "temp"):
            tier_skills = self.skill_registry.index.get(
                "tiers", {}
            ).get(tier, {}).get("skills", {})
            if skill_name in tier_skills:
                return tier_skills[skill_name]
        return {}

    @staticmethod
    def _extract_description(content: str) -> str:
        """Extract description from SKILL.md frontmatter."""
        if not content.startswith("---"):
            return content[:100].split("\n")[0]
        try:
            end = content.index("---", 3)
        except ValueError:
            return content[:100].split("\n")[0]
        for line in content[3:end].split("\n"):
            stripped = line.strip()
            if stripped.startswith("description:"):
                desc = stripped[len("description:"):].strip().strip('"').strip("'")
                return desc[:200]
        # Fallback: first non-empty line after frontmatter
        body = content[end + 3:].strip()
        first_line = body.split("\n")[0] if body else ""
        return first_line[:200]

    @staticmethod
    def extract_metadata(skill_name: str, skill_path: Path) -> Dict[str, str]:
        """
        Extract name + description from SKILL.md frontmatter.

        Reads only the first 2KB to avoid loading full content.
        Used for progressive disclosure routing decisions.
        """
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            return {"name": skill_name, "description": ""}
        try:
            with open(skill_md, "r", encoding="utf-8", errors="replace") as f:
                header = f.read(2048)
        except OSError:
            return {"name": skill_name, "description": ""}

        meta = SkillRegistry._parse_frontmatter(header)
        return {
            "name": meta.get("name", skill_name),
            "description": meta.get("description", ""),
        }

    @staticmethod
    def execute_skill_script(
        skill_path: Path,
        script_name: str,
        sandbox_pool: Any = None,
    ) -> Optional[str]:
        """
        Execute a script from a skill's scripts/ directory inside OpenSandbox.

        Args:
            skill_path: Root directory of the skill.
            script_name: Filename within scripts/ to execute.
            sandbox_pool: SandboxPoolManager for containerized execution.

        Returns:
            Script stdout on success, None if script doesn't exist or fails.
        """
        script_path = skill_path / "scripts" / script_name
        if not script_path.exists():
            return None

        if sandbox_pool is None:
            logger.error(f"Cannot execute skill script {script_name}: sandbox_pool is required")
            return None

        try:
            handle = sandbox_pool.acquire()
            try:
                result = handle.sandbox.run(str(script_path))
                return result.stdout
            finally:
                sandbox_pool.release(handle)
        except Exception as e:
            logger.warning(f"Script execution failed: {script_name}: {e}")
            return None


def load_skills(state: AgentState, skill_registry: SkillRegistry) -> AgentState:
    """
    Convenience function for graph integration.

    Args:
        state: Current agent state
        skill_registry: Shared SkillRegistry instance

    Returns:
        Updated state with loaded skills
    """
    loader = SkillLoaderNode(skill_registry)
    return loader.execute(state)
