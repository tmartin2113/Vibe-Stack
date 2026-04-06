#!/usr/bin/env node
// Reset admin password in the embedded Paperclip DB.
// Reads PAPERCLIP_ADMIN_PASSWORD from .env

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

let password = process.argv[2];

if (!password) {
  const envPath = path.resolve(__dirname, ".env");
  if (fs.existsSync(envPath)) {
    for (const line of fs.readFileSync(envPath, "utf-8").split("\n")) {
      const match = line.match(/^\s*PAPERCLIP_ADMIN_PASSWORD\s*=\s*(.*)\s*$/);
      if (match) {
        password = match[1].split("#")[0].trim();
        break;
      }
    }
  }
}

if (!password) {
  console.error("Usage: node reset-admin-pw.js <password>");
  console.error("Or set PAPERCLIP_ADMIN_PASSWORD in .env");
  process.exit(1);
}

// Build the Node.js script to run inside the container
const innerScript = [
  "import{randomBytes,scryptSync}from'node:crypto';",
  "import postgres from'/app/node_modules/.pnpm/postgres@3.4.8/node_modules/postgres/src/index.js';",
  "const sql=postgres({host:'/tmp',port:54329,user:'paperclip',database:'paperclip'});",
  `const p=process.env._RESET_PW;`,
  "const s=randomBytes(16).toString('hex');",
  "const h=scryptSync(p,s,64).toString('hex');",
  "const r=await sql`UPDATE account SET password=${s+':'+h} WHERE user_id='YtMkMeflDrY4oULdA2KTPLQYXZL1EFbB' RETURNING id`;",
  "console.log(r.length?'Password reset done':'No account found');",
  "await sql.end();",
].join("");

const result = execFileSync("docker", [
  "compose", "exec", "-T", "-w", "/app",
  "-e", `_RESET_PW=${password}`,
  "server", "node", "--input-type=module", "-e", innerScript,
], { cwd: __dirname, encoding: "utf-8" });

console.log(result.trim());
