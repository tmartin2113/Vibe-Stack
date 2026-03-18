/**
 * Slack reply poller for the two-way clarification bridge.
 *
 * After the notifier sends a DM with clarification questions, this module
 * polls the Slack DM thread for the human's reply. When a reply arrives,
 * it is returned so the caller can forward it to Paperclip as a comment.
 *
 * Design choices:
 *   - Polls conversations.replies (thread-based) rather than channel history,
 *     so only threaded replies to the question message are captured.
 *   - Filters out bot messages (the notifier's own message).
 *   - Concatenates multiple human replies into one response.
 *   - Gives up after a configurable timeout (default 300s / 5 min).
 */

export interface SlackPollOptions {
  /** Slack bot token (same as notifier) */
  botToken: string;
  /** DM channel ID returned by the notifier */
  channelId: string;
  /** Message timestamp of the clarification DM (thread parent) */
  messageTs: string;
  /** Bot's own user ID — used to filter out bot messages.
   *  If not provided, only the parent message (matching messageTs) is filtered. */
  botUserId?: string;
  /** Maximum time to poll in seconds (default 300 = 5 min) */
  timeoutSeconds?: number;
  /** Interval between polls in seconds (default 5) */
  pollIntervalSeconds?: number;
}

export interface SlackPollResult {
  /** Whether a human reply was found */
  replied: boolean;
  /** The human's reply text (concatenated if multiple messages) */
  replyText?: string;
  /** Whether the poller timed out waiting */
  timedOut: boolean;
}

interface SlackRepliesResponse {
  ok: boolean;
  error?: string;
  messages?: Array<{
    user?: string;
    bot_id?: string;
    text?: string;
    ts?: string;
  }>;
}

/**
 * Poll a Slack DM thread for human replies to a clarification message.
 *
 * Blocks until a reply is found or the timeout expires.
 * Never throws — errors are logged and treated as "no reply yet".
 */
export async function pollForSlackReply(
  options: SlackPollOptions,
  onLog?: (line: string) => void,
): Promise<SlackPollResult> {
  const log = onLog ?? (() => {});
  const {
    botToken,
    channelId,
    messageTs,
    botUserId,
    timeoutSeconds = 300,
    pollIntervalSeconds = 5,
  } = options;

  const headers = {
    Authorization: `Bearer ${botToken}`,
    "Content-Type": "application/json",
  };

  const deadline = Date.now() + timeoutSeconds * 1000;
  log(
    `[slack-reply-poller] Polling thread ${messageTs} in ${channelId} (timeout ${timeoutSeconds}s)`,
  );

  while (Date.now() < deadline) {
    try {
      const params = new URLSearchParams({
        channel: channelId,
        ts: messageTs,
        oldest: messageTs,
      });

      const res = await fetch(
        `https://slack.com/api/conversations.replies?${params.toString()}`,
        { method: "GET", headers },
      );
      const data = (await res.json()) as SlackRepliesResponse;

      if (!data.ok) {
        log(
          `[slack-reply-poller] API error: ${data.error ?? "unknown"} — will retry`,
        );
      } else if (data.messages) {
        // Filter: keep only human replies (not the parent, not bot messages)
        const humanReplies = data.messages.filter((msg) => {
          // Skip the parent message itself
          if (msg.ts === messageTs) return false;
          // Skip bot messages
          if (msg.bot_id) return false;
          // Skip messages from the bot user
          if (botUserId && msg.user === botUserId) return false;
          // Must have text content
          if (!msg.text?.trim()) return false;
          return true;
        });

        if (humanReplies.length > 0) {
          const replyText = humanReplies
            .map((msg) => msg.text!.trim())
            .join("\n\n");
          log(
            `[slack-reply-poller] Got ${humanReplies.length} reply message(s) from human`,
          );
          return { replied: true, replyText, timedOut: false };
        }
      }
    } catch (err) {
      log(
        `[slack-reply-poller] Fetch error: ${err instanceof Error ? err.message : String(err)} — will retry`,
      );
    }

    // Wait before next poll
    await sleep(pollIntervalSeconds * 1000);
  }

  log(`[slack-reply-poller] Timed out after ${timeoutSeconds}s — no reply`);
  return { replied: false, timedOut: true };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
