export const type = "vibe_local";
export const label = "Vibe (Local)";

export const models: { id: string; label: string }[] = [];

export const agentConfigurationDoc = `# vibe_local agent configuration

Adapter: vibe_local

Runs a local Vibe multi-agent system in heartbeat mode. Each heartbeat,
Vibe fetches its assigned task from Paperclip, runs the full workflow
(router → skills → specialist → critic → quality gate), and posts results
back.

Core fields:
- command (string, optional): Python command (default "python")
- args (string[], optional): CLI arguments (default ["-m", "agents.main", "--heartbeat"])
- cwd (string, optional): Vibe project directory
- taskType (string, optional): Force a specific Vibe task type (e.g., "code", "test_generation", "security_audit"). If unset, Vibe's router auto-classifies from issue content.
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

Vibe-specific env vars:
- VIBE_BACKEND_HOST: Ollama host (default "localhost")
- VIBE_BACKEND_PORT: Ollama port (default "11434")
- VIBE_TASK_TYPE: Same as taskType above (env var form)
- VIBE_SLACK_BOT_TOKEN: Same as slackBotToken (env var form)
- VIBE_SLACK_NOTIFY_USER_ID: Same as slackNotifyUserId (env var form)
`;
