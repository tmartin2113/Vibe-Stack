#!/usr/bin/env node
// Enable two-phase execution on Senior Engineer agents.
// Run after deploying the updated Paperclip server with two-phase support.

const http = require("http");
const fs = require("fs");
const path = require("path");

// Load .env
const envPath = path.resolve(__dirname, ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
    const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*)\s*$/);
    if (match && !process.env[match[1]]) process.env[match[1]] = match[2].split('#')[0].trim();
  }
}

const PASSWORD = process.env.PAPERCLIP_ADMIN_PASSWORD;
if (!PASSWORD) {
  console.error("Set PAPERCLIP_ADMIN_PASSWORD in .env");
  process.exit(1);
}

function req(method, urlPath, cookie, body) {
  return new Promise((resolve) => {
    const opts = { hostname: "localhost", port: 3100, path: urlPath, method, headers: { "Content-Type": "application/json" } };
    if (cookie) opts.headers["Cookie"] = cookie;
    const r = http.request(opts, (res) => {
      let d = "";
      res.on("data", (c) => (d += c));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode, body: JSON.parse(d), setCookies: res.headers["set-cookie"] || [] });
        } catch {
          resolve({ status: res.statusCode, body: d, setCookies: [] });
        }
      });
    });
    if (body) r.write(JSON.stringify(body));
    r.end();
  });
}

// Senior Engineers to enable two-phase on
const AGENTS = [
  { id: "08c677aa-95b9-4724-bb56-ed21d9264f94", name: "Backend Engineer" },
  // Uncomment to enable on other Senior Engineers after pilot:
  // { id: "<frontend-engineer-id>", name: "Frontend Engineer" },
  // { id: "<qa-engineer-id>", name: "QA Engineer" },
  // { id: "<ux-engineer-id>", name: "UX Engineer" },
  // { id: "<security-engineer-id>", name: "Security Engineer" },
];

async function main() {
  // Login
  const login = await req("POST", "/api/auth/sign-in/email", null, {
    email: process.env.PAPERCLIP_ADMIN_EMAIL || "prime@vibe.local",
    password: PASSWORD,
  });
  if (login.status !== 200) {
    console.error("Login failed:", login.status, login.body);
    process.exit(1);
  }
  const cookie = login.setCookies.map((c) => c.split(";")[0]).join("; ");
  console.log("Logged in");

  for (const agent of AGENTS) {
    // Get current config
    const get = await req("GET", `/api/agents/${agent.id}`, cookie);
    if (get.status !== 200) {
      console.error(`Failed to get ${agent.name}:`, get.status);
      continue;
    }
    const currentConfig = get.body.adapterConfig || {};

    // Patch with two-phase enabled
    const newConfig = {
      ...currentConfig,
      twoPhaseEnabled: true,
      maxPlanningTurns: 10,
      planningModel: "claude-haiku-4-5-20251001",
    };

    const patch = await req("PATCH", `/api/agents/${agent.id}`, cookie, {
      adapterConfig: newConfig,
    });

    if (patch.status === 200) {
      console.log(`  ${agent.name}: two-phase ENABLED (planning: 10 turns, model: haiku)`);
    } else {
      console.error(`  ${agent.name}: FAILED`, patch.status, patch.body);
    }
  }

  console.log("\nDone. Agents will use two-phase on their next heartbeat run.");
}

main().catch(console.error);
