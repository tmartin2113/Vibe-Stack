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

const PROJECTS_DIR = process.env.PROJECTS_DIR || "/srv/sftp/workspace/files";
const REPO_DIR = process.cwd();

function request(method, path, cookie, data) {
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

const agents = [
  {
    name: "CTO",
    role: "general",
    title: "Chief Technology Officer",
    adapterType: "claude_local",
    adapterConfig: {
      cwd: REPO_DIR,
      model: "claude-opus-4-6",
      effort: "high",
    },
    permissions: { canCreateAgents: false },
  },
  {
    name: "DevOps Engineer",
    role: "engineer",
    title: "DevOps Engineer",
    adapterType: "claude_local",
    adapterConfig: {
      cwd: REPO_DIR,
      model: "claude-sonnet-4-6",
      effort: "high",
    },
    permissions: { canCreateAgents: false },
  },
  {
    name: "Software Engineer",
    role: "engineer",
    title: "Software Engineer",
    adapterType: "claude_local",
    adapterConfig: {
      cwd: PROJECTS_DIR,
      model: "claude-sonnet-4-6",
      effort: "high",
    },
    permissions: { canCreateAgents: false },
  },
  {
    name: "QA Engineer",
    role: "engineer",
    title: "QA Engineer",
    adapterType: "claude_local",
    adapterConfig: {
      cwd: PROJECTS_DIR,
      model: "claude-sonnet-4-6",
      effort: "high",
    },
    permissions: { canCreateAgents: false },
  },
  {
    name: "UX Designer",
    role: "engineer",
    title: "UX Designer",
    adapterType: "claude_local",
    adapterConfig: {
      cwd: PROJECTS_DIR,
      model: "claude-sonnet-4-6",
      effort: "high",
    },
    permissions: { canCreateAgents: false },
  },
];

(async () => {
  // Sign in
  const signin = await request("POST", "/api/auth/sign-in/email", "", {
    email: "prime@vibe.local",
    password: PASSWORD,
  });
  if (signin.status !== 200) {
    console.error("Signin failed:", signin.status, signin.body);
    process.exit(1);
  }
  const cookie = signin.setCookies.map((c) => c.split(";")[0]).join("; ");
  console.log("Signed in as", signin.body.user.name);

  // Get company
  const companies = await request("GET", "/api/companies", cookie);
  const companyId = companies.body[0]?.id;
  if (!companyId) {
    console.error("No company found");
    process.exit(1);
  }
  console.log("Company:", companies.body[0].name, companyId);

  // Get CEO agent ID for reportsTo
  const existingAgents = await request("GET", `/api/companies/${companyId}/agents`, cookie);
  const ceo = existingAgents.body.find((a) => a.role === "ceo");
  if (!ceo) {
    console.error("CEO agent not found");
    process.exit(1);
  }
  console.log("CEO:", ceo.name, ceo.id);

  // Create agents
  for (const agent of agents) {
    agent.reportsTo = ceo.id;
    const result = await request("POST", `/api/companies/${companyId}/agents`, cookie, agent);
    if (result.status === 201) {
      console.log(`  Created ${agent.name} (${result.body.id})`);
    } else {
      console.error(`  FAILED ${agent.name}:`, result.status, JSON.stringify(result.body));
    }
  }

  console.log("\n=== Org setup complete ===");
})().catch((e) => console.error(e));
