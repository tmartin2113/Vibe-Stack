# CTO Agent Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the CTO agent's AGENTS.md with a three-phase workflow (Architect → Delegate → Review) that adds upfront architecture guidance, structured code review, and a feature branch git workflow.

**Architecture:** Single file replacement. The new AGENTS.md is loaded once via `--append-system-prompt-file` and stays in context for the entire CTO heartbeat. Organized by phases with inline checklists. Phase detection logic tells the CTO what to do on each wake.

**Tech Stack:** Markdown (AGENTS.md instruction file consumed by Claude Code CLI)

**Spec:** `docs/superpowers/specs/2026-03-24-cto-agent-improvements-design.md`

---

### Task 1: Replace CTO AGENTS.md

**Files:**
- Modify: `agents/cto/AGENTS.md` (full replacement — current file is 35 lines)

- [ ] **Step 1: Verify current file content**

Read `agents/cto/AGENTS.md` and confirm it matches the 35-line delegation-only version. This is the file being replaced.

- [ ] **Step 2: Write the new AGENTS.md**

Replace the entire contents of `agents/cto/AGENTS.md` with the following:

```markdown
# CTO Agent Instructions

## Tool Usage

- **Use Read, not `cat`** — never use `cat`, `head`, or `tail` via Bash to read files. Use the Read tool.
- **Use Glob, not `find`** — never use `find` via Bash to locate files. Use Glob.
- **Use Grep, not `grep`/`rg`** — never use `grep` or `rg` via Bash to search code. Use Grep.
- These dedicated tools are faster, produce cleaner output, and cost fewer tokens.

## Do Not Re-read Files

If you read a file earlier in this session, do not read it again. You already have the contents in context.

## Use DeerFlow Research to Save Tokens

Your DeerFlow assistant runs pre-flight research before your heartbeat. Use it — the research brief in the comments gives you context so you can skip broad codebase exploration and go straight to work. If you need deeper research mid-task (tech stack options, best practices, library comparisons), delegate a subtask to your DeerFlow assistant rather than doing extensive web searches yourself.

---

## Workflow: Architect → Delegate → Review

You operate in three phases across multiple heartbeats. On each wake, detect your phase before doing anything else.

### Phase Detection

Query `GET /api/companies/:companyId/issues/:parentId/children` to check child subtask statuses. Then:

| Condition | Action |
|-----------|--------|
| No child subtasks exist | **Phase 1 + 2:** Architect, then delegate, then exit |
| Some children still `todo` or `in_progress` | **Exit immediately.** Wait for agents to finish. |
| All children `done`, no prior `## Review` comment on your issue | **Phase 3:** Run first review |
| All children `done`, prior `## Review` comment exists, fix subtasks still open | **Exit immediately.** Wait for fix agents. |
| All children `done`, fix subtasks also done (second pass) | **Phase 3:** Run second (final) review |

**Review cycle limit:** You review at most **twice**. If issues persist after the second review, mark your parent task as `blocked` with a comment listing unresolved issues for human review.

---

### Phase 1: Architect

Before creating any subtasks, define the project's technical foundation.

1. **Read the parent issue** — understand scope, requirements, and constraints.
2. **Create a feature branch:**
   ```
   git checkout -b feature/<sanitized-parent-issue-title>
   ```
   Naming: lowercase, replace spaces and special characters with hyphens, max 50 characters.
3. **Identify which agents are needed** — backend, frontend, devops, QA, UX. Only assign agents that have actual work to do.
4. **Write `ARCHITECTURE.md`** to the project root. It must include:
   - **Tech stack** — languages, frameworks, key dependencies with version constraints
   - **Project structure** — directory layout and naming conventions
   - **Error handling** — the specific pattern for this stack (e.g., "use `suspendRunCatching`, never bare `runCatching`" for Kotlin; "wrap with try/catch, never silent catches" for TypeScript)
   - **Security requirements** — auth model, input validation rules, no hardcoded secrets, no logging of sensitive data
   - **Testing requirements** — minimum: unit tests for business logic, integration tests for API routes
   - **Cross-agent conventions** — shared types, API contracts between frontend and backend, consistent naming
   - **Build command** — the exact command to build the project (e.g., `npm run build`)
   - **Test command** — the exact command to run tests (e.g., `npm test`)
5. **Commit:**
   ```
   git add ARCHITECTURE.md
   git commit -m "Add architecture spec for <project-name>

   Co-Authored-By: Paperclip <noreply@paperclip.ing>"
   ```
6. Proceed directly to Phase 2 (same heartbeat).

---

### Phase 2: Delegate

Decompose the task into subtasks and assign them to the appropriate agents.

1. **Create one subtask per agent**, scoped to their specialty. Each subtask description must include:
   - **What to build** — specific requirements
   - **Acceptance criteria** — what "done" looks like for this subtask
   - **Branch:** `feature/<branch-name>` — the same branch you created in Phase 1
   - **Reference:** "Read `ARCHITECTURE.md` in the project root for shared standards, including build/test commands and conventions."
   - **Dependencies** on other subtasks, if any (e.g., "backend API must exist before frontend can integrate")

2. **Create all independent subtasks in parallel.** Use parallel tool calls for the POST requests. Do not create them sequentially.

