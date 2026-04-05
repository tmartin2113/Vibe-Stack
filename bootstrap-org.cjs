#!/usr/bin/env node
/**
 * bootstrap-org.js — Create the 10-agent Vibe Stack engineering org.
 *
 * Run inside the Paperclip server container after onboarding:
 *   node bootstrap-org.cjs
 *
 * Uses direct database inserts (no API auth required).
 * Idempotent — skips agents that already exist (matched by name).
 * Writes agent IDs back to .env for vibe container pickup.
 *
 * MIGRATION NOTE (2026-04-05): Agent roles were changed from generic
 * ("engineer", "ceo") to specialized ("backend_engineer", "cto",
 * "research_assistant", etc.) for role-based tool filtering. Existing
 * deployments with agents already in the DB will NOT get updated roles
 * because this script skips agents matched by name. Two options:
 *
 *   1. (Recommended) Run this SQL to update roles in-place:
 *      UPDATE agents SET role = 'cto' WHERE name = 'CTO';
 *      UPDATE agents SET role = 'backend_engineer' WHERE name = 'Sr. Backend Engineer';
 *      UPDATE agents SET role = 'frontend_engineer' WHERE name = 'Sr. Frontend Engineer';
 *      UPDATE agents SET role = 'qa_engineer' WHERE name = 'Sr. QA Engineer';
 *      UPDATE agents SET role = 'devops_engineer' WHERE name = 'Sr. DevOps Engineer';
 *      UPDATE agents SET role = 'research_assistant' WHERE name LIKE '%Assistant';
 *
 *   2. (Fallback) Do nothing — filter_for_role() has a title-based
 *      fallback that infers the correct role from the agent's title
 *      field (e.g., "Senior Backend Engineer" → backend_engineer).
 *      A warning is logged when this fallback is used.
 */

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

// Load .env file if present (don't override existing env vars)
const envPath = path.resolve(__dirname, ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
    const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*)\s*$/);
    if (match && !process.env[match[1]]) process.env[match[1]] = match[2];
  }
}

const PROJECTS_DIR = process.env.PAPERCLIP_PROJECTS_DIR
  || path.join(process.env.HOME || "/home/prime", "Projects");

/* ------------------------------------------------------------------ */
/*  .env writer                                                       */
/* ------------------------------------------------------------------ */

function updateEnvVar(key, value) {
  const ep = path.join(__dirname, ".env");
  let content = fs.existsSync(ep) ? fs.readFileSync(ep, "utf-8") : "";
  const regex = new RegExp(`^${key}=.*$`, "m");
  if (regex.test(content)) {
    content = content.replace(regex, `${key}=${value}`);
  } else {
    content += `\n${key}=${value}`;
  }
  fs.writeFileSync(ep, content);
}

/* ------------------------------------------------------------------ */
/*  Org definition — 10 agents                                        */
/* ------------------------------------------------------------------ */

