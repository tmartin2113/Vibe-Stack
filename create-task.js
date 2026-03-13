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

const COMPANY_ID = process.env.PAPERCLIP_COMPANY_ID;
const CEO_ID = process.env.PAPERCLIP_CEO_ID;
if (!COMPANY_ID || !CEO_ID) {
  console.error("Set PAPERCLIP_COMPANY_ID and PAPERCLIP_CEO_ID in .env or environment");
  process.exit(1);
}

function request(method, reqPath, cookie, data) {
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
      { hostname: "127.0.0.1", port: 3100, path: reqPath, method, headers },
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

(async () => {
  const signin = await request("POST", "/api/auth/sign-in/email", "", {
    email: "prime@vibe.local",
    password: PASSWORD,
  });
  if (signin.status !== 200) {
    console.error("Signin failed:", signin.status, JSON.stringify(signin.body));
    process.exit(1);
  }
  const cookie = signin.setCookies.map((c) => c.split(";")[0]).join("; ");
  console.log("Signed in as", signin.body.user.name);

  const issue = await request("POST", `/api/companies/${COMPANY_ID}/issues`, cookie, {
    title: "Example issue — replace with your task",
    description: "Describe the task here. This is a template.",
    status: "backlog",
    priority: "high",
    assigneeAgentId: CEO_ID,
  });

  if (issue.status === 201) {
    console.log("\nIssue created successfully:");
    console.log("  ID:", issue.body.id);
    console.log("  Title:", issue.body.title);
    console.log("  Status:", issue.body.status);
    console.log("  Assigned to: CEO (" + CEO_ID + ")");
  } else {
    console.error("Failed:", issue.status, JSON.stringify(issue.body));
  }
})().catch(e => console.error(e));
