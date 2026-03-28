#!/usr/bin/env node
// Patch existing agents with delegation prompts and DeerFlow descriptions.
// Run once after bootstrap to update agents already in the database.

const http = require("http");
const fs = require("fs");
const path = require("path");

// Load .env
const envPath = path.resolve(__dirname, ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
    const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*)\s*$/);
    if (match && !process.env[match[1]]) process.env[match[1]] = match[2];
  }
}

const PASSWORD = process.env.PAPERCLIP_ADMIN_PASSWORD;
const COMPANY_ID = process.env.PAPERCLIP_COMPANY_ID;

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
          resolve({ status: res.statusCode, body: d, setCookies: res.headers["set-cookie"] || [] });
        }
      });
    });
    if (body) r.write(JSON.stringify(body));
    r.end();
  });
}

const DF_DESC =
  "Haiku-tier assistant running local Qwen 3.5 9B on GPU. Strong at: research, summarization, analysis, documentation, data gathering, boilerplate code. Not suited for: complex multi-file refactoring, subtle architecture decisions, nuanced code review. Runs fast and free — no API cost.";

const DELEGATION_HINT =
  "You have a DeerFlow assistant (Haiku-tier, local Qwen 3.5 9B) that reports to you. " +
  "Query the roster with GET /api/companies/{companyId}/agents to find them. " +
  "Delegate research, summarization, analysis, documentation, and simple code scaffolding to your assistant — they run fast and free on a local GPU. " +
  "Keep complex implementation, architecture, debugging, and code review for yourself. " +
  "Create subtasks via POST /api/companies/{companyId}/issues with parentId and assigneeAgentId set to your assistant's id.";

const ENGINEER_PROMPTS = {
  "DevOps Engineer": "You are the DevOps Engineer for Vibe Stack. You own infrastructure, CI/CD, Docker, networking, and deployment. ",
  "Frontend Engineer": "You are the Frontend Engineer for Vibe Stack. You own UI/UX implementation, React/Next.js, CSS, and client-side architecture. ",
  "Backend Engineer": "You are the Backend Engineer for Vibe Stack. You own APIs, databases, server-side logic, and backend architecture. ",
  "QA Engineer": "You are the QA Engineer for Vibe Stack. You own testing strategy, test automation, quality gates, and bug triage. ",
  "UX Designer": "You are the UX Designer for Vibe Stack. You own user experience, design systems, wireframes, and usability. ",
};

const CTO_PROMPT =
  "You are the CTO of Vibe Stack, an autonomous software development company. You break down high-level objectives into actionable tasks, delegate to specialist engineers, review architecture and deliverables, and ensure quality. You have final authority on technical decisions, task prioritization, and resource allocation.\n\n" +
  "## Delegation Strategy\n\n" +
  "Each senior engineer has a DeerFlow assistant (Haiku-tier, local Qwen 3.5 9B). Before delegating, query the roster: GET /api/companies/{companyId}/agents.\n\n" +
  "**DeerFlow assistants** — delegate: research, summarization, analysis, documentation, boilerplate generation, data gathering, simple code scaffolding. They run fast and free on a local GPU.\n\n" +
  "**Claude engineers** (Sonnet-tier) — reserve for: complex multi-file implementation, architecture decisions, nuanced code review, debugging, and tasks requiring deep contextual reasoning.\n\n" +
  "Always delegate to the cheapest tier that can succeed. When in doubt, start with the DeerFlow assistant — the senior engineer can escalate if needed.";

(async () => {
  // Sign in
  const signin = await req("POST", "/api/auth/sign-in/email", "", { email: "prime@vibe.local", password: PASSWORD });
  if (signin.status !== 200) {
    console.error("Sign-in failed:", signin.status);
    process.exit(1);
  }
  const cookie = signin.setCookies.map((c) => c.split(";")[0]).join("; ");
  console.log("Signed in OK\n");

  // List agents
  const agents = await req("GET", "/api/companies/" + COMPANY_ID + "/agents", cookie);
  if (!Array.isArray(agents.body)) {
    console.error("Failed to list agents:", agents.body);
    process.exit(1);
  }

  for (const a of agents.body) {
    let patch = null;

    if (a.name.startsWith("DeerFlow ")) {
      patch = { description: DF_DESC };
    } else if (ENGINEER_PROMPTS[a.name]) {
      patch = { systemPrompt: ENGINEER_PROMPTS[a.name] + DELEGATION_HINT };
    } else if (a.name === "CTO") {
      patch = { systemPrompt: CTO_PROMPT };
    }

    if (patch) {
      const result = await req("PATCH", "/api/agents/" + a.id, cookie, patch);
      console.log("  PATCH " + a.name + ": " + result.status);
    } else {
      console.log("  SKIP  " + a.name);
    }
  }

  console.log("\nDone!");
})();
