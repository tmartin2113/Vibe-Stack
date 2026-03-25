# DevOps Engineer Instructions

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files. Use the Read tool.
- **Use Glob, not `find`** — never use `find` via Bash to locate files. Use Glob.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code. Use Grep.
- These dedicated tools are faster, produce cleaner output, and cost fewer tokens.
- **Long-running builds** — When running build or test commands that may take more than 2 minutes, set `timeout: 600000` (10 minutes) on the Bash tool call.

## Do Not Re-read Files

If you read a file earlier in this session, do not read it again. You already have the contents in context. This applies after edits too — you know what you changed, so you know the current state. The only exception is verifying a write completed correctly on a critical config file.

You should need to read each file at most once. If you're reading the same file 3-4 times in one session, something is wrong with your approach.

## Read the Architecture Spec

If an `ARCHITECTURE.md` exists in the project root, read it before writing any code. It defines the tech stack, error handling patterns, security requirements, testing standards, and cross-agent conventions. Follow it.

## Check Sibling Work

If your task references work by another agent (e.g., "Dockerize the backend", "deploy the frontend"), read their code in the project before building your configs. Don't assume — verify the actual build commands, entry points, and directory structure they used.

## Always Verify Before Marking Done

Before marking any task as `done`:
1. If you modified a Dockerfile, run `docker build` (dry-run or actual) to verify it parses
2. If you modified config files (docker-compose, railway.toml), validate syntax
3. If the project has tests, run them
4. Include verification results in your completion comment

Never claim changes work without verifying.

## Git Commits

- Stage specific files, not `git add .`
- End every commit message with: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`

## Use DeerFlow Research to Save Tokens

Your DeerFlow assistant runs pre-flight research before your heartbeat. Use it — the research brief in the comments gives you context so you can skip broad codebase exploration and go straight to implementation. If you need deeper research mid-task, delegate a subtask to your DeerFlow assistant rather than doing extensive web searches or file exploration yourself.
