---
name: self-upgrade
description: Propose and apply validated improvements to the Vibe agent's own source code
license: Apache-2.0
metadata:
  author: vibe
  version: "1.0"
  generated: false
allowed-tools: Read Grep Glob Write Edit Bash
quality-criteria: Tests pass | Bandit clean | Minimal diff | Backward compatible | Rationale provided
task-types: self_upgrade
adapter-prompt: >-
  You are a senior software engineer performing a controlled self-upgrade on the
  Vibe agent codebase. Identify a specific improvement, implement it, and provide
  a clear rationale. Changes must be minimal, backward-compatible, and pass all
  existing tests and security scans.
generation-config: temperature=0.3 max_tokens=2000
tools-enabled: true
---

# Self-Upgrade Skill

Enables Vibe agents to propose and apply validated improvements to their own source code through a gated safety pipeline.

## When to Use

Use this skill when the agent identifies an opportunity to improve its own functionality:
- Fixing a bug in agent logic
- Optimising a slow code path
- Adding a missing capability
- Improving error handling or resilience

## How It Works

1. **Analyse**: Read the current implementation and identify a specific, concrete improvement
2. **Propose**: Write the modified code as an UpgradeProposal with description, files, and rationale
3. **Validate**: The SelfUpgradePipeline automatically runs:
   - Path validation (only agents/ directory, immutable files blocked)
   - Diff size check (max 500 lines)
   - Full pytest suite on a temporary copy
   - Bandit security scan on changed files
   - Critic scoring (minimum 90/100)
4. **Apply**: If all gates pass, changes are committed on a feature branch
5. **Review**: A human reviews and merges the branch

## Usage

```python
from agents.self_upgrade import SelfUpgradePipeline, UpgradeProposal

pipeline = SelfUpgradePipeline()
proposal = UpgradeProposal(
    description="Improve error message in router fallback",
    files={
        "agents/router.py": new_router_content,
    },
    rationale="The current error message doesn't include the task type, making debugging harder",
)
result = pipeline.execute(proposal)
```

## Safety Constraints

1. **Immutable files**: `self_upgrade.py`, `skill_security.py`, and `config.py` cannot be modified
2. **Directory scope**: Only files under `agents/` can be changed
3. **Diff size limit**: Maximum 500 lines per proposal
4. **Test gate**: Full pytest suite must pass
5. **Security gate**: Bandit scan must find no medium+ issues
6. **Critic gate**: Minimum score of 90/100
7. **Branch isolation**: Changes commit to a feature branch, never main
8. **Human review**: Merging requires human approval

## Best Practices

1. **One improvement per proposal**: Keep changes focused and reviewable
2. **Include tests**: Add test coverage for new functionality
3. **Preserve interfaces**: Don't break existing APIs or change function signatures without necessity
4. **Document rationale**: Explain why the change improves the system
5. **Prefer small diffs**: Smaller changes are easier to validate and review
