import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Load .env file if present
const __dirname = path.dirname(fileURLToPath(import.meta.url));
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

const COMPANY_ID = process.env.PAPERCLIP_COMPANY_ID;
if (!COMPANY_ID) {
  console.error("Set PAPERCLIP_COMPANY_ID in .env or environment");
  process.exit(1);
}

function request(method, reqPath, cookie, data) {
  return new Promise((resolve, reject) => {
    const body = data ? JSON.stringify(data) : null;
    const headers = { Origin: `https://${HOSTNAME}`, Host: HOSTNAME };
    if (cookie) headers["Cookie"] = cookie;
    if (body) { headers["Content-Type"] = "application/json"; headers["Content-Length"] = Buffer.byteLength(body); }
    const req = http.request({ hostname: "127.0.0.1", port: 3100, path: reqPath, method, headers }, (res) => {
      let chunks = "";
      const setCookies = res.headers["set-cookie"] || [];
      res.on("data", (c) => (chunks += c));
      res.on("end", () => { try { resolve({ status: res.statusCode, body: JSON.parse(chunks), setCookies }); } catch { resolve({ status: res.statusCode, body: chunks, setCookies }); } });
    });
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}
(async () => {
  const signin = await request("POST", "/api/auth/sign-in/email", "", { email: "prime@vibe.local", password: PASSWORD });
  const cookie = signin.setCookies.map((c) => c.split(";")[0]).join("; ");

  const live = await request("GET", "/api/companies/" + COMPANY_ID + "/live-runs", cookie);
  const runs = Array.isArray(live.body) ? live.body : [];
  console.log("Live runs:", runs.length);
  runs.forEach(r => console.log("  ", r.agentName, "|", r.status, "|", r.id));

  const agents = await request("GET", "/api/companies/" + COMPANY_ID + "/agents", cookie);
  agents.body.forEach(a => {
    if (a.status !== "idle") console.log(a.name, ":", a.status);
  });

  // Check latest issues
  const issues = await request("GET", "/api/companies/" + COMPANY_ID + "/issues", cookie);
  const recent = issues.body.slice(0, 5);
  if (recent.length) {
    console.log("\nRecent issues:");
    recent.forEach(i => console.log(" ", i.identifier || i.id, ":", i.status, "|", i.title));
  }
})().catch(e => console.error(e));
