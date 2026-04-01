# Org Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate creation of the 10-agent org (5 seniors + 5 DeerFlow assistants) during setup, so a fresh install produces a working engineering company with zero manual agent configuration.

**Architecture:** Replace `bootstrap-all.js` with a new `bootstrap-org.js` that creates all 10 agents via the Paperclip API, generates JWT credentials, writes agent IDs to `.env`, and copies AGENTS.md instruction files. The existing `setup.sh` runs this after Paperclip onboarding. Each agent gets its own AGENTS.md with role-specific instructions and its paired assistant's shortname.

**Tech Stack:** Node.js (runs inside Paperclip server container), Paperclip REST API, Docker Compose, bash

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `bootstrap-org.js` | Rewrite | Creates 10 agents, generates credentials, writes .env |
| `agents/cto/AGENTS.md` | Update | CTO instructions: architect, delegate with research pairs, review |
| `agents/backend-engineer/AGENTS.md` | Update | Sr. Backend instructions with assistant pairing |
| `agents/frontend-engineer/AGENTS.md` | Update | Sr. Frontend instructions with assistant pairing |
| `agents/qa-engineer/AGENTS.md` | Update | Sr. QA instructions (testing + security) with assistant pairing |
| `agents/devops-engineer/AGENTS.md` | Update | Sr. DevOps instructions with assistant pairing |
| `agents/cto-assistant/AGENTS.md` | Create | DeerFlow research assistant instructions for CTO |
| `agents/backend-assistant/AGENTS.md` | Create | DeerFlow research assistant instructions for backend |
| `agents/frontend-assistant/AGENTS.md` | Create | DeerFlow research assistant instructions for frontend |
| `agents/qa-assistant/AGENTS.md` | Create | DeerFlow research assistant instructions for QA |
| `agents/devops-assistant/AGENTS.md` | Create | DeerFlow research assistant instructions for DevOps |
| `setup.sh` | Update | Run bootstrap-org.js after Paperclip onboarding |
| `.env.example` | Update | Add new agent ID variables for 10-agent org |
| `docker-compose.yml` | Update | Ensure vibe service can run per-agent via PAPERCLIP_AGENT_ID |

---

### Task 1: Write the bootstrap-org.js script

**Files:**
- Create: `bootstrap-org.js` (rewrite from scratch, existing one is stale)

This is the core script. It authenticates with Paperclip, creates all 10 agents in the right order, and writes results to `.env`.

- [ ] **Step 1: Create the bootstrap-org.js skeleton with HTTP helpers and auth**

```javascript
#!/usr/bin/env node
/**
 * bootstrap-org.js — Create the 10-agent Vibe Stack engineering org.
 *
 * Run inside the Paperclip server container after onboarding:
 *   node bootstrap-org.js
 *
 * Idempotent — skips agents that already exist (matched by name).
 * Writes agent IDs back to .env for vibe container pickup.
 */

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── Config ────────────────────────────────────────────────────────
const API_URL = process.env.PAPERCLIP_API_URL || "http://localhost:3100";
const ADMIN_EMAIL = process.env.PAPERCLIP_ADMIN_EMAIL;
const ADMIN_PASSWORD = process.env.PAPERCLIP_ADMIN_PASSWORD;
const TAILSCALE_HOSTNAME = process.env.TAILSCALE_HOSTNAME || "localhost";
const PROJECTS_DIR = process.env.PAPERCLIP_PROJECTS_DIR
  || path.join(process.env.HOME || "/home/prime", "Projects");

if (!ADMIN_EMAIL || !ADMIN_PASSWORD) {
  console.error("Set PAPERCLIP_ADMIN_EMAIL and PAPERCLIP_ADMIN_PASSWORD in .env");
  process.exit(1);
}

// ── HTTP helper ───────────────────────────────────────────────────
function request(method, urlPath, cookie, data) {
  return new Promise((resolve, reject) => {
    const url = new URL(urlPath, API_URL);
    const opts = {
      method,
      hostname: url.hostname,
      port: url.port,
      path: url.pathname + url.search,
      headers: {
        "Content-Type": "application/json",
        Origin: `https://${TAILSCALE_HOSTNAME}`,
        Host: TAILSCALE_HOSTNAME,
      },
    };
    if (cookie) opts.headers.Cookie = cookie;

    const req = http.request(opts, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => {
        const setCookie = res.headers["set-cookie"]?.map((c) => c.split(";")[0]).join("; ");
        try {
          resolve({ status: res.statusCode, data: JSON.parse(body), cookie: setCookie || cookie });
        } catch {
          resolve({ status: res.statusCode, data: body, cookie: setCookie || cookie });
        }
      });
    });
    req.on("error", reject);
    if (data) req.write(JSON.stringify(data));
    req.end();
  });
}

