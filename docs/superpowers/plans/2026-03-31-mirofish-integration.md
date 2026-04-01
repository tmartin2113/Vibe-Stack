# MiroFish Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the lightweight simulation.py with the full MiroFish simulation engine as a Docker service, invocable by agents via a MiroFishSimulation tool with complexity-based LLM routing (local vLLM for simple, cloud for complex).

**Architecture:** MiroFish runs as a Docker Compose service alongside a self-hosted Zep CE stack (PostgreSQL+pgvector, Neo4j, Graphiti). Agents invoke it through a new `MiroFishSimulation` tool that estimates complexity upfront and routes to the appropriate LLM backend. The existing `simulation.py` module and its 6 integration points are removed.

**Tech Stack:** Docker Compose, Python, MiroFish (OASIS + CAMEL-AI), Zep CE (pgvector + Neo4j + Graphiti)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `docker-compose.infra.yml` | Modify | Add mirofish, zep, zep-db, neo4j, graphiti services |
| `agents/tools/mirofish_tool.py` | Create | MiroFishSimulation tool with complexity routing |
| `agents/tools/registry.py` | Modify | Register MiroFishSimulation in role-based tool sets |
| `agents/simulation.py` | Delete | Replaced by MiroFish service |
| `agents/simulation_adapters.py` | Delete | Replaced by MiroFish service |
| `agents/simulation_budget.py` | Delete | Replaced by MiroFish service |
| `agents/workflow_factory.py` | Modify | Remove simulation adapter registration |
| `agents/specialist_nodes.py` | Modify | Remove clarification simulation calls |
| `agents/parallel_subtasks.py` | Modify | Remove integration simulation sidecar |
| `agents/aggregator.py` | Modify | Remove simulation report formatting |
| `agents/skill_generator.py` | Modify | Remove skill vetting simulation |
| `tests/test_simulation.py` | Delete | Replace with MiroFish tool tests |
| `tests/test_mirofish_tool.py` | Create | Tests for MiroFishSimulation tool |
| `.env.example` | Modify | Add MiroFish + Zep env vars |
| `README.md` | Modify | Add MiroFish to infrastructure services |
| `CLAUDE.md` | Modify | Update simulation section |

---

### Task 1: Add MiroFish and Zep services to docker-compose.infra.yml

**Files:**
- Modify: `docker-compose.infra.yml`

- [ ] **Step 1: Read the current docker-compose.infra.yml**

Read the file to understand the existing service pattern and volume definitions.

- [ ] **Step 2: Add the Zep infrastructure services**

Add before the existing services in `docker-compose.infra.yml`:

```yaml
  # ── Zep (agent memory for MiroFish simulations) ────────────────
  zep-db:
    image: ankane/pgvector:v0.5.1
    restart: unless-stopped
    environment:
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=postgres
      - POSTGRES_DB=zep
    volumes:
      - zep-db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "postgres"]
      interval: 10s
      timeout: 5s
      retries: 5

  neo4j:
    image: neo4j:5.22.0
    restart: unless-stopped
    environment:
      - NEO4J_AUTH=neo4j/zep-neo4j-pass
    volumes:
      - neo4j-data:/data
    healthcheck:
      test: ["CMD", "neo4j", "status"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

  graphiti:
    image: zepai/graphiti:0.3
    restart: unless-stopped
    depends_on:
      neo4j:
        condition: service_healthy
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - MODEL_NAME=${MIROFISH_LLM_CLOUD_MODEL:-gpt-4o-mini}
      - NEO4J_URI=bolt://neo4j:7687
      - NEO4J_USER=neo4j
      - NEO4J_PASSWORD=zep-neo4j-pass
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8003/healthz')"]
      interval: 10s
      timeout: 5s
      retries: 5

  zep:
    image: zepai/zep:latest
    restart: unless-stopped
    depends_on:
      zep-db:
        condition: service_healthy
      graphiti:
        condition: service_healthy
    environment:
      - ZEP_STORE_TYPE=postgres
      - ZEP_STORE_POSTGRES_DSN=postgres://postgres:postgres@zep-db:5432/zep?sslmode=disable
      - ZEP_GRAPHITI_URL=http://graphiti:8003
    volumes:
      - ./zep.yaml:/app/zep.yaml:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/healthz"]
      interval: 10s
      timeout: 5s
      retries: 5
```

