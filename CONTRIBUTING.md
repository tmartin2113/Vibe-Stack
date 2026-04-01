# Contributing to Vibe-Stack

Practical guide for contributors. For full architecture details, see [CLAUDE.md](./CLAUDE.md).

---

## 1. Getting Started

### Clone and install

```bash
git clone <repo-url> && cd Vibe-Stack
pip install -r requirements.txt
cp .env.example .env   # then edit as needed
```

### Start the stack

```bash
sudo ./setup.sh        # First-time: installs deps, configures services, bootstraps org
docker compose up -d    # Start all services
```

vLLM starts automatically on port 8000. The model is auto-selected by `setup.sh` based on GPU VRAM.

### Run the test suite

```bash
python -m pytest tests/ -x -o "addopts="
```

The `-o "addopts="` clears any default pytest flags from config. There are ~2891 tests across 46 files; expect them to pass without a running LLM since tests mock all LLM calls.

### Run in interactive mode

For quick manual testing:

```bash
python -m agents.main
```

This accepts a single request on stdin, runs the full workflow, and prints the result. Set `DEV_MODE=true` for debug logging.

### Run health checks

```bash
python -m agents.main --doctor
```

Validates LLM connectivity, sandbox availability, hardware, and downstream services.

---

## 2. Architecture Overview

See [CLAUDE.md](./CLAUDE.md) for the full reference. Here is the essential mental model.

### Pipeline

```
Router --> Skill Loader --> Spec Builder --> Specialist --> Critic --+
                                                                    |
                              (loop if score < threshold) <---------+
                              (finish if score >= threshold) ---------> Result
```

Each node receives and returns an `AgentState` (a `TypedDict` defined in `agents/state.py`). The graph is built in `agents/graph.py`; node implementations live in `agents/nodes.py`.

### Adapter pattern

All task specialization happens via **prompt-based adapters** -- there are no LoRA weights or fine-tuned models. A single base model serves every task type; the `PromptAdapter` (in `agents/adapters.py`) injects task-specific system prompts and generation parameters. The `AdapterRegistry` maps task types to their adapters.

### Configuration

`agents/config.py` defines a hierarchy of dataclasses:

```
SystemConfig
  +-- ModelConfig          (model name, backend type)
  +-- GenerationConfig     (per-task temperature, top_p, max_tokens)
  +-- WorkflowConfig       (iteration limits, timeouts, retry settings)
  +-- SkillsConfig         (remote skill sources, security toggles)
  +-- PaperclipConfig      (control plane integration)
  +-- SpendingConfig       (cost tracking / circuit breaker)
  +-- MessageStoreConfig   (inter-agent messaging)
  +-- ...
```

Use `SystemConfig.from_env()` to build config from environment variables.

---

## 3. Adding a New Backend

Backends live in `vibe/backends/`. Currently: `vllm.py`, `openai_backend.py`.

### Step 1 -- Implement `BackendBase`

Create `vibe/backends/my_backend.py`:

```python
from vibe.backends.base import BackendBase
from typing import Dict, Any, Optional

class MyBackend(BackendBase):
    def __init__(self, host: str, port: int, model: Optional[str] = None, **kwargs):
        super().__init__(host, port, model=model)

    def generate(self, prompt: str, max_tokens: Optional[int] = None,
                 temperature: Optional[float] = None,
                 stop: Optional[list] = None) -> Dict[str, Any]:
        # Must return {"text": "...", "usage": {"prompt_tokens": N, "completion_tokens": N}}
        ...

    def health_check(self) -> bool:
        ...
```

The `BackendBase` constructor sets `self.host`, `self.port`, `self.timeout`, `self.model`, and `self.base_url`.

### Step 2 -- Register in `agents/llm_backend.py`

Import your backend and wire it into `LLMBackend.__init__()` with a new backend type string.

### Step 3 -- Add a health check to `agents/doctor.py`

Add a check function that instantiates your backend and calls `health_check()`. Follow the existing pattern for vLLM.

### Step 4 -- Add tests

Create `tests/test_my_backend.py`. Mock HTTP calls; do not require a running server.

---

## 4. Adding a New Skill

Skills are markdown files (`SKILL.md`) loaded at runtime by the skill loader.

### SKILL.md format

```markdown
---
name: my-skill
version: 1.0.0
task-types: [code, test_generation]
allowed-tools: [PythonExecutor, FileReader]
description: Short description of what this skill does.
---

## Instructions

Prompt content that gets injected into the specialist's system prompt.
```

### Three-tier registry

| Tier | Location | Trust | How to add |
|------|----------|-------|------------|
| **builtin** | Shipped with the repo | Highest | PR to this repo |
| **approved** | Vetted remote sources (see `SkillsConfig`) | Medium | PR to the source repo |
| **community** | Any remote source | Lowest | Extra security scanning |

