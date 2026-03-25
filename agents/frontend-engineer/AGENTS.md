# Frontend Engineer Instructions

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files. Use the Read tool.
- **Use Glob, not `find`** — never use `find` via Bash to locate files. Use Glob.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code. Use Grep.
- These dedicated tools are faster, produce cleaner output, and cost fewer tokens.

## Do Not Re-read Files

If you read a file earlier in this session, do not read it again. You already have the contents in context. This applies after edits too — you know what you changed, so you know the current state.

## Always Verify Before Marking Done

Before marking any task as `done`:
1. If the project has tests, run them
2. If the project uses TypeScript, run `npx tsc --noEmit` to verify types compile
3. Include verification results in your completion comment

Never claim code works without verifying.

## Git Commits

- Stage specific files, not `git add .`
- End every commit message with: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`

## Targeted Fixes Don't Need Research

When a task gives you specific file paths and line numbers to fix, go directly to the code. Do not explore the codebase broadly or research the topic — the instructions already tell you exactly what to change.
