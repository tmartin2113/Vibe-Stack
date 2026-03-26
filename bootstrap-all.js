#!/usr/bin/env node
//
// bootstrap-all.js — one-shot Vibe Stack bootstrap
//
// Creates admin user, grants instance_admin role, creates company,
// CTO agent, full org hierarchy, CTO API key, and Gitea admin user.
// Writes PAPERCLIP_AGENT_ID, PAPERCLIP_API_KEY, and PAPERCLIP_COMPANY_ID
// back to .env so the vibe heartbeat can connect immediately.
// Idempotent where possible.
//
// Usage:
//   1. Set PAPERCLIP_ADMIN_PASSWORD and GITEA_ADMIN_PASSWORD in .env
//   2. docker compose up -d
//   3. node bootstrap-all.js
//

const http = require("http");
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

// ── Load .env ──────────────────────────────────────────────────
const envPath = path.resolve(__dirname, ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
    const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*)\s*$/);
    if (match && !process.env[match[1]]) process.env[match[1]] = match[2];
  }
}

const HOSTNAME = process.env.TAILSCALE_HOSTNAME || "localhost";
const PASSWORD = process.env.PAPERCLIP_ADMIN_PASSWORD;
if (!PASSWORD) {
  console.error("Set PAPERCLIP_ADMIN_PASSWORD in .env or environment");
  process.exit(1);
}

const PROJECTS_DIR = process.env.PROJECTS_DIR || "/srv/sftp/workspace/files";
const REPO_DIR = process.cwd();
const EMAIL = "prime@vibe.local";
const USER_NAME = "Prime";

// ── HTTP helper ────────────────────────────────────────────────
function request(method, urlPath, cookie, data) {
  return new Promise((resolve, reject) => {
    const body = data ? JSON.stringify(data) : null;
    const headers = {
      Origin: `https://${HOSTNAME}`,
      Host: HOSTNAME,
    };
    if (cookie) headers["Cookie"] = cookie;
    if (body) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = Buffer.byteLength(body);
    }
    const req = http.request(
      { hostname: "127.0.0.1", port: 3100, path: urlPath, method, headers },
      (res) => {
        let chunks = "";
        const setCookies = res.headers["set-cookie"] || [];
        res.on("data", (c) => (chunks += c));
        res.on("end", () => {
          try {
            resolve({ status: res.statusCode, body: JSON.parse(chunks), setCookies });
          } catch {
            resolve({ status: res.statusCode, body: chunks, setCookies });
          }
        });
      }
    );
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

function extractCookie(setCookies) {
  return setCookies.map((c) => c.split(";")[0]).join("; ");
}

// ── Wait for server health ─────────────────────────────────────
async function waitForHealth(maxWaitMs = 60000) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    try {
      const res = await request("GET", "/api/health", "");
      if (res.status === 200) return;
    } catch {
      // server not up yet
    }
    await new Promise((r) => setTimeout(r, 2000));
    process.stdout.write(".");
  }
  throw new Error("Server did not become healthy within " + (maxWaitMs / 1000) + "s");
}

