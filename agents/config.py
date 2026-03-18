"""
Configuration for Multi-Agent System

Centralized configuration for:
- Model settings
- Generation parameters
- Mattermost integration
- System behavior
"""

from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@dataclass
class ModelConfig:
    """Base model configuration"""
    model_name: str = "Qwen/Qwen3.5-9B"

    # Backend — vLLM only
    backend: str = "vllm"


@dataclass
class AdapterConfig:
    """Adapter configuration (prompt-based adapters only)"""
    # No LoRA paths or VRAM settings — all adapters are prompt-based.
    # Per-task generation parameters live in GenerationConfig.
    pass


@dataclass
class GenerationConfig:
    """Default generation parameters for different tasks"""

    # Per-task configs
    configs: Dict[str, Dict[str, Any]] = field(default_factory=lambda: {
        "code": {
            "temperature": 0.3,
            "top_p": 0.95,
            "max_tokens": 1500,
            "repetition_penalty": 1.1
        },
        "creative": {
            "temperature": 0.8,
            "top_p": 0.95,
            "max_tokens": 2000,
            "repetition_penalty": 1.0
        },
        "research": {
            "temperature": 0.4,
            "top_p": 0.9,
            "max_tokens": 1500,
            "repetition_penalty": 1.1
        },
        "general": {
            "temperature": 0.5,
            "top_p": 0.9,
            "max_tokens": 1000,
            "repetition_penalty": 1.1
        },
        # Adapter-specific overrides
        "genesia": {
            "temperature": 0.4,
            "top_p": 0.9,
            "max_tokens": 600
        },
        "critic": {
            "temperature": 0.1,  # Very deterministic for consistent scoring
            "top_p": 0.9,
            "max_tokens": 500
        },
        "refinement": {
            "temperature": 0.4,
            "top_p": 0.9,
            "max_tokens": 300
        }
    })

    def get_config(self, config_name: str) -> Dict[str, Any]:
        """Get generation config for a specific task/adapter"""
        return self.configs.get(config_name, self.configs["general"])


@dataclass
class WorkflowConfig:
    """Workflow behavior configuration"""

    # Iteration limits
    max_iterations: int = 3
    quality_threshold: int = 85

    # Timeouts (seconds)
    node_timeout: int = 120  # Per-node timeout
    workflow_timeout: int = 600  # Total workflow timeout

    # LLM retry settings
    llm_max_retries: int = 3  # Max retry attempts on transient LLM failures
    llm_retry_base_delay: float = 1.0  # Base delay (seconds) for exponential backoff

    # Parallel sub-task execution
    parallel_max_workers: int = 4  # Max concurrent sub-tasks in parallel mode
    parallel_subtask_timeout: int = 300  # Per-sub-task timeout (seconds)


@dataclass
class MattermostConfig:
    """Mattermost integration configuration"""

    enabled: bool = False

    # Webhook posting
    webhook_url: Optional[str] = field(
        default_factory=lambda: os.getenv("MATTERMOST_WEBHOOK_URL")
    )
    default_channel: str = "ai-outputs"
    username: str = "Genesia Multi-Agent System"

    # Bot mode (full interactive bot)
    bot_enabled: bool = False
    bot_token: Optional[str] = field(
        default_factory=lambda: os.getenv("MATTERMOST_BOT_TOKEN")
    )
    mattermost_url: Optional[str] = field(
        default_factory=lambda: os.getenv("MATTERMOST_URL")
    )




@dataclass
class SkillSourceConfig:
    """A single vetted remote skill source."""
    name: str                          # "anthropics" | "superpowers" | "vercel"
    repo: str                          # GitHub owner/repo
    branch: str = "main"
    skills_path: str = "skills"        # Path within repo where skills live
    confidence_threshold: float = 0.25
    trust_level: str = "standard"      # "high" | "standard" | "restricted"
    default_allowed_tools: str = ""    # Fallback when skill has no allowed-tools frontmatter
    catalog_ttl_seconds: int = 300
    enabled: bool = True


@dataclass
class SkillsConfig:
    """Locked-down skill sources. Exactly 3 vetted sources — not extensible."""
    sources: List[SkillSourceConfig] = field(default_factory=lambda: [
        SkillSourceConfig(
            name="anthropics",
            repo="anthropics/skills",
            trust_level="high",
        ),
        SkillSourceConfig(
            name="superpowers",
            repo="obra/superpowers",
            trust_level="standard",
            default_allowed_tools="Read Grep Glob",
        ),
        SkillSourceConfig(
            name="vercel",
            repo="vercel-labs/agent-skills",
            trust_level="standard",
        ),
    ])
    enable_remote: bool = True
    scan_scripts: bool = True          # Download + security-scan scripts/
    execute_scripts: bool = True       # Execute scanned scripts (requires sandbox)


@dataclass
class CacheConfig:
    """Result caching / artifact store configuration."""

    enabled: bool = True
    max_entries: int = 1000
    default_ttl_seconds: int = 3600  # 1 hour
    min_score_to_cache: int = 70  # Only cache results scoring >= this
    db_path: Optional[str] = None  # None = ~/.genesia/artifact_cache.db


