# Skill-Only Pipeline Refactor

**Date**: 2026-03-09
**Status**: Approved

## Problem

The Vibe node runs before skills are discovered and rewrites the user request into an "enhanced prompt." By the time the skill is loaded, the specialist receives conflicting instructions — Vibe's rewritten spec AND the skill's directives. The intent classifier adds unnecessary complexity since the router already handles task classification.

## Decision

Surgically remove the Vibe prompt enhancement node and intent classifier. The pipeline becomes skill-driven: every task is executed via a skill (discovered or ephemeral).

## New Flow

```
START → Router → Skill Generator (if no match) → Skill Loader → Memory Injection → Cache Lookup → Specialist → Heuristic Critic → (LLM Critic) → (Refinement Loop | Format → Post → Skill Cleanup → END)
```

Sub-task decomposition path:
```
Router (decompose) → Skill Loader → Sub-task Loop [Specialist → Critic] → Aggregator → Final Critic → Format → END
```

## What's Removed

- `classify_task` — intent classifier node (keyword-based, redundant with router)
- `vibe_build_specification` — prompt enhancement node
- `vibe_build_sub_specification` — sub-task prompt enhancement
- `VIBE_SYSTEM_PROMPT` — the "expert prompt engineer" system prompt
- `should_approve_specification` — spec critic loop edges (no spec to validate)
- `should_generate_code` — legacy gate function (always returned "code")
- `complexity_tier` / intent-based path selection from graph edges
- Spec critic fields from state (`spec_critic_score`, `spec_critic_feedback`, etc.)

## What Stays

- Router (regex/LLM/hybrid classification)
- Skill Generator (ephemeral skill creation when no skill matches)
- Skill Loader (loads SKILL.md, parses frontmatter: adapter_prompt, generation_config, tools_enabled, quality_criteria, task_types)
- Specialist execution (tool calling loop, skill-driven adapter prompts via `get_or_create`)
- Critic (heuristic + LLM, refinement loop)
- Multi-specialist decomposition + aggregation
- Memory injection, cache lookup, skill cleanup
- All security (skill content scanning, tool permissions, SHA-256 integrity)

## Specialist Prompt Change

The specialist currently receives `specification` (Vibe's enhanced output). After refactor:

- **First attempt**: `user_request` + skill content + memory context
- **Refinement**: `user_request` + skill content + previous output + critic feedback
- **Sub-tasks**: router seed spec + skill content + sibling context (no Vibe expansion)

The skill's `adapter_prompt` (from SKILL.md frontmatter) becomes the system prompt. The skill content becomes the primary instruction set. `generation_config` from the skill sets temperature/max_tokens.

## No-Skill Fallback

When no skill matches a task, the skill generator creates an ephemeral skill. Every execution path always has a skill driving it.

## Files Changed

| File | Change |
|------|--------|
| `agents/graph.py` | Remove intent_classifier + vibe nodes/edges. Wire START → Router directly. Remove spec critic loop. Flatten complexity tiering. |
| `agents/nodes.py` | Remove `classify_task`, `vibe_build_specification`, `vibe_build_sub_specification`, `_parse_vibe_output` |
| `agents/specialist_nodes.py` | Update `execute_with_specialist` to use `user_request` instead of `specification` as primary input |
| `agents/adapters.py` | Remove `VIBE_SYSTEM_PROMPT` |
| `agents/state.py` | Remove spec-critic fields, remove `get_context_for_node("vibe")` path |
| `agents/decision_functions.py` | Remove `should_approve_specification`, `should_generate_code` |

## Test Impact

Tests that directly test the Vibe node or spec critic loop will need updating. Tests for router, skill loader, specialist, critic, tool calling, and security should pass unchanged.
