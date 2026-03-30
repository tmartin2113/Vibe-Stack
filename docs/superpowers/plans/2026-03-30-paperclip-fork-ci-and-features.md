# Paperclip Fork CI & Auto-Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the paperclip fork (`tmartin2113/paperclip`) publish pre-built Docker images to GHCR and auto-provision DeerFlow assistants for cloud adapter agents.

**Architecture:** GitHub Actions CI builds three images (paperclip-server, deerflow-langgraph, deerflow-gateway) on push to the release branch. The server gains a feature where creating a cloud adapter agent auto-creates a paired DeerFlow assistant when vLLM is available.

**Tech Stack:** GitHub Actions, Docker, TypeScript (paperclip server), GHCR

**Spec:** `docs/superpowers/specs/2026-03-30-vibe-stack-public-repo-design.md`

**Repo:** `/home/prime/paperclip` (branch: `feat/deerflow-adapter`)

**Important:** This plan depends on the DeerFlow directory being committed to the paperclip fork. Task 1 handles committing all pending work first.

---

### Task 1: Commit All Pending Fork Work

**Files:**
- Commit: `.dockerignore` (already modified — `.env` exclusion)
- Commit: `packages/adapters/deerflow/src/server/skills.ts` (must be recreated — lost during stash)
- Commit: `packages/adapters/deerflow/src/index.ts`
- Commit: `packages/adapters/deerflow/src/server/index.ts`
- Commit: `server/src/adapters/registry.ts`
- Commit: `server/src/routes/agents.ts`
- Commit: `server/src/routes/company-skills.ts`
- Commit: `server/src/services/company-skills.ts`
- Commit: `skills/paperclip/references/company-skills.md`
- Stage: `deerflow/` directory (currently untracked)

- [ ] **Step 1: Recreate the missing `skills.ts`**

The file was lost during a stash operation. It must export three functions: `readDesiredSkillContent`, `listSkills`, `syncSkills`.

Read the Claude-local adapter's skills.ts for reference:
```bash
cat packages/adapters/claude-local/src/server/skills.ts
```

Read execute.ts to understand how `readDesiredSkillContent` is called:
```bash
grep -A5 -B5 'readDesiredSkillContent\|skillsContent' packages/adapters/deerflow/src/server/execute.ts
```

Read index.ts to understand the `listSkills` and `syncSkills` exports:
```bash
cat packages/adapters/deerflow/src/server/index.ts
```

Create `packages/adapters/deerflow/src/server/skills.ts` that:
- Exports `readDesiredSkillContent(config)` — reads `config.paperclipRuntimeSkills` and returns an array of `{ name: string, content: string }` objects
- Exports `listSkills(ctx)` — returns an `AdapterSkillSnapshot` listing available skills
- Exports `syncSkills(ctx, desiredSkills)` — returns an `AdapterSkillSnapshot` (DeerFlow skills are passed via run context, so sync is a no-op that returns current state)

Model the implementation after the claude-local adapter's skills.ts but adapted for DeerFlow's context-passing model (skills are injected into LangGraph run context, not written to filesystem).

- [ ] **Step 2: Verify the TypeScript compiles**

```bash
cd /home/prime/paperclip && pnpm --filter @paperclipai/server build 2>&1 | tail -5
```

Expected: build succeeds with no errors.

- [ ] **Step 3: Add the deerflow directory to git**

The `deerflow/` directory contains docker-owned files with root permissions. Only add the source files:

```bash
git add deerflow/backend/Dockerfile deerflow/backend/pyproject.toml deerflow/backend/uv.lock deerflow/backend/src/ deerflow/config.yaml deerflow/skills/
git add deerflow/backend/langgraph.json 2>/dev/null  # may not exist
```

Check what's staged:
```bash
git diff --cached --stat | tail -10
```

Do NOT add: `deerflow/backend/.deer-flow/`, `deerflow/backend/__pycache__/`, `deerflow/backend/.venv/`, `deerflow/backend/Oh_My_Gauss/`

- [ ] **Step 4: Add a `.gitignore` for the deerflow directory**

Create `deerflow/.gitignore`:

```
.deer-flow/
__pycache__/
*.pyc
.venv/
Oh_My_Gauss/
```

```bash
git add deerflow/.gitignore
```

- [ ] **Step 5: Commit all pending work**

