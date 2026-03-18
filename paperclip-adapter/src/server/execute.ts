import type {
  AdapterExecutionContext,
  AdapterExecutionResult,
} from "@paperclipai/adapter-utils";
import {
  asString,
  asNumber,
  asStringArray,
  parseObject,
  buildPaperclipEnv,
  redactEnvForLogs,
  runChildProcess,
  ensureAbsoluteDirectory,
  ensureCommandResolvable,
  ensurePathInEnv,
} from "@paperclipai/adapter-utils/server-utils";
import { parseVibeOutput } from "./parse.js";
import {
  notifyClarificationViaSlack,
  type SlackNotifyResult,
} from "./slack-notifier.js";
import { pollForSlackReply } from "./slack-reply-poller.js";

export async function execute(
  ctx: AdapterExecutionContext,
): Promise<AdapterExecutionResult> {
  try {
    return await _executeInner(ctx);
  } catch (err: unknown) {
    const message =
      err instanceof Error ? err.message : String(err);
    return {
      exitCode: 1,
      signal: null,
      timedOut: false,
      errorMessage: `Adapter crash: ${message}`,
      resultJson: { adapterError: message },
    };
  }
}

async function _executeInner(
  ctx: AdapterExecutionContext,
): Promise<AdapterExecutionResult> {
  const { runId, agent, config, context, onLog, onMeta, authToken } = ctx;

  const command = asString(config.command, "python");
  const args = (() => {
    const configured = asStringArray(config.args);
    return configured.length > 0
      ? configured
      : ["-m", "agents.main", "--heartbeat"];
  })();
  const cwd = asString(config.cwd, process.cwd());
  const taskType = asString(config.taskType, "");
  const timeoutSec = asNumber(config.timeoutSec, 660);
  const graceSec = asNumber(config.graceSec, 15);

  await ensureAbsoluteDirectory(cwd, { createIfMissing: false });

  // Build environment
  const envConfig = parseObject(config.env);
  const env: Record<string, string> = { ...buildPaperclipEnv(agent) };
  env.PAPERCLIP_RUN_ID = runId;

  // Inject wake context from Paperclip
  const wakeTaskId =
    asString(context.taskId, "") || asString(context.issueId, "");
  const wakeReason = asString(context.wakeReason, "");
  const wakeCommentId =
    asString(context.wakeCommentId, "") || asString(context.commentId, "");

  if (wakeTaskId) env.PAPERCLIP_TASK_ID = wakeTaskId;
  if (wakeReason) env.PAPERCLIP_WAKE_REASON = wakeReason;
  if (wakeCommentId) env.PAPERCLIP_WAKE_COMMENT_ID = wakeCommentId;
  if (taskType) env.VIBE_TASK_TYPE = taskType;

  // User-provided env overrides (last, so they win)
  for (const [key, value] of Object.entries(envConfig)) {
    if (typeof value === "string") env[key] = value;
  }

  // Inject API key from JWT if no explicit key
  const hasExplicitApiKey =
    typeof envConfig.PAPERCLIP_API_KEY === "string" &&
    (envConfig.PAPERCLIP_API_KEY as string).trim().length > 0;
  if (!hasExplicitApiKey && authToken) {
    env.PAPERCLIP_API_KEY = authToken;
  }

  const runtimeEnv = ensurePathInEnv({ ...process.env, ...env });
  await ensureCommandResolvable(command, cwd, runtimeEnv);

  if (onMeta) {
    await onMeta({
      adapterType: "vibe_local",
      command,
      cwd,
      commandArgs: args,
      env: redactEnvForLogs(env),
    });
  }

  const proc = await runChildProcess(runId, command, args, {
    cwd,
    env,
    timeoutSec,
    graceSec,
    onLog,
  });

  if (proc.timedOut) {
    return {
      exitCode: proc.exitCode,
      signal: proc.signal,
      timedOut: true,
      errorMessage: `Vibe timed out after ${timeoutSec}s`,
    };
  }

  const parsed = parseVibeOutput(proc.stdout);

  if ((proc.exitCode ?? 0) !== 0 && !parsed.resultJson) {
    return {
      exitCode: proc.exitCode,
      signal: proc.signal,
      timedOut: false,
      errorMessage: `Vibe exited with code ${proc.exitCode ?? -1}`,
      resultJson: { stdout: proc.stdout, stderr: proc.stderr },
    };
  }

  // Build result JSON, merging clarification into it for the orchestrator
  const resultJson: Record<string, unknown> = parsed.resultJson ?? {};
  if (parsed.clarification) {
    resultJson.clarification = parsed.clarification;
  }

  // Two-way Slack bridge: send clarification DM, poll for reply, forward to Paperclip
  if (parsed.clarification && parsed.clarification.questions.length > 0) {
    const slackToken =
      asString(config.slackBotToken, "") ||
      process.env.VIBE_SLACK_BOT_TOKEN ||
      "";
    const slackUserId =
      asString(config.slackNotifyUserId, "") ||
      process.env.VIBE_SLACK_NOTIFY_USER_ID ||
      "";
    const slackBotUserId =
      asString(config.slackBotUserId, "") ||
      process.env.VIBE_SLACK_BOT_USER_ID ||
      "";
    const issueBaseUrl = asString(config.paperclipIssueBaseUrl, "");
    const issueId = asString(resultJson.issue_id, wakeTaskId);
    const slackReplyTimeout = asNumber(config.slackReplyTimeoutSec, 300);

    if (slackToken && slackUserId) {
      try {
        const logFn = onLog ? (line: string) => onLog("stdout", line) : undefined;

        // Step 1: Send the DM (now returns metadata)
        const notifyResult: SlackNotifyResult =
          await notifyClarificationViaSlack(
            {
              botToken: slackToken,
              userId: slackUserId,
              questions: parsed.clarification.questions,
              issueId,
              issueUrl: issueBaseUrl
                ? `${issueBaseUrl}/${issueId}`
                : undefined,
              agentName: agent.name ?? undefined,
            },
            logFn,
          );

        // Step 2: Poll for reply if DM was sent successfully
        if (notifyResult.ok && notifyResult.channelId && notifyResult.messageTs) {
          const pollResult = await pollForSlackReply(
            {
              botToken: slackToken,
              channelId: notifyResult.channelId,
              messageTs: notifyResult.messageTs,
              botUserId: slackBotUserId || undefined,
              timeoutSeconds: slackReplyTimeout,
            },
            logFn,
          );

          // Step 3: Forward reply to Paperclip as issue comment
          if (pollResult.replied && pollResult.replyText && issueId) {
            const forwarded = await forwardReplyToPaperclip(
              issueId,
              pollResult.replyText,
              env,
              logFn,
            );
            if (forwarded) {
              resultJson._slackReplyForwarded = true;
            }
          }
        }
      } catch {
        // Best-effort — bridge failure must never affect the result
      }
    }
  }

  // Map Vibe heartbeat status to adapter-level exit code and error message
  // so Paperclip can distinguish idle/blocked/clarification from true success.
  const vibeStatus = String(resultJson.status ?? "");
  let effectiveExitCode = proc.exitCode ?? 0;
  let effectiveErrorMessage: string | null = null;

  switch (vibeStatus) {
    case "success":
      effectiveExitCode = 0;
      effectiveErrorMessage = null;
      break;
    case "idle":
      // No work available — not an error, but not a successful task completion.
      // Exit code 0 (no failure), but errorMessage signals "nothing happened".
      effectiveExitCode = 0;
      effectiveErrorMessage = null;
      resultJson._adapterNote = "idle_no_work_available";
      break;
    case "blocked":
      // Quality gate failed or issue stuck — needs human attention.
      effectiveExitCode = 1;
      effectiveErrorMessage = `Task blocked: ${parsed.summary || "quality below threshold"}`;
      break;
    case "clarification_needed":
      // Agent is waiting for human input — not a failure.
      effectiveExitCode = 0;
      effectiveErrorMessage = null;
      resultJson._adapterNote = "awaiting_human_clarification";
      break;
    case "cancelled":
      // User cancelled the task — not a failure.
      effectiveExitCode = 0;
      effectiveErrorMessage = null;
      resultJson._adapterNote = "workflow_cancelled_by_user";
      break;
    case "failed":
      effectiveExitCode = 1;
      effectiveErrorMessage = `Vibe failed: ${parsed.summary || "unknown error"}`;
      break;
    default:
      // Unknown or missing status — fall back to process exit code
      if (effectiveExitCode !== 0) {
        effectiveErrorMessage = `Vibe exited with code ${effectiveExitCode}`;
      }
      break;
  }

  return {
    exitCode: effectiveExitCode,
    signal: proc.signal,
    timedOut: false,
    errorMessage: effectiveErrorMessage,
    usage: parsed.usage ?? undefined,
    provider: parsed.provider || null,
    model: parsed.model || null,
    billingType: "api",
    costUsd: parsed.costCents > 0 ? parsed.costCents / 100 : 0,
    resultJson,
    summary: parsed.summary || undefined,
  };
}