// ── Grant instance_admin via Docker + pg ───────────────────────
function grantInstanceAdmin(userId) {
  console.log("  Granting instance_admin role via database...");

  // Find the server container
  let container;
  try {
    container = execSync(
      `docker ps --filter "label=com.docker.compose.service=server" --format "{{.Names}}"`,
      { encoding: "utf-8" }
    ).trim().split("\n")[0];
  } catch {
    // fallback: try ancestor-based filter
  }

  if (!container) {
    try {
      container = execSync(
        `docker ps --format "{{.Names}}" | grep -i server | head -1`,
        { encoding: "utf-8" }
      ).trim();
    } catch {
      throw new Error("Cannot find server container. Is docker compose running?");
    }
  }

  if (!container) {
    throw new Error("Cannot find server container. Is docker compose running?");
  }
  console.log("  Container:", container);

  // Find pg module inside the container
  const pgPath = execSync(
    `docker exec ${container} find /app/node_modules -path "*/pg/lib/index.js" -print -quit 2>/dev/null`,
    { encoding: "utf-8" }
  ).trim();

  if (!pgPath) {
    throw new Error("Cannot find pg module in container");
  }

  // Insert instance_admin role via inline Node.js (piped via stdin to avoid
  // shell escaping issues with Node v24's TypeScript eval)
  const script = `
    const { Client } = require('${pgPath.replace("/lib/index.js", "")}');
    const c = new Client({
      host: '127.0.0.1', port: 54329,
      user: 'paperclip', password: 'paperclip', database: 'paperclip'
    });
    (async () => {
      await c.connect();
      await c.query(
        "INSERT INTO instance_user_roles (user_id, role) VALUES ($1, 'instance_admin') ON CONFLICT DO NOTHING",
        ['${userId}']
      );
      await c.query("DELETE FROM session WHERE user_id = $1", ['${userId}']);
      const check = await c.query(
        "SELECT role FROM instance_user_roles WHERE user_id = $1",
        ['${userId}']
      );
      console.log(JSON.stringify({ roles: check.rows.map(r => r.role) }));
      await c.end();
    })().catch(e => { console.error(e.message); process.exit(1); });
  `;

  const result = execSync(
    `docker exec -i ${container} node --input-type=commonjs`,
    { input: script, encoding: "utf-8" }
  ).trim();

  console.log("  DB result:", result);
}

// ── Agent definitions ──────────────────────────────────────────
// Senior engineers — report to CTO, run on Claude
const ENGINEER_DELEGATION_PROMPT =
  "You have a DeerFlow assistant (Haiku-tier, local Qwen 3.5 9B) that reports to you. " +
  "Query the roster with GET /api/companies/{companyId}/agents to find them. " +
  "Delegate research, summarization, analysis, documentation, and simple code scaffolding to your assistant — they run fast and free on a local GPU. " +
  "Keep complex implementation, architecture, debugging, and code review for yourself. " +
  "Create subtasks via POST /api/companies/{companyId}/issues with parentId and assigneeAgentId set to your assistant's id.";

const seniorAgents = [
  {
    name: "DevOps Engineer",
    role: "engineer",
    title: "DevOps Engineer",
    adapterType: "claude_local",
    adapterConfig: { cwd: REPO_DIR, model: "claude-sonnet-4-6", effort: "high" },
    systemPrompt: "You are the DevOps Engineer for Vibe Stack. You own infrastructure, CI/CD, Docker, networking, and deployment. " + ENGINEER_DELEGATION_PROMPT,
    permissions: { canCreateAgents: false },
  },
  {
    name: "Frontend Engineer",
    role: "engineer",
    title: "Frontend Engineer",
    adapterType: "claude_local",
    adapterConfig: { cwd: PROJECTS_DIR, model: "claude-sonnet-4-6", effort: "high" },
    systemPrompt: "You are the Frontend Engineer for Vibe Stack. You own UI/UX implementation, React/Next.js, CSS, and client-side architecture. " + ENGINEER_DELEGATION_PROMPT,
    permissions: { canCreateAgents: false },
  },
  {
    name: "Backend Engineer",
    role: "engineer",
    title: "Backend Engineer",
    adapterType: "claude_local",
    adapterConfig: { cwd: PROJECTS_DIR, model: "claude-sonnet-4-6", effort: "high" },
    systemPrompt: "You are the Backend Engineer for Vibe Stack. You own APIs, databases, server-side logic, and backend architecture. " + ENGINEER_DELEGATION_PROMPT,
    permissions: { canCreateAgents: false },
  },
  {
    name: "QA Engineer",
    role: "engineer",
    title: "QA Engineer",
    adapterType: "claude_local",
    adapterConfig: { cwd: PROJECTS_DIR, model: "claude-sonnet-4-6", effort: "high" },
    systemPrompt: "You are the QA Engineer for Vibe Stack. You own testing strategy, test automation, quality gates, and bug triage. " + ENGINEER_DELEGATION_PROMPT,
    permissions: { canCreateAgents: false },
  },
  {
    name: "UX Designer",
    role: "engineer",
    title: "UX Designer",
    adapterType: "claude_local",
    adapterConfig: { cwd: PROJECTS_DIR, model: "claude-sonnet-4-6", effort: "high" },
    systemPrompt: "You are the UX Designer for Vibe Stack. You own user experience, design systems, wireframes, and usability. " + ENGINEER_DELEGATION_PROMPT,
    permissions: { canCreateAgents: false },
  },
];