- [ ] **Step 3: Add the MiroFish service**

Add after the Zep services:

```yaml
  # ── MiroFish (multi-agent simulation engine) ───────────────────
  mirofish:
    image: ghcr.io/666ghj/mirofish:latest
    restart: unless-stopped
    depends_on:
      zep:
        condition: service_healthy
    ports:
      - "5001:5001"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      - LLM_API_KEY=${MIROFISH_LLM_API_KEY:-no-key-needed-for-local}
      - LLM_BASE_URL=${MIROFISH_LLM_API_URL:-http://host.docker.internal:8000/v1}
      - LLM_MODEL_NAME=${MIROFISH_LLM_MODEL:-QuantTrio/Qwen3.5-9B-AWQ}
      - ZEP_API_KEY=${ZEP_API_KEY:-}
      - ZEP_API_URL=http://zep:8000
    volumes:
      - mirofish-uploads:/app/backend/uploads
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5001/health"]
      interval: 15s
      timeout: 10s
      retries: 5
      start_period: 60s
```

- [ ] **Step 4: Add new volumes**

Add to the `volumes:` section at the bottom of `docker-compose.infra.yml`:

```yaml
  zep-db-data:
  neo4j-data:
  mirofish-uploads:
```

- [ ] **Step 5: Create a minimal zep.yaml config**

Create `zep.yaml` in the repo root:

```yaml
# Zep CE configuration
server:
  host: 0.0.0.0
  port: 8000

store:
  type: postgres
  postgres:
    dsn: postgres://postgres:postgres@zep-db:5432/zep?sslmode=disable
```

- [ ] **Step 6: Commit**

```bash
git add docker-compose.infra.yml zep.yaml
git commit -m "infra: add MiroFish simulation engine + Zep memory stack

MiroFish on port 5001, backed by self-hosted Zep CE (pgvector + Neo4j +
Graphiti). Uses local vLLM by default. All services on compose default
network."
```

---

### Task 2: Create the MiroFishSimulation tool

**Files:**
- Create: `agents/tools/mirofish_tool.py`
- Create: `tests/test_mirofish_tool.py`

- [ ] **Step 1: Write the test file**

Create `tests/test_mirofish_tool.py`:

```python
"""Tests for MiroFishSimulation tool."""

import os
from unittest.mock import MagicMock, patch

import pytest

from agents.tools.mirofish_tool import MiroFishSimulationTool


class TestComplexityRouting:
    """Test the complexity-based LLM routing logic."""

    def test_simple_simulation_uses_local(self):
        tool = MiroFishSimulationTool()
        config = tool._select_llm_config(agent_count=10, iterations=5)
        assert config["base_url"] == os.getenv(
            "MIROFISH_LLM_API_URL", "http://host.docker.internal:8000/v1"
        )

    def test_high_agent_count_uses_cloud(self):
        with patch.dict(os.environ, {
            "MIROFISH_LLM_CLOUD_API_URL": "https://api.anthropic.com",
            "MIROFISH_LLM_CLOUD_API_KEY": "sk-test",
            "MIROFISH_LLM_CLOUD_MODEL": "claude-sonnet-4-6",
        }):
            tool = MiroFishSimulationTool()
            config = tool._select_llm_config(agent_count=50, iterations=5)
            assert config["base_url"] == "https://api.anthropic.com"
            assert config["model"] == "claude-sonnet-4-6"

    def test_high_iteration_count_uses_cloud(self):
        with patch.dict(os.environ, {
            "MIROFISH_LLM_CLOUD_API_URL": "https://api.openai.com/v1",
            "MIROFISH_LLM_CLOUD_API_KEY": "sk-test",
            "MIROFISH_LLM_CLOUD_MODEL": "gpt-4o",
        }):
            tool = MiroFishSimulationTool()
            config = tool._select_llm_config(agent_count=10, iterations=25)
            assert config["base_url"] == "https://api.openai.com/v1"

    def test_cloud_not_configured_falls_back_to_local(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MIROFISH_LLM_CLOUD_API_URL", None)
            os.environ.pop("MIROFISH_LLM_CLOUD_API_KEY", None)
            tool = MiroFishSimulationTool()
            config = tool._select_llm_config(agent_count=50, iterations=30)
            # Falls back to local even though complex
            assert "host.docker.internal" in config["base_url"] or "localhost" in config["base_url"]
            assert config.get("fallback") is True

    def test_custom_thresholds(self):
        with patch.dict(os.environ, {
            "MIROFISH_COMPLEXITY_AGENT_THRESHOLD": "20",
            "MIROFISH_COMPLEXITY_ITER_THRESHOLD": "10",
            "MIROFISH_LLM_CLOUD_API_URL": "https://api.test.com",
            "MIROFISH_LLM_CLOUD_API_KEY": "sk-test",
            "MIROFISH_LLM_CLOUD_MODEL": "test-model",
        }):
            tool = MiroFishSimulationTool()
            # 25 agents > 20 threshold
            config = tool._select_llm_config(agent_count=25, iterations=5)
            assert config["base_url"] == "https://api.test.com"


class TestToolExecution:
    """Test the tool's execute method."""

    @patch("agents.tools.mirofish_tool.requests.post")
    def test_successful_simulation(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "report": "Simulation complete. No conflicts detected.",
            "agents": 20,
            "iterations": 10,
        }
        mock_post.return_value = mock_response

        tool = MiroFishSimulationTool()
        result = tool.execute(
            seed_material="Build a REST API with user authentication",
            agent_count=20,
            iterations=10,
        )
        assert result.success is True
        assert "No conflicts detected" in result.output

    @patch("agents.tools.mirofish_tool.requests.post")
    def test_simulation_failure(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"
        mock_post.return_value = mock_response

        tool = MiroFishSimulationTool()
        result = tool.execute(
            seed_material="Test scenario",
            agent_count=10,
            iterations=5,
        )
        assert result.success is False
        assert result.error is not None

    @patch("agents.tools.mirofish_tool.requests.post")
    def test_connection_error(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")

        tool = MiroFishSimulationTool()
        result = tool.execute(
            seed_material="Test scenario",
            agent_count=10,
            iterations=5,
        )
        assert result.success is False
        assert "Connection refused" in result.error

    def test_parameter_schema(self):
        tool = MiroFishSimulationTool()
        schema = tool._get_parameters_schema()
        assert "seed_material" in schema["properties"]
        assert "agent_count" in schema["properties"]
        assert "iterations" in schema["properties"]
        assert "question" in schema["properties"]
        assert schema["required"] == ["seed_material"]


class TestToolMetadata:
    """Test tool registration metadata."""

    def test_tool_name(self):
        tool = MiroFishSimulationTool()
        assert tool.name == "MiroFishSimulation"

    def test_tool_category(self):
        from agents.tools.base import ToolCategory
        tool = MiroFishSimulationTool()
        assert tool.category == ToolCategory.EXTERNAL_SERVICE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_mirofish_tool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.tools.mirofish_tool'`

- [ ] **Step 3: Write the MiroFishSimulation tool**

Create `agents/tools/mirofish_tool.py`:

```python
"""
MiroFish Simulation Tool

Invokes the MiroFish multi-agent simulation engine to predict outcomes,
detect conflicts, and assess risk for architecture decisions, deployment
plans, and complex integrations.

Complexity-based LLM routing:
  - Simple (<40 agents, <20 iterations) → local vLLM (free)
  - Complex (>=40 agents or >=20 iterations) → cloud API (if configured)
"""

import logging
import os
from typing import Any, Dict, Optional

import requests

from .base import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)

# Complexity thresholds for LLM routing
_AGENT_THRESHOLD = int(os.getenv("MIROFISH_COMPLEXITY_AGENT_THRESHOLD", "40"))
_ITER_THRESHOLD = int(os.getenv("MIROFISH_COMPLEXITY_ITER_THRESHOLD", "20"))

# MiroFish service URL
_MIROFISH_URL = os.getenv("MIROFISH_URL", "http://mirofish:5001")

# Local LLM defaults
_LOCAL_API_URL = os.getenv("MIROFISH_LLM_API_URL", "http://host.docker.internal:8000/v1")
_LOCAL_MODEL = os.getenv("MIROFISH_LLM_MODEL", "QuantTrio/Qwen3.5-9B-AWQ")

# Cloud LLM (optional — for complex simulations)
_CLOUD_API_URL = os.getenv("MIROFISH_LLM_CLOUD_API_URL", "")
_CLOUD_API_KEY = os.getenv("MIROFISH_LLM_CLOUD_API_KEY", "")
_CLOUD_MODEL = os.getenv("MIROFISH_LLM_CLOUD_MODEL", "")


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

    def _select_llm_config(
        self, agent_count: int, iterations: int
    ) -> Dict[str, Any]:
        """Select LLM backend based on simulation complexity."""
        is_complex = agent_count >= _AGENT_THRESHOLD or iterations >= _ITER_THRESHOLD
        cloud_available = bool(_CLOUD_API_URL and _CLOUD_API_KEY and _CLOUD_MODEL)

        if is_complex and cloud_available:
            logger.info(
                "Complex simulation (%d agents, %d iters) — using cloud LLM: %s",
                agent_count, iterations, _CLOUD_MODEL,
            )
            return {
                "base_url": _CLOUD_API_URL,
                "api_key": _CLOUD_API_KEY,
                "model": _CLOUD_MODEL,
                "fallback": False,
            }

        if is_complex and not cloud_available:
            logger.warning(
                "Complex simulation (%d agents, %d iters) but no cloud LLM configured "
                "— falling back to local vLLM. Quality may be reduced.",
                agent_count, iterations,
            )
            return {
                "base_url": _LOCAL_API_URL,
                "api_key": "no-key-needed",
                "model": _LOCAL_MODEL,
                "fallback": True,
            }

        return {
            "base_url": _LOCAL_API_URL,
            "api_key": "no-key-needed",
            "model": _LOCAL_MODEL,
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
                f"{_MIROFISH_URL}/api/simulate",
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
                timeout=300,  # Simulations can be slow
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
                "llm_backend": "cloud" if llm_config.get("fallback") is False and llm_config["base_url"] != _LOCAL_API_URL else "local",
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_mirofish_tool.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tools/mirofish_tool.py tests/test_mirofish_tool.py
git commit -m "feat: add MiroFishSimulation tool with complexity-based LLM routing

Simple sims (<40 agents, <20 iters) use local vLLM. Complex sims
escalate to cloud API if configured, fall back to local with warning
if not. 9 tests covering routing, execution, errors, and schema."
```

---

### Task 3: Register MiroFishSimulation in the tool registry

**Files:**
- Modify: `agents/tools/registry.py`

- [ ] **Step 1: Read agents/tools/registry.py to find the registration pattern**

Look for where existing tools are imported and registered, and where role-based tool sets are defined.

- [ ] **Step 2: Import and register the tool**

Add the import near the other tool imports:

```python
from .mirofish_tool import MiroFishSimulationTool
```

Add registration where other tools are registered (in the registry setup function or class):

```python
registry.register(MiroFishSimulationTool())
```

- [ ] **Step 3: Add to role-based tool sets**

In the role-to-tools mapping, add `"MiroFishSimulation"` to the tool sets for:
- `cto` (architecture decisions)
- `backend_engineer` (integration predictions)
- `qa_engineer` (risk assessment)
- `devops_engineer` (deployment risk)

Do NOT add it to `frontend_engineer` or assistant roles — frontend and research assistants don't need simulation.

- [ ] **Step 4: Run existing tool tests to verify nothing broke**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/test_tool_system.py -v -x`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add agents/tools/registry.py
git commit -m "feat: register MiroFishSimulation tool for CTO, backend, QA, DevOps roles"
```

---

### Task 4: Remove the old simulation module