```bash
git add .dockerignore
git add packages/adapters/deerflow/src/
git add server/src/adapters/registry.ts server/src/routes/agents.ts
git add server/src/routes/company-skills.ts server/src/services/company-skills.ts
git add skills/paperclip/references/company-skills.md
git commit -m "feat: DeerFlow adapter, company skills, and .dockerignore fix

- Add DeerFlow adapter with skills support for Vibe Stack integration
- Add company skills routes and service
- Add .env to .dockerignore (fixes embedded-postgres breakage when
  baked .env overrides DATABASE_URL)
- Add deerflow/ directory with LangGraph backend source"
```

- [ ] **Step 6: Verify the build works in Docker**

```bash
cd ~/Repos/Vibe-Stack && docker compose build server 2>&1 | tail -10
```

Expected: build completes successfully.

- [ ] **Step 7: Push to fork**

```bash
git push fork feat/deerflow-adapter
```

---

### Task 2: Create GitHub Actions CI for GHCR Images

**Files:**
- Create: `.github/workflows/docker-images.yml`

- [ ] **Step 1: Create the workflow file**

```yaml
name: Build and Push Docker Images

on:
  push:
    branches:
      - feat/deerflow-adapter
      - main
    tags:
      - 'v*'

env:
  REGISTRY: ghcr.io
  SERVER_IMAGE: ghcr.io/${{ github.repository_owner }}/paperclip-server
  DEERFLOW_IMAGE: ghcr.io/${{ github.repository_owner }}/deerflow

jobs:
  build-server:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.SERVER_IMAGE }}
          tags: |
            type=ref,event=branch
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}
            type=sha
            type=raw,value=latest,enable={{is_default_branch}}

      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  build-deerflow:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract metadata (langgraph)
        id: meta-langgraph
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.DEERFLOW_IMAGE }}-langgraph
          tags: |
            type=ref,event=branch
            type=semver,pattern={{version}}
            type=sha
            type=raw,value=latest,enable={{is_default_branch}}

      - name: Extract metadata (gateway)
        id: meta-gateway
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.DEERFLOW_IMAGE }}-gateway
          tags: |
            type=ref,event=branch
            type=semver,pattern={{version}}
            type=sha
            type=raw,value=latest,enable={{is_default_branch}}

      - name: Build and push langgraph image
        uses: docker/build-push-action@v6
        with:
          context: ./deerflow
          file: ./deerflow/backend/Dockerfile
          push: true
          tags: ${{ steps.meta-langgraph.outputs.tags }}
          labels: ${{ steps.meta-langgraph.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Build and push gateway image
        uses: docker/build-push-action@v6
        with:
          context: ./deerflow
          file: ./deerflow/backend/Dockerfile
          push: true
          tags: ${{ steps.meta-gateway.outputs.tags }}
          labels: ${{ steps.meta-gateway.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
          build-args: |
            DEERFLOW_MODE=gateway
```

Note: The DeerFlow Dockerfile needs to support a `DEERFLOW_MODE` build arg (or use a different CMD). Check the actual Dockerfile to determine how langgraph vs gateway are differentiated. If it's just a different command, the compose files handle it and both images can be identical — in that case, build one image and tag it twice, or use a single image with command override in compose.

- [ ] **Step 2: Verify the workflow YAML is valid**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/docker-images.yml'))" && echo "Valid YAML"
```

Expected: `Valid YAML`

- [ ] **Step 3: Commit and push**

```bash
git add .github/workflows/docker-images.yml
git commit -m "ci: add GitHub Actions workflow to build and push GHCR images

Builds three images on push to feat/deerflow-adapter or tags:
- ghcr.io/tmartin2113/paperclip-server
- ghcr.io/tmartin2113/deerflow-langgraph
- ghcr.io/tmartin2113/deerflow-gateway"

