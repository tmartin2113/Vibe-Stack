/**
 * Refreshes the Claude Code OAuth token using the stored refresh token.
 * Intended to run at container boot before the main service starts.
 *
 * Reads from $HOME/.claude/.credentials.json, exchanges the refresh token
 * for a new access token, and writes the updated credentials back.
 */

import fs from "node:fs";
import path from "node:path";
import https from "node:https";

const TOKEN_URL = "https://platform.claude.com/v1/oauth/token";
const CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";
const SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers";

const credentialsPath = path.join(
  process.env.HOME || "/paperclip",
  ".claude",
  ".credentials.json"
);

function post(url, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const parsed = new URL(url);
    const req = https.request(
      {
        hostname: parsed.hostname,
        path: parsed.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(data),
        },
      },
      (res) => {
        let chunks = "";
        res.on("data", (c) => (chunks += c));
        res.on("end", () => {
          try {
            resolve({ status: res.statusCode, body: JSON.parse(chunks) });
          } catch {
            resolve({ status: res.statusCode, body: chunks });
          }
        });
      }
    );
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

try {
  if (!fs.existsSync(credentialsPath)) {
    console.log("[refresh-token] No credentials file found, skipping.");
    process.exit(0);
  }

  const creds = JSON.parse(fs.readFileSync(credentialsPath, "utf-8"));
  const oauth = creds.claudeAiOauth;

  if (!oauth?.refreshToken) {
    console.log("[refresh-token] No refresh token found, skipping.");
    process.exit(0);
  }

  const buffer = 300_000; // 5 minutes
  if (oauth.expiresAt && Date.now() + buffer < oauth.expiresAt) {
    const minutesLeft = Math.round((oauth.expiresAt - Date.now()) / 60_000);
    console.log(
      `[refresh-token] Token still valid for ~${minutesLeft} minutes, skipping.`
    );
    process.exit(0);
  }

  console.log("[refresh-token] Token expired or expiring soon, refreshing...");

  const result = await post(TOKEN_URL, {
    grant_type: "refresh_token",
    refresh_token: oauth.refreshToken,
    client_id: CLIENT_ID,
    scope: SCOPES,
  });

  if (result.status !== 200 || !result.body.access_token) {
    console.error(
      "[refresh-token] Failed to refresh:",
      result.status,
      result.body
    );
    process.exit(1);
  }

  const newOauth = {
    ...oauth,
    accessToken: result.body.access_token,
    refreshToken: result.body.refresh_token || oauth.refreshToken,
    expiresAt: Date.now() + result.body.expires_in * 1000,
    scopes: result.body.scope
      ? result.body.scope.split(" ")
      : oauth.scopes,
  };

  creds.claudeAiOauth = newOauth;
  fs.writeFileSync(credentialsPath, JSON.stringify(creds), "utf-8");

  const minutesValid = Math.round((newOauth.expiresAt - Date.now()) / 60_000);
  console.log(
    `[refresh-token] Token refreshed successfully. Valid for ~${minutesValid} minutes.`
  );
} catch (err) {
  console.error("[refresh-token] Error:", err.message);
  process.exit(1);
}