**Files:**
- Delete: `agents/simulation.py`
- Delete: `agents/simulation_adapters.py`
- Delete: `agents/simulation_budget.py`
- Delete: `tests/test_simulation.py`
- Modify: `agents/workflow_factory.py` — remove simulation adapter registration
- Modify: `agents/specialist_nodes.py` — remove clarification simulation
- Modify: `agents/parallel_subtasks.py` — remove integration simulation sidecar
- Modify: `agents/aggregator.py` — remove simulation report formatting
- Modify: `agents/skill_generator.py` — remove skill vetting simulation

This is the most delicate task — 6 files import from simulation. Each removal must be surgical.

- [ ] **Step 1: Read each importing file to understand the exact lines to change**

Read these files and note the simulation-related imports and call sites:
- `agents/workflow_factory.py` (line 37, line 100)
- `agents/specialist_nodes.py` (lines 12-14, lines 330-371, lines 664-700)
- `agents/parallel_subtasks.py` (lines 23-26, lines 215-225, lines 310-316)
- `agents/aggregator.py` (line 17)
- `agents/skill_generator.py` (line 256)

- [ ] **Step 2: Update agents/workflow_factory.py**

Remove the simulation adapter import (line 37):
```python
# DELETE: from .simulation import register_simulation_adapters
```

Remove the registration call (line ~100):
```python
# DELETE: register_simulation_adapters(registry, self._base_model)
```

- [ ] **Step 3: Update agents/specialist_nodes.py**

Remove simulation imports (lines 12-14):
```python
# DELETE: from .simulation import simulate_clarification, format_clarification_for_spec
```

At the call sites (lines ~330-371 and ~664-700), replace the simulation-based clarification short-circuit with a direct pass-through. Where the code currently does:

```python
clar_result = simulate_clarification(...)
if clar_result.resolved and clar_result.answers:
    # inject answers into spec
```

Replace with:

```python
# Clarification goes directly to human (MiroFish handles simulation externally)
```

Remove the simulation call and let the clarification flow through to the human as-is.

- [ ] **Step 4: Update agents/parallel_subtasks.py**

Remove simulation imports (lines 23-26):
```python
# DELETE: from .simulation import SimulationReport, run_integration_simulation, format_simulation_for_aggregator
```

Remove the simulation sidecar submission (lines ~215-225):
```python
# DELETE: sim_future = executor.submit(run_integration_simulation, ...)
```

Remove the simulation result merge (lines ~310-316):
```python
# DELETE: simulation_report, simulation_conflicts, simulation_risk_level, simulation_skipped
```

Ensure the parallel subtask execution still works without the simulation sidecar — it should just run the subtasks without the extra prediction step.

- [ ] **Step 5: Update agents/aggregator.py**

Remove simulation import (line 17):
```python
# DELETE: from .simulation import format_simulation_for_aggregator, SimulationReport
```

Remove any references to `format_simulation_for_aggregator` in the aggregation logic. The aggregator should still combine subtask outputs — just without the simulation supplement.

- [ ] **Step 6: Update agents/skill_generator.py**

Remove the lazy simulation import (line ~256):
```python
# DELETE: from .simulation import vet_skill_with_simulation, _VET_SKILLS_ENABLED
```

In the `_vet_skill_if_low_confidence()` method, remove the simulation-based vetting. Skills with low confidence should fall through without the vetting step (or log a message that vetting is no longer available inline).

- [ ] **Step 7: Delete the old simulation files**

```bash
rm agents/simulation.py agents/simulation_adapters.py agents/simulation_budget.py tests/test_simulation.py
```

- [ ] **Step 8: Run the full test suite to verify nothing broke**

Run: `cd ~/Repos/Vibe-Stack && python -m pytest tests/ -x -m "not e2e" --no-header -q`
Expected: All tests PASS (minus the 41 deleted simulation tests)

- [ ] **Step 9: Commit**

```bash
git add -A agents/simulation.py agents/simulation_adapters.py agents/simulation_budget.py tests/test_simulation.py
git add agents/workflow_factory.py agents/specialist_nodes.py agents/parallel_subtasks.py agents/aggregator.py agents/skill_generator.py
git commit -m "refactor: remove inline simulation module (replaced by MiroFish service)

Remove simulation.py, simulation_adapters.py, simulation_budget.py and
all 6 integration points. Simulation is now an external service invoked
via the MiroFishSimulation tool rather than inline LLM calls."
```

