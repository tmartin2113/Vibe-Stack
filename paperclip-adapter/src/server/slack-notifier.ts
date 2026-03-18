/**
 * Slack notification client for human-in-the-loop clarification.
 *
 * When a Genesia agent needs clarification, this module sends a Slack DM
 * to the relevant human with the questions. The human can reply directly
 * in the Slack DM thread — the adapter polls for their reply and forwards
 * it to Paperclip as an issue comment, triggering the normal resume flow.
 *
 * Configuration:
 *   - slackBotToken: Slack bot token (xoxb-*) in adapter config or env
 *   - slackNotifyUserId: Default Slack user ID to notify (fallback)
 *
 * Requirements:
 *   - Node.js >= 18 (uses global `fetch()` — no polyfill included)
 *
 * The notifier is best-effort — failures are logged but never block the
 * adapter result from being returned to Paperclip.
 */

export interface SlackNotifyOptions {
  /** Slack bot token (xoxb-...) */
  botToken: string;
  /** Slack user ID to DM (e.g., "U01AB2C3D4E") */
  userId: string;
  /** The clarification questions from the agent */
  questions: string[];
  /** Paperclip issue identifier (e.g., "GEN-42") for context */
  issueId: string;
  /** Optional Paperclip web URL for the issue */
  issueUrl?: string;
  /** Agent name for attribution */
  agentName?: string;
}

interface SlackApiResponse {
  ok: boolean;
  error?: string;
  channel?: { id: string };
  ts?: string;
}

/** Metadata returned on successful DM send, used by the reply poller. */
export interface SlackNotifyResult {
  ok: boolean;
  /** DM channel ID (e.g., "D01AB2C3D4E") */
  channelId?: string;
  /** Timestamp of the posted message — used as thread parent for replies */
  messageTs?: string;
}

/**
 * Send a clarification notification to a human via Slack DM.
 *
 * Opens a DM channel with the user and posts a formatted message
 * containing the agent's clarification questions.
 *
 * Returns metadata (channelId, messageTs) on success so the caller
 * can poll for threaded replies. Never throws.
 */
export async function notifyClarificationViaSlack(
  options: SlackNotifyOptions,
  onLog?: (line: string) => void,
): Promise<SlackNotifyResult> {
  const log = onLog ?? (() => {});
  const { botToken, userId, questions, issueId, issueUrl, agentName } =
    options;

  if (!botToken || !userId) {
    log(
      "[slack-notifier] Skipping: missing botToken or userId",
    );
    return { ok: false };
  }

  const headers = {
    Authorization: `Bearer ${botToken}`,
    "Content-Type": "application/json",
  };

  try {
    // Step 1: Open DM channel
    const openRes = await fetch("https://slack.com/api/conversations.open", {
      method: "POST",
      headers,
      body: JSON.stringify({ users: userId }),
    });
    const openData = (await openRes.json()) as SlackApiResponse;

    if (!openData.ok || !openData.channel?.id) {
      log(
        `[slack-notifier] Failed to open DM: ${openData.error ?? "no channel"}`,
      );
      return { ok: false };
    }

    const channelId = openData.channel.id;

    // Step 2: Build message
    const message = formatClarificationMessage(
      questions,
      issueId,
      issueUrl,
      agentName,
    );

    // Step 3: Send message
    const postRes = await fetch("https://slack.com/api/chat.postMessage", {
      method: "POST",
      headers,
      body: JSON.stringify({
        channel: channelId,
        text: message.fallbackText,
        blocks: message.blocks,
      }),
    });
    const postData = (await postRes.json()) as SlackApiResponse;

    if (!postData.ok) {
      log(`[slack-notifier] Failed to send DM: ${postData.error}`);
      return { ok: false };
    }

    log(
      `[slack-notifier] Sent clarification notification to ${userId} for ${issueId}`,
    );
    return { ok: true, channelId, messageTs: postData.ts };
  } catch (err) {
    log(
      `[slack-notifier] Error: ${err instanceof Error ? err.message : String(err)}`,
    );
    return { ok: false };
  }
}

interface SlackMessage {
  fallbackText: string;
  blocks: SlackBlock[];
}

interface SlackBlock {
  type: string;
  text?: { type: string; text: string };
  elements?: { type: string; text: string }[];
}

function formatClarificationMessage(
  questions: string[],
  issueId: string,
  issueUrl?: string,
  agentName?: string,
): SlackMessage {
  const agent = agentName ?? "An agent";
  const issueRef = issueUrl ? `<${issueUrl}|${issueId}>` : issueId;

  const questionList = questions
    .map((q, i) => `${i + 1}. ${q}`)
    .join("\n");

  const fallbackText = `${agent} needs your input on ${issueId}: ${questions.join("; ")}`;

  const blocks: SlackBlock[] = [
    {
      type: "header",
      text: {
        type: "plain_text",
        text: "Clarification Needed",
      },
    },
    {
      type: "section",
      text: {
        type: "mrkdwn",
        text: `${agent} is blocked on ${issueRef} and needs your input:`,
      },
    },
    {
      type: "section",
      text: {
        type: "mrkdwn",
        text: questionList,
      },
    },
    {
      type: "context",
      elements: [
        {
          type: "mrkdwn",
          text: `Reply in this thread to unblock the agent. Your answer will be forwarded automatically.`,
        },
      ],
    },
  ];

  return { fallbackText, blocks };
}
