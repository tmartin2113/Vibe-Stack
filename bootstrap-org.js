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

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

// Load .env file if present
const envPath = path.resolve(__dirname, ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
    const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*)\s*$/);
    if (match && !process.env[match[1]]) process.env[match[1]] = match[2];
  }
}

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

/* ------------------------------------------------------------------ */
/*  HTTP helper                                                       */
/* ------------------------------------------------------------------ */

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

const REPO_DIR = process.cwd();

function loadPrompt(relPath) {
  const full = path.join(__dirname, relPath);
  if (fs.existsSync(full)) return fs.readFileSync(full, "utf-8");
  console.warn(`  WARN: ${relPath} not found, using empty system prompt`);
  return "";
}

const ORG = [
  // 1. CTO
  {
    name: "CTO",
    shortName: "cto",
    role: "ceo",
    title: "Chief Technology Officer",
    adapterType: "claude_local",
    adapterConfig: {
      model: "claude-opus-4-6",
      effort: "high",
      cwd: REPO_DIR,
    },
    permissions: { canCreateAgents: true },
    systemPrompt: loadPrompt("agents/cto/AGENTS.md"),
    envKey: "PAPERCLIP_AGENT_ID_CTO",
    managerRef: null,
  },
  // 2. CTO Assistant
  {
    name: "CTO Assistant",
    shortName: "cto-assistant",
    role: "engineer",
    title: "CTO Research Assistant",
    adapterType: "deerflow",
    adapterConfig: {
      model: "qwen3.5-9b",
      effort: "high",
      cwd: REPO_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt:
      "You are a research assistant for the CTO. " +
      "Run DeerFlow research tasks: gather documentation, compare technologies, " +
      "summarize findings. Post concise briefs as issue comments so the CTO can " +
      "make informed decisions without spending tokens on exploration.",
    envKey: "PAPERCLIP_AGENT_ID_CTO_ASSISTANT",
    managerRef: "CTO",
  },
  // 3. Sr. Backend Engineer
  {
    name: "Sr. Backend Engineer",
    shortName: "backend",
    role: "engineer",
    title: "Senior Backend Engineer",
    adapterType: "claude_local",
    adapterConfig: {
      model: "claude-opus-4-6",
      effort: "high",
      cwd: PROJECTS_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt: loadPrompt("agents/backend-engineer/AGENTS.md"),
    envKey: "PAPERCLIP_AGENT_ID_BACKEND",
    managerRef: "CTO",
  },
  // 4. Backend Assistant
  {
    name: "Backend Assistant",
    shortName: "backend-assistant",
    role: "engineer",
    title: "Backend Research Assistant",
    adapterType: "deerflow",
    adapterConfig: {
      model: "qwen3.5-9b",
      effort: "high",
      cwd: PROJECTS_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt:
      "You are a research assistant for the Sr. Backend Engineer. " +
      "Run DeerFlow research tasks: look up API docs, find library examples, " +
      "investigate error messages, research best practices. Post concise briefs " +
      "as issue comments so the backend engineer can focus on implementation.",
    envKey: "PAPERCLIP_AGENT_ID_BACKEND_ASSISTANT",
    managerRef: "CTO",
  },
  // 5. Sr. Frontend Engineer
  {
    name: "Sr. Frontend Engineer",
    shortName: "frontend",
    role: "engineer",
    title: "Senior Frontend Engineer",
    adapterType: "claude_local",
    adapterConfig: {
      model: "claude-opus-4-6",
      effort: "high",
      cwd: PROJECTS_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt: loadPrompt("agents/frontend-engineer/AGENTS.md"),
    envKey: "PAPERCLIP_AGENT_ID_FRONTEND",
    managerRef: "CTO",
  },
  // 6. Frontend Assistant
  {
    name: "Frontend Assistant",
    shortName: "frontend-assistant",
    role: "engineer",
    title: "Frontend Research Assistant",
    adapterType: "deerflow",
    adapterConfig: {
      model: "qwen3.5-9b",
      effort: "high",
      cwd: PROJECTS_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt:
      "You are a research assistant for the Sr. Frontend Engineer. " +
      "Run DeerFlow research tasks: look up component libraries, CSS patterns, " +
      "framework docs, accessibility guidelines. Post concise briefs as issue " +
      "comments so the frontend engineer can focus on implementation.",
    envKey: "PAPERCLIP_AGENT_ID_FRONTEND_ASSISTANT",
    managerRef: "CTO",
  },
  // 7. Sr. QA Engineer
  {
    name: "Sr. QA Engineer",
    shortName: "qa",
    role: "engineer",
    title: "Senior QA Engineer",
    adapterType: "claude_local",
    adapterConfig: {
      model: "claude-opus-4-6",
      effort: "high",
      cwd: PROJECTS_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt: loadPrompt("agents/qa-engineer/AGENTS.md"),
    envKey: "PAPERCLIP_AGENT_ID_QA",
    managerRef: "CTO",
  },
  // 8. QA Assistant
  {
    name: "QA Assistant",
    shortName: "qa-assistant",
    role: "engineer",
    title: "QA Research Assistant",
    adapterType: "deerflow",
    adapterConfig: {
      model: "qwen3.5-9b",
      effort: "high",
      cwd: PROJECTS_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt:
      "You are a research assistant for the Sr. QA Engineer. " +
      "Run DeerFlow research tasks: find testing strategies, security checklists, " +
      "vulnerability databases, and coverage tools. Post concise briefs as issue " +
      "comments so the QA engineer can focus on writing tests.",
    envKey: "PAPERCLIP_AGENT_ID_QA_ASSISTANT",
    managerRef: "CTO",
  },
  // 9. Sr. DevOps Engineer
  {
    name: "Sr. DevOps Engineer",
    shortName: "devops",
    role: "engineer",
    title: "Senior DevOps Engineer",
    adapterType: "claude_local",
    adapterConfig: {
      model: "claude-opus-4-6",
      effort: "high",
      cwd: PROJECTS_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt: loadPrompt("agents/devops-engineer/AGENTS.md"),
    envKey: "PAPERCLIP_AGENT_ID_DEVOPS",
    managerRef: "CTO",
  },
  // 10. DevOps Assistant
  {
    name: "DevOps Assistant",
    shortName: "devops-assistant",
    role: "engineer",
    title: "DevOps Research Assistant",
    adapterType: "deerflow",
    adapterConfig: {
      model: "qwen3.5-9b",
      effort: "high",
      cwd: PROJECTS_DIR,
    },
    permissions: { canCreateAgents: false },
    systemPrompt:
      "You are a research assistant for the Sr. DevOps Engineer. " +
      "Run DeerFlow research tasks: look up Docker best practices, CI/CD patterns, " +
      "infrastructure docs, cloud provider APIs. Post concise briefs as issue " +
      "comments so the DevOps engineer can focus on implementation.",
    envKey: "PAPERCLIP_AGENT_ID_DEVOPS_ASSISTANT",
    managerRef: "CTO",
  },
];

/* ------------------------------------------------------------------ */
/*  Main                                                              */
/* ------------------------------------------------------------------ */

(async () => {
  /* 1. Authenticate */
  console.log("Signing in...");
  const auth = await request("POST", "/api/auth/sign-in/email", null, {
    email: ADMIN_EMAIL,
    password: ADMIN_PASSWORD,
  });
  if (auth.status !== 200) {
    console.error("Sign-in failed:", auth.status, auth.data);
    process.exit(1);
  }
  let cookie = auth.cookie;
  console.log("  Signed in as", auth.data.user?.name || ADMIN_EMAIL);

  /* 2. Get or create the "Vibe Stack" company */
  const companiesRes = await request("GET", "/api/companies", cookie);
  if (companiesRes.status !== 200) {
    console.error("Failed to list companies:", companiesRes.status, companiesRes.data);
    process.exit(1);
  }
  cookie = companiesRes.cookie || cookie;

  let company = companiesRes.data.find((c) => c.name === "Vibe Stack");
  if (!company) {
    console.log("  Creating company 'Vibe Stack'...");
    const createRes = await request("POST", "/api/companies", cookie, { name: "Vibe Stack" });
    if (createRes.status !== 201 && createRes.status !== 200) {
      console.error("Failed to create company:", createRes.status, createRes.data);
      process.exit(1);
    }
    cookie = createRes.cookie || cookie;
    company = createRes.data;
    console.log("  Created company:", company.id);
  } else {
    console.log("  Found company:", company.name, company.id);
  }
  const companyId = company.id;

  /* 3. Fetch existing agents */
  const agentsRes = await request("GET", `/api/companies/${companyId}/agents`, cookie);
  if (agentsRes.status !== 200) {
    console.error("Failed to list agents:", agentsRes.status, agentsRes.data);
    process.exit(1);
  }
  cookie = agentsRes.cookie || cookie;
  const existingAgents = agentsRes.data;
  console.log(`  Found ${existingAgents.length} existing agent(s)`);

  /* 4. Create agents in order */
  const createdIds = {}; // name -> id

  // Index existing agents by name for idempotency
  for (const ea of existingAgents) {
    createdIds[ea.name] = ea.id;
  }

  for (const agent of ORG) {
    // Skip if already exists
    if (createdIds[agent.name]) {
      console.log(`  SKIP ${agent.name} (already exists: ${createdIds[agent.name]})`);
      updateEnvVar(agent.envKey, createdIds[agent.name]);
      continue;
    }

    // Resolve managerIds
    const managerIds = [];
    if (agent.managerRef) {
      const managerId = createdIds[agent.managerRef];
      if (!managerId) {
        console.error(`  ERROR: Manager "${agent.managerRef}" not found for ${agent.name}. Skipping.`);
        continue;
      }
      managerIds.push(managerId);
    }

    const payload = {
      name: agent.name,
      shortName: agent.shortName,
      role: agent.role,
      title: agent.title,
      adapterType: agent.adapterType,
      adapterConfig: agent.adapterConfig,
      permissions: agent.permissions,
      systemPrompt: agent.systemPrompt,
      managerIds,
    };

    const res = await request("POST", `/api/companies/${companyId}/agents`, cookie, payload);
    cookie = res.cookie || cookie;

    if (res.status === 201 || res.status === 200) {
      const id = res.data.id;
      createdIds[agent.name] = id;
      updateEnvVar(agent.envKey, id);
      console.log(`  CREATED ${agent.name} -> ${id}`);
    } else {
      console.error(`  FAILED ${agent.name}:`, res.status, JSON.stringify(res.data));
    }
  }

  /* 5. Set default PAPERCLIP_AGENT_ID to CTO */
  if (createdIds["CTO"]) {
    updateEnvVar("PAPERCLIP_AGENT_ID", createdIds["CTO"]);
    console.log(`\n  Default PAPERCLIP_AGENT_ID set to CTO: ${createdIds["CTO"]}`);
  }

  console.log("\n=== Vibe Stack org bootstrap complete ===");
})().catch((err) => {
  console.error("Bootstrap failed:", err);
  process.exit(1);
});
