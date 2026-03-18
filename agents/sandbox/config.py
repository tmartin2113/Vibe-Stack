"""Sandbox configuration for OpenSandbox integration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from ..resource_allocator import ResourcePlan


@dataclass
class SandboxConfig:
    """Configuration for the OpenSandbox execution backend.

    Auto-populated from ResourcePlan at startup, but every field can
    be overridden by environment variables.
    """

    # Backend (OpenSandbox is the only supported execution backend)
    backend: str = "opensandbox"

    # OpenSandbox server connection
    server_url: str = "http://opensandbox:8080"
    api_key: str = ""  # Empty = dev mode (no auth)

    # Sandbox images
    sandbox_image: str = "opensandbox/code-interpreter:v1.0.1"
    gpu_sandbox_image: str = "genesia/sandbox-gpu:latest"

    # Per-sandbox resource limits (K8s format)
    cpu_limit: str = "500m"
    memory_limit: str = "512Mi"

    # Pool settings
    pool_size: int = 2
    sandbox_timeout: int = 300  # TTL per sandbox in seconds

    # GPU access for sandboxes
    gpu_enabled: bool = False
    gpu_device_ids: str = ""    # Comma-separated, e.g. "1,2". Empty = all

    # Network
    network_egress: bool = False  # Allow outbound network in sandboxes

    # Firecrawl (web scraping API — requires network_egress=True to take effect)
    firecrawl_api_key: str = ""

    # File access — colon-separated list of directories the agent may read/write.
    # Empty string means use the built-in defaults (/home/user/Genesia, /tmp).
    allowed_file_dirs: str = ""

    @classmethod
    def from_resource_plan(cls, plan: "ResourcePlan") -> "SandboxConfig":
        """Build SandboxConfig from an auto-discovered ResourcePlan."""
        sp = plan.sandbox_pool
        return cls(
            pool_size=sp.pool_size,
            cpu_limit=sp.per_sandbox_cpu,
            memory_limit=sp.per_sandbox_memory,
            gpu_enabled=sp.gpu_enabled,
            gpu_device_ids=",".join(str(i) for i in sp.gpu_device_ids),
            sandbox_image=sp.sandbox_image,
        )

    def apply_env_overrides(self) -> None:
        """Apply environment variable overrides.

        Explicit env vars always win over auto-detected values.
        Only overrides fields that have a corresponding env var set.
        """
        env_map = {
            "GENESIA_SANDBOX_BACKEND": ("backend", str),
            "GENESIA_SANDBOX_URL": ("server_url", str),
            "GENESIA_SANDBOX_API_KEY": ("api_key", str),
            "GENESIA_SANDBOX_IMAGE": ("sandbox_image", str),
            "GENESIA_SANDBOX_GPU_IMAGE": ("gpu_sandbox_image", str),
            "GENESIA_SANDBOX_CPU_LIMIT": ("cpu_limit", str),
            "GENESIA_SANDBOX_MEMORY_LIMIT": ("memory_limit", str),
            "GENESIA_SANDBOX_POOL_SIZE": ("pool_size", int),
            "GENESIA_SANDBOX_TIMEOUT": ("sandbox_timeout", int),
            "GENESIA_SANDBOX_GPU": ("gpu_enabled", _parse_bool),
            "GENESIA_SANDBOX_GPU_IDS": ("gpu_device_ids", str),
            "GENESIA_SANDBOX_EGRESS": ("network_egress", _parse_bool),
            "FIRECRAWL_API_KEY": ("firecrawl_api_key", str),
            "GENESIA_ALLOWED_FILE_DIRS": ("allowed_file_dirs", str),
        }
        for env_key, (attr, converter) in env_map.items():
            value = os.environ.get(env_key)
            if value is not None:
                setattr(self, attr, converter(value))

    @property
    def gpu_device_id_list(self) -> List[int]:
        """Parse gpu_device_ids string into a list of ints."""
        if not self.gpu_device_ids:
            return []
        return [int(x.strip()) for x in self.gpu_device_ids.split(",") if x.strip()]

    @property
    def allowed_file_dir_list(self) -> List[str]:
        """Parse allowed_file_dirs string into a list of directory paths.

        Returns an empty list when unconfigured (callers should fall back
        to their built-in defaults).
        """
        if not self.allowed_file_dirs:
            return []
        return [d.strip() for d in self.allowed_file_dirs.split(":") if d.strip()]


def _parse_bool(value: str) -> bool:
    """Parse a string to bool (truthy: true, 1, yes)."""
    return value.lower() in ("true", "1", "yes")