const ORG = [
  // 1. CTO
  {
    name: "CTO",
    role: "cto",
    title: "Chief Technology Officer",
    adapterType: "claude_local",
    model: "claude-opus-4-6",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/cto",
    permissions: { canCreateAgents: true },
    capabilities: "Architecture, task decomposition, code review, branch management, cross-agent consistency. Delegates all implementation to senior engineers.",
    icon: "crown",
    envKey: "PAPERCLIP_AGENT_ID_CTO",
    managerRef: null,
  },
  // 2. CTO Assistant
  {
    name: "CTO Assistant",
    role: "research_assistant",
    title: "CTO Research Assistant",
    adapterType: "deerflow",
    model: "qwen3.5-9b",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/cto-assistant",
    permissions: { canCreateAgents: false },
    capabilities: "Pre-flight research, codebase exploration, documentation lookups, architecture notes.",
    icon: "search",
    envKey: "PAPERCLIP_AGENT_ID_CTO_ASSISTANT",
    managerRef: "CTO",
  },
  // 3. Sr. Backend Engineer
  {
    name: "Sr. Backend Engineer",
    role: "backend_engineer",
    title: "Senior Backend Engineer",
    adapterType: "claude_local",
    model: "claude-opus-4-6",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/backend-engineer",
    permissions: { canCreateAgents: false },
    capabilities: "APIs, server logic, databases, authentication, integrations, DeerFlow/LangGraph Python.",
    icon: "server",
    envKey: "PAPERCLIP_AGENT_ID_BACKEND",
    managerRef: "CTO",
  },
  // 4. Backend Assistant
  {
    name: "Backend Assistant",
    role: "research_assistant",
    title: "Backend Research Assistant",
    adapterType: "deerflow",
    model: "qwen3.5-9b",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/backend-assistant",
    permissions: { canCreateAgents: false },
    capabilities: "Pre-flight research for backend: API docs, library examples, error investigation, best practices.",
    icon: "search",
    envKey: "PAPERCLIP_AGENT_ID_BACKEND_ASSISTANT",
    managerRef: "CTO",
  },
  // 5. Sr. Frontend Engineer
  {
    name: "Sr. Frontend Engineer",
    role: "frontend_engineer",
    title: "Senior Frontend Engineer",
    adapterType: "claude_local",
    model: "claude-opus-4-6",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/frontend-engineer",
    permissions: { canCreateAgents: false },
    capabilities: "UI components, client-side code, styling/CSS, UX implementation, browser-side logic.",
    icon: "layout",
    envKey: "PAPERCLIP_AGENT_ID_FRONTEND",
    managerRef: "CTO",
  },
  // 6. Frontend Assistant
  {
    name: "Frontend Assistant",
    role: "research_assistant",
    title: "Frontend Research Assistant",
    adapterType: "deerflow",
    model: "qwen3.5-9b",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/frontend-assistant",
    permissions: { canCreateAgents: false },
    capabilities: "Pre-flight research for frontend: component libraries, CSS patterns, framework docs, accessibility.",
    icon: "search",
    envKey: "PAPERCLIP_AGENT_ID_FRONTEND_ASSISTANT",
    managerRef: "CTO",
  },
  // 7. Sr. QA Engineer
  {
    name: "Sr. QA Engineer",
    role: "qa_engineer",
    title: "Senior QA Engineer",
    adapterType: "claude_local",
    model: "claude-opus-4-6",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/qa-engineer",
    permissions: { canCreateAgents: false },
    capabilities: "Test plans, test suites (unit/integration/e2e), security audits, quality gates, coverage analysis.",
    icon: "shield-check",
    envKey: "PAPERCLIP_AGENT_ID_QA",
    managerRef: "CTO",
  },
  // 8. QA Assistant
  {
    name: "QA Assistant",
    role: "research_assistant",
    title: "QA Research Assistant",
    adapterType: "deerflow",
    model: "qwen3.5-9b",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/qa-assistant",
    permissions: { canCreateAgents: false },
    capabilities: "Pre-flight research for QA: testing strategies, security checklists, vulnerability databases, coverage tools.",
    icon: "search",
    envKey: "PAPERCLIP_AGENT_ID_QA_ASSISTANT",
    managerRef: "CTO",
  },
  // 9. Sr. DevOps Engineer
  {
    name: "Sr. DevOps Engineer",
    role: "devops_engineer",
    title: "Senior DevOps Engineer",
    adapterType: "claude_local",
    model: "claude-opus-4-6",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/devops-engineer",
    permissions: { canCreateAgents: false },
    capabilities: "Docker, CI/CD, deployment, infrastructure config, monitoring, networking, Tailscale.",
    icon: "container",
    envKey: "PAPERCLIP_AGENT_ID_DEVOPS",
    managerRef: "CTO",
  },
  // 10. DevOps Assistant
  {
    name: "DevOps Assistant",
    role: "research_assistant",
    title: "DevOps Research Assistant",
    adapterType: "deerflow",
    model: "qwen3.5-9b",
    cwd: PROJECTS_DIR,
    instructionsDir: "agents/devops-assistant",
    permissions: { canCreateAgents: false },
    capabilities: "Pre-flight research for DevOps: Docker best practices, CI/CD patterns, infrastructure docs.",
    icon: "search",
    envKey: "PAPERCLIP_AGENT_ID_DEVOPS_ASSISTANT",
    managerRef: "CTO",
  },
];

