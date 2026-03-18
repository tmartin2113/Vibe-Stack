export const type = "genesia_local";
export const label = "Genesia (Local)";

export const models: { id: string; label: string }[] = [];

export const agentConfigurationDoc = `# genesia_local agent configuration

Adapter: genesia_local

Runs a local Genesia multi-agent system in heartbeat mode. Each heartbeat,
Genesia fetches its assigned task from Paperclip, runs the full workflow
(router → skills → specialist → critic → quality gate), and posts results
back.

Core fields:
- command (string, optional): Python command (default "python")
- args (string[], optional): CLI arguments (default ["-m", "agents.main", "--heartbeat"])
- cwd (string, optional): Genesia project directory
- taskType (string, optional): Force a specific Genesia task type (e.g., "code", "test_generation", "security_audit"). If unset, Genesia's router auto-classifies from issue content.
- env (object, optional): KEY=VALUE environment variables

Operational fields:
- timeoutSec (number, optional): run timeout in seconds (default 600)
- graceSec (number, optional): SIGTERM grace period in seconds (default 15)

Human-in-the-loop (Slack notifications):
- slackBotToken (string, optional): Slack bot token (xoxb-*) for DM notifications
- slackNotifyUserId (string, optional): Default Slack user ID to notify
- paperclipIssueBaseUrl (string, optional): Base URL for issue links (e.g., "https://app.paperclip.dev/issues")

When the agent needs clarification, it blocks and posts questions on the Paperclip
issue. If slackBotToken + slackNotifyUserId are configured, the adapter also sends
a Slack DM to the human with the questions and a link to the issue. The human
replies on the Paperclip issue, which wakes the agent to continue.

Genesia-specific env vars:
- GENESIA_BACKEND_HOST: Ollama host (default "localhost")
- GENESIA_BACKEND_PORT: Ollama port (default "11434")
- GENESIA_TASK_TYPE: Same as taskType above (env var form)
- GENESIA_SLACK_BOT_TOKEN: Same as slackBotToken (env var form)
- GENESIA_SLACK_NOTIFY_USER_ID: Same as slackNotifyUserId (env var form)
`;
