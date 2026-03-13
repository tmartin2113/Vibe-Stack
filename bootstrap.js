const http = require("http");
const fs = require("fs");
const path = require("path");

// Load .env file if present
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

function request(method, path, cookie, data) {
  return new Promise((resolve, reject) => {
    const body = data ? JSON.stringify(data) : null;
    const headers = {
      "Origin": `https://${HOSTNAME}`,
      "Host": HOSTNAME,
    };
    if (cookie) headers["Cookie"] = cookie;
    if (body) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = Buffer.byteLength(body);
    }
    const req = http.request(
      { hostname: "127.0.0.1", port: 3100, path, method, headers },
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

(async () => {
  // Sign in as Prime
  const signin = await request("POST", "/api/auth/sign-in/email", "", {
    email: "prime@vibe.local",
    password: PASSWORD,
  });
  console.log("Signin:", signin.status, signin.body.user ? signin.body.user.name : signin.body);
  const cookie = extractCookie(signin.setCookies);
  if (signin.status !== 200) {
    console.error("Signin failed");
    process.exit(1);
  }

  // Create company
  const company = await request("POST", "/api/companies", cookie, {
    name: "Vibe Stack",
    description: "Autonomous agent network for software development",
  });
  console.log("Company:", company.status, company.body.name || company.body);
  if (!company.body.id) {
    console.error("Company creation failed:", JSON.stringify(company.body));
    process.exit(1);
  }
  const companyId = company.body.id;

  // Create CEO agent
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
  console.log("CEO:", ceo.status, ceo.body.name || ceo.body);
  if (!ceo.body.id) {
    console.error("CEO creation failed:", JSON.stringify(ceo.body));
    process.exit(1);
  }

  console.log("\n=== Bootstrap complete ===");
  console.log("Company ID:", companyId);
  console.log("CEO Agent ID:", ceo.body.id);
})().catch((e) => console.error(e));
