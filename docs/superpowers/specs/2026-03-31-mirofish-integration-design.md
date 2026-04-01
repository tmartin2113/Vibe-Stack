# MiroFish Integration Design Spec

## Overview

Replace the lightweight `simulation.py` module with the full MiroFish simulation engine (https://github.com/666ghj/MiroFish.git), deployed as a Docker Compose service. Agents invoke it via a `MiroFishSimulation` tool when they need multi-agent prediction — architecture decisions, deployment risk assessment, integration conflict detection.

## Architecture

```
Agent (vibe container)
  |
  |── MiroFishSimulation tool call
  |
  v
MiroFish service (Docker, port 5001)
  |── Backend: Python simulation engine (OASIS + CAMEL-AI)
  |── Memory: self-hosted Zep (Docker, port 8001)
  |── LLM: complexity-based routing
  |       |── Simple (<40 agents, <20 iters) → local vLLM (free)
  |       └── Complex (>=40 or >=20) → cloud API (paid)
  └── Storage: /uploads volume (persistent)
```

## Components

### 1. MiroFish Docker Service

Added to `docker-compose.infra.yml`. Runs the MiroFish backend on port 5001. Uses the official GHCR image `ghcr.io/666ghj/mirofish:latest`.

**Configuration via env vars:**
- LLM backend (local vLLM by default, cloud API for complex simulations)
- Zep connection for long-term agent memory
- Upload storage volume

**Network:** Default compose network. Agents reach it at `http://mirofish:5001`.

### 2. Self-Hosted Zep Service

Added to `docker-compose.infra.yml`. Provides long-term agent memory for MiroFish simulations without any cloud dependency. Keeps the entire stack self-contained and functional offline.

**Network:** Default compose network. MiroFish reaches it at `http://zep:8001`.

### 3. MiroFishSimulation Tool

New tool in `agents/tools/mirofish_tool.py` implementing the existing `Tool` interface from `agents/tools/registry.py`.

**Parameters:**
- `seed_material` (str, required) — the input text/scenario to simulate (code architecture, deployment plan, spec, etc.)
- `agent_count` (int, optional, default: 20) — number of simulated agents
- `iterations` (int, optional, default: 10) — simulation iteration count
- `question` (str, optional) — specific question to answer via the simulation

**Returns:** `ToolResult` with the prediction report text.

**Complexity Router (inside the tool):**

Before starting a simulation, the tool checks two thresholds:

```python
if agent_count >= AGENT_THRESHOLD or iterations >= ITER_THRESHOLD:
    # Use cloud API (higher capability, paid)
    llm_config = cloud_config
else:
    # Use local vLLM (free, sufficient for simple sims)
    llm_config = local_config
```

Default thresholds: 40 agents, 20 iterations (matching MiroFish's own recommendation to "test with fewer than 40 iterations before full deployment").

If cloud API credentials are not configured and the simulation exceeds thresholds, the tool falls back to local vLLM with a warning in the output.

### 4. Remove Existing Simulation Module

Delete the lightweight MiroFish-inspired simulation and all its references:

- `agents/simulation.py` — main simulation module
- `agents/simulation_adapters.py` — prompt adapters for simulation personas
- `agents/simulation_budget.py` — VRAM budget assessment

Remove all imports and calls to these modules from:
- `agents/graph.py` / `agents/graph_nodes.py` — simulation sidecar integration
- `agents/nodes.py` — clarification simulation short-circuit
- `agents/workflow_factory.py` — simulation adapter registration
- Any test files that test simulation functionality

## Environment Variables

```env
# ── MiroFish Simulation ───────────────────────────────────────
MIROFISH_URL=http://mirofish:5001
MIROFISH_COMPLEXITY_AGENT_THRESHOLD=40
MIROFISH_COMPLEXITY_ITER_THRESHOLD=20

# MiroFish LLM — local vLLM by default, cloud for complex simulations
MIROFISH_LLM_API_URL=http://host.docker.internal:8000/v1
MIROFISH_LLM_MODEL=QuantTrio/Qwen3.5-9B-AWQ
# MIROFISH_LLM_CLOUD_API_URL=           # Cloud API for complex sims
# MIROFISH_LLM_CLOUD_API_KEY=           # Cloud API key
# MIROFISH_LLM_CLOUD_MODEL=            # Cloud model name

# ── Zep (self-hosted agent memory for MiroFish) ──────────────
ZEP_URL=http://zep:8001
```

## File Changes

| File/Dir | Action | Description |
|----------|--------|-------------|
| `docker-compose.infra.yml` | Modify | Add `mirofish` and `zep` services |
| `agents/tools/mirofish_tool.py` | Create | MiroFishSimulation tool with complexity routing |
| `agents/tools/__init__.py` or registration | Modify | Register MiroFishSimulation tool |
| `agents/simulation.py` | Delete | Replaced by MiroFish service |
| `agents/simulation_adapters.py` | Delete | Replaced by MiroFish service |
| `agents/simulation_budget.py` | Delete | Replaced by MiroFish service |
| `agents/graph.py` | Modify | Remove simulation sidecar references |
| `agents/graph_nodes.py` | Modify | Remove simulation node references |
| `agents/nodes.py` | Modify | Remove clarification simulation short-circuit |
| `agents/workflow_factory.py` | Modify | Remove simulation adapter registration |
| `tests/test_simulation.py` | Delete | Replace with MiroFish tool tests |
| `.env.example` | Modify | Add MiroFish + Zep env vars |
| `CLAUDE.md` | Modify | Update simulation section to reference MiroFish |
| `README.md` | Modify | Add MiroFish to infrastructure services table |

## Design Decisions

- **Tool, not workflow node.** Not every task needs simulation. Making it a tool lets agents invoke it selectively for architecture decisions, deployment plans, and complex integrations. Routine work (CSS fixes, test writing) skips it entirely.
- **Complexity-based routing, not quality-based.** Pre-screening by agent count and iteration count is cheap and deterministic. Quality-based routing would require running the full simulation locally first, wasting GPU time on failures. Complexity is known upfront.
- **Self-hosted Zep.** Keeps the stack fully self-contained. No cloud dependency for core simulation functionality. The stack works offline.
- **Graceful degradation.** If cloud credentials aren't configured and a simulation exceeds complexity thresholds, fall back to local vLLM with a warning rather than failing. The simulation may be lower quality but still provides value.
- **Shared vLLM.** MiroFish points at the same vLLM instance the DeerFlow assistants use. No additional GPU resources needed for simple simulations.
