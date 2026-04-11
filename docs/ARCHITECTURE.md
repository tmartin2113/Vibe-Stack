# Vibe Stack Architecture

## Service Topology

```
                    Internet
                       |
                  [ Tailscale ]
                       |
               [ Paperclip Server :3100 ]
              /        |        \
    DeerFlow LangGraph  |   DeerFlow Gateway
         :2024          |        :8001
              \         |        /
               [ Vibe Agent :8080 ]
                       |
              [ vLLM :8000 (GPU) ]
```

### Core Services (`docker-compose.yml`)

| Service | Port | Role |
|---------|------|------|
| **server** | 3100 | Paperclip control plane — task scheduling, issue tracking, agent orchestration |
| **deerflow-langgraph** | 2024 | LangGraph agent runtime — stateful workflow execution with checkpointing |
| **deerflow-gateway** | 8001 | FastAPI gateway — REST API for models, skills, memory, uploads, MCP |
| **vibe** | 8080 | Vibe agent — heartbeat-driven task execution with tool access |
| **tailscale** | — | Mesh VPN for remote access |

### Infrastructure (`docker-compose.infra.yml`)

| Service | Port | Role |
|---------|------|------|
| **searxng** | 8888 | Self-hosted metasearch engine |
| **playwright** | 3003 | Headless browser automation |
| **gitea** | 3000 | Self-hosted Git |
| **minio** | 9000/9002 | S3-compatible object storage |
| **penpot** | 9001 | Design tool (frontend + backend + exporter + postgres + redis) |
| **prometheus** | 9091 | Metrics collection + health probes |
| **grafana** | 3333 | Dashboards + alerting |
| **blackbox-exporter** | — | HTTP health probes for Prometheus |
| **zep** + **neo4j** + **graphiti** | — | Agent memory for MiroFish simulations |
| **mirofish** | 5001 | Multi-agent simulation engine |
| **paddleocr** | 8868 | OCR text extraction |
| **ssh-relay** | — | SSH tunneling for dev access |
| **dev-runner** | — | Sandboxed code execution |

### GPU Services (`docker-compose.gpu.yml`)

| Service | Port | Role |
|---------|------|------|
| **vllm** | 8000 | Local LLM inference (OpenAI-compatible API) |
| **opensandbox** | 9090 | GPU-accelerated code sandbox |
| **comfyui** | 8188 | Image generation pipeline |

## Data Flow

### Heartbeat (normal task execution)

```
1. Paperclip Server creates issue/task
2. Vibe Agent heartbeat polls for assignments
3. Agent checks out highest-priority task
4. Workflow graph executes:
   Router → Skill Loader → Memory Inject → Specialist → Critic → (loop or done)
5. Specialist calls tools (bash, file I/O, web search, etc.)
6. DeerFlow LangGraph handles research subtasks (parallel, with sandbox)
7. Results posted back to Paperclip as comments
8. Agent releases checkout, exits
```

### DeerFlow research flow

```
1. Paperclip streams task to DeerFlow via HTTP/SSE adapter
2. Lead agent receives task with skills + system prompt
3. Middleware chain: ThreadData → Uploads → Sandbox → DanglingToolCall → LoopDetection → Summarization → Title → Memory → ViewImage → SubagentLimit → Clarification
4. Agent uses tools: web_search, crawl, ls, read_file, glob, grep, write_file, str_replace, bash
5. MCP servers provide extended capabilities (MemPalace, Graphify)
6. Results streamed back to Paperclip
```

### Memory layers

```
Layer 0: MemoryStore (SQLite, BM25 + vector search) — task-scoped recall
Layer 1: MemPalace (wings/rooms/halls, temporal KG) — structured long-term
Layer 2: Graphify (AST-parsed codebase graph, Leiden communities) — structural awareness
Layer 3: DeerFlow Memory (fact extraction, debounced updates) — conversation context
```

## Key Design Decisions

- **Prompt-based adapters only** — no LoRA. Task specialization via system prompts + skills.
- **Local-first LLM** — vLLM is the default. Cloud backends (OpenAI, Anthropic) for fallback.
- **Paperclip owns orchestration** — scheduling, pod lifecycle, environment injection.
- **Per-agent skill isolation** — each agent has its own skill volume.
- **Storage is pluggable** — SQLite for dev, PostgreSQL + Redis for production.
- **Bridge pattern for passive context** — MemPalace and Graphify inject context automatically via try/except wrappers. Never blocks workflow.
- **Tool registry is role-gated** — each agent role sees only its allowed tools.
- **DeerFlow fork tracks upstream manually** — ported features, not git merge. Namespace is `src.*` not `deerflow.*`.

## Config Files

| File | Location | Purpose |
|------|----------|---------|
| `.env` | Root | All environment variables (secrets, URLs, feature flags) |
| `docker-compose.yml` | Root | Core service definitions |
| `docker-compose.infra.yml` | Root | Infrastructure services |
| `docker-compose.gpu.yml` | Root | GPU services |
| `deerflow/config.yaml` | Mounted into DeerFlow containers | Model, tools, sandbox, summarization, memory config |
| `deerflow/extensions_config.json` | Mounted into DeerFlow containers | MCP servers (MemPalace, Graphify) |
| `monitoring/prometheus/prometheus.yml` | Mounted into Prometheus | Scrape targets |
| `monitoring/grafana/dashboards/` | Mounted into Grafana | Pre-built dashboards |