// ── .env writer ───────────────────────────────────────────────────
function updateEnvVar(key, value) {
  const envPath = path.join(__dirname, ".env");
  let content = fs.existsSync(envPath) ? fs.readFileSync(envPath, "utf-8") : "";
  const regex = new RegExp(`^${key}=.*$`, "m");
  if (regex.test(content)) {
    content = content.replace(regex, `${key}=${value}`);
  } else {
    content += `\n${key}=${value}`;
  }
  fs.writeFileSync(envPath, content);
}
```

- [ ] **Step 2: Add the org definition — all 10 agents with their config**

Add this after the helper functions in `bootstrap-org.js`:

```javascript
// ── Org Definition ────────────────────────────────────────────────
// Agents are created in order. Seniors first, then their assistants.
// managerRef uses the senior's name to resolve the ID after creation.

const ORG = [
  // ── CTO ──
  {
    name: "CTO",
    shortName: "cto",
    role: "ceo",
    title: "Chief Technology Officer",
    adapterType: "claude_local",
    adapterConfig: { model: "claude-opus-4-6", effort: "high", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: true },
    systemPrompt: "You are the CTO. You architect solutions, decompose tasks into subtasks for senior engineers, and review completed work. You never write production code — you delegate. For each engineer subtask, also create a research subtask assigned to their paired assistant (named <role>-assistant). Mark the engineer subtask as blocked by the research subtask.",
    envKey: "PAPERCLIP_AGENT_ID_CTO",
    instructionsDir: "agents/cto",
  },
  {
    name: "CTO Assistant",
    shortName: "cto-assistant",
    role: "engineer",
    title: "CTO Research Assistant",
    adapterType: "deerflow",
    adapterConfig: { model: "qwen3.5-9b", effort: "medium", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a research assistant for the CTO. Your job is pre-flight research: explore codebases, read documentation, summarize findings, and draft architecture notes. Post your research brief as a comment on your task. You do not write production code or make final implementation decisions.",
    envKey: "PAPERCLIP_AGENT_ID_CTO_ASSISTANT",
    instructionsDir: "agents/cto-assistant",
  },
  // ── Sr. Backend ──
  {
    name: "Sr. Backend Engineer",
    shortName: "backend",
    role: "engineer",
    title: "Senior Backend Engineer",
    adapterType: "claude_local",
    adapterConfig: { model: "claude-opus-4-6", effort: "high", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a senior backend engineer. You build APIs, server logic, databases, authentication, and integrations. You have a research assistant named backend-assistant — delegate research subtasks to it when you need codebase exploration or documentation lookups.",
    envKey: "PAPERCLIP_AGENT_ID_BACKEND",
    instructionsDir: "agents/backend-engineer",
  },
  {
    name: "Backend Assistant",
    shortName: "backend-assistant",
    role: "engineer",
    title: "Backend Research Assistant",
    adapterType: "deerflow",
    adapterConfig: { model: "qwen3.5-9b", effort: "medium", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a research assistant for the Sr. Backend Engineer. Your job is pre-flight research: explore codebases, read documentation, summarize findings, and draft implementation notes. Post your research brief as a comment on your task. You do not write production code or make final implementation decisions.",
    envKey: "PAPERCLIP_AGENT_ID_BACKEND_ASSISTANT",
    instructionsDir: "agents/backend-assistant",
  },
  // ── Sr. Frontend ──
  {
    name: "Sr. Frontend Engineer",
    shortName: "frontend",
    role: "engineer",
    title: "Senior Frontend Engineer",
    adapterType: "claude_local",
    adapterConfig: { model: "claude-opus-4-6", effort: "high", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a senior frontend engineer. You build UI components, client-side logic, styling, and browser interactions. You have a research assistant named frontend-assistant — delegate research subtasks to it when you need codebase exploration or documentation lookups.",
    envKey: "PAPERCLIP_AGENT_ID_FRONTEND",
    instructionsDir: "agents/frontend-engineer",
  },
  {
    name: "Frontend Assistant",
    shortName: "frontend-assistant",
    role: "engineer",
    title: "Frontend Research Assistant",
    adapterType: "deerflow",
    adapterConfig: { model: "qwen3.5-9b", effort: "medium", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a research assistant for the Sr. Frontend Engineer. Your job is pre-flight research: explore codebases, read documentation, summarize findings, and draft implementation notes. Post your research brief as a comment on your task. You do not write production code or make final implementation decisions.",
    envKey: "PAPERCLIP_AGENT_ID_FRONTEND_ASSISTANT",
    instructionsDir: "agents/frontend-assistant",
  },
  // ── Sr. QA ──
  {
    name: "Sr. QA Engineer",
    shortName: "qa",
    role: "engineer",
    title: "Senior QA Engineer",
    adapterType: "claude_local",
    adapterConfig: { model: "claude-opus-4-6", effort: "high", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a senior QA engineer. You write test plans, test suites (unit/integration/e2e), perform security audits, and verify quality gates. You have a research assistant named qa-assistant — delegate research subtasks to it when you need codebase exploration or documentation lookups.",
    envKey: "PAPERCLIP_AGENT_ID_QA",
    instructionsDir: "agents/qa-engineer",
  },
  {
    name: "QA Assistant",
    shortName: "qa-assistant",
    role: "engineer",
    title: "QA Research Assistant",
    adapterType: "deerflow",
    adapterConfig: { model: "qwen3.5-9b", effort: "medium", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a research assistant for the Sr. QA Engineer. Your job is pre-flight research: explore codebases, read test coverage, find testing gaps, and summarize findings. Post your research brief as a comment on your task. You do not write production code or make final implementation decisions.",
    envKey: "PAPERCLIP_AGENT_ID_QA_ASSISTANT",
    instructionsDir: "agents/qa-assistant",
  },
  // ── Sr. DevOps ──
  {
    name: "Sr. DevOps Engineer",
    shortName: "devops",
    role: "engineer",
    title: "Senior DevOps Engineer",
    adapterType: "claude_local",
    adapterConfig: { model: "claude-opus-4-6", effort: "high", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a senior DevOps engineer. You manage Docker, CI/CD, deployment, infrastructure config, monitoring, and networking. You have a research assistant named devops-assistant — delegate research subtasks to it when you need codebase exploration or documentation lookups.",
    envKey: "PAPERCLIP_AGENT_ID_DEVOPS",
    instructionsDir: "agents/devops-engineer",
  },
  {
    name: "DevOps Assistant",
    shortName: "devops-assistant",
    role: "engineer",
    title: "DevOps Research Assistant",
    adapterType: "deerflow",
    adapterConfig: { model: "qwen3.5-9b", effort: "medium", cwd: PROJECTS_DIR },
    permissions: { canCreateAgents: false },
    managerRef: "CTO",
    systemPrompt: "You are a research assistant for the Sr. DevOps Engineer. Your job is pre-flight research: explore infrastructure configs, read deployment docs, and summarize findings. Post your research brief as a comment on your task. You do not write production code or make final implementation decisions.",
    envKey: "PAPERCLIP_AGENT_ID_DEVOPS_ASSISTANT",
    instructionsDir: "agents/devops-assistant",
  },
];
```

- [ ] **Step 3: Add the main bootstrap function**

Add after the ORG definition:

```javascript
// ── Main ──────────────────────────────────────────────────────────
async function main() {
  console.log("\n🏗️  Vibe Stack Org Bootstrap\n");

  // 1. Authenticate
  console.log("Authenticating...");
  const signIn = await request("POST", "/api/auth/sign-in/email", null, {
    email: ADMIN_EMAIL,
    password: ADMIN_PASSWORD,
  });
  if (signIn.status !== 200) {
    console.error("Auth failed:", signIn.status, signIn.data);
    process.exit(1);
  }
  const cookie = signIn.cookie;
  console.log("  ✓ Authenticated as", ADMIN_EMAIL);

  // 2. Get or create company
  const companiesRes = await request("GET", "/api/companies", cookie);
  let company = companiesRes.data?.[0];
  if (!company) {
    const createRes = await request("POST", "/api/companies", cookie, { name: "Vibe Stack" });
    company = createRes.data;
    console.log("  ✓ Created company:", company.name, company.id);
  } else {
    console.log("  ✓ Using company:", company.name, company.id);
  }
  const companyId = company.id;
  updateEnvVar("PAPERCLIP_COMPANY_ID", companyId);

  // 3. Fetch existing agents to avoid duplicates
  const existingRes = await request("GET", `/api/companies/${companyId}/agents`, cookie);
  const existingAgents = existingRes.data || [];
  const existingByName = new Map(existingAgents.map((a) => [a.name, a]));

  // 4. Create agents in order, resolving managerRef to IDs
  const createdIds = new Map(); // name -> id

  for (const agentDef of ORG) {
    const existing = existingByName.get(agentDef.name);
    if (existing) {
      console.log(`  ✓ Exists: ${agentDef.name} (${existing.id})`);
      createdIds.set(agentDef.name, existing.id);
      updateEnvVar(agentDef.envKey, existing.id);
      continue;
    }

    const body = {
      name: agentDef.name,
      shortName: agentDef.shortName,
      role: agentDef.role,
      title: agentDef.title,
      adapterType: agentDef.adapterType,
      adapterConfig: agentDef.adapterConfig,
      systemPrompt: agentDef.systemPrompt,
      permissions: agentDef.permissions || {},
      status: "active",
    };

    // Resolve manager reference
    if (agentDef.managerRef) {
      const managerId = createdIds.get(agentDef.managerRef);
      if (managerId) {
        body.managerIds = [managerId];
      }
    }

    const res = await request("POST", `/api/companies/${companyId}/agents`, cookie, body);
    if (res.status >= 400) {
      console.error(`  ✗ Failed to create ${agentDef.name}:`, res.status, res.data);
      continue;
    }

    const agent = res.data;
    createdIds.set(agentDef.name, agent.id);
    updateEnvVar(agentDef.envKey, agent.id);
    console.log(`  ✓ Created: ${agentDef.name} (${agent.id})`);
  }

  // 5. Set the default PAPERCLIP_AGENT_ID to the CTO
  const ctoId = createdIds.get("CTO");
  if (ctoId) {
    updateEnvVar("PAPERCLIP_AGENT_ID", ctoId);
  }

  console.log("\n✅ Org bootstrap complete. Agent IDs written to .env\n");
  console.log("Agents created:", createdIds.size);
  console.log("Company ID:", companyId);
}

main().catch((err) => {
  console.error("Bootstrap failed:", err);
  process.exit(1);
});
```

- [ ] **Step 4: Test the script manually**

Run inside the Paperclip container:

```bash
docker compose exec -it server node bootstrap-org.js
```

Expected output: 10 agents created (or skipped if they exist), IDs written to `.env`.

- [ ] **Step 5: Commit**

```bash
git add bootstrap-org.js
git commit -m "feat: add org bootstrap script for 10-agent engineering company

Creates CTO + 4 senior engineers (Claude Opus) and 5 paired DeerFlow
research assistants (local vLLM). Idempotent — skips existing agents.
Writes all agent IDs to .env for vibe container pickup."
```

---

### Task 2: Create DeerFlow assistant AGENTS.md files

**Files:**
- Create: `agents/cto-assistant/AGENTS.md`
- Create: `agents/backend-assistant/AGENTS.md`
- Create: `agents/frontend-assistant/AGENTS.md`
- Create: `agents/qa-assistant/AGENTS.md`
- Create: `agents/devops-assistant/AGENTS.md`

All 5 assistants share the same base instructions with role-specific context. The template is short — assistants do research, not implementation.

- [ ] **Step 1: Create the cto-assistant instructions**

```bash
mkdir -p agents/cto-assistant
```

Write `agents/cto-assistant/AGENTS.md`:

```markdown
# CTO Research Assistant

You are a DeerFlow research assistant paired with the **CTO**. You run on local vLLM (Qwen3.5-9B) to save API costs.

## What You Do

- **Pre-flight research** — Before the CTO's heartbeat, explore the codebase, read docs, and summarize findings.
- **Ad-hoc research** — When the CTO creates a research subtask for you, investigate the topic and post findings.

## What You Do NOT Do

- Write production code or make commits to feature branches
- Make architectural decisions — report findings, let the CTO decide
- Create subtasks or delegate work
- Perform code review

## Output Format

Post your findings as a comment on your task. Structure:

```
## Research Brief

**Question:** <what was asked>

**Findings:**
- <key finding 1>
- <key finding 2>

**Relevant Files:**
- `path/to/file.py:123` — <why it's relevant>

**Recommendation:** <your suggestion, if appropriate>
```

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files.
- **Use Glob, not `find`** — never use `find` via Bash to locate files.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code.
```

- [ ] **Step 2: Create the 4 engineer assistant instructions**

Create directories and AGENTS.md for each. Each file follows the same template as cto-assistant but with the paired senior's name swapped:

```bash
mkdir -p agents/backend-assistant agents/frontend-assistant agents/qa-assistant agents/devops-assistant
```

Write `agents/backend-assistant/AGENTS.md`:

```markdown
# Backend Research Assistant

You are a DeerFlow research assistant paired with the **Sr. Backend Engineer**. You run on local vLLM (Qwen3.5-9B) to save API costs.

## What You Do

- **Pre-flight research** — Before the Sr. Backend Engineer's heartbeat, explore the codebase, read docs, and summarize findings related to APIs, server logic, databases, and integrations.
- **Ad-hoc research** — When the Sr. Backend Engineer creates a research subtask for you, investigate the topic and post findings.

## What You Do NOT Do

- Write production code or make commits to feature branches
- Make architectural decisions — report findings, let the engineer decide
- Create subtasks or delegate work
- Perform code review

## Output Format

Post your findings as a comment on your task. Structure:

```
## Research Brief

**Question:** <what was asked>

**Findings:**
- <key finding 1>
- <key finding 2>

**Relevant Files:**
- `path/to/file.py:123` — <why it's relevant>

**Recommendation:** <your suggestion, if appropriate>
```

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files.
- **Use Glob, not `find`** — never use `find` via Bash to locate files.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code.
```

Write `agents/frontend-assistant/AGENTS.md` (same template, swap "Sr. Backend Engineer" for "Sr. Frontend Engineer" and "APIs, server logic, databases, and integrations" for "UI components, client-side code, styling, and browser interactions").

Write `agents/qa-assistant/AGENTS.md` (same template, swap for "Sr. QA Engineer" and "test plans, test suites, security audits, and quality coverage").

Write `agents/devops-assistant/AGENTS.md` (same template, swap for "Sr. DevOps Engineer" and "Docker, CI/CD, deployment, infrastructure, and monitoring").

- [ ] **Step 3: Commit**

```bash
git add agents/cto-assistant/ agents/backend-assistant/ agents/frontend-assistant/ agents/qa-assistant/ agents/devops-assistant/
git commit -m "feat: add AGENTS.md instruction files for 5 DeerFlow research assistants

Each assistant is paired with a senior engineer via naming convention
(<role>-assistant). Instructions define research-only scope, output
format, and tool usage rules."
```

---

### Task 3: Update senior engineer AGENTS.md files with assistant pairing

**Files:**
- Modify: `agents/cto/AGENTS.md`
- Modify: `agents/backend-engineer/AGENTS.md`
- Modify: `agents/frontend-engineer/AGENTS.md`
- Modify: `agents/qa-engineer/AGENTS.md`
- Modify: `agents/devops-engineer/AGENTS.md`

Each senior's AGENTS.md needs a section documenting their paired assistant and how to use it.

- [ ] **Step 1: Add assistant pairing section to each senior AGENTS.md**

Add the following section after the existing "Tool Usage" section in each file. Replace `<assistant-shortname>` and `<role>` with the appropriate values.

For `agents/cto/AGENTS.md`, add after the Tool Usage section:

```markdown
## Your Research Assistant

You have a paired DeerFlow research assistant: **cto-assistant**

### Pre-flight Research (during task decomposition)

When creating subtasks for senior engineers, also create a research subtask for each engineer's assistant:

1. Create research subtask → assign to `<role>-assistant` (e.g., `backend-assistant`)
2. Create implementation subtask → assign to the senior (e.g., `backend`), mark as **blocked by** the research subtask

This ensures engineers wake with research context already available.

### Ad-hoc Research

If you need research mid-task, create a subtask and assign it to `cto-assistant`.
```

For `agents/backend-engineer/AGENTS.md`, add after the Tool Usage section:

```markdown
## Your Research Assistant

You have a paired DeerFlow research assistant: **backend-assistant**

When you need codebase exploration, documentation lookups, or background research:

1. Create a subtask describing what you need researched
2. Assign it to `backend-assistant`
3. Continue other work or mark yourself `blocked` if you need the results first
4. The assistant will post findings as a comment on the research subtask
```

Apply the same pattern to `frontend-engineer/AGENTS.md` (assistant: `frontend-assistant`), `qa-engineer/AGENTS.md` (assistant: `qa-assistant`), and `devops-engineer/AGENTS.md` (assistant: `devops-assistant`).

- [ ] **Step 2: Commit**

```bash
git add agents/cto/AGENTS.md agents/backend-engineer/AGENTS.md agents/frontend-engineer/AGENTS.md agents/qa-engineer/AGENTS.md agents/devops-engineer/AGENTS.md
git commit -m "feat: add assistant pairing documentation to senior AGENTS.md files

Each senior engineer now documents their paired DeerFlow assistant
and how to delegate research subtasks to it."
```

---

### Task 4: Update .env.example with new agent ID variables

**Files:**
- Modify: `.env.example:129-137` (replace commented agent ID block)

- [ ] **Step 1: Replace the old agent ID variables**

Find the existing commented-out agent ID block in `.env.example` and replace it with:

```env
# ── Agent IDs (auto-populated by bootstrap-org.js) ────────────────
# PAPERCLIP_AGENT_ID_CTO=
# PAPERCLIP_AGENT_ID_CTO_ASSISTANT=
# PAPERCLIP_AGENT_ID_BACKEND=
# PAPERCLIP_AGENT_ID_BACKEND_ASSISTANT=
# PAPERCLIP_AGENT_ID_FRONTEND=
# PAPERCLIP_AGENT_ID_FRONTEND_ASSISTANT=
# PAPERCLIP_AGENT_ID_QA=
# PAPERCLIP_AGENT_ID_QA_ASSISTANT=
# PAPERCLIP_AGENT_ID_DEVOPS=
# PAPERCLIP_AGENT_ID_DEVOPS_ASSISTANT=
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "chore: update .env.example with 10-agent org ID variables"
```

---

### Task 5: Update setup.sh to run org bootstrap after onboarding

**Files:**
- Modify: `setup.sh` (add bootstrap step after Claude Code login check)

- [ ] **Step 1: Add the org bootstrap step**

After the Claude Code login check (step 23) and before the credential helper restore, add a new step:

```bash
# ══════════════════════════════════════════════════════════════
# 24. Bootstrap org (create agents)
# ══════════════════════════════════════════════════════════════
step "Bootstrapping agent org"
# Check if agents already exist (more than the 2 from onboarding wizard)
AGENT_COUNT=$(docker compose exec -T server sh -c '
  node --input-type=module -e "
    import pg from \"/app/node_modules/.pnpm/postgres@3.4.8/node_modules/postgres/src/index.js\";
    const sql = pg({host:\"/tmp\",port:54329,database:\"paperclip\",username:\"paperclip\"});
    const r = await sql\`SELECT count(*) as c FROM agents\`;
    console.log(r[0].c);
    await sql.end();
  "' 2>/dev/null || echo "0")

if [[ "$AGENT_COUNT" -ge 10 ]]; then
    success "Org already bootstrapped ($AGENT_COUNT agents) — skipping"
else
    info "Creating 10-agent engineering org..."
    # Copy bootstrap script into the container and run it
    docker compose cp bootstrap-org.js server:/app/bootstrap-org.js
    docker compose exec -T server node /app/bootstrap-org.js
    success "Org bootstrap complete — agent IDs written to .env"
fi
```

Also update `TOTAL_STEPS` from 23 to 24.

- [ ] **Step 2: Commit**

```bash
git add setup.sh
git commit -m "feat: add org bootstrap step to setup.sh (step 24)

Automatically creates the 10-agent org after Paperclip onboarding.
Skips if agents already exist (idempotent)."
```

---

### Task 6: Update docker-compose.yml for multi-agent support

**Files:**
- Modify: `docker-compose.yml:82-109` (vibe service)

The current vibe service uses a single `PAPERCLIP_AGENT_ID`. For the 10-agent org, Paperclip spawns agent containers on-demand — the vibe service definition is used as a template. Each heartbeat invocation gets a different `PAPERCLIP_AGENT_ID` via environment override.

- [ ] **Step 1: Ensure the vibe service accepts per-invocation agent ID**

The current setup already works — `PAPERCLIP_AGENT_ID` is read from `.env` and Paperclip can override it per-container spawn. Verify the vibe service has `restart: on-failure` (not `unless-stopped`) so it exits cleanly after each heartbeat.

Check that the vibe service definition in `docker-compose.yml` has these properties:
- `restart: on-failure` (exits after heartbeat, restarts on crash)
- `env_file: .env` (picks up all agent IDs and JWT secret)
- Memory limit to prevent OOM on 32GB system

Add a deploy memory limit:

```yaml
  vibe:
    build: .
    restart: on-failure
    env_file: .env
    deploy:
      resources:
        limits:
          memory: 2G
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "chore: add memory limit to vibe service for 32GB systems"
```

---

### Task 7: Remove stale agent directories and files

**Files:**
- Delete: `agents/ux-designer/` (merged into frontend-engineer per spec)

- [ ] **Step 1: Remove the ux-designer directory**

```bash
rm -rf agents/ux-designer
```

- [ ] **Step 2: Commit**

```bash
git add -A agents/ux-designer
git commit -m "chore: remove ux-designer agent (merged into Sr. Frontend Engineer)"
```

---

### Task 8: Final integration test

- [ ] **Step 1: Run the full bootstrap end-to-end**

```bash
# From the host
cd ~/Repos/Vibe-Stack

# Copy bootstrap-org.js into the server container's app directory
docker compose cp bootstrap-org.js server:/app/bootstrap-org.js

# Run it
docker compose exec -T server node /app/bootstrap-org.js
```

Expected output:
```
🏗️  Vibe Stack Org Bootstrap

Authenticating...
  ✓ Authenticated as <email>
  ✓ Using company: Vibe Stack <uuid>
  ✓ Created: CTO (<uuid>)
  ✓ Created: CTO Assistant (<uuid>)
  ✓ Created: Sr. Backend Engineer (<uuid>)
  ✓ Created: Backend Assistant (<uuid>)
  ✓ Created: Sr. Frontend Engineer (<uuid>)
  ✓ Created: Frontend Assistant (<uuid>)
  ✓ Created: Sr. QA Engineer (<uuid>)
  ✓ Created: QA Assistant (<uuid>)
  ✓ Created: Sr. DevOps Engineer (<uuid>)
  ✓ Created: DevOps Assistant (<uuid>)

✅ Org bootstrap complete. Agent IDs written to .env

Agents created: 10
Company ID: <uuid>
```

- [ ] **Step 2: Verify agents appear in the Paperclip UI**

Open `https://vibe.tail2fb792.ts.net` and check the Agents sidebar — should show all 10 agents.

- [ ] **Step 3: Verify .env has all agent IDs**

```bash
grep "PAPERCLIP_AGENT_ID" .env
```

Expected: 11 lines (1 default + 10 role-specific).

- [ ] **Step 4: Run idempotency test — run bootstrap again**

```bash
docker compose exec -T server node /app/bootstrap-org.js
```

Expected: All 10 agents show "Exists" — no duplicates created.

- [ ] **Step 5: Commit all remaining changes and push**

```bash
git add -A
git commit -m "feat: complete 10-agent org bootstrap implementation

Org structure: CTO + 4 senior engineers (Claude Opus) with 5 paired
DeerFlow research assistants (local vLLM). Setup is fully automated —
bootstrap-org.js creates all agents, generates credentials, and writes
agent IDs to .env."
git push origin main
```