@dataclass
class SpendingConfig:
    """Spending tracker & circuit breaker configuration.

    Controls the local spending ledger that detects runaway agents
    and trips a circuit breaker to halt heartbeat execution.
    """

    enabled: bool = True

    # Rolling window for threshold evaluation
    window_seconds: int = 3600  # 1 hour

    # Spend velocity: max cents in rolling window
    max_cents_per_window: int = 500  # $5/hour

    # Heartbeat frequency: max non-idle heartbeats in window
    max_heartbeats_per_window: int = 30

    # Consecutive non-idle streak before trip
    max_consecutive_non_idle: int = 10

    # Cooldown after trip (doubles on re-trip, capped by max)
    cooldown_seconds: int = 300  # 5 minutes
    max_cooldown_seconds: int = 7200  # 2 hours

    # SQLite database path (None = ~/.genesia/spending_ledger.db)
    db_path: Optional[str] = None

    # Retention for old cost events
    retention_days: int = 30


@dataclass
class PaperclipConfig:
    """Paperclip control plane integration configuration.

    When enabled, Genesia runs in heartbeat mode — receiving tasks from
    Paperclip and reporting results back via REST API.  Connection details
    are read from PAPERCLIP_* env vars injected by the Paperclip adapter,
    but can be overridden here.
    """
    enabled: bool = False
    api_url: str = ""           # Override PAPERCLIP_API_URL
    api_key: str = ""           # Override PAPERCLIP_API_KEY
    task_type: str = ""         # Override GENESIA_TASK_TYPE
    cost_reporting: bool = True # Report token usage back to Paperclip
    output_format: str = "json" # "json" (for adapter parsing) or "text"

    # Orchestrator settings (for GENESIA_TASK_TYPE=orchestrator)
    orchestrator_max_children: int = 5    # Max subtasks to fan-out
    orchestrator_retry_failed: bool = True  # Auto-retry blocked children
    orchestrator_max_retries: int = 1     # Max retries per child
    orchestrator_skip_decomposition_for_fast: bool = True  # Skip decomposition for fast-tier tasks
    orchestrator_poll_timeout: int = 300  # Max seconds to WS-wait in POLL phase


@dataclass
class SystemConfig:
    """Overall system configuration"""

    model: ModelConfig = field(default_factory=ModelConfig)
    adapters: AdapterConfig = field(default_factory=AdapterConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    mattermost: MattermostConfig = field(default_factory=MattermostConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    sandbox: "SandboxConfig" = field(default_factory=lambda: _default_sandbox_config())
    paperclip: PaperclipConfig = field(default_factory=PaperclipConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    spending: SpendingConfig = field(default_factory=SpendingConfig)

    # Logging
    log_level: str = "INFO"
    log_file: Optional[Path] = None

    # Development mode
    dev_mode: bool = False  # Extra logging, no Mattermost posting

    @classmethod
    def from_env(cls) -> "SystemConfig":
        """Create config from environment variables"""
        config = cls()

        # Override with env vars if present
        model_name = os.getenv("MODEL_NAME")
        if model_name:
            config.model.model_name = model_name

        if os.getenv("DEV_MODE", "").lower() == "true":
            config.dev_mode = True
            config.log_level = "DEBUG"

        # Spending tracker env overrides
        if os.getenv("GENESIA_SPEND_ENABLED", "").lower() == "false":
            config.spending.enabled = False
        for attr, env_key in [
            ("window_seconds", "GENESIA_SPEND_WINDOW_SECONDS"),
            ("max_cents_per_window", "GENESIA_SPEND_MAX_CENTS"),
            ("max_heartbeats_per_window", "GENESIA_SPEND_MAX_HEARTBEATS"),
            ("max_consecutive_non_idle", "GENESIA_SPEND_MAX_CONSECUTIVE"),
            ("cooldown_seconds", "GENESIA_SPEND_COOLDOWN_SECONDS"),
            ("max_cooldown_seconds", "GENESIA_SPEND_MAX_COOLDOWN_SECONDS"),
            ("retention_days", "GENESIA_SPEND_RETENTION_DAYS"),
        ]:
            val = os.getenv(env_key)
            if val:
                setattr(config.spending, attr, int(val))
        db_path = os.getenv("GENESIA_SPEND_DB_PATH")
        if db_path:
            config.spending.db_path = db_path

        # Orchestrator poll timeout override
        poll_timeout = os.getenv("PAPERCLIP_ORCHESTRATOR_POLL_TIMEOUT")
        if poll_timeout:
            config.paperclip.orchestrator_poll_timeout = int(poll_timeout)

        return config

    def validate(self) -> bool:
        """Validate configuration"""
        issues = []

        # Check model configuration
        if not self.model.model_name:
            issues.append("Model name not specified")

        # Check Mattermost config if enabled
        if self.mattermost.enabled and not self.mattermost.webhook_url:
            issues.append("Mattermost enabled but webhook URL not configured")

        if issues:
            print("⚠️  Configuration Issues:")
            for issue in issues:
                print(f"   • {issue}")
            return False

        return True


# ===== DEFAULT CONFIGURATION =====

def get_dev_config() -> SystemConfig:
    """Get development configuration (no Mattermost, debug logging)"""
    config = SystemConfig()
    config.dev_mode = True
    config.log_level = "DEBUG"
    config.mattermost.enabled = False
    return config


def get_production_config() -> SystemConfig:
    """Get production configuration from environment"""
    return SystemConfig.from_env()


# ── Sandbox config (late import to avoid circular dependency) ──


def _default_sandbox_config():
    """Create default SandboxConfig (avoids import at class definition time)."""
    from .sandbox.config import SandboxConfig
    return SandboxConfig()