/* ------------------------------------------------------------------ */
/*  Main — direct DB inserts                                          */
/* ------------------------------------------------------------------ */

async function main() {
  // Dynamic import of postgres (ESM module in CJS context)
  const pg = (await import("/app/node_modules/.pnpm/postgres@3.4.8/node_modules/postgres/src/index.js")).default;
  const sql = pg({ host: "/tmp", port: 54329, database: "paperclip", username: "paperclip" });

  console.log("\n  Vibe Stack Org Bootstrap (direct DB)\n");

  try {
    // 1. Get the company
    const companies = await sql`SELECT id, name FROM companies LIMIT 1`;
    if (companies.length === 0) {
      console.error("  No company found. Run 'pnpm paperclipai onboard' first.");
      process.exit(1);
    }
    const companyId = companies[0].id;
    console.log(`  Company: ${companies[0].name} (${companyId})`);
    updateEnvVar("PAPERCLIP_COMPANY_ID", companyId);

    // 2. Fetch existing agents
    const existing = await sql`SELECT id, name FROM agents WHERE company_id = ${companyId}`;
    const existingByName = new Map(existing.map((a) => [a.name, a.id]));
    console.log(`  Existing agents: ${existing.length}`);

    // 3. Create agents in order
    const createdIds = new Map(existingByName);

    for (const agent of ORG) {
      if (existingByName.has(agent.name)) {
        const id = existingByName.get(agent.name);
        console.log(`  [OK]  ${agent.name} (exists: ${id})`);
        updateEnvVar(agent.envKey, id);
        continue;
      }

      const id = crypto.randomUUID();
      const now = new Date();

      const adapterConfig = {
        cwd: agent.cwd,
        model: agent.model,
        graceSec: 15,
        timeoutSec: 0,
        maxTurnsPerRun: 80,
        instructionsFilePath: path.join(PROJECTS_DIR, agent.instructionsDir, "AGENTS.md"),
        dangerouslySkipPermissions: true,
      };

      const runtimeConfig = {
        heartbeat: {
          enabled: true,
          intervalSec: 0,       // No timer polling — agents wake on-demand only
          wakeOnDemand: true,
          maxConcurrentRuns: 1,
        },
      };

      await sql`
        INSERT INTO agents (
          id, company_id, name, role, title, status, capabilities,
          adapter_type, adapter_config, budget_monthly_cents, spent_monthly_cents,
          runtime_config, permissions, icon, created_at, updated_at
        ) VALUES (
          ${id}, ${companyId}, ${agent.name}, ${agent.role}, ${agent.title},
          'active', ${agent.capabilities || ""},
          ${agent.adapterType}, ${sql.json(adapterConfig)},
          0, 0,
          ${sql.json(runtimeConfig)}, ${sql.json(agent.permissions)},
          ${agent.icon || null},
          ${now}, ${now}
        )
      `;

      createdIds.set(agent.name, id);
      updateEnvVar(agent.envKey, id);
      console.log(`  [NEW] ${agent.name} (${id})`);

      // Set up manager relationship
      if (agent.managerRef) {
        const managerId = createdIds.get(agent.managerRef);
        if (managerId) {
          const relId = crypto.randomUUID();
          await sql`
            INSERT INTO agent_managers (id, agent_id, manager_id, created_at)
            VALUES (${relId}, ${id}, ${managerId}, ${now})
          `;
        }
      }
    }

    // 4. Set default PAPERCLIP_AGENT_ID to CTO
    const ctoId = createdIds.get("CTO");
    if (ctoId) {
      updateEnvVar("PAPERCLIP_AGENT_ID", ctoId);
    }

    console.log(`\n  Org bootstrap complete. ${createdIds.size} agents total.`);
    console.log(`  Agent IDs written to .env\n`);
  } finally {
    await sql.end();
  }
}

main().catch((err) => {
  console.error("Bootstrap failed:", err);
  process.exit(1);
});
