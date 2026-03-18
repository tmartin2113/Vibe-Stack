/**
 * Adapter configuration shape for genesia_local agents.
 *
 * Stored in `agents.adapter_config` in Paperclip's database.
 */
export interface GenesiaAdapterConfig {
  /** Python command to execute (default: "python") */
  command?: string;
  /** CLI arguments (default: ["-m", "agents.main", "--heartbeat"]) */
  args?: string[];
  /** Working directory for the Genesia project */
  cwd?: string;
  /** Force a specific Genesia task type */
  taskType?: string;
  /** Environment variable overrides */
  env?: Record<string, string>;
  /** Run timeout in seconds (default: 600) */
  timeoutSec?: number;
  /** SIGTERM grace period in seconds (default: 15) */
  graceSec?: number;

  // ── Human-in-the-loop notification ──

  /** Slack bot token (xoxb-*) for sending clarification DMs to humans */
  slackBotToken?: string;
  /** Default Slack user ID to notify when clarification is needed */
  slackNotifyUserId?: string;
  /** Slack bot's own user ID — used to filter out bot messages when polling replies */
  slackBotUserId?: string;
  /**
   * Base URL for Paperclip issue links in Slack notifications.
   * e.g., "https://app.paperclip.dev/issues" → links become
   * "https://app.paperclip.dev/issues/{issueId}"
   */
  paperclipIssueBaseUrl?: string;
  /**
   * Maximum seconds to poll Slack for a human reply before giving up.
   * Default: 300 (5 minutes). Set to 0 to disable polling (notification-only).
   */
  slackReplyTimeoutSec?: number;
}