/**
 * Forward a Slack reply to Paperclip as an issue comment.
 *
 * Uses the Paperclip REST API: POST /api/issues/{issueId}/comments
 * with the auth token from the runtime environment.
 */
async function forwardReplyToPaperclip(
  issueId: string,
  replyText: string,
  env: Record<string, string>,
  onLog?: (line: string) => void,
): Promise<boolean> {
  const log = onLog ?? (() => {});

  const apiUrl = (env.PAPERCLIP_API_URL || "").replace(/\/+$/, "");
  const apiKey = env.PAPERCLIP_API_KEY || "";

  if (!apiUrl || !apiKey) {
    log(
      "[slack-bridge] Cannot forward reply: missing PAPERCLIP_API_URL or PAPERCLIP_API_KEY",
    );
    return false;
  }

  const body = `## Clarification Reply (via Slack)\n\n${replyText}`;

  try {
    const res = await fetch(`${apiUrl}/api/issues/${issueId}/comments`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ body }),
    });

    if (!res.ok) {
      log(
        `[slack-bridge] Failed to post comment to Paperclip: HTTP ${res.status}`,
      );
      return false;
    }

    log(`[slack-bridge] Forwarded Slack reply to Paperclip issue ${issueId}`);
    return true;
  } catch (err) {
    log(
      `[slack-bridge] Error posting to Paperclip: ${err instanceof Error ? err.message : String(err)}`,
    );
    return false;
  }
}
