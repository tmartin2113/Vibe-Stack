"""
Skill Security — Hardened security layer for Vibe's skill system.

Prevents:
- Path traversal attacks via malicious skill names
- Prompt injection via malicious SKILL.md content
- Unauthorized tool usage via allowed-tools enforcement
- Integrity tampering via SHA-256 content hashing
- Unreviewed skill promotion via approval gating
- Malicious bundled scripts via regex + AST analysis

Inspired by Cisco's findings on OpenClaw skill exfiltration (Feb 2026):
third-party skills can perform data exfiltration and prompt injection
without user awareness when skill repositories lack adequate vetting.
"""

import ast
import hashlib
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Tool permission sets ──────────────────────────────────────────────

# Safe read-only tools that any skill may use
DEFAULT_ALLOWED_TOOLS: FrozenSet[str] = frozenset({
    "Read", "Glob", "Grep", "WebFetch", "WebSearch",
    "web_search", "web_scrape",  # SearXNG + Playwright (read-only, safe)
    "dependency_scanner",  # Read-only CVE scanning (pip-audit / npm audit)
    "container_inspect",   # Read-only Docker container status/logs/health
    "lighthouse_seo", "page_analyzer", "seo_checklist",  # SEO analysis (read-only)
    "memory_store", "memory_recall",  # Persistent memory (always allowed)
    "bulletin_board",  # Inter-agent bulletin board (shared scratchpad, safe)
})

# Tools that require explicit allowlisting in SKILL.md frontmatter
RESTRICTED_TOOLS: FrozenSet[str] = frozenset({
    "Write", "Edit", "Bash", "NotebookEdit",
    "browser_automation",  # Can interact with external sites
    "database",            # Can query/modify databases
    "design",              # Creates/modifies design files
    "image_generation",    # GPU-intensive
    "git_forge",           # Can create repos/commits
    "artifact_storage",    # Can write/delete objects
})

# All known tool names (union of safe + restricted)
ALL_KNOWN_TOOLS: FrozenSet[str] = DEFAULT_ALLOWED_TOOLS | RESTRICTED_TOOLS

# Trust-level defaults for sources that lack allowed-tools frontmatter
TRUST_LEVEL_DEFAULTS: Dict[str, FrozenSet[str]] = {
    "high": ALL_KNOWN_TOOLS,                           # anthropics: full trust
    "standard": DEFAULT_ALLOWED_TOOLS,                 # obra, vercel: safe read-only
    "restricted": frozenset({"Read", "Glob", "Grep"}), # minimal
}


# ── Content scanning patterns ─────────────────────────────────────────

# Patterns in SKILL.md content that indicate potential prompt injection
# or data exfiltration.  Each entry is (pattern, severity, description).
SUSPICIOUS_PATTERNS: List[Tuple[str, str, str]] = [
    # Prompt injection
    (r"ignore\s+(?:all\s+)?previous\s+instructions", "critical",
     "Prompt injection: ignore previous instructions"),
    (r"you\s+are\s+now\s+(?:a|an)\s+", "critical",
     "Prompt injection: role reassignment"),
    (r"<\s*(?:system|assistant|human)\s*>", "critical",
     "Prompt injection: XML role tag injection"),
    (r"system\s*:\s*\n", "high",
     "Prompt injection: system prompt injection attempt"),

    # Code execution
    (r"(?:subprocess|exec|eval|__import__)\s*\(", "critical",
     "Code execution: dynamic eval/exec/import call"),
    (r"(?:os\.(?:system|popen|exec))\s*\(", "critical",
     "Code execution: os command execution"),
    (r"(?:rm\s+-rf|chmod\s+777|mkfs)\b", "high",
     "Dangerous shell command in skill content"),

    # Data exfiltration
    (r"(?:curl|wget|fetch)\s+https?://(?!(?:api\.github\.com|raw\.githubusercontent\.com))", "high",
     "Potential data exfiltration: outbound HTTP to non-GitHub URL"),
    (r"(?:requests|httpx|aiohttp)\.(?:get|post|put|patch|delete|request)\s*\(", "high",
     "Potential data exfiltration: Python HTTP library call"),
    (r"urllib\.request\.(?:urlopen|Request)\s*\(", "high",
     "Potential data exfiltration: urllib request call"),
    (r"(?:exfiltrat|send\s+(?:data\s+)?to\s+(?:https?|ftp))", "critical",
     "Data exfiltration language detected"),

    # Credential harvesting
    (r"(?:api[_-]?key|password|secret|token|credential)\s*[:=]\s*['\"]", "critical",
     "Credential harvesting: hardcoded secret pattern"),
    (r"(?:env|environ|getenv)\s*\[\s*['\"](?!VIBE_)", "high",
     "Reading non-Vibe environment variables"),
    (r"(?:environ|os\.environ)\.get\s*\(\s*['\"](?!VIBE_)", "high",
     "Reading non-Vibe environment variables via .get()"),
]