// ── Main ───────────────────────────────────────────────────────
(async () => {
  console.log("=== Vibe Stack Bootstrap ===\n");

  // 1. Wait for server
  process.stdout.write("Waiting for server");
  await waitForHealth();
  console.log(" ready!\n");

  // 1b. Patch allowedHostnames to include "server" (Docker service name)
  console.log("Patching allowedHostnames...");
  try {
    const configPath = "/paperclip/instances/default/config.json";
    const serverContainer = execSync(
      `docker ps --filter "label=com.docker.compose.service=server" --format "{{.Names}}"`,
      { encoding: "utf-8" }
    ).trim().split("\n")[0];

    if (serverContainer) {
      // Read existing config, add "server" to allowedHostnames if missing
      const patchScript = `
        const fs = require('fs');
        const cfgPath = '${configPath}';
        try {
          const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
          const hosts = cfg.allowedHostnames || [];
          let changed = false;
          for (const h of ['server', 'localhost']) {
            if (!hosts.includes(h)) { hosts.push(h); changed = true; }
          }
          if (changed) {
            cfg.allowedHostnames = hosts;
            fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));
            console.log(JSON.stringify({ patched: true, allowedHostnames: hosts }));
          } else {
            console.log(JSON.stringify({ patched: false, allowedHostnames: hosts }));
          }
        } catch (e) {
          if (e.code === 'ENOENT') {
            console.log(JSON.stringify({ patched: false, reason: 'config not found yet (first run)' }));
          } else {
            throw e;
          }
        }
      `;
      const patchResult = execSync(
        `docker exec -i ${serverContainer} node --input-type=commonjs`,
        { input: patchScript, encoding: "utf-8" }
      ).trim();
      console.log("  Config:", patchResult);
    } else {
      console.warn("  Server container not found — skipping allowedHostnames patch");
    }
  } catch (e) {
    console.warn("  allowedHostnames patch failed (non-fatal):", e.message);
  }

  // 2. Sign in (or sign up first)
  let signin = await request("POST", "/api/auth/sign-in/email", "", {
    email: EMAIL,
    password: PASSWORD,
  });

  if (signin.status !== 200) {
    console.log("Sign-in failed (user may not exist), attempting sign-up...");
    const signup = await request("POST", "/api/auth/sign-up/email", "", {
      name: USER_NAME,
      email: EMAIL,
      password: PASSWORD,
    });
    if (signup.status !== 200) {
      console.error("Sign-up failed:", signup.status, JSON.stringify(signup.body));
      process.exit(1);
    }
    console.log("Sign-up OK:", signup.body.user?.name || signup.body);

    // Now sign in
    signin = await request("POST", "/api/auth/sign-in/email", "", {
      email: EMAIL,
      password: PASSWORD,
    });
    if (signin.status !== 200) {
      console.error("Sign-in after sign-up failed:", signin.status, JSON.stringify(signin.body));
      process.exit(1);
    }
  }

  let cookie = extractCookie(signin.setCookies);
  const userId = signin.body.user?.id;
  console.log("Signed in as", signin.body.user?.name, `(${userId})\n`);

  // 3. Check for existing company (idempotent)
  const existingCompanies = await request("GET", "/api/companies", cookie);
  let company;
  let companyId;
  const existing = Array.isArray(existingCompanies.body)
    ? existingCompanies.body.find((c) => c.name === "Vibe Stack")
    : null;

  if (existing) {
    company = { body: existing };
    companyId = existing.id;
    console.log("Company already exists:", existing.name, `(${companyId})\n`);
  } else {
    // Create company
    company = await request("POST", "/api/companies", cookie, {
      name: "Vibe Stack",
      description: "Autonomous agent network for software development",
    });

    // 4. If 403 → grant instance_admin, re-auth, retry
    if (company.status === 403) {
      console.log("Company creation returned 403 — need instance_admin role");
      grantInstanceAdmin(userId);

      // Re-sign-in to pick up new role
      console.log("  Re-authenticating...");
      signin = await request("POST", "/api/auth/sign-in/email", "", {
        email: EMAIL,
        password: PASSWORD,
      });
      if (signin.status !== 200) {
        console.error("Re-sign-in failed:", signin.status);
        process.exit(1);
      }
      cookie = extractCookie(signin.setCookies);
      console.log("  Re-authenticated.\n");

      // Retry company creation
      company = await request("POST", "/api/companies", cookie, {
        name: "Vibe Stack",
        description: "Autonomous agent network for software development",
      });
    }

    if (!company.body.id) {
      console.error("Company creation failed:", company.status, JSON.stringify(company.body));
      process.exit(1);
    }
    companyId = company.body.id;
    console.log("Company created:", company.body.name, `(${companyId})\n`);
  }

  // 5. Fetch existing agents to avoid duplicates
  const existingAgents = await request("GET", `/api/companies/${companyId}/agents`, cookie);
  const agentsByName = {};
  if (Array.isArray(existingAgents.body)) {
    for (const a of existingAgents.body) {
      agentsByName[a.name] = a;
    }
  }

  // 6. Create CTO (top-level agent) — skip if exists
  let cto;
  if (agentsByName["CTO"]) {
    cto = { body: agentsByName["CTO"] };
    console.log("CTO already exists:", cto.body.name, `(${cto.body.id})`);
  } else {
    cto = await request("POST", `/api/companies/${companyId}/agents`, cookie, {
      name: "CTO",
      role: "ceo",
      title: "Chief Technology Officer",
      adapterType: "claude_local",
      adapterConfig: { cwd: REPO_DIR, model: "claude-opus-4-6", effort: "high" },
      systemPrompt:
        "You are the CTO of Vibe Stack, an autonomous software development company. You break down high-level objectives into actionable tasks, delegate to specialist engineers, review architecture and deliverables, and ensure quality. You have final authority on technical decisions, task prioritization, and resource allocation.\n\n## Delegation Strategy\n\nEach senior engineer has a DeerFlow assistant (Haiku-tier, local Qwen 3.5 9B). Before delegating, query the roster: GET /api/companies/{companyId}/agents.\n\n**DeerFlow assistants** — delegate: research, summarization, analysis, documentation, boilerplate generation, data gathering, simple code scaffolding. They run fast and free on a local GPU.\n\n**Claude engineers** (Sonnet-tier) — reserve for: complex multi-file implementation, architecture decisions, nuanced code review, debugging, and tasks requiring deep contextual reasoning.\n\nAlways delegate to the cheapest tier that can succeed. When in doubt, start with the DeerFlow assistant — the senior engineer can escalate if needed.",
      permissions: { canCreateAgents: true },
    });
    if (!cto.body.id) {
      console.error("CTO creation failed:", cto.status, JSON.stringify(cto.body));
      process.exit(1);
    }
    console.log("CTO created:", cto.body.name, `(${cto.body.id})`);
  }

  // Verify CTO has tasks:assign permission
  console.log("\nVerifying CTO permissions...");
  const permsCheck = await request("GET", `/api/agents/${cto.body.id}`, cookie);
  const accessState = permsCheck.body?.accessState || {};
  if (!accessState.canAssignTasks) {
    console.log("  CTO missing tasks:assign — granting explicitly...");
    const patchRes = await request("PATCH", `/api/agents/${cto.body.id}/permissions`, cookie, {
      canAssignTasks: true,
    });
    if (patchRes.status === 200) {
      console.log("  tasks:assign granted");
    } else {
      console.warn("  Permission grant failed:", patchRes.status, JSON.stringify(patchRes.body));
    }
  } else {
    console.log("  CTO has tasks:assign (source: " + (accessState.canAssignTasksSource || "unknown") + ")");
  }

  // 7. Create senior engineers — all report to CTO, skip existing
  console.log("\nCreating senior engineers...");
  const created = [];
  for (const agent of seniorAgents) {
    if (agentsByName[agent.name]) {
      console.log(`  ${agent.name} already exists (${agentsByName[agent.name].id})`);
      created.push({ name: agent.name, id: agentsByName[agent.name].id });
      continue;
    }
    agent.managerIds = [cto.body.id];
    const result = await request("POST", `/api/companies/${companyId}/agents`, cookie, agent);
    if (result.status === 201 || result.body.id) {
      console.log(`  Created ${agent.name} (${result.body.id})`);
      created.push({ name: agent.name, id: result.body.id });
    } else {
      console.error(`  FAILED ${agent.name}:`, result.status, JSON.stringify(result.body));
    }
  }

  // 8. Create DeerFlow assistants — one per senior engineer, skip existing
  console.log("\nCreating DeerFlow assistants...");
  const seniors = [...created];
  for (const senior of seniors) {
    const dfName = `DeerFlow ${senior.name} Assistant`;
    if (agentsByName[dfName]) {
      console.log(`  ${dfName} already exists (${agentsByName[dfName].id})`);
      created.push({ name: dfName, id: agentsByName[dfName].id });
      continue;
    }
    const deerflow = await request("POST", `/api/companies/${companyId}/agents`, cookie, {
      name: dfName,
      role: "engineer",
      title: dfName,
      description: "Haiku-tier assistant running local Qwen 3.5 9B on GPU. Strong at: research, summarization, analysis, documentation, data gathering, boilerplate code. Not suited for: complex multi-file refactoring, subtle architecture decisions, nuanced code review. Runs fast and free — no API cost.",
      adapterType: "deerflow",
      adapterConfig: { model: process.env.VLLM_MODEL_SHORT || "qwen3.5-9b", skill: "deep-research" },
      managerIds: [senior.id],
      permissions: { canCreateAgents: false },
    });
    if (deerflow.body.id) {
      console.log(`  Created ${deerflow.body.name} (${deerflow.body.id})`);
      created.push({ name: deerflow.body.name, id: deerflow.body.id });
    } else {
      console.error(`  FAILED ${dfName}:`, deerflow.status, JSON.stringify(deerflow.body));
    }
  }

  // 9. Create CTO API key for heartbeat (skip if PAPERCLIP_API_KEY already set)
  let apiKey = process.env.PAPERCLIP_API_KEY || "";
  if (apiKey) {
    console.log("\nCTO API key already in .env — skipping creation");
  } else {
    console.log("\nCreating CTO API key...");
    const apiKeyRes = await request("POST", `/api/agents/${cto.body.id}/keys`, cookie, {
      name: "heartbeat"
    });
    if (apiKeyRes.status === 201 && apiKeyRes.body.token) {
      apiKey = apiKeyRes.body.token;
      console.log(`  API key created: ${apiKey.slice(0, 12)}...`);
    } else {
      console.warn("  API key creation failed:", apiKeyRes.status, JSON.stringify(apiKeyRes.body));
      console.warn("  You'll need to create one manually and set PAPERCLIP_API_KEY in .env");
    }
  }

  // 10. Write back to .env
  console.log("\nUpdating .env...");
  const envFile = path.resolve(__dirname, ".env");
  let envContent = fs.existsSync(envFile) ? fs.readFileSync(envFile, "utf-8") : "";

  function setEnvVar(content, key, value) {
    const re = new RegExp(`^${key}=.*$`, "m");
    if (re.test(content)) {
      return content.replace(re, `${key}=${value}`);
    }
    return content.trimEnd() + `\n${key}=${value}\n`;
  }

  envContent = setEnvVar(envContent, "PAPERCLIP_AGENT_ID", cto.body.id);
  envContent = setEnvVar(envContent, "PAPERCLIP_COMPANY_ID", companyId);
  if (apiKey) {
    envContent = setEnvVar(envContent, "PAPERCLIP_API_KEY", apiKey);
  }
  fs.writeFileSync(envFile, envContent);
  console.log("  PAPERCLIP_AGENT_ID, PAPERCLIP_COMPANY_ID" + (apiKey ? ", PAPERCLIP_API_KEY" : "") + " written to .env");

  // 11. Bootstrap Gitea admin user
  console.log("\nBootstrapping Gitea...");
  const GITEA_USER = process.env.GITEA_ADMIN_USER || "vibe";
  const GITEA_PASS = process.env.GITEA_ADMIN_PASSWORD;

  if (!GITEA_PASS) {
    console.warn("  GITEA_ADMIN_PASSWORD not set — skipping Gitea admin creation");
    console.warn("  Set it in .env and re-run, or create the admin manually");
  } else {
    try {
      // Find gitea container
      const giteaContainer = execSync(
        `docker ps --filter "label=com.docker.compose.service=gitea" --format "{{.Names}}"`,
        { encoding: "utf-8" }
      ).trim().split("\n")[0];

      if (!giteaContainer) {
        console.warn("  Gitea container not found — skipping");
      } else {
        // Check if admin already exists
        try {
          execSync(
            `docker exec --user git ${giteaContainer} gitea admin user list --admin`,
            { encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
          );
          const userList = execSync(
            `docker exec --user git ${giteaContainer} gitea admin user list --admin`,
            { encoding: "utf-8" }
          );
          if (userList.includes(GITEA_USER)) {
            console.log(`  Admin user '${GITEA_USER}' already exists`);
          } else {
            execSync(
              `docker exec --user git ${giteaContainer} gitea admin user create ` +
              `--admin --username ${GITEA_USER} --password '${GITEA_PASS}' ` +
              `--email admin@vibe.local --must-change-password=false`,
              { encoding: "utf-8", stdio: "inherit" }
            );
            console.log(`  Created Gitea admin user '${GITEA_USER}'`);
          }
        } catch (e) {
          // user create fails if already exists — that's fine
          if (e.message && e.message.includes("already exists")) {
            console.log(`  Admin user '${GITEA_USER}' already exists`);
          } else {
            console.warn("  Gitea admin creation failed:", e.message);
          }
        }
      }
    } catch (e) {
      console.warn("  Could not reach Gitea container:", e.message);
    }
  }

  // 12. Summary
  console.log("\n=== Bootstrap complete ===");
  console.log(`Company:  ${company.body.name} (${companyId})`);
  console.log(`CTO:      ${cto.body.name} (${cto.body.id})`);
  for (const a of created) {
    console.log(`  ${a.name}: ${a.id}`);
  }
  if (apiKey) {
    console.log(`\nCTO API key written to .env — vibe heartbeat ready`);
  }
  if (GITEA_PASS) {
    console.log(`Gitea:    http://localhost:3000 (user: ${process.env.GITEA_ADMIN_USER || "vibe"})`);
  }
  console.log(`Paperclip: http://localhost:3100 (user: ${EMAIL})`);
})().catch((e) => {
  console.error("\nFatal:", e.message || e);
  process.exit(1);
});
