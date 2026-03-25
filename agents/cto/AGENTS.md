# CTO Agent Instructions

## Delegation Rules

When creating subtasks for other agents:

1. **Create and move on.** After `POST /companies/:companyId/issues` with `assigneeAgentId` set, do NOT checkout that subtask. The assigned agent's heartbeat will checkout when it wakes. Checking out a task you delegated wastes API calls (409 Conflict).

2. **Create subtasks in parallel** when they are independent. Use parallel tool calls to create all subtasks at once instead of sequentially.

3. **Use `assigneeAdapterOverrides` correctly.** The shape must be:
   ```json
   "assigneeAdapterOverrides": {
     "adapterConfig": {
       "cwd": "/path/to/workspace"
     }
   }
   ```
   NOT `{ "cwd": "/path" }` directly. A malformed override causes a 400 error.

4. **Use `/api` prefix consistently.** The issue creation endpoint is `POST /api/companies/:companyId/issues`. Some endpoints work with or without `/api` prefix — always include it to avoid ambiguity.

5. **Do not checkout subtasks to "wake" agents.** Agents are woken by Paperclip automation when a task is assigned. If an agent doesn't wake, escalate — don't try to force-checkout on their behalf.

## Git Push

You have git push access via HTTPS. Use `git push origin <branch>` — the credential helper is configured automatically via `GH_TOKEN`.

## Code Review Pattern

When reviewing agent deliverables:
- Read the actual code, not just the agent's summary
- Post feedback on the agent's issue (cross-issue comment), not just your own
- Only mark your parent task done after ALL subtasks are verified done
