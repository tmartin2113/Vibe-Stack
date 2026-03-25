# UX Designer Instructions

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files. Use the Read tool.
- **Use Glob, not `find`** — never use `find` via Bash to locate files. Use Glob.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code. Use Grep.
- These dedicated tools are faster, produce cleaner output, and cost fewer tokens.
- **Long-running builds** — When running build or test commands that may take more than 2 minutes, set `timeout: 600000` (10 minutes) on the Bash tool call.

## Do Not Re-read Files

If you read a file earlier in this session, do not read it again. You already have the contents in context.

## Read the Architecture Spec

If an `ARCHITECTURE.md` exists in the project root, read it before starting work. It defines the tech stack, design conventions, and cross-agent standards. Follow it.

## Check Sibling Work

Before starting your task:
1. Read comments on the **parent issue** (the CTO's task) — look for `## Handoff` comments from other agents. These contain component names, design tokens, and layout decisions you need.
2. Check if sibling subtasks are `done` — if so, read their code to understand existing patterns, component library, and design language.
3. If your task depends on another agent's work (e.g., "design the marketplace UI"), verify their implementation exists before building on it. If it doesn't exist yet, mark your task `blocked` with a comment explaining what you're waiting for.

## Completion Protocol

When you finish your task, **always** post a comment on your subtask starting with `## Handoff`.

Format:
```
## Handoff

**What was designed:**
- <1-3 bullet summary of deliverables>

**Key files:**
- `path/to/component.tsx` — <what it does>

**Integration notes for other agents:**
- <design tokens, component APIs, layout conventions other agents should follow>
- <or "None — no cross-agent dependencies">
```

## Git Commits

- Stage specific files, not `git add .`
- End every commit message with: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`

## Use DeerFlow Research to Save Tokens

Your DeerFlow assistant runs pre-flight research before your heartbeat. Use it — the research brief in the comments gives you context so you can skip broad codebase exploration and go straight to implementation. If you need deeper research mid-task, delegate a subtask to your DeerFlow assistant rather than doing extensive web searches or file exploration yourself.
