"""
MiroFish Simulation Tool

Invokes the MiroFish multi-agent simulation engine to predict outcomes,
detect conflicts, and assess risk for architecture decisions, deployment
plans, and complex integrations.

Complexity-based LLM routing:
  - Simple (<40 agents, <20 iterations) -> local vLLM (free)
  - Complex (>=40 agents or >=20 iterations) -> cloud API (if configured)
"""

import logging
import os
from typing import Any, Dict

import requests

from .base import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class MiroFishSimulationTool(Tool):
    """Run multi-agent simulations via MiroFish to predict outcomes."""

    def __init__(self):
        super().__init__(
            name="MiroFishSimulation",
            description=(
                "Run a multi-agent simulation to predict outcomes for architecture "
                "decisions, deployment plans, or complex integrations. Populates a "
                "virtual world with autonomous agents that have distinct personas and "
                "behavioral logic. Returns a prediction report with conflict detection "
                "and risk assessment."
            ),
            category=ToolCategory.EXTERNAL_SERVICE,
        )
        # Read thresholds at init time so patched env vars take effect in tests
        self._agent_threshold = int(
            os.getenv("MIROFISH_COMPLEXITY_AGENT_THRESHOLD", "40")
        )
        self._iter_threshold = int(
            os.getenv("MIROFISH_COMPLEXITY_ITER_THRESHOLD", "20")
        )
        self._mirofish_url = os.getenv("MIROFISH_URL", "http://mirofish:5001")
        self._local_api_url = os.getenv(
            "MIROFISH_LLM_API_URL", "http://host.docker.internal:8000/v1"
        )
        self._local_model = os.getenv(
            "MIROFISH_LLM_MODEL", "QuantTrio/Qwen3.5-9B-AWQ"
        )

    def _select_llm_config(
        self, agent_count: int, iterations: int
    ) -> Dict[str, Any]:
        """Select LLM backend based on simulation complexity."""
        is_complex = (
            agent_count >= self._agent_threshold
            or iterations >= self._iter_threshold
        )

        cloud_api_url = os.getenv("MIROFISH_LLM_CLOUD_API_URL", "")
        cloud_api_key = os.getenv("MIROFISH_LLM_CLOUD_API_KEY", "")
        cloud_model = os.getenv("MIROFISH_LLM_CLOUD_MODEL", "")
        cloud_available = bool(cloud_api_url and cloud_api_key and cloud_model)

        if is_complex and cloud_available:
            logger.info(
                "Complex simulation (%d agents, %d iters) — using cloud LLM: %s",
                agent_count, iterations, cloud_model,
            )
            return {
                "base_url": cloud_api_url,
                "api_key": cloud_api_key,
                "model": cloud_model,
                "fallback": False,
            }

        if is_complex and not cloud_available:
            logger.warning(
                "Complex simulation (%d agents, %d iters) but no cloud LLM "
                "configured — falling back to local vLLM. Quality may be reduced.",
                agent_count, iterations,
            )
            return {
                "base_url": self._local_api_url,
                "api_key": "no-key-needed",
                "model": self._local_model,
                "fallback": True,
            }

        return {
            "base_url": self._local_api_url,
            "api_key": "no-key-needed",
            "model": self._local_model,
            "fallback": False,
        }

    def execute(self, **kwargs) -> ToolResult:
        """Execute a MiroFish simulation."""
        seed_material = kwargs.get("seed_material", "")
        agent_count = kwargs.get("agent_count", 20)
        iterations = kwargs.get("iterations", 10)
        question = kwargs.get("question", "")

        if not seed_material:
            return ToolResult(
                success=False,
                output="",
                error="seed_material is required",
            )

        llm_config = self._select_llm_config(agent_count, iterations)

        try:
            response = requests.post(
                f"{self._mirofish_url}/api/simulate",
                json={
                    "seed_material": seed_material,
                    "agent_count": agent_count,
                    "iterations": iterations,
                    "question": question,
                    "llm_config": {
                        "base_url": llm_config["base_url"],
                        "api_key": llm_config["api_key"],
                        "model": llm_config["model"],
                    },
                },
                timeout=300,
            )

            if response.status_code != 200:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"MiroFish returned {response.status_code}: {response.text[:500]}",
                )

            data = response.json()
            report = data.get("report", str(data))

            metadata = {
                "agent_count": agent_count,
                "iterations": iterations,
                "llm_backend": (
                    "cloud"
                    if not llm_config.get("fallback")
                    and llm_config["base_url"] != self._local_api_url
                    else "local"
                ),
                "fallback": llm_config.get("fallback", False),
            }

            if llm_config.get("fallback"):
                report = (
                    "WARNING: Cloud LLM not configured. This complex simulation "
                    "ran on local vLLM — results may be lower quality.\n\n" + report
                )

            return ToolResult(success=True, output=report, metadata=metadata)

        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"MiroFish simulation failed: {str(e)}",
            )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "seed_material": {
                    "type": "string",
                    "description": (
                        "The scenario to simulate — architecture spec, deployment plan, "
                        "code change description, or any narrative to run through the "
                        "multi-agent prediction engine."
                    ),
                },
                "agent_count": {
                    "type": "integer",
                    "description": "Number of simulated agents (default: 20). Above 40 triggers cloud LLM.",
                    "default": 20,
                },
                "iterations": {
                    "type": "integer",
                    "description": "Simulation iteration count (default: 10). Above 20 triggers cloud LLM.",
                    "default": 10,
                },
                "question": {
                    "type": "string",
                    "description": "Specific question to answer via the simulation (optional).",
                    "default": "",
                },
            },
            "required": ["seed_material"],
        }
