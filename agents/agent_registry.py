"""
Agent registry — resolves agent IDs from the Paperclip API at startup.

On boot the orchestrator calls ``resolve_all()`` once to build an internal
{role: agent_id} map.  No UUIDs are hardcoded — if ``bootstrap-org.cjs``
recreates agents with new UUIDs, the orchestrator picks them up on restart.
"""

import logging
from typing import Dict, List, Optional, Set

from .paperclip_client import PaperclipClient

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Resolve and cache agent identities from Paperclip."""

    def __init__(
        self,
        client: PaperclipClient,
        disabled_roles: Optional[List[str]] = None,
    ):
        self._client = client
        self._disabled: Set[str] = set(disabled_roles or [])
        self._role_map: Dict[str, str] = {}

    def resolve_all(self) -> Dict[str, str]:
        """Fetch all agents from Paperclip and build {role: agent_id} map.

        Skips agents whose role is in ``disabled_roles`` or whose status
        is not ``active``.

        Returns:
            Dict mapping role name → agent UUID.

        Raises:
            PaperclipAPIError: If the API call fails.
        """
        agents = self._client.list_agents()
        self._role_map = {}

        for agent in agents:
            if agent.role in self._disabled:
                logger.info("Skipping disabled agent: %s (%s)", agent.role, agent.id)
                continue
            if agent.status != "active":
                logger.info("Skipping inactive agent: %s (%s, status=%s)",
                            agent.role, agent.id, agent.status)
                continue
            self._role_map[agent.role] = agent.id
            logger.debug("Registered agent: %s → %s", agent.role, agent.id)

        logger.info("Resolved %d agents from Paperclip (skipped %d disabled, %d inactive)",
                     len(self._role_map),
                     sum(1 for a in agents if a.role in self._disabled),
                     sum(1 for a in agents if a.status != "active" and a.role not in self._disabled))
        return dict(self._role_map)

    def get_agent_id(self, role: str) -> Optional[str]:
        """Look up an agent ID by role.  Returns None if not found."""
        return self._role_map.get(role)

    @property
    def roles(self) -> List[str]:
        """List of all resolved (active, non-disabled) role names."""
        return list(self._role_map.keys())
