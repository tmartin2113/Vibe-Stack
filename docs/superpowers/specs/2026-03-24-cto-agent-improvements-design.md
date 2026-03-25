# CTO Agent Improvements — Design Spec

## Problem

The CTO agent currently acts as a task decomposer and delegator. Review of two agent-built projects (CortexOS, Oh_My_Gauss) revealed gaps:

- **No upfront architecture guidance** — Agents made good but inconsistent choices (different error handling patterns, inconsistent conventions across agents' work).
- **No structured quality review** — Oh_My_Gauss shipped 14 security vulnerabilities caught only by human review. CortexOS accumulated 94 bugs fixed in a single late audit.
- **No security oversight** — Neither project had security review baked into the agent workflow.
- **Unclear git workflow** — Push failures and confusion about when/where to push.

## Design

The CTO agent operates in three sequential phases: **Architect → Delegate → Review**. Each phase maps to a distinct heartbeat (wake, work, exit).

### Phase Detection

The CTO is woken multiple times during a task lifecycle. Each wake must determine which phase to execute. The CTO checks state via the Paperclip API:

| Condition | Phase | Action |
|-----------|-------|--------|
| No child subtasks exist | Phase 1 + 2 | Architect, then delegate, then exit |
| Child subtasks exist, some still in_progress or todo | — | Exit immediately (wait for agents to finish) |
| All child subtasks are done, no prior review comment | Phase 3 | Run review |
| All child subtasks are done, prior review exists, fix subtasks still open | — | Exit immediately (wait for fix agents) |
| All child subtasks are done, fix subtasks done (or no fix subtasks) | Post-fix review | Run review (second pass) |

**Review cycle limit:** The CTO reviews at most **twice**. If issues persist after the first round of fix subtasks, the CTO marks the parent task as `blocked` with a comment listing unresolved issues for human review. This prevents infinite fix-review loops.

**How to detect phase:** Query `GET /api/companies/:companyId/issues/:parentId/children` and check statuses. Check own issue's comments for a prior review comment (search for `## Review` in comments).

### Phase 1: Architect

Before creating any subtasks, the CTO analyzes the parent issue and writes an architecture spec.

**Actions:**
1. Read the parent issue and understand scope
2. Create a feature branch: `feature/<sanitized-parent-issue-title>` (lowercase, hyphens, max 50 chars)
3. Identify which agents are needed (backend, frontend, devops, QA, UX)
4. Write `ARCHITECTURE.md` to the project root covering:
   - Tech stack and key dependency choices
   - Project directory structure and naming conventions
   - Error handling pattern (specific to the stack — e.g., "use `suspendRunCatching`" for Kotlin, "wrap with try/catch, never silent catches" for TypeScript)
   - Security requirements (auth model, input validation, no hardcoded secrets, no logging of sensitive data)
   - Testing requirements (minimum: unit tests for business logic, integration tests for API routes)
   - Cross-agent conventions (shared types, API contracts between frontend and backend, consistent naming)
   - Build command (e.g., `npm run build`, `./gradlew assembleDebug`)
   - Test command (e.g., `npm test`, `./gradlew test`)
5. Commit `ARCHITECTURE.md` to the feature branch
6. Proceed directly to Phase 2 (same heartbeat)

**Why:** Agents working independently made contradictory decisions. A committed spec gives all agents a shared reference point and catches architectural drift before it compounds.

### Phase 2: Delegate

After committing the architecture spec, the CTO decomposes the task and assigns subtasks.

**Actions:**
1. Decompose the parent task into one subtask per agent, scoped to their specialty
2. Write each subtask description with:
   - Specific requirements (what to build)
   - Branch name to commit to: `feature/<branch-name>` (same branch created in Phase 1)
   - Explicit reference: "Read `ARCHITECTURE.md` in the project root for shared standards, including build/test commands"
   - Acceptance criteria (what "done" looks like for this subtask)
   - Dependencies on other subtasks, if any
3. Create all independent subtasks in parallel (single batch of API calls)
4. Exit — do not checkout, poll, or wait for subtask completion

**Existing delegation rules:**
- Use correct `assigneeAdapterOverrides` shape: `{ "adapterConfig": { "cwd": "..." } }`
- Use `/api` prefix on all endpoints
- Do not checkout tasks assigned to other agents
- Do not attempt to "wake" agents manually

**Why:** The CTO's token budget is best spent on architecture and review, not on idle-waiting. Paperclip's wake-on-assignment handles agent scheduling.

### Phase 3: Review

When the CTO wakes and ALL child subtasks are done, it performs a structured audit. If any children are still in progress, exit immediately.

**Review priority order** (highest priority first, in case of token budget constraints):

1. **Security checklist:**
   - No hardcoded secrets, API keys, or credentials in source files
   - No logging or printing of sensitive data
   - Input validation at all system boundaries (API routes, user input, external data)
   - Error handling — no silent catches, no swallowed exceptions, proper error propagation

2. **Build and test verification:**
   - Build succeeds (run the build command from ARCHITECTURE.md)
   - Tests pass (run the test command from ARCHITECTURE.md)

3. **Cross-agent consistency:**
   - Frontend API calls match backend route contracts
   - Shared types and naming conventions are consistent
   - No duplicate or conflicting implementations

4. **Architecture conformance:**
   - Each agent's work conforms to patterns defined in ARCHITECTURE.md
   - Dependencies are pinned or range-locked (no `*` or `latest` versions)
   - Project-specific requirements from the spec are met

**Severity-based fix routing:**
- **Trivial** (missing import, typo, small config change) — CTO fixes inline and commits
- **Substantial** (security vulnerability, missing test coverage, architectural deviation, broken feature) — CTO creates a new subtask assigned to the responsible agent with specific findings and expected fix

**After all issues are resolved (or after second review pass):**
- Push the feature branch to origin
- Mark the parent task as done

**Why:** A structured review with a fixed baseline catches the recurring issues (security, silent errors) while architecture conformance catches project-specific gaps. Priority ordering ensures the most important checks run even if the token budget is tight. Severity routing avoids wasting agent heartbeats on one-line fixes while ensuring real issues get proper attention.

## Git Workflow

1. **Phase 1:** CTO creates `feature/<sanitized-parent-issue-title>` and commits `ARCHITECTURE.md`
2. **Phase 2:** Subtask descriptions include the branch name; agents commit to this branch
3. **Phase 3:** CTO commits trivial fixes to the same branch, pushes to origin
4. **Branch naming:** Sanitize the parent issue title — lowercase, replace spaces/special chars with hyphens, max 50 chars
5. **Commit convention:** End every commit message with `Co-Authored-By: Paperclip <noreply@paperclip.ing>`

## Tool Usage

The CTO AGENTS.md will include the standard tool usage rules shared by all agents:
- Use Read, not `cat`/`head`/`tail`
- Use Glob, not `find`
- Use Grep, not `grep`/`rg`
- Do not re-read files already in context

## DeerFlow Integration

The CTO should use its DeerFlow assistant for research-heavy work:
- Phase 1: DeerFlow can research tech stack options, best practices for the project type
- Phase 3: DeerFlow preflight provides context on what agents built, reducing exploration tokens

Include the standard "Use DeerFlow Research to Save Tokens" section in the AGENTS.md.

## Changes Required

### File: `agents/cto/AGENTS.md`

Replace the current 35-line file with the new phased workflow instructions. Includes:
- Tool usage rules (same as other agents)
- DeerFlow integration
- Phase detection logic
- Phase 1: Architect (create branch, write ARCHITECTURE.md, commit)
- Phase 2: Delegate (subtask creation rules, include branch name)
- Phase 3: Review (prioritized checklist, fix routing, push feature branch)
- Git workflow (branch naming, commit conventions)

### No other files changed

The improvement is entirely in the CTO's instructions. No Paperclip config, adapter code, or other agent changes needed.

## Success Criteria

After this change, the CTO agent should:
1. Detect its current phase on each wake and act accordingly
2. Exit immediately on partial child completion (no wasted heartbeats)
3. Create a feature branch and commit `ARCHITECTURE.md` (with build/test commands) before delegating
4. Reference the spec and branch name in every subtask description
5. Run the prioritized review checklist after all agents complete
6. Catch security issues, test gaps, and cross-agent inconsistencies before marking done
7. Push to a feature branch, not main
8. Fix trivial issues inline, route substantial issues back to agents
9. Review at most twice — escalate to human if issues persist
10. Use DeerFlow for research and context gathering
