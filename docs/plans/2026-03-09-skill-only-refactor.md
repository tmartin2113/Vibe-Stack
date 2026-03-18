# Skill-Only Pipeline Refactor — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the Vibe prompt enhancement node and intent classifier so the pipeline is purely skill-driven: Router → Skill Loader → Specialist → Critic.

**Architecture:** Surgical removal of 3 nodes (intent_classifier, vibe, critic_spec) and their graph edges. The specialist receives user_request + skill content directly instead of an "enhanced specification." The skill generator creates ephemeral skills when no match is found, so every execution path always has a skill.

**Tech Stack:** Python, custom workflow state machine (agents/graph.py)

---

### Task 1: Remove Vibe + Intent Classifier from Graph Wiring

**Files:**
- Modify: `agents/graph.py:1-70` (imports)
- Modify: `agents/graph.py:444-465` (node registrations)
- Modify: `agents/graph.py:732-789` (edges)
- Modify: `agents/graph.py:841-855` (sub_vibe edges)

**Step 1: Update imports in graph.py**

Remove `should_approve_specification`, `should_generate_code` from the nodes import, remove `classify_intent` import, remove `classify_complexity` import.

```python
# agents/graph.py lines 52-69
# BEFORE:
from .nodes import (
    AgentNodes,
    should_approve_specification,
    should_approve_output,
    should_approve_sub_specification,
    should_approve_sub_output,
    has_more_subtasks,
    should_decompose,
    should_generate_code,
    should_use_llm_critic,
)
from .adapters import AdapterRegistry
from .router import route_to_specialist
from .aggregator import aggregate_outputs
from .training_collector import TrainingDataCollector
from .tools import ToolRegistry, create_default_tool_registry
from .intent_classifier import classify_intent
from .complexity_triage import classify_complexity
from .heuristic_critic import heuristic_evaluate_output

# AFTER:
from .nodes import (
    AgentNodes,
    should_approve_output,
    should_approve_sub_specification,
    should_approve_sub_output,
    has_more_subtasks,
    should_decompose,
    should_use_llm_critic,
)
from .adapters import AdapterRegistry
from .router import route_to_specialist
from .aggregator import aggregate_outputs
from .training_collector import TrainingDataCollector
from .tools import ToolRegistry, create_default_tool_registry
from .heuristic_critic import heuristic_evaluate_output
```

**Step 2: Remove intent_classifier, vibe, and critic_spec node registrations**

Remove these lines from the node registration section (~lines 446-465):

```python
# DELETE these:
workflow.add_node("intent_classifier", classify_intent)
workflow.add_node("vibe", nodes.vibe_build_specification)
# And the critic_spec_wrapper function + its add_node call
```

**Step 3: Rewire entry point and edges**

Replace the entire entry point + tier routing section (lines 732-789) with:

```python
# ===== SET ENTRY POINT =====
workflow.set_entry_point("router")

# ===== ADD EDGES =====
# Router -> Skill Generator -> Skill Loader -> Memory -> Cache
workflow.add_edge("router", "skill_generator")
workflow.add_edge("skill_generator", "skill_loader")
workflow.add_edge("skill_loader", "inject_memory")
workflow.add_edge("inject_memory", "cache_lookup")
```

This removes:
- `tier_route` conditional function
- `vibe_next` conditional function
- `should_approve_specification` conditional edges
- The entire complexity_tier routing

**Step 4: Replace sub_vibe with sub_specialist in decomposition path**

The sub-task decomposition currently goes: `sub_vibe → sub_critic_spec → sub_specialist`.
Replace with: direct to `sub_specialist` (skip sub-spec building).

```python
# BEFORE (lines 807, 843-855):
# "decompose": "sub_vibe"
# workflow.add_edge("sub_vibe", "sub_critic_spec")
# workflow.add_conditional_edges("sub_critic_spec", should_approve_sub_specification, {...})

# AFTER:
# "decompose": "sub_specialist"
# (delete sub_vibe edge, delete sub_critic_spec conditional edges)
```

In `cache_hit_or_miss`, change `"decompose"` target from `"sub_vibe"` to `"sub_specialist"`.

Update `sub_output_and_more_check` to route `"next_subtask"` to `"sub_specialist"` instead of `"sub_vibe"`.

**Step 5: Remove sub_vibe and sub_critic_spec node registrations**

```python
# DELETE:
workflow.add_node("sub_vibe", nodes.vibe_build_sub_specification)
# DELETE the sub_critic_spec_wrapper function + its add_node call
```

**Step 6: Set specification = user_request in router entry**

Since downstream nodes (cache_lookup, specialist prompts) still reference `state["specification"]`, set it early:

In the `router_wrapper` function, add a line before calling `route_to_specialist`:

```python
def router_wrapper(state: AgentState) -> AgentState:
    """Wrapper to pass shared skill registry and base model to router."""
    # Skill-only pipeline: user_request IS the specification
    state["specification"] = state.get("user_request", "")
    return route_to_specialist(
        state,
        skill_registry=shared_skill_registry,
        base_model=base_model,
    )
```

**Step 7: Update docstring and print_graph_structure**

Update the module docstring (lines 1-42) and `print_graph_structure` function (lines 911-976) to reflect the new flow.

**Step 8: Run tests to see what breaks**

Run: `python -m pytest tests/ -x --tb=short 2>&1 | head -80`
Expected: Some test failures in test_integration.py and test_complexity_triage.py

**Step 9: Commit**

```bash
git add agents/graph.py
git commit -m "refactor: remove vibe + intent classifier from workflow graph

Skill-only pipeline: START → Router → Skill Generator → Skill Loader → Specialist → Critic.
The specialist receives user_request + skill content directly."
```

---

### Task 2: Remove Vibe Methods from AgentNodes

**Files:**
- Modify: `agents/nodes.py:135-287` (classify_task, vibe_build_specification, etc.)

**Step 1: Remove classify_task method**

Delete the `classify_task` method (lines 137-180) from the `AgentNodes` class.

**Step 2: Remove vibe_build_specification method**

Delete the `vibe_build_specification` method (lines 184-256).

**Step 3: Remove _parse_vibe_output method**

Delete the `_parse_vibe_output` method (lines 258-286).

**Step 4: Remove vibe_build_sub_specification method**

Delete the `vibe_build_sub_specification` method (lines 325-410).

**Step 5: Remove execute_task method (legacy)**

Delete the `execute_task` method (lines 290-321) — it was the pre-router executor that used task_type-based adapter routing. The specialist now uses `execute_with_specialist`.

**Step 6: Run tests**

Run: `python -m pytest tests/test_integration.py -x --tb=short 2>&1 | head -40`
Expected: Tests that call removed methods will fail — these are fixed in Task 5.

**Step 7: Commit**

```bash
git add agents/nodes.py
git commit -m "refactor: remove vibe node methods from AgentNodes

Removed: classify_task, vibe_build_specification,
vibe_build_sub_specification, _parse_vibe_output, execute_task"
```

---

### Task 3: Update Specialist Prompts to Use user_request

**Files:**
- Modify: `agents/specialist_nodes.py:107-210` (execute_with_specialist)

**Step 1: Simplify first-attempt prompt in execute_with_specialist**

The specialist currently has two first-attempt paths: `complexity_tier == "fast"` (uses user_request) and normal (uses specification). Merge them into one path that always uses user_request + skill content:

```python
# BEFORE (lines 162-187): Two branches checking complexity_tier
# AFTER: Single branch

        if specialist_iteration == 0:
            base_prompt = f"""Complete the following task.

**Task**: {user_request}

**Task Type**: {routed_task_type} (confidence: {routing_confidence:.0%})
{skill_context}
{memory_context}

Provide a complete, high-quality solution that directly addresses the task."""
```

**Step 2: Update refinement prompt**

In the refinement path (lines 189-209), replace `specification` reference with `user_request`:

```python
        else:
            previous_output = state.get("specialist_output", "")
            feedback = state.get("output_critic_feedback", "")
            score = state.get("output_critic_score", 0)

            base_prompt = f"""Your previous attempt scored {score}/100. Improve it based on the feedback.

**Original Task**: {user_request}
{skill_context}
{memory_context}

**Your Previous Output**:
{previous_output}

**Critic Feedback**:
{feedback}

Focus on the specific issues identified in the feedback. Provide an improved solution."""
```

**Step 3: Remove complexity_tier reference**

Delete the `complexity_tier = state.get("complexity_tier", "")` line and the associated branching.

**Step 4: Run tests**

Run: `python -m pytest tests/test_integration.py -x --tb=short 2>&1 | head -40`

**Step 5: Commit**

```bash
git add agents/specialist_nodes.py
git commit -m "refactor: specialist uses user_request directly instead of specification

Removed complexity_tier branching. Single prompt path for all tasks."
```

---

### Task 4: Clean Up Adapters and State

**Files:**
- Modify: `agents/adapters.py:191-204` (VIBE_SYSTEM_PROMPT)
- Modify: `agents/state.py:39-43, 289` (spec-critic state fields, vibe context)
- Modify: `agents/decision_functions.py:48-102, 359-372` (should_approve_specification, should_generate_code)

**Step 1: Remove VIBE_SYSTEM_PROMPT from adapters.py**

Delete lines 191-204 (the `VIBE_SYSTEM_PROMPT` constant).

**Step 2: Remove should_approve_specification and should_generate_code**

In `agents/decision_functions.py`:
- Delete `should_approve_specification` function (lines 48-102)
- Delete `should_generate_code` function (lines 359-372)

**Step 3: Update nodes.py re-exports**

In `agents/nodes.py`, remove the re-exports of the deleted functions:

```python
# BEFORE:
from .decision_functions import (
    should_approve_specification,
    should_approve_output,
    ...
    should_generate_code,
)

# AFTER:
from .decision_functions import (
    should_approve_output,
    should_approve_sub_specification,
    should_approve_sub_output,
    has_more_subtasks,
    should_decompose,
    should_use_llm_critic,
)
```

Update `__all__` to remove the deleted names.

**Step 4: Clean up state.py**

In `agents/state.py`:
- Remove `get_context_for_node("vibe")` branch (lines 289-293)
- The spec-critic fields (`spec_critic_score`, `spec_critic_feedback`, etc.) can stay in the TypedDict for now — they're optional (total=False) and won't cause issues. Removing them risks breaking serialization in heartbeat/session store.

**Step 5: Run tests**

Run: `python -m pytest tests/ -x --tb=short 2>&1 | head -60`

**Step 6: Commit**

```bash
git add agents/adapters.py agents/decision_functions.py agents/nodes.py agents/state.py
git commit -m "refactor: remove VIBE_SYSTEM_PROMPT, spec approval functions, vibe context"
```

---

### Task 5: Fix Broken Tests

**Files:**
- Modify: `tests/test_integration.py`
- Modify: `tests/test_parallel_subtasks.py`
- Modify: `tests/test_complexity_triage.py`
- Modify: `tests/conftest.py` (if it references removed functions)

**Step 1: Identify all failing tests**

Run: `python -m pytest tests/ --tb=line 2>&1 | grep "FAILED\|ERROR" | head -40`

**Step 2: Fix test_integration.py**

Remove tests for deleted functionality:
- `test_should_generate_code_routes_code` (line ~280)
- `test_should_generate_code_always_returns_code` (line ~284)
- `test_should_approve_specification_approved` (line ~435)
- `test_should_approve_specification_refine` (line ~442)
- `test_should_approve_specification_max_iterations_fails` (line ~451)
- `test_classify_task_code` (line ~625)
- `test_classify_task_creative` (line ~632)
- `test_classify_task_research` (line ~639)
- `test_classify_task_general` (line ~646)
- `test vibe_build_specification populates specification` (line ~654)

Remove imports: `should_approve_specification`, `should_generate_code`, `classify_intent`.

**Step 3: Fix test_parallel_subtasks.py**

Line ~114 mocks `nodes.vibe_build_sub_specification`. Update the parallel test to skip sub_vibe (sub-tasks go directly to sub_specialist now).

**Step 4: Handle test_complexity_triage.py**

These tests test the `classify_complexity` function which still exists but is no longer called by the graph. The function itself isn't deleted — it's just not wired. The tests can stay as-is (they test the function in isolation) or be marked as testing a utility that's no longer in the main path. Leave them for now.

**Step 5: Run full test suite**

Run: `python -m pytest tests/ -x --tb=short`
Expected: All tests pass.

**Step 6: Commit**

```bash
git add tests/
git commit -m "test: update tests for skill-only pipeline refactor

Removed tests for deleted vibe/intent-classifier/spec-approval functions.
Updated parallel subtask mocks to skip sub_vibe."
```

---

### Task 6: Update Heartbeat + Workflow Factory References

**Files:**
- Modify: `agents/heartbeat.py` (if it references vibe/spec-building)
- Modify: `agents/workflow_factory.py` (if it registers vibe adapter)
- Modify: `agents/main.py` (if it references vibe)

**Step 1: Check heartbeat.py for vibe references**

Search for `vibe`, `specification`, `complexity_tier`, `intent` in heartbeat.py. Update any references to the removed flow.

**Step 2: Check workflow_factory.py**

The factory creates adapter registrations. If it registers a "vibe" adapter, keep it — the adapter name is still used as a fallback in `AdapterRegistry.get_or_create`. But remove any vibe-specific system prompt usage.

**Step 3: Check main.py**

Line ~258 may reference vibe. Update interactive mode to use the new pipeline.

**Step 4: Run full test suite**

Run: `python -m pytest tests/ -v --tb=short`
Expected: All tests pass.

**Step 5: Commit**

```bash
git add agents/heartbeat.py agents/workflow_factory.py agents/main.py
git commit -m "refactor: update heartbeat + factory for skill-only pipeline"
```

---

### Task 7: Final Verification

**Step 1: Run full test suite**

Run: `python -m pytest tests/ -v 2>&1 | tail -20`
Expected: All tests pass.

**Step 2: Run a quick integration check**

Run: `python -m agents.main --doctor`
Expected: Health checks pass.

**Step 3: Verify graph structure**

Run: `python -c "from agents.graph import print_graph_structure; print_graph_structure()"`
Expected: Shows new flow without vibe/intent_classifier.

**Step 4: Final commit if any fixups needed**

```bash
git add -A
git commit -m "refactor: skill-only pipeline - final cleanup"
```
