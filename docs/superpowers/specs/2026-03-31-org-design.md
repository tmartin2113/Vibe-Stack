# Vibe Stack Org Design Spec

## Overview

Vibe Stack operates as an autonomous software engineering company with a flat hierarchy optimized for both self-development (improving Vibe Stack, Paperclip, DeerFlow) and external client projects. Every senior role runs on Claude Opus for deep reasoning. Every senior gets a paired DeerFlow assistant running on local vLLM for research and grunt work.

## Org Structure

```
             Human (CEO)
                  |
                 CTO  (Claude Opus)
                  |── cto-assistant (vLLM)
            /     |        \        \
    Sr. Backend  Sr. Frontend  Sr. QA  Sr. DevOps
    (Opus)       (Opus)        (Opus)  (Opus)
      |            |             |       |
    backend-     frontend-     qa-     devops-
    assistant    assistant   assistant assistant
    (vLLM)       (vLLM)      (vLLM)   (vLLM)
```

10 agents total. 5 seniors on Claude Opus (API), 5 DeerFlow assistants on Qwen3.5-9B-AWQ (local vLLM on RTX 3090).

## Agent Naming Convention

Assistants are named by appending `-assistant` to their senior's shortname. This convention is how the CTO and engineers know which assistant to assign research to.

| Senior Agent | ShortName | Assistant Agent | ShortName |
|-------------|-----------|----------------|-----------|
| CTO | `cto` | CTO Assistant | `cto-assistant` |
| Sr. Backend Engineer | `backend` | Backend Assistant | `backend-assistant` |
| Sr. Frontend Engineer | `frontend` | Frontend Assistant | `frontend-assistant` |
| Sr. QA Engineer | `qa` | QA Assistant | `qa-assistant` |
| Sr. DevOps Engineer | `devops` | DevOps Assistant | `devops-assistant` |

## Adapter Mapping

| Agent Type | LLM Backend | Adapter Type | Cost |
|------------|-------------|-------------|------|
| CTO + 4 Senior Engineers | Claude Opus via Anthropic API | `claude_local` | API credits |
| 5 DeerFlow Assistants | Qwen3.5-9B-AWQ via local vLLM | `deerflow` | Free (local GPU) |

## Responsibility Boundaries

### CTO

**Owns:** Architecture specs (`ARCHITECTURE.md`), task decomposition, code review, branch management (create feature branches, push after review), cross-agent consistency checks, quality gates.

**Does NOT do:** Write production code. Delegates all implementation to senior engineers.

**Workflow:** Architect (write spec, create branch) -> Delegate (create subtasks + paired research tasks) -> Review (audit code, create fix subtasks if needed, push branch).

### Sr. Backend Engineer

**Owns:** APIs, server logic, databases, authentication, third-party integrations, DeerFlow/LangGraph Python code, Paperclip backend modifications.

**Does NOT do:** UI components, CSS/styling, deployment scripts, infrastructure config.

### Sr. Frontend Engineer

**Owns:** UI components, client-side code, styling/CSS, UX implementation, browser-side logic, Paperclip UI modifications.

**Does NOT do:** Server routes, database schemas, infrastructure config.

### Sr. QA Engineer

**Owns:** Test plans, test suites (unit/integration/e2e), security audits, quality gate verification, coverage analysis, vulnerability scanning.

**Does NOT do:** Feature implementation, infrastructure changes, production code (except test code).

### Sr. DevOps Engineer

**Owns:** Docker/Compose configuration, CI/CD pipelines, deployment scripts, infrastructure config (Caddy, iptables, systemd), monitoring, networking, Tailscale setup.

**Does NOT do:** Business logic, UI code, application features.

### DeerFlow Assistants (all 5)

**Owns:** Pre-flight research before senior heartbeats, codebase exploration, documentation lookups, boilerplate/scaffold drafts, library comparisons, best-practice summaries.

**Does NOT do:** Final implementation decisions, code review, task decomposition, direct commits to feature branches.

## Task Flow

### Standard Feature/Bug Flow

1. **Human** creates a high-level issue in Paperclip UI (e.g., "Build user dashboard" or "Fix auth token refresh bug").

2. **CTO** wakes on heartbeat:
   - Reads the issue, understands scope and requirements.
   - Creates a feature branch: `feature/<sanitized-issue-title>`.
   - Writes `ARCHITECTURE.md` to the project root (tech stack, patterns, conventions, build/test commands).
   - Identifies which senior engineers are needed.
   - For each engineer, creates **two subtasks**:
     - **Research subtask** -> assigned to `<role>-assistant` (e.g., `backend-assistant`)
     - **Implementation subtask** -> assigned to the senior (e.g., `backend`), marked as **blocked by** the research subtask
   - Exits.

