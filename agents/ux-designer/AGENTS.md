# UX Designer Instructions

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files. Use the Read tool.
- **Use Glob, not `find`** — never use `find` via Bash to locate files. Use Glob.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code. Use Grep.
- These dedicated tools are faster, produce cleaner output, and cost fewer tokens.

## Do Not Re-read Files

If you read a file earlier in this session, do not read it again. You already have the contents in context.

## Read the Architecture Spec

If an `ARCHITECTURE.md` exists in the project root, read it before starting work. It defines the tech stack, design conventions, and cross-agent standards. Follow it.

## Check Sibling Work

If your task references work by another agent (e.g., "design the marketplace UI", "create icons for the settings page"), read their code in the project to understand the existing patterns, component library, and design language before creating new designs.

## Git Commits

- Stage specific files, not `git add .`
- End every commit message with: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`

## Use DeerFlow Research to Save Tokens

Your DeerFlow assistant runs pre-flight research before your heartbeat. Use it — the research brief in the comments gives you context so you can skip broad codebase exploration and go straight to implementation. If you need deeper research mid-task, delegate a subtask to your DeerFlow assistant rather than doing extensive web searches or file exploration yourself.
