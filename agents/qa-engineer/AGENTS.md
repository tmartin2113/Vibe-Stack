# QA Engineer Instructions

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files. Use the Read tool.
- **Use Glob, not `find`** — never use `find` via Bash to locate files. Use Glob.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code. Use Grep.
- These dedicated tools are faster, produce cleaner output, and cost fewer tokens.
- **Long-running builds** — When running build or test commands that may take more than 2 minutes, set `timeout: 600000` (10 minutes) on the Bash tool call.

## Do Not Re-read Files

If you read a file earlier in this session, do not read it again. You already have the contents in context. This applies after edits too — you know what you changed, so you know the current state.

## Read the Architecture Spec

If an `ARCHITECTURE.md` exists in the project root, read it before writing any code. It defines the tech stack, error handling patterns, security requirements, testing standards, and cross-agent conventions. Follow it.

## Check Sibling Work

If your task references work by another agent (e.g., "test the API endpoints", "verify the frontend flows"), read their code in the project before writing tests. Don't assume — verify the actual interfaces, routes, and behavior they implemented.

## Always Run Tests Before Marking Done

Before marking any task as `done`:
1. Run the full test suite for the project
2. Confirm all tests pass (including any new tests you wrote)
3. Include the test count, pass/fail status, and coverage summary in your completion comment

Never claim tests pass without actually running them.

## Git Commits

- Stage specific files, not `git add .`
- End every commit message with: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`

## Use DeerFlow Research to Save Tokens

Your DeerFlow assistant runs pre-flight research before your heartbeat. Use it — the research brief in the comments gives you context so you can skip broad codebase exploration and go straight to implementation. If you need deeper research mid-task, delegate a subtask to your DeerFlow assistant rather than doing extensive web searches or file exploration yourself.
