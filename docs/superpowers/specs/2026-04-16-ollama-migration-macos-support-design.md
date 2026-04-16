# Ollama Migration + macOS Support

## Problem

Vibe Stack only runs on Linux and depends on vLLM (NVIDIA-only Docker container) for local LLM inference. This blocks macOS users entirely and adds unnecessary complexity (20GB Docker image, NVIDIA Container Toolkit, manual KV cache tuning). Ollama now supports the same tool-calling features that originally drove the vLLM choice.

## Design

### Platform Detection

Add `uname -s` detection at the top of `setup.sh`:
- `HOST_OS=linux` — existing Linux flow with distro detection
- `HOST_OS=darwin` — macOS flow with brew, skip server-hardening

macOS differences:
- No root check (Docker Desktop runs as user)
- `brew` as package manager
- Skip: iptables, fail2ban, auditd, unattended-upgrades, systemd services, SFTP workspace, SSH hardening
- `pf` firewall for Tailscale-only port access
- Caddy via `brew services` instead of systemd
- Docker Desktop check instead of Docker CE install

### Ollama Replaces vLLM (Both Platforms)

Ollama runs natively on the host (not in Docker), exposes an OpenAI-compatible API at port 11434. The existing `vibe/backends/vllm.py` works with it unmodified.

**Installation:**
- Linux: `curl -fsSL https://ollama.com/install.sh | sh`
- macOS: `brew install ollama`

**Memory Detection + Model Selection:**

On Linux, prefer GPU VRAM if NVIDIA GPU detected, fall back to system RAM. On macOS, use unified memory via `sysctl hw.memsize`.

| Memory | Model | Context | Notes |
|--------|-------|---------|-------|
| >= 40 GB | qwen3.5:27b | 65K | Full model, A6000/L40/Mac Studio |
| >= 20 GB | qwen3.5:9b | 65K | Sweet spot, 3090/Mac M2 Pro 32GB |
| 12-19 GB | qwen3.5:9b | 32K | Reduced context |
| 8-11 GB | qwen3.5:4b | 16K | Minimum viable |
| < 8 GB | None | — | Cloud-only fallback |

**Service Management:**
- Linux: `systemctl enable --now ollama`
- macOS: `brew services start ollama`

**Model Pre-pull:** `ollama pull <model>` during setup.

### docker-compose.gpu.yml Changes

Remove the `vllm` service. Keep `opensandbox` and `comfyui` — these still need NVIDIA GPUs for code sandbox and image generation respectively.

COMPOSE_FILE logic changes:
- GPU detected + Linux: `docker-compose.yml:docker-compose.infra.yml:docker-compose.gpu.yml` (for opensandbox/comfyui)
- No GPU or macOS: `docker-compose.yml:docker-compose.infra.yml`

The LLM backend is no longer tied to COMPOSE_FILE — Ollama runs on the host regardless.

### Environment Variable Changes

**New:**
- `OLLAMA_MODEL` — model name (set by auto-tuning, e.g. `qwen3.5:9b`)

**Updated defaults:**
- `VIBE_BACKEND_HOST=host.docker.internal` (was `vllm`)
- `VIBE_BACKEND_PORT=11434` (was `8000`)

**Removed from auto-tuning (kept in .env.example as reference):**
- `VLLM_MODEL`, `VLLM_MAX_MODEL_LEN`, `VLLM_MAX_NUM_SEQS`, `VLLM_GPU_MEM_UTIL`, `VLLM_QUANTIZATION`, `VLLM_TOOL_CALL_PARSER`

### Dockerfile Changes

Update default env vars:
```dockerfile
ENV VIBE_BACKEND_HOST=host.docker.internal
ENV VIBE_BACKEND_PORT=11434
```

### macOS Security

- Enable macOS `pf` firewall to block external traffic to Docker-exposed ports
- Tailscale provides the network perimeter (same as Linux)
- Skip fail2ban, auditd, unattended-upgrades (not applicable)

### What Stays the Same

- All agent code (vLLM backend code works with Ollama's OpenAI-compatible API)
- Paperclip server, DeerFlow, all infra services (containerized, platform-agnostic)
- Secrets generation, skill sources, org bootstrap, Claude Code login
- Prometheus, Grafana, health checks (VIB-58)

## Files Changed

| File | Action | What |
|------|--------|------|
| `setup.sh` | Modify | Platform detection, Ollama install, macOS support, remove vLLM tuning |
| `docker-compose.gpu.yml` | Modify | Remove vllm service, keep opensandbox + comfyui |
| `docker-compose.yml` | Modify | Update vibe backend env defaults |
| `Dockerfile` | Modify | Update default VIBE_BACKEND_HOST/PORT |
| `.env.example` | Modify | Update defaults, add OLLAMA_MODEL, document legacy VLLM_* |

## Out of Scope

- Ollama backend module (`vibe/backends/ollama.py`) — not needed, existing vLLM backend works via OpenAI-compatible API
- Windows support
- Removing opensandbox/comfyui GPU services