git push fork feat/deerflow-adapter
```

- [ ] **Step 4: Verify the CI runs**

```bash
gh run list --repo tmartin2113/paperclip --limit 3
```

Expected: a new workflow run in progress or completed.

- [ ] **Step 5: Verify images are published**

After CI completes:

```bash
gh api user/packages/container/paperclip-server/versions --jq '.[0].metadata.container.tags' 2>/dev/null || echo "Check https://github.com/tmartin2113?tab=packages"
```

---

### Task 3: DeerFlow Assistant Auto-Provisioning (Server Feature)

**Files:**
- Modify: `server/src/routes/agents.ts` — agent creation endpoint
- Modify: `server/src/services/agents.ts` (or equivalent) — auto-provisioning logic
- Modify: DB schema — add `assistant_agent_id` field to agents table

This is a larger feature that requires understanding the paperclip server's agent creation flow. The implementation should:

1. On agent creation with a cloud adapter type (claude-local, codex-local, etc.):
   - Check if DeerFlow gateway health endpoint returns OK
   - If yes: auto-create a linked DeerFlow agent with `assistant_agent_id` pointing back
   - If no: skip, agent works in cloud-only mode

2. On task dispatch for a cloud agent with a linked assistant:
   - Check if the assistant's DeerFlow backend is healthy
   - Route menial subtasks (tool execution, file ops, search) to the assistant
   - Keep reasoning/spec tasks on the cloud model

**This task requires deeper exploration of the agent creation code before writing the implementation.** The steps below outline the research and implementation approach.

- [ ] **Step 1: Map the agent creation flow**

```bash
grep -rn 'createAgent\|create.*agent\|POST.*agent' server/src/routes/ server/src/services/ --include='*.ts' | head -20
```

Understand:
- Which endpoint handles agent creation
- What fields are in the agent model/schema
- Where the DB schema is defined (drizzle? raw SQL?)

- [ ] **Step 2: Map the adapter type detection**

```bash
grep -rn 'adapterType\|adapter_type\|claude.local\|deerflow' server/src/ packages/adapters/ --include='*.ts' | head -20
```

Understand:
- How adapter types are identified
- Which types are "cloud" adapters vs local
- Where the DeerFlow health check would be called

- [ ] **Step 3: Add `assistantAgentId` to the agent schema**

Find the agents table schema (likely in `packages/db/src/schema/`):

```bash
grep -rn 'agents.*table\|createTable.*agent' packages/db/src/ --include='*.ts' | head -5
```

Add a nullable `assistant_agent_id` column that references the agents table.

- [ ] **Step 4: Create a migration**

Use the project's migration tooling (likely drizzle-kit):

```bash
grep -n 'migration\|drizzle-kit\|migrate' packages/db/package.json
```

Generate and apply the migration.

- [ ] **Step 5: Implement auto-provisioning in agent creation**

In the agent creation service/route, after the agent is created:

```typescript
// After creating the cloud agent:
if (isCloudAdapter(agent.adapterType)) {
  const deerflowHealthy = await checkDeerflowHealth();
  if (deerflowHealthy) {
    const assistant = await createAgent({
      ...defaultDeerflowConfig,
      name: `${agent.name} (DeerFlow Assistant)`,
      adapterType: 'deerflow',
      companyId: agent.companyId,
    });
    await updateAgent(agent.id, { assistantAgentId: assistant.id });
  }
}
```

- [ ] **Step 6: Add health check for DeerFlow gateway**

Create a utility function that checks the DeerFlow gateway health:

```typescript
async function checkDeerflowHealth(): Promise<boolean> {
  try {
    const url = process.env.DEERFLOW_GATEWAY_URL ?? 'http://deerflow-gateway:8001';
    const res = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) });
    return res.ok;
  } catch {
    return false;
  }
}
```

- [ ] **Step 7: Test the auto-provisioning**

Start the full stack locally:
```bash
cd ~/Repos/Vibe-Stack && docker compose up -d
```

Create a cloud agent via the Paperclip UI. Verify:
1. A DeerFlow assistant agent is auto-created
2. The assistant's `assistantAgentId` links back to the cloud agent
3. When vLLM is stopped, creating a cloud agent does NOT create an assistant

- [ ] **Step 8: Commit and push**

```bash
git add packages/db/src/ server/src/
git commit -m "feat: auto-provision DeerFlow assistant for cloud adapter agents

When creating an agent with a cloud adapter (claude, codex, etc.),
automatically create a paired DeerFlow assistant if vLLM is available.
The assistant handles menial tasks (tool execution, file ops, search)
while the cloud model handles reasoning and spec-building.

Adds assistant_agent_id field to agents table."

git push fork feat/deerflow-adapter
```

---

### Task 4: Dashboard Capability Tier Display

**Files:**
- Modify: UI components that display agent status

This is a UI enhancement showing "Cloud + Local" vs "Cloud Only" on the agent dashboard.

- [ ] **Step 1: Find the agent status display component**

```bash
grep -rn 'agent.*status\|AgentCard\|agent.*badge' ui/src/ --include='*.tsx' | head -10
```

- [ ] **Step 2: Add capability tier indicator**

The component should check `agent.assistantAgentId`:
- If set → show "Cloud + Local" badge
- If null → show "Cloud Only" badge

- [ ] **Step 3: Commit and push**

```bash
git add ui/src/
git commit -m "feat(ui): show agent capability tier on dashboard

Displays 'Cloud + Local' when a DeerFlow assistant is linked,
'Cloud Only' when running without local inference."

git push fork feat/deerflow-adapter
```
