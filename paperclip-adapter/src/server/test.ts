import { execSync } from "node:child_process";
import type {
  AdapterEnvironmentTestContext,
  AdapterEnvironmentTestResult,
  AdapterEnvironmentCheck,
} from "@paperclipai/adapter-utils";
import { asString } from "@paperclipai/adapter-utils/server-utils";

export async function testEnvironment(
  ctx: AdapterEnvironmentTestContext,
): Promise<AdapterEnvironmentTestResult> {
  const checks: AdapterEnvironmentCheck[] = [];
  const command = asString((ctx.config as Record<string, unknown>).command, "python");

  // Check 1: Python is available
  try {
    const version = execSync(`${command} --version`, {
      timeout: 5000,
      encoding: "utf-8",
    }).trim();
    checks.push({
      code: "python_available",
      level: "info",
      message: `Python found: ${version}`,
    });
  } catch {
    checks.push({
      code: "python_missing",
      level: "error",
      message: `Python command "${command}" not found or not executable`,
      hint: "Install Python 3.10+ or set the 'command' field in adapter config",
    });
    return {
      adapterType: "vibe_local",
      status: "fail",
      checks,
      testedAt: new Date().toISOString(),
    };
  }

  // Check 2: Vibe agents package importable
  try {
    execSync(`${command} -c "import agents"`, {
      timeout: 10000,
      encoding: "utf-8",
    });
    checks.push({
      code: "vibe_importable",
      level: "info",
      message: "Vibe agents package is importable",
    });
  } catch {
    checks.push({
      code: "vibe_not_importable",
      level: "error",
      message: "Cannot import 'agents' Python package",
      hint: "Ensure Vibe is installed: pip install -e . (from the Vibe project root)",
    });
  }

  // Check 3: Ollama reachable (optional, warn-only)
  try {
    execSync("curl -sf http://localhost:11434/api/tags", {
      timeout: 3000,
      encoding: "utf-8",
    });
    checks.push({
      code: "ollama_reachable",
      level: "info",
      message: "Ollama is reachable at localhost:11434",
    });
  } catch {
    checks.push({
      code: "ollama_unreachable",
      level: "warn",
      message: "Ollama not reachable at localhost:11434",
      hint: "Ollama is required for local LLM inference. Start it with: ollama serve",
    });
  }

  // Check 4: Node.js version (Slack notifier requires >= 18 for global fetch)
  const nodeVersion = parseInt(process.versions.node.split(".")[0], 10);
  if (nodeVersion < 18) {
    checks.push({
      code: "node_version_low",
      level: "warn",
      message: `Node.js ${process.versions.node} detected — Slack notifications require Node.js >= 18 (global fetch)`,
      hint: "Upgrade to Node.js 18+ if you plan to use Slack clarification DMs",
    });
  }

  // Check 5: Slack notification configuration (optional, info-only)
  const cfg = ctx.config as Record<string, unknown>;
  const slackToken =
    asString(cfg.slackBotToken, "") ||
    process.env.VIBE_SLACK_BOT_TOKEN ||
    "";
  const slackUser =
    asString(cfg.slackNotifyUserId, "") ||
    process.env.VIBE_SLACK_NOTIFY_USER_ID ||
    "";
  if (slackToken && slackUser) {
    checks.push({
      code: "slack_configured",
      level: "info",
      message: "Slack notifications configured for human-in-the-loop",
    });
  } else {
    checks.push({
      code: "slack_not_configured",
      level: "info",
      message:
        "Slack notifications not configured (optional — set slackBotToken + slackNotifyUserId for clarification DMs)",
    });
  }

  const hasError = checks.some((c) => c.level === "error");
  const hasWarn = checks.some((c) => c.level === "warn");

  return {
    adapterType: "vibe_local",
    status: hasError ? "fail" : hasWarn ? "warn" : "pass",
    checks,
    testedAt: new Date().toISOString(),
  };
}
