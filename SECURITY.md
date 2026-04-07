# Security Policy

Vibe-Stack is a multi-agent code generation system. The agents have access to
sandboxed code execution, network egress through filtered tools, and (when
self-upgrade is enabled) the ability to propose modifications to their own
source. We take security reports seriously.

## Supported versions

Only the `main` branch and the latest tagged release receive security fixes.
There are no LTS branches.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.** Public disclosure before a fix is available
can put deployments at risk.

Instead, please report vulnerabilities through GitHub's private advisory
mechanism:

1. Go to [https://github.com/tmartin2113/Vibe-Stack/security/advisories/new](https://github.com/tmartin2113/Vibe-Stack/security/advisories/new)
2. Fill in the report with as much detail as you can:
   - The component or file affected
   - Steps to reproduce, including the minimum config needed to trigger it
   - The impact you observed (e.g. arbitrary file read, RCE, privilege bypass)
   - Any suggested mitigation

You should receive an acknowledgment within 5 business days. We'll work with
you to confirm the issue, prepare a fix, and coordinate disclosure.

## Scope

In scope:

- The agent runtime in `agents/` and `vibe/`
- Sandbox isolation (`agents/sandbox/`, OpenSandbox integration)
- Skill loading and validation (`agents/skill_*.py`)
- Self-upgrade pipeline (`agents/self_upgrade/`, `agents/self_upgrade_*.py`)
- Tool security (`agents/tools/registry.py`, role-based filtering)
- Storage backends (SQL injection, deserialization, secret leakage)
- Container images published to GHCR from this repo

Out of scope:

- Vulnerabilities in upstream dependencies (file those upstream first; we will
  pick up the fix on the next dependency update)
- Vulnerabilities in Paperclip itself (file those at the Paperclip repo)
- Issues that require physical access to a host already running the agent
- Denial-of-service via excessive resource consumption (this is a config / SRE
  concern, not a security vuln)

## Security model

This is a public repo for a self-hosted system. The threat model assumes:

- The operator running Vibe-Stack controls their own host and network
- Agents run inside Docker containers with explicit tool capability lists
- Agent-generated code executes only inside the OpenSandbox or subprocess
  sandbox, never on the host filesystem directly
- The self-upgrade pipeline can ONLY produce file changes that pass test +
  Bandit + critic gates AND land on a feature branch (never directly to main)

A vulnerability is anything that breaks one of these invariants — for example,
sandbox escape, ability to write outside `agents/` via self-upgrade, or
arbitrary code execution from a malicious skill that bypasses validation.

## Hall of fame

We'll credit reporters here once we have any to credit.
