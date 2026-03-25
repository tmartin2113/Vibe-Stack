# Backend Engineer Instructions

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files. Use the Read tool.
- **Use Glob, not `find`** — never use `find` via Bash to locate files. Use Glob.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code. Use Grep.
- These dedicated tools are faster, produce cleaner output, and cost fewer tokens.

## Do Not Re-read Files

If you read a file earlier in this session, do not read it again. You already have the contents in context. This applies after edits too — you know what you changed, so you know the current state. The only exception is if the file was modified by an external process (e.g., `npm install` updated `package-lock.json`).

## Read the Architecture Spec

If an `ARCHITECTURE.md` exists in the project root, read it before writing any code. It defines the tech stack, error handling patterns, security requirements, testing standards, and cross-agent conventions. Follow it.

## Check Sibling Work

If your task references work by another agent (e.g., "integrate with frontend", "use the API from backend"), read their code in the project before building your integration. Don't assume — verify the actual interfaces, types, and contracts they implemented.

## Always Run Tests Before Marking Done

Before marking any task as `done`:
1. Run `npm test` (or the project's test command) in the backend directory
2. Confirm all tests pass
3. Include the test count and pass status in your completion comment

Never claim tests pass without actually running them.

## Git Commits

- Stage specific files, not `git add .`
- End every commit message with: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`
- Run `git diff --cached` before committing to verify you're committing what you intend

## Use DeerFlow Research to Save Tokens

Your DeerFlow assistant runs pre-flight research before your heartbeat. Use it — the research brief in the comments gives you context so you can skip broad codebase exploration and go straight to implementation. If you need deeper research mid-task, delegate a subtask to your DeerFlow assistant rather than doing extensive web searches or file exploration yourself.
