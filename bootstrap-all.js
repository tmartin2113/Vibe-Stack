#!/usr/bin/env node
//
// bootstrap-all.js — one-shot Vibe Stack bootstrap
//
// Creates admin user, grants instance_admin role, creates company,
// CEO agent, and full org hierarchy. Idempotent where possible.
//
// Usage:
//   1. Set PAPERCLIP_ADMIN_PASSWORD in .env
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

  // Insert instance_admin role via inline Node.js
  const script = `
    const { Client } = require('${pgPath.replace("/lib/index.js", "")}');
    const c = new Client({
      host: '127.0.0.1', port: 54329,
      user: 'paperclip', password: 'paperclip', database: 'paperclip'
    });
    (async () => {
      await c.connect();
      // Insert role (ignore if exists)
      await c.query(
        "INSERT INTO instance_user_roles (user_id, role) VALUES ($1, 'instance_admin') ON CONFLICT DO NOTHING",
        ['${userId}']
      );
      // Clear stale sessions so next sign-in picks up new role
      await c.query("DELETE FROM session WHERE user_id = $1", ['${userId}']);
      const check = await c.query(
        "SELECT role FROM instance_user_roles WHERE user_id = $1",
        ['${userId}']
      );
      console.log(JSON.stringify({ roles: check.rows.map(r => r.role) }));
      await c.end();
    })().catch(e => { console.error(e.message); process.exit(1); });
  `.replace(/\n/g, " ");

  const result = execSync(
    `docker exec ${container} node -e "${script.replace(/"/g, '\\"').replace(/\$/g, '\\$')}"`,
    { encoding: "utf-8" }
  ).trim();

  console.log("  DB result:", result);
}

// ── Agent definitions ──────────────────────────────────────────
const orgAgents = [
  {
    name: "CTO",
    role: "general",
    title: "Chief Technology Officer",
    adapterType: "claude_local",
    adapterConfig: { cwd: REPO_DIR, model: "claude-opus-4-6", effort: "high" },
    permissions: { canCreateAgents: false },
  },
  {
    name: "DevOps Engineer",
    role: "engineer",
    title: "DevOps Engineer",
    adapterType: "claude_local",
    adapterConfig: { cwd: REPO_DIR, model: "claude-sonnet-4-6", effort: "high" },
    permissions: { canCreateAgents: false },
  },
  {
    name: "Software Engineer",
    role: "engineer",
    title: "Software Engineer",
    adapterType: "claude_local",
    adapterConfig: { cwd: PROJECTS_DIR, model: "claude-sonnet-4-6", effort: "high" },
    permissions: { canCreateAgents: false },
  },
  {
    name: "QA Engineer",
    role: "engineer",
    title: "QA Engineer",
    adapterType: "claude_local",
    adapterConfig: { cwd: PROJECTS_DIR, model: "claude-sonnet-4-6", effort: "high" },
    permissions: { canCreateAgents: false },
  },
  {
    name: "UX Designer",
    role: "engineer",
    title: "UX Designer",
    adapterType: "claude_local",
    adapterConfig: { cwd: PROJECTS_DIR, model: "claude-sonnet-4-6", effort: "high" },
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

  // 3. Try to create company
  let company = await request("POST", "/api/companies", cookie, {
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
  const companyId = company.body.id;
  console.log("Company created:", company.body.name, `(${companyId})\n`);

  // 5. Create CEO agent
  const ceo = await request("POST", `/api/companies/${companyId}/agents`, cookie, {
    name: "CEO",
    role: "ceo",
    title: "Chief Executive Officer",
    adapterType: "claude_local",
    adapterConfig: {},
    systemPrompt:
      "You are the CEO of Vibe Stack, an autonomous software development company. You break down high-level objectives into actionable tasks, delegate to specialist agents, review deliverables, and ensure quality. You have final authority on task prioritization and resource allocation.",
    permissions: { canCreateAgents: true },
  });
  if (!ceo.body.id) {
    console.error("CEO creation failed:", ceo.status, JSON.stringify(ceo.body));
    process.exit(1);
  }
  console.log("CEO created:", ceo.body.name, `(${ceo.body.id})`);

  // 6. Create org hierarchy — all report to CEO
  console.log("\nCreating org hierarchy...");
  const created = [];
  for (const agent of orgAgents) {
    agent.reportsTo = ceo.body.id;
    const result = await request("POST", `/api/companies/${companyId}/agents`, cookie, agent);
    if (result.status === 201 || result.body.id) {
      console.log(`  Created ${agent.name} (${result.body.id})`);
      created.push({ name: agent.name, id: result.body.id });
    } else {
      console.error(`  FAILED ${agent.name}:`, result.status, JSON.stringify(result.body));
    }
  }

  // 7. Summary
  console.log("\n=== Bootstrap complete ===");
  console.log(`Company:  ${company.body.name} (${companyId})`);
  console.log(`CEO:      ${ceo.body.name} (${ceo.body.id})`);
  for (const a of created) {
    console.log(`  ${a.name}: ${a.id}`);
  }
  console.log(`\nSign in at http://localhost:3100 with ${EMAIL}`);
})().catch((e) => {
  console.error("\nFatal:", e.message || e);
  process.exit(1);
});