---

### Task 5: Update documentation and env vars

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add MiroFish + Zep env vars to .env.example**

Add a new section after the cloud API keys section:

```env
# ── MiroFish Simulation ───────────────────────────────────────
# MIROFISH_URL=http://mirofish:5001        # MiroFish backend URL
# MIROFISH_COMPLEXITY_AGENT_THRESHOLD=40   # Agent count trigger for cloud LLM
# MIROFISH_COMPLEXITY_ITER_THRESHOLD=20    # Iteration count trigger for cloud LLM
# MIROFISH_LLM_API_URL=http://host.docker.internal:8000/v1  # Local vLLM (default)
# MIROFISH_LLM_MODEL=QuantTrio/Qwen3.5-9B-AWQ              # Local model
# MIROFISH_LLM_CLOUD_API_URL=             # Cloud API for complex sims
# MIROFISH_LLM_CLOUD_API_KEY=             # Cloud API key
# MIROFISH_LLM_CLOUD_MODEL=              # Cloud model name

# ── Zep (self-hosted agent memory for MiroFish) ──────────────
# ZEP_API_KEY=                             # Zep API key (if auth enabled)
# ZEP_URL=http://zep:8000                 # Zep service URL
```

Also remove the old `VIBE_SIM_*` variables if they exist in .env.example.

- [ ] **Step 2: Update README.md infrastructure services table**

Add MiroFish and Zep to the infrastructure services table:

```markdown
| MiroFish | Multi-agent simulation engine | 5001 |
| Zep | Agent memory (for MiroFish) | 8000 (internal) |
```

- [ ] **Step 3: Update CLAUDE.md simulation section**

Replace the existing `## Simulation` section with:

```markdown
## Simulation (MiroFish)

Multi-agent prediction is handled by an external MiroFish service, invoked via the `MiroFishSimulation` tool. Agents use it selectively for architecture decisions, deployment risk assessment, and integration conflict detection.

**Complexity-based LLM routing:**
- Simple simulations (<40 agents, <20 iterations) → local vLLM (free)
- Complex simulations → cloud API (if configured, else local with warning)

**Infrastructure:** MiroFish service (port 5001) + self-hosted Zep CE (pgvector + Neo4j + Graphiti) for agent memory.

Configurable via `MIROFISH_*` and `ZEP_*` environment variables. See `.env.example`.
```

- [ ] **Step 4: Commit**

```bash
git add .env.example README.md CLAUDE.md
git commit -m "docs: update env vars and docs for MiroFish integration

Add MiroFish + Zep env vars to .env.example, add MiroFish to README
infrastructure table, replace simulation section in CLAUDE.md."
```

---

### Task 6: Integration test

- [ ] **Step 1: Verify Docker services start**

```bash
cd ~/Repos/Vibe-Stack
docker compose -f docker-compose.yml -f docker-compose.infra.yml up -d mirofish zep zep-db neo4j graphiti
```

Wait for all services to become healthy. Check:
```bash
docker compose -f docker-compose.yml -f docker-compose.infra.yml ps
```

Expected: All 5 new services running and healthy.

- [ ] **Step 2: Test MiroFish connectivity from vibe container**

```bash
docker compose run --rm --no-deps --entrypoint sh vibe -c '
python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen(\"http://mirofish:5001/health\", timeout=10)
    print(\"MiroFish:\", r.status)
except Exception as e:
    print(\"MiroFish:\", str(e)[:80])
"
'
```

Expected: `MiroFish: 200`

- [ ] **Step 3: Run the full test suite**

```bash
cd ~/Repos/Vibe-Stack && python -m pytest tests/ -x -m "not e2e" --no-header -q
```

Expected: All tests pass. The 41 deleted simulation tests are gone, replaced by 9 new MiroFish tool tests.

- [ ] **Step 4: Commit and push**

```bash
git add -A
git commit -m "test: verify MiroFish integration end-to-end"
git push origin main
```
