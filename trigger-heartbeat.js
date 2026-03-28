#!/usr/bin/env node
// Trigger a heartbeat run for a given agent via the Paperclip API.
// Usage: node trigger-heartbeat.js <agent-id>

const http = require("http");
const fs = require("fs");
const path = require("path");

const envPath = path.resolve(__dirname, ".env");
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
    const match = line.match(/^\s*([^#=]+?)\s*=\s*(.*)\s*$/);
    if (match && !process.env[match[1]]) process.env[match[1]] = match[2];
  }
}

const agentId = process.argv[2] || process.env.PAPERCLIP_AGENT_ID;
if (!agentId) { console.error("Usage: node trigger-heartbeat.js <agent-id> OR set PAPERCLIP_AGENT_ID"); process.exit(1); }
const PASSWORD = process.env.PAPERCLIP_ADMIN_PASSWORD;

function req(method, urlPath, cookie, body) {
  return new Promise((resolve) => {
    const opts = { hostname: "localhost", port: 3100, path: urlPath, method, headers: { "Content-Type": "application/json", "Origin": "http://localhost:3100" } };
    if (cookie) opts.headers["Cookie"] = cookie;
    const r = http.request(opts, (res) => {
      let d = "";
      res.on("data", (c) => (d += c));
      res.on("end", () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(d), cookies: res.headers["set-cookie"] || [] }); }
        catch { resolve({ status: res.statusCode, body: d, cookies: res.headers["set-cookie"] || [] }); }
      });
    });
    if (body) r.write(JSON.stringify(body));
    r.end();
  });
}

(async () => {
  const email = process.env.PAPERCLIP_ADMIN_EMAIL;
  if (!email) { console.error("PAPERCLIP_ADMIN_EMAIL not set"); process.exit(1); }
  const signin = await req("POST", "/api/auth/sign-in/email", "", { email, password: PASSWORD });
  if (signin.status !== 200) {
    console.error("Sign-in failed:", signin.status, JSON.stringify(signin.body));
    process.exit(1);
  }
  const cookie = signin.cookies.map((c) => c.split(";")[0]).join("; ");
  console.log("Signed in OK");

  const issueId = process.argv[3] || "";
  const payload = { source: "on_demand", trigger: "manual" };
  if (issueId) payload.payload = { issueId };
  const result = await req("POST", "/api/agents/" + agentId + "/heartbeat/invoke", cookie, payload);
  console.log("Heartbeat trigger:", result.status);
  console.log(JSON.stringify(result.body, null, 2).slice(0, 500));
})();
