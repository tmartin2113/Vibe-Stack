#!/usr/bin/env node
// Assign all available skills to all agents.
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

const PASSWORD = process.env.PAPERCLIP_ADMIN_PASSWORD;
const companyId = "ddc70828-3757-40bf-8ace-853d8c469426";

function req(method, urlPath, cookie, body) {
  return new Promise((resolve, reject) => {
    const opts = {
      hostname: "localhost", port: 3100, path: urlPath, method,
      headers: { "Content-Type": "application/json", "Origin": "http://localhost:3100" },
    };
    if (cookie) opts.headers["Cookie"] = cookie;
    const r = http.request(opts, (res) => {
      let d = "";
      res.on("data", (c) => (d += c));
      res.on("end", () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(d), cookies: res.headers["set-cookie"] || [] }); }
        catch { resolve({ status: res.statusCode, body: d, cookies: res.headers["set-cookie"] || [] }); }
      });
    });
    r.on("error", reject);
    if (body) r.write(JSON.stringify(body));
    r.end();
  });
}

(async () => {
  // 1. Sign in
  const signin = await req("POST", "/api/auth/sign-in/email", "", {
    email: "tmartin2113@gmail.com", password: PASSWORD,
  });
  if (signin.status !== 200) { console.error("Sign-in failed:", signin.status); process.exit(1); }
  const cookie = signin.cookies.map((c) => c.split(";")[0]).join("; ");
  console.log("Signed in OK\n");

  // 2. Get all agents
  const agentsRes = await req("GET", "/api/companies/" + companyId + "/agents", cookie);
  const agents = agentsRes.body;
  if (!Array.isArray(agents)) { console.error("Failed to list agents:", agentsRes.status); process.exit(1); }
  console.log("Agents (" + agents.length + "):");
  for (const a of agents) console.log("  " + a.name + " (" + a.adapterType + ") " + a.id);

  // 3. Get all skills
  const skillsRes = await req("GET", "/api/companies/" + companyId + "/skills", cookie);
  const skills = Array.isArray(skillsRes.body) ? skillsRes.body : (skillsRes.body.skills || []);
  const skillKeys = skills.map((s) => s.key || s.slug || s.name);
  console.log("\nSkills (" + skillKeys.length + "):");
  for (const k of skillKeys) console.log("  " + k);

  // 4. Sync all skills to each agent
  console.log("\n--- Syncing skills to agents ---\n");
  for (const agent of agents) {
    console.log("Syncing " + agent.name + " (" + agent.adapterType + ")...");
    const syncRes = await req("POST", "/api/agents/" + agent.id + "/skills/sync", cookie, {
      desiredSkills: skillKeys,
    });
    if (syncRes.status === 200) {
      const snap = syncRes.body;
      const assigned = snap.entries ? snap.entries.length : 0;
      const warnings = snap.warnings || [];
      console.log("  OK: " + assigned + " skills assigned, mode=" + snap.mode);
      if (warnings.length > 0) console.log("  Warnings: " + warnings.join("; "));
    } else {
      console.log("  FAILED (" + syncRes.status + "): " + JSON.stringify(syncRes.body).slice(0, 300));
    }
  }

  console.log("\nDone.");
})();