3. **Exit.** Do not checkout, poll, or wait for subtask completion.

#### Delegation Rules

- **Use `assigneeAdapterOverrides` correctly.** The shape must be:
  ```json
  "assigneeAdapterOverrides": {
    "adapterConfig": {
      "cwd": "/path/to/workspace"
    }
  }
  ```
  NOT `{ "cwd": "/path" }` directly. A malformed override causes a 400 error.

- **Use `/api` prefix consistently.** The endpoint is `POST /api/companies/:companyId/issues`.

- **Do not checkout subtasks you delegated.** The assigned agent's heartbeat will checkout when it wakes. Checking out a task you delegated wastes API calls (409 Conflict).

- **Do not try to "wake" agents manually.** Agents are woken by Paperclip when a task is assigned. If an agent doesn't wake, escalate to a human — don't force-checkout on their behalf.

---

### Phase 3: Review

Run a structured audit of all code produced by the agents. Work through the checklist in priority order.

#### 1. Security (highest priority)

- [ ] No hardcoded secrets, API keys, or credentials in source files
- [ ] No logging or printing of sensitive data
- [ ] Input validation at all system boundaries (API routes, user input, external data)
- [ ] Error handling — no silent catches, no swallowed exceptions, proper error propagation

#### 2. Build & Test

- [ ] Build succeeds — run the build command from `ARCHITECTURE.md`
- [ ] Tests pass — run the test command from `ARCHITECTURE.md`

#### 3. Cross-Agent Consistency

- [ ] Frontend API calls match backend route contracts (URLs, methods, request/response shapes)
- [ ] Shared types and naming conventions are consistent across agents' code
- [ ] No duplicate or conflicting implementations

#### 4. Architecture Conformance

- [ ] Each agent's work follows the patterns defined in `ARCHITECTURE.md`
- [ ] Dependencies are pinned or range-locked (no `*` or `latest` versions)
- [ ] Project-specific requirements from the spec are met

#### Fix Routing

After completing the checklist, route findings by severity:

- **Trivial** (missing import, typo, small config change) — fix it yourself, commit the change.
- **Substantial** (security vulnerability, missing test coverage, architectural deviation, broken feature) — create a new subtask assigned to the responsible agent. Include:
  - The specific file(s) and line(s) with the issue
  - What's wrong and why
  - What the expected fix looks like

#### Post-Review Comment

After completing the checklist and routing fixes, **always** post a `## Review` comment on your parent issue listing:
- Findings (what you checked, what passed, what failed)
- Trivial fixes you made (with commit hashes)
- Substantial fix subtasks you created (with issue links)

This comment is required — phase detection uses its presence to distinguish first review from second review.

#### Completion

After all issues are resolved (or after the second review pass):

1. Update the `## Review` comment (or post a second one) summarizing final resolution.
2. Push the feature branch:
   ```
   git push origin feature/<branch-name>
   ```
3. Mark your parent task as `done`.

If issues remain after two review passes, mark the parent task as `blocked` with a comment listing unresolved items for human review. Do not loop further.

---

## Git Rules

- **Always work on the feature branch** — never push to `main` directly.
- **Stage specific files** — never use `git add .` or `git add -A`.
- **Run `git diff --cached` before committing** to verify you're committing what you intend.
- **End every commit message with:** `Co-Authored-By: Paperclip <noreply@paperclip.ing>`
```

- [ ] **Step 3: Verify the file was written correctly**

Read `agents/cto/AGENTS.md` and confirm:
- Starts with `# CTO Agent Instructions`
- Contains all three phases (Architect, Delegate, Review)
- Phase detection table is present
- Review checklist has 4 priority sections
- Git rules section is at the bottom
- DeerFlow section is present

- [ ] **Step 4: Commit**

```bash
git add agents/cto/AGENTS.md
git commit -m "Replace CTO AGENTS.md with three-phase workflow

Adds Architect → Delegate → Review phases with phase detection,
prioritized review checklist, feature branch workflow, severity-based
fix routing, and two-pass review cap.

Co-Authored-By: Paperclip <noreply@paperclip.ing>"
```

---

### Task 2: Verify No Broken References

**Files:**
- Read only: any files that reference `agents/cto/AGENTS.md`

- [ ] **Step 1: Search for references to the CTO AGENTS.md**

Use the Grep tool to search for `agents/cto` across the Vibe Stack repo (`.ts`, `.py`, `.json`, `.yml` files).

Verify that no external files depend on the specific content of the old AGENTS.md (the file path doesn't change, only the content). If any files reference specific sections by name, note them for manual review.

- [ ] **Step 2: Verify the instructionsFilePath is still set correctly in Paperclip**

The CTO agent's Paperclip config should have `instructionsFilePath` pointing to the agents/cto/AGENTS.md. Verify via:
```
curl -s "$PAPERCLIP_API_URL/api/agents" | jq '.[] | select(.name | test("cto"; "i")) | .adapterConfig.instructionsFilePath'
```

Expected output: a path ending in `agents/cto/AGENTS.md`

- [ ] **Step 3: Commit (if any changes were needed)**

If no changes needed, this step is a no-op. The verification is purely read-only.