Skills are loaded by `agents/skill_loader.py` and filtered by the router's classified task type.

### Security requirements

Every skill passes through `agents/skill_security.py`:

1. **Name/path validation** -- no path traversal, restricted characters.
2. **Content scanning** -- AST + regex checks for dangerous patterns (imports, exec, subprocess).
3. **Tool permission enforcement** -- only tools listed in `allowed-tools` frontmatter are available at runtime.
4. **SHA-256 integrity** -- content hash verified on load.

If your skill needs a tool not in the default set, declare it explicitly in `allowed-tools`.

---

## 5. Adding a New Tool

Tools live in `agents/tools/`. Each tool is a class inheriting from `Tool` (defined in `agents/tools/registry.py`).

### Step 1 -- Implement the tool

Create `agents/tools/my_tool.py`:

```python
from agents.tools.registry import Tool, ToolCategory, ToolResult
from typing import Dict, Any

class MyTool(Tool):
    def __init__(self):
        super().__init__(
            name="MyTool",
            description="What this tool does (the LLM reads this).",
            category=ToolCategory.SPECIALIZED,
        )

    def execute(self, **kwargs) -> ToolResult:
        self.validate_params(**kwargs)
        # Do work...
        return ToolResult(success=True, output="result")

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "The input value"},
            },
            "required": ["input"],
        }
```

### Step 2 -- Register it

Import and register your tool in the tool registry setup (see existing registrations in `agents/tools/__init__.py` or wherever the `ToolRegistry` is populated).

### Step 3 -- Test it

Create `tests/test_my_tool.py`. Test both success and error paths.

---

## 6. Testing

### Conventions

- **File naming**: `tests/test_<module>.py` mirrors the source module.
- **Framework**: pytest with `unittest.mock.MagicMock` / `patch`.
- **No real LLM calls**: Always mock `LLMBackend.generate()` and similar methods. Tests must pass without a running vLLM server.
- **No real network calls**: Mock HTTP requests, sandbox interactions, and file I/O to external paths.

### Running tests

```bash
# Full suite
python -m pytest tests/ -x -o "addopts="

# Single file
python -m pytest tests/test_router.py -x

# Keyword filter
python -m pytest tests/ -k "test_critic_scoring" -x

# Verbose output
python -m pytest tests/ -x -v -o "addopts="
```

### Writing a test

```python
from unittest.mock import MagicMock, patch

def test_router_classifies_code_task():
    mock_backend = MagicMock()
    mock_backend.generate.return_value = {"text": "code"}
    router = Router(backend=mock_backend)
    result = router.classify("Write a Python function")
    assert result == "code"
```

---

## 7. Code Style

- **Type hints**: Use them on all function signatures and return types.
- **Logging**: Use `logging.getLogger(__name__)` -- never `print()` in library code.
- **Dataclasses**: Use `@dataclass` for configuration, state, and result objects (see `agents/config.py` for examples).
- **Defense-in-depth for skills**: Each security layer (AST scanning, tool permissions, integrity checks, sandbox isolation) is independent. Do not rely on a single layer.
- **Imports**: Standard library first, then third-party, then local. Use relative imports within packages (`from .llm_retry import ...`).
- **Error handling**: Raise specific exceptions. Catch narrowly. Log errors with context.
- **No global mutable state**: Pass configuration and dependencies explicitly.

---

## 8. Environment Variables

Quick reference for the most commonly needed variables. See `.env.example` for the full list.

| Variable | Purpose | Default |
|----------|---------|---------|
| `MODEL_NAME` | LLM model name | `Qwen/Qwen3.5-9B` |
| `DEV_MODE` | Enable debug logging, disable Mattermost | `false` |
| `VIBE_SANDBOX_BACKEND` | `opensandbox` (Docker) or `subprocess` | `subprocess` |
| `VIBE_SKILLS_DIR` | Root directory for skill tiers | `~/.vibe/skills` |
| `PAPERCLIP_API_URL` | Paperclip control plane URL | -- |
| `PAPERCLIP_AGENT_ID` | Agent identity for Paperclip | -- |
| `VIBE_TASK_TYPE` | Override task type (used by Paperclip) | -- |
| `MESSAGE_STORE_PATH` | SQLite path for inter-agent messages | -- |
| `VIBE_MSG_MAX_MESSAGES` | FIFO eviction cap for message store | `5000` |
| `VIBE_MSG_DEFAULT_TTL` | Default message TTL in seconds | `604800` |
| `VIBE_SPEND_ENABLED` | Enable/disable spending tracker | `true` |
| `VIBE_SPEND_MAX_CENTS` | Max cents per rolling window | `500` |
| `MATTERMOST_WEBHOOK_URL` | Webhook URL for Mattermost posting | -- |
| `MATTERMOST_BOT_TOKEN` | Bot token for daemon mode | -- |