# Maximum allowed SKILL.md file size (512 KB)
MAX_SKILL_FILE_SIZE: int = 512 * 1024

# Valid skill name: kebab-case, starts with letter, 1–64 chars.
# An optional ``__v{N}`` suffix (where N is one or more digits) is
# permitted for A/B refinement candidates produced by the Tier 1a
# builder (agents/skill_ab.py). This is the only use of underscores
# in skill names and the only deviation from strict kebab-case.
VALID_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:__v\d+)?$")

# Maximum skill name length
MAX_SKILL_NAME_LENGTH: int = 64


class SkillSecurityError(Exception):
    """Raised when a skill fails security validation."""
    pass


class SkillSecurity:
    """
    Security layer for the skill registry.

    Validates skill content, enforces tool permissions, maintains
    integrity hashes, and gates promotions through approval.
    """

    def __init__(self, require_promotion_approval: bool = True):
        """
        Args:
            require_promotion_approval: If True, temp->local promotion
                requires explicit approval instead of auto-promoting.
        """
        self.require_promotion_approval = require_promotion_approval
        self._pending_promotions: Dict[str, Dict] = {}
        self._integrity_hashes: Dict[str, str] = {}

    # ── Persistence helpers ────────────────────────────────────────────

    def export_state(self) -> Dict:
        """
        Export integrity hashes and pending promotions for persistence.

        Returns a dict suitable for storing in .index.json so that
        hashes and pending promotions survive process restarts.
        """
        return {
            "integrity_hashes": dict(self._integrity_hashes),
            "pending_promotions": dict(self._pending_promotions),
        }

    def import_state(self, state: Dict) -> None:
        """
        Restore integrity hashes and pending promotions from persisted state.

        Args:
            state: Dict previously returned by export_state().
        """
        if not isinstance(state, dict):
            return
        self._integrity_hashes = dict(state.get("integrity_hashes", {}))
        self._pending_promotions = dict(state.get("pending_promotions", {}))

    # ── Name validation ───────────────────────────────────────────────

    @staticmethod
    def _extract_frontmatter(content: str) -> Optional[str]:
        """
        Extract YAML frontmatter text from content.

        The closing ``---`` must appear on its own line (with optional
        trailing whitespace) to avoid matching ``---`` embedded inside
        YAML string values.

        Returns:
            The frontmatter text (between delimiters) or None.
        """
        if not content.startswith("---"):
            return None
        lines = content.split("\n")
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[1:i])
        return None

    def validate_skill_name(self, name: str) -> None:
        """
        Validate a skill name to prevent path traversal and injection.

        Raises:
            SkillSecurityError: If name is invalid or dangerous.
        """
        if not name:
            raise SkillSecurityError("Skill name cannot be empty")

        if ".." in name or "/" in name or "\\" in name:
            raise SkillSecurityError(
                f"Skill name contains path traversal characters: {name!r}"
            )

        if "\x00" in name:
            raise SkillSecurityError(
                f"Skill name contains null bytes: {name!r}"
            )

        if len(name) > MAX_SKILL_NAME_LENGTH:
            raise SkillSecurityError(
                f"Skill name too long ({len(name)} > {MAX_SKILL_NAME_LENGTH}): "
                f"{name!r}"
            )

        if not VALID_SKILL_NAME_RE.match(name):
            raise SkillSecurityError(
                f"Invalid skill name: {name!r}. "
                f"Must be kebab-case (e.g., 'my-skill-name'), "
                f"start with a letter, and contain only a-z, 0-9, hyphens. "
                f"An optional '__v{{N}}' suffix is permitted for Tier 1a "
                f"A/B refinement candidates (e.g., 'my-skill-name__v2')."
            )

    # ── Path validation ───────────────────────────────────────────────

    def validate_skill_path(self, skill_path: Path, expected_base: Path) -> None:
        """
        Validate that a skill path is within the expected base directory.

        Prevents path traversal where a crafted skill name resolves
        outside the skills directory via symlinks or relative segments.

        Raises:
            SkillSecurityError: If path escapes the base directory.
        """
        try:
            resolved = skill_path.resolve()
            base = expected_base.resolve()
            resolved.relative_to(base)
        except ValueError:
            raise SkillSecurityError(
                f"Path traversal detected: {skill_path} escapes "
                f"base directory {expected_base}"
            )

    # ── Content validation ────────────────────────────────────────────

    def validate_skill_content(
        self, content: str, skill_name: str
    ) -> List[Dict[str, str]]:
        """
        Scan SKILL.md content for suspicious patterns.

        Args:
            content: Raw SKILL.md content.
            skill_name: Name of the skill (for logging).

        Returns:
            List of warning dicts with keys: pattern, severity, description.

        Raises:
            SkillSecurityError: If content contains critical violations
                or exceeds the size limit.
        """
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_SKILL_FILE_SIZE:
            raise SkillSecurityError(
                f"Skill {skill_name}: SKILL.md exceeds maximum size "
                f"({len(content_bytes):,} > {MAX_SKILL_FILE_SIZE:,} bytes)"
            )

        warnings: List[Dict[str, str]] = []
        critical_count = 0

        for pattern, severity, description in SUSPICIOUS_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                warning = {
                    "pattern": pattern,
                    "severity": severity,
                    "description": description,
                }
                warnings.append(warning)
                logger.warning(
                    f"Skill {skill_name}: [{severity.upper()}] {description}"
                )
                if severity == "critical":
                    critical_count += 1

        # Any critical finding → reject immediately
        if critical_count > 0:
            descriptions = [
                w["description"] for w in warnings if w["severity"] == "critical"
            ]
            raise SkillSecurityError(
                f"Skill {skill_name}: {critical_count} critical security "
                f"violation(s) detected. Skill rejected.\n"
                + "\n".join(f"  - {d}" for d in descriptions)
            )

        # Multiple high-severity findings → also reject
        high_count = sum(1 for w in warnings if w["severity"] == "high")
        if high_count >= 3:
            raise SkillSecurityError(
                f"Skill {skill_name}: {high_count} high-severity warnings "
                f"detected (threshold: 3). Skill rejected for safety."
            )

        return warnings

    # ── Allowed-tools enforcement ─────────────────────────────────────

    def parse_allowed_tools(
        self,
        content: str,
        trust_level: str = "standard",
        default_tools_override: str = "",
    ) -> Set[str]:
        """
        Parse the allowed-tools field from SKILL.md frontmatter.

        Args:
            content: Full SKILL.md content.
            trust_level: Source trust level ("high", "standard", "restricted").
                Used to select default tools when allowed-tools is absent.
            default_tools_override: Space-separated tool names to use as default
                when allowed-tools is absent.  Takes priority over trust_level.

        Returns:
            Set of validated tool names.  Falls back to trust-level defaults
            (or override) if the field is absent.
        """
        fallback = self._resolve_tool_defaults(trust_level, default_tools_override)

        frontmatter = self._extract_frontmatter(content)
        if frontmatter is None:
            return fallback
        for line in frontmatter.split("\n"):
            stripped = line.strip()
            if stripped.startswith("allowed-tools:"):
                tools_str = stripped[len("allowed-tools:"):].strip()
                # Bug #6 fix: Empty "allowed-tools:" means no tools,
                # distinct from absent field which gets defaults.
                if not tools_str:
                    return set()
                declared = {t.strip() for t in tools_str.split() if t.strip()}
                validated = declared & ALL_KNOWN_TOOLS
                unknown = declared - ALL_KNOWN_TOOLS
                if unknown:
                    logger.warning(
                        f"Ignoring unknown tools in allowed-tools: {unknown}"
                    )
                return validated

        return fallback

    @staticmethod
    def _resolve_tool_defaults(
        trust_level: str, default_tools_override: str
    ) -> Set[str]:
        """Resolve default allowed tools from override string or trust level."""
        if default_tools_override:
            declared = {t.strip() for t in default_tools_override.split() if t.strip()}
            return declared & ALL_KNOWN_TOOLS
        return set(TRUST_LEVEL_DEFAULTS.get(trust_level, DEFAULT_ALLOWED_TOOLS))

    def parse_quality_criteria(self, content: str) -> Optional[List[str]]:
        """
        Parse the quality-criteria field from SKILL.md frontmatter.

        Returns:
            List of criteria strings if declared, or None if absent.
            An empty ``quality-criteria:`` line returns an empty list
            (meaning "no custom criteria"), distinct from None (absent,
            meaning "use hardcoded defaults").
        """
        frontmatter = self._extract_frontmatter(content)
        if frontmatter is None:
            return None

        # Look for the quality-criteria key.  Supports two formats:
        #   1. Inline:  quality-criteria: Criterion A | Criterion B
        #   2. Block:   quality-criteria: (followed by indented lines)
        found_key = False
        inline_value = ""
        block_lines: List[str] = []

        for line in frontmatter.split("\n"):
            stripped = line.strip()

            if stripped.startswith("quality-criteria:"):
                found_key = True
                inline_value = stripped[len("quality-criteria:"):].strip()
                # Skip YAML block indicators
                if inline_value in (">-", ">", "|", "|-"):
                    inline_value = ""
                continue

            # Collect indented continuation lines (block format)
            if found_key and line[:1].isspace() and stripped:
                # Strip optional leading "- " for YAML list syntax
                if stripped.startswith("- "):
                    stripped = stripped[2:].strip()
                block_lines.append(stripped)
                continue

            # A non-indented line after the key ends the block
            if found_key and not line[:1].isspace():
                break

        if not found_key:
            return None

        # Inline format: pipe-separated
        if inline_value:
            return [c.strip() for c in inline_value.split("|") if c.strip()]

        # Block format
        if block_lines:
            return block_lines

        # Key present but empty value — explicit "no custom criteria"
        return []

    def parse_adapter_prompt(self, content: str) -> Optional[str]:
        """
        Parse the adapter-prompt field from SKILL.md frontmatter.

        When a skill declares an adapter-prompt, it overrides the hardcoded
        specialist system prompt — allowing the orchestrator + skills to
        define arbitrary agent types without code changes.

        Returns:
            The adapter prompt string if declared, or None if absent.
        """
        return self._parse_frontmatter_string(content, "adapter-prompt")

    def validate_adapter_prompt(self, prompt: str, skill_name: str) -> bool:
        """
        Scan an adapter-prompt override for injection patterns.

        The override is used verbatim as a specialist system prompt, so it
        must be re-scanned at load time even though the surrounding SKILL.md
        passed validation at registration.  This catches the narrow case
        where a tampered or poisoned frontmatter string escaped the initial
        scan (e.g. via an encoding trick).

        Returns:
            True if the prompt is safe; False if any critical or high
            severity SUSPICIOUS_PATTERNS match.  On False, callers should
            drop the override and fall back to the hardcoded adapter prompt
            — the rest of the skill is still usable.
        """
        if not prompt:
            return True
        for pattern, severity, description in SUSPICIOUS_PATTERNS:
            if severity in ("critical", "high") and re.search(
                pattern, prompt, re.IGNORECASE
            ):
                logger.warning(
                    f"Skill {skill_name}: adapter-prompt override rejected "
                    f"[{severity.upper()}] {description}"
                )
                return False
        return True

    def parse_generation_config(self, content: str) -> Optional[Dict[str, float]]:
        """
        Parse the generation-config field from SKILL.md frontmatter.

        Allows skills to declare generation parameters (temperature,
        max_tokens, top_p) that override per-specialist defaults.

        Returns:
            Dict of generation parameters if declared, or None if absent.
        """
        raw = self._parse_frontmatter_string(content, "generation-config")
        if raw is None:
            return None

        config: Dict[str, float] = {}
        # Format: "temperature=0.3 max_tokens=2000 top_p=0.9"
        for pair in raw.split():
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            key = key.strip()
            value_str = value.strip()
            # Only allow known safe generation parameters
            if key not in ("temperature", "max_tokens", "top_p", "top_k"):
                logger.warning(f"Ignoring unknown generation-config key: {key!r}")
                continue
            try:
                config[key] = float(value_str)
            except ValueError:
                logger.warning(f"Ignoring non-numeric generation-config value: {key}={value_str!r}")
        return config if config else None

    def parse_task_types(self, content: str) -> Optional[List[str]]:
        """
        Parse the task-types field from SKILL.md frontmatter.

        Allows skills to declare which task types they handle, expanding
        the router's vocabulary beyond the 12 hardcoded types.  Skills
        can register arbitrary types (e.g., "ml_pipeline", "infrastructure")
        that the router will accept as valid classification targets.

        Supports two formats:
          - Inline:  task-types: ml_pipeline infrastructure_as_code
          - Block:   task-types: (followed by indented lines)

        Returns:
            List of task type strings if declared, or None if absent.
        """
        raw = self._parse_frontmatter_string(content, "task-types")
        if raw is None:
            return None

        # Space-separated inline format
        types = [t.strip() for t in raw.split() if t.strip()]
        return types if types else None

    def parse_tools_enabled(self, content: str) -> Optional[bool]:
        """
        Parse the tools-enabled field from SKILL.md frontmatter.

        Allows skills to explicitly opt in or out of tool access for
        their specialist, regardless of the hardcoded tool_enabled_specialists set.

        Returns:
            True/False if declared, or None if absent (use default behavior).
        """
        raw = self._parse_frontmatter_string(content, "tools-enabled")
        if raw is None:
            return None
        return raw.lower() in ("true", "yes", "1")

    def _parse_frontmatter_string(self, content: str, key: str) -> Optional[str]:
        """
        Extract a single string value from SKILL.md frontmatter by key.

        Supports multiline values via YAML block scalar indicators (>-, |)
        and indented continuation lines.

        Returns:
            The string value if the key is found, or None if absent.
        """
        frontmatter = self._extract_frontmatter(content)
        if frontmatter is None:
            return None

        found_key = False
        inline_value = ""
        block_lines: List[str] = []

        for line in frontmatter.split("\n"):
            stripped = line.strip()

            if stripped.startswith(f"{key}:"):
                found_key = True
                inline_value = stripped[len(f"{key}:"):].strip()
                # Skip YAML block indicators
                if inline_value in (">-", ">", "|", "|-"):
                    inline_value = ""
                continue

            # Collect indented continuation lines (block format)
            if found_key and line[:1].isspace() and stripped:
                block_lines.append(stripped)
                continue

            # A non-indented line after the key ends the block
            if found_key and not line[:1].isspace():
                break

        if not found_key:
            return None

        if inline_value:
            return inline_value

        if block_lines:
            return "\n".join(block_lines)

        # Key present but empty value
        return ""

    def check_tool_permission(
        self, skill_name: str, tool_name: str, allowed_tools: Set[str]
    ) -> bool:
        """
        Check whether a skill is permitted to use a given tool.

        Returns:
            True if permitted, False otherwise.
        """
        if tool_name in allowed_tools:
            return True

        logger.warning(
            f"BLOCKED: Skill {skill_name!r} attempted to use disallowed "
            f"tool {tool_name!r}. Allowed: {sorted(allowed_tools)}"
        )
        return False

    # ── Integrity hashing ─────────────────────────────────────────────

    def compute_integrity_hash(self, content: str) -> str:
        """Compute SHA-256 hex digest of skill content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def store_integrity_hash(self, skill_name: str, content: str) -> str:
        """Compute, store, and return the integrity hash for a skill."""
        h = self.compute_integrity_hash(content)
        self._integrity_hashes[skill_name] = h
        return h

    def verify_integrity(self, skill_name: str, content: str) -> bool:
        """
        Verify that skill content matches its stored hash.

        Uses Trust-On-First-Use (TOFU): when no hash is stored, the
        content is assumed trustworthy, its hash is computed and stored,
        and a warning is logged.  Subsequent loads verify against this
        stored hash — any mismatch means tampering.

        Returns True if the hash matches or is newly established.
        Returns False if tampered.
        """
        stored = self._integrity_hashes.get(skill_name)
        if stored is None:
            # TOFU: trust on first use, store hash for future verification
            h = self.store_integrity_hash(skill_name, content)
            logger.warning(
                f"TOFU: No integrity hash for skill {skill_name!r}. "
                f"Establishing trust: {h[:16]}..."
            )
            return True

        current = self.compute_integrity_hash(content)
        if current != stored:
            logger.error(
                f"INTEGRITY VIOLATION: Skill {skill_name!r} content modified! "
                f"Expected: {stored[:16]}..., got: {current[:16]}..."
            )
            return False

        return True

    def get_integrity_hash(self, skill_name: str) -> Optional[str]:
        """Return the stored integrity hash for a skill, or None."""
        return self._integrity_hashes.get(skill_name)

    def remove_integrity_hash(self, skill_name: str) -> bool:
        """Remove the stored integrity hash for an evicted skill.

        Returns True if a hash was removed, False if none existed.
        """
        if skill_name in self._integrity_hashes:
            del self._integrity_hashes[skill_name]
            return True
        return False

    # ── Promotion gating ──────────────────────────────────────────────

    def gate_promotion(
        self, skill_name: str, usage_count: int, avg_score: float
    ) -> Tuple[bool, str]:
        """
        Gate temp->local promotion with optional approval requirement.

        Returns:
            Tuple of (approved: bool, reason: str).
        """
        if not self.require_promotion_approval:
            return True, "Auto-promotion enabled (approval not required)"

        self._pending_promotions[skill_name] = {
            "usage_count": usage_count,
            "avg_score": avg_score,
            "requested_at": datetime.utcnow().isoformat() + "Z",
        }
        logger.info(
            f"Skill {skill_name!r} queued for promotion approval "
            f"(uses={usage_count}, avg_score={avg_score:.1f})"
        )
        return False, "Pending approval"

    def approve_promotion(self, skill_name: str) -> bool:
        """Approve a pending promotion. Returns True if was pending."""
        if skill_name in self._pending_promotions:
            del self._pending_promotions[skill_name]
            logger.info(f"Promotion approved: {skill_name!r}")
            return True
        logger.warning(f"No pending promotion for: {skill_name!r}")
        return False

    def deny_promotion(self, skill_name: str) -> bool:
        """Deny and remove a pending promotion. Returns True if was pending."""
        if skill_name in self._pending_promotions:
            del self._pending_promotions[skill_name]
            logger.info(f"Promotion denied: {skill_name!r}")
            return True
        logger.warning(f"No pending promotion for: {skill_name!r}")
        return False

    def get_pending_promotions(self) -> Dict[str, Dict]:
        """Get all skills pending promotion approval."""
        return dict(self._pending_promotions)

    # ── Effective tool permissions ─────────────────────────────────────

    @staticmethod
    def compute_effective_allowed_tools(
        loaded_skills: list,
    ) -> Optional[set]:
        """
        Compute the effective set of allowed tools from loaded skills.

        When skills are loaded, their declared allowed-tools are unioned
        to determine what tools the specialist may use.  When no skills
        are loaded, returns None (no restriction — the specialist runs
        without skill-imposed constraints).

        Args:
            loaded_skills: List of loaded skill dicts, each with an
                optional 'allowed_tools' set.

        Returns:
            Set of allowed tool names, or None if unrestricted.
        """
        if not loaded_skills:
            return None  # No skills → no restriction

        skills_with_tools = [
            s for s in loaded_skills if s.get("allowed_tools") is not None
        ]

        if not skills_with_tools:
            return None  # Skills present but none declare tool permissions

        effective: set = set()
        for skill in skills_with_tools:
            effective |= skill["allowed_tools"]

        return effective

    # ── Bundled script scanning ─────────────────────────────────────────

    # Dangerous imports that AST analysis should flag.
    # Maps module name → (severity, description).
    _AST_DANGEROUS_IMPORTS: Dict[str, Tuple[str, str]] = {
        "subprocess": ("critical", "Imports subprocess module"),
        "shutil": ("high", "Imports shutil module (filesystem manipulation)"),
        "ctypes": ("critical", "Imports ctypes (native code execution)"),
        "importlib": ("high", "Imports importlib (dynamic module loading)"),
        "socket": ("critical", "Imports socket (raw network access)"),
        "http.client": ("high", "Imports http.client (HTTP requests)"),
        "http.server": ("high", "Imports http.server (starts HTTP server)"),
        "ftplib": ("high", "Imports ftplib (FTP access)"),
        "smtplib": ("high", "Imports smtplib (email sending)"),
        "xmlrpc": ("high", "Imports xmlrpc (remote procedure calls)"),
    }

    # Dangerous function calls that AST analysis should flag.
    # Maps function name → (severity, description).
    _AST_DANGEROUS_CALLS: Dict[str, Tuple[str, str]] = {
        "eval": ("critical", "Calls eval() — arbitrary code execution"),
        "exec": ("critical", "Calls exec() — arbitrary code execution"),
        "compile": ("high", "Calls compile() — dynamic code compilation"),
        "__import__": ("critical", "Calls __import__() — dynamic import"),
        "globals": ("high", "Calls globals() — global scope access"),
        "getattr": ("high", "Calls getattr() — dynamic attribute access"),
        "setattr": ("high", "Calls setattr() — dynamic attribute mutation"),
        "delattr": ("high", "Calls delattr() — dynamic attribute deletion"),
    }

    # Dangerous attribute access patterns (module.function).
    _AST_DANGEROUS_ATTRS: Dict[Tuple[str, str], Tuple[str, str]] = {
        ("os", "system"): ("critical", "Calls os.system() — shell execution"),
        ("os", "popen"): ("critical", "Calls os.popen() — shell execution"),
        ("os", "exec"): ("critical", "Calls os.exec*() — process replacement"),
        ("os", "execvp"): ("critical", "Calls os.execvp() — process replacement"),
        ("os", "execve"): ("critical", "Calls os.execve() — process replacement"),
        ("os", "spawn"): ("high", "Calls os.spawn*() — process creation"),
        ("os", "remove"): ("high", "Calls os.remove() — file deletion"),
        ("os", "unlink"): ("high", "Calls os.unlink() — file deletion"),
        ("os", "rmdir"): ("high", "Calls os.rmdir() — directory deletion"),
    }

    def _ast_scan_script(
        self, content: str, file_path: str, skill_name: str
    ) -> List[Dict[str, str]]:
        """
        AST-based analysis of a Python script for dangerous constructs.

        Unlike regex scanning, AST analysis cannot be bypassed by string
        obfuscation (e.g., splitting dangerous names across strings or
        using unusual whitespace).  It catches:
        - Dangerous module imports (subprocess, ctypes, socket, etc.)
        - Dangerous function calls (eval, exec, __import__, etc.)
        - Dangerous attribute calls (os.system, os.popen, etc.)

        Args:
            content: Python source code to analyze.
            file_path: Relative path for reporting.
            skill_name: Skill name for logging.

        Returns:
            List of finding dicts with file, severity, description.
        """
        findings: List[Dict[str, str]] = []

        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError:
            # Unparseable Python — flag but don't block
            findings.append({
                "file": file_path,
                "severity": "high",
                "description": "[ast] SyntaxError: script cannot be parsed",
            })
            return findings

        for node in ast.walk(tree):
            # Check imports: import subprocess / import ctypes
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    full_mod = alias.name
                    if full_mod in self._AST_DANGEROUS_IMPORTS:
                        sev, desc = self._AST_DANGEROUS_IMPORTS[full_mod]
                        findings.append({
                            "file": file_path,
                            "severity": sev,
                            "description": f"[ast] {desc}",
                        })
                    elif mod in self._AST_DANGEROUS_IMPORTS:
                        sev, desc = self._AST_DANGEROUS_IMPORTS[mod]
                        findings.append({
                            "file": file_path,
                            "severity": sev,
                            "description": f"[ast] {desc}",
                        })

            # Check from-imports: from subprocess import run
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                full_mod = node.module or ""
                if full_mod in self._AST_DANGEROUS_IMPORTS:
                    sev, desc = self._AST_DANGEROUS_IMPORTS[full_mod]
                    findings.append({
                        "file": file_path,
                        "severity": sev,
                        "description": f"[ast] {desc}",
                    })
                elif mod in self._AST_DANGEROUS_IMPORTS:
                    sev, desc = self._AST_DANGEROUS_IMPORTS[mod]
                    findings.append({
                        "file": file_path,
                        "severity": sev,
                        "description": f"[ast] {desc}",
                    })

            # Check function calls: eval(...), exec(...)
            elif isinstance(node, ast.Call):
                # Direct call: eval(x)
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                    if name in self._AST_DANGEROUS_CALLS:
                        sev, desc = self._AST_DANGEROUS_CALLS[name]
                        findings.append({
                            "file": file_path,
                            "severity": sev,
                            "description": f"[ast] {desc}",
                        })

                # Attribute call: os.system(x)
                elif isinstance(node.func, ast.Attribute):
                    attr_name = node.func.attr
                    if isinstance(node.func.value, ast.Name):
                        obj_name = node.func.value.id
                        key = (obj_name, attr_name)
                        if key in self._AST_DANGEROUS_ATTRS:
                            sev, desc = self._AST_DANGEROUS_ATTRS[key]
                            findings.append({
                                "file": file_path,
                                "severity": sev,
                                "description": f"[ast] {desc}",
                            })
                        # Also catch partial matches for os.exec* family
                        elif obj_name == "os" and attr_name.startswith("exec"):
                            findings.append({
                                "file": file_path,
                                "severity": "critical",
                                "description": f"[ast] Calls os.{attr_name}() — process replacement",
                            })

        if findings:
            logger.warning(
                f"Skill {skill_name} script {file_path}: "
                f"AST analysis found {len(findings)} issue(s)"
            )

        return findings

    def scan_bundled_scripts(
        self, skill_path: Path, skill_name: str
    ) -> List[Dict[str, str]]:
        """
        Scan Python scripts bundled inside a skill directory.

        Uses two complementary analysis layers:
        1. Regex scanning (SUSPICIOUS_PATTERNS) — catches text patterns
           including non-Python content in .py files.
        2. AST analysis — parses Python into a syntax tree to detect
           dangerous imports, function calls, and attribute access.
           Much harder to bypass than regex (immune to string splitting,
           whitespace tricks, comment interleaving).

        This method itself does not raise — callers (register_skill,
        _download_github_skill) are responsible for rejecting skills
        with critical-severity findings.

        Args:
            skill_path: Path to the skill directory.
            skill_name: Name of the skill (for logging).

        Returns:
            List of warning dicts with file, pattern, severity, description.
        """
        warnings: List[Dict[str, str]] = []

        if not skill_path.is_dir():
            return warnings

        for py_file in skill_path.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            rel_path = str(py_file.relative_to(skill_path))

            # Layer 1: Regex scanning
            for pattern, severity, description in SUSPICIOUS_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    warning = {
                        "file": rel_path,
                        "pattern": pattern,
                        "severity": severity,
                        "description": f"[script] {description}",
                    }
                    warnings.append(warning)
                    logger.warning(
                        f"Skill {skill_name} script {py_file.name}: "
                        f"[{severity.upper()}] {description}"
                    )

            # Layer 2: AST analysis (harder to bypass)
            ast_findings = self._ast_scan_script(content, rel_path, skill_name)
            warnings.extend(ast_findings)

        return warnings

    # ── Full validation pipeline ──────────────────────────────────────

    def validate_skill(
        self, name: str, content: str, skill_path: Path, base_dir: Path
    ) -> List[Dict[str, str]]:
        """
        Run the full validation pipeline on a skill.

        Validates name, path, content, and bundled scripts in sequence.
        Raises on critical SKILL.md violations, returns warnings for
        non-critical issues (including bundled script findings).

        Args:
            name: Skill name.
            content: SKILL.md content.
            skill_path: Path to the skill directory.
            base_dir: Expected base directory.

        Returns:
            List of non-critical warnings.

        Raises:
            SkillSecurityError: On any critical violation.
        """
        self.validate_skill_name(name)
        self.validate_skill_path(skill_path, base_dir)
        warnings = self.validate_skill_content(content, name)
        # Bug #3 fix: Also scan bundled Python scripts
        warnings.extend(self.scan_bundled_scripts(skill_path, name))
        return warnings
