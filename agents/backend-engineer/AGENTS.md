# Backend Engineer Instructions

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files. Use the Read tool.
- **Use Glob, not `find`** — never use `find` via Bash to locate files. Use Glob.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code. Use Grep.
- These dedicated tools are faster, produce cleaner output, and cost fewer tokens.

## Do Not Re-read Files

If you read a file earlier in this session, do not read it again. You already have the contents in context. This applies after edits too — you know what you changed, so you know the current state. The only exception is if the file was modified by an external process (e.g., `npm install` updated `package-lock.json`).

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

## Targeted Fixes Don't Need Research

When a task gives you specific file paths and line numbers to fix, go directly to the code. Do not explore the codebase broadly or research the topic — the instructions already tell you exactly what to change.