3. **DeerFlow assistants** wake (research subtasks are unblocked):
   - Run pre-flight research: explore codebase, read docs, gather context.
   - Post a research brief as a comment on their own research subtask (the engineer reads sibling comments when they wake).
   - Mark their research subtask as `done`.

4. **Paperclip** auto-unblocks the implementation subtasks.

5. **Senior engineers** wake with research context already available:
   - Read the research brief + `ARCHITECTURE.md`.
   - Execute their subtask (write code, tests, configs).
   - Post a `## Handoff` comment summarizing what they built, files changed, integration notes.
   - Mark subtask as `done`.
   - Can create **ad-hoc research subtasks** for their own assistant mid-task if they need deeper investigation.

6. **CTO** wakes again (all subtasks complete):
   - Runs structured review (security, build, tests, cross-agent consistency).
   - Fixes trivial issues directly.
   - Creates fix subtasks for substantial issues, assigned to the responsible engineer.
   - After fixes resolve (or after 2 review passes), pushes the feature branch.
   - Marks the parent task as `done`.

### Ad-Hoc Research Flow

At any point during implementation, a senior engineer can:

1. Create a subtask with research instructions.
2. Assign it to their paired assistant (e.g., `backend` assigns to `backend-assistant`).
3. Continue working on other aspects or mark themselves `blocked` waiting for the research.
4. Assistant completes research, posts findings as a comment.
5. Engineer resumes with the new context.

### Dual Assignment for Assistants

DeerFlow assistants accept work from **two sources**:
- **CTO** — pre-flight research subtasks created during task decomposition
- **Their paired senior** — ad-hoc research subtasks created during implementation

No special mechanism needed. Both the CTO and the paired senior know the assistant's shortname by convention (`<role>-assistant`) and can create subtasks assigned to it.

## Setup Automation

The `setup.sh` script (or a post-onboard bootstrap step) should:

1. Create all 10 agents in Paperclip with correct roles, names, and shortnames.
2. Set each agent's adapter type (`claude_local` for seniors, `deerflow` for assistants).
3. Write all agent IDs to `.env` for the vibe container.
4. Generate JWT credentials from the shared secret (no manual API key creation).
5. Copy AGENTS.md instruction files into each agent's instruction path.

### Agent Creation Sequence

Agents should be created in this order (managers before reports):

1. CTO
2. CTO Assistant
3. Sr. Backend + Backend Assistant
4. Sr. Frontend + Frontend Assistant
5. Sr. QA + QA Assistant
6. Sr. DevOps + DevOps Assistant

### Environment Variables

Each vibe agent container needs:

```env
PAPERCLIP_AGENT_ID=<uuid>           # This agent's ID
PAPERCLIP_COMPANY_ID=<uuid>         # Company ID
PAPERCLIP_API_URL=http://server:3100
PAPERCLIP_AGENT_JWT_SECRET=<secret> # Shared secret for JWT auto-generation
```

The JWT is auto-generated per heartbeat by `PaperclipClient` — no static API keys.

## Infrastructure Context

- **Hardware:** Z690 motherboard, 16 cores, 64GB RAM, RTX 3090 (22GB VRAM)
- **Local LLM:** Qwen3.5-9B-AWQ via vLLM (context=32768, mem=0.92)
- **Cloud LLM:** Claude Opus via Anthropic API
- **Orchestration:** Paperclip control plane at `https://vibe.tail2fb792.ts.net`
- **Services:** Docker Compose with Caddy reverse proxy, SearXNG, Gitea, MinIO, Penpot, Playwright

## Design Decisions

- **CEO is human.** No autonomous CEO agent. The human creates high-level issues and the CTO decomposes them. This keeps human-in-the-loop for strategic decisions and avoids runaway agent chains.
- **CTO is the only management layer.** Dropped the COO role. Engineering is the core competency; ops tasks (QA, DevOps) report directly to the CTO alongside engineering roles.
- **All seniors on Opus.** Every role that makes implementation decisions uses the most capable model. Cost is managed by offloading research/exploration to free local inference.
- **DeerFlow assistants are paired, not pooled.** Each senior gets their own assistant rather than sharing a pool. This keeps context isolation clean and avoids contention.
- **Convention-based pairing.** Assistants are named `<role>-assistant`. No configuration needed — agents discover their assistant by name convention.
- **Pre-flight + on-demand research.** Assistants do both: CTO assigns pre-flight research that blocks engineer subtasks (so engineers wake with context), and engineers can assign ad-hoc research mid-task.
- **Security merged into QA.** Security auditing is a quality function. The `security_audit` builtin task type already exists as an adapter. Sr. QA handles both testing and security.
- **DeerFlow/UX merged into existing roles.** DeerFlow work is Python backend. UX design is frontend implementation. No need for separate agents.
