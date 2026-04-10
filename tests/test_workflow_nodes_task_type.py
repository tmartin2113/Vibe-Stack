"""Test: the specialist call in agents/specialist_nodes.py passes task_type= through to generate.

This is a source-level invariant test: we parse agents/specialist_nodes.py with ast
and assert that a specialist generate() call includes task_type= keyword.
This avoids depending on the exact specialist function signature, which
varies across refactors.
"""

import ast
from pathlib import Path


def _find_generate_calls_with_task_type(filepath: str = "agents/specialist_nodes.py"):
    """Return all ast.Call nodes that match `*.generate(...)` and have
    'task_type' as a keyword argument.
    """
    source = Path(filepath).read_text()
    tree = ast.parse(source)
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "generate":
            continue
        kwarg_names = {kw.arg for kw in node.keywords if kw.arg}
        if "task_type" in kwarg_names:
            matches.append(node)
    return matches


class TestSpecialistPassesTaskType:
    def test_some_generate_call_passes_task_type(self):
        """At least one .generate() call in specialist_nodes.py must pass task_type=."""
        matches = _find_generate_calls_with_task_type()
        assert len(matches) >= 1, (
            "no .generate() call in agents/specialist_nodes.py passes task_type= kwarg. "
            "The specialist invocation must forward state's routed_task_type "
            "so runtime Tier 1b overrides apply."
        )

    def test_task_type_kwarg_sources_from_routed_task_type(self):
        """The task_type kwarg should come from state's routed_task_type field.

        Checks that at least one generate() call has a keyword
        task_type=... where the value textually references
        'routed_task_type'. This keeps the field name consistent with
        what self_upgrade_trigger.py already reads from state.
        """
        source = Path("agents/specialist_nodes.py").read_text()
        tree = ast.parse(source)
        hit = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "generate":
                continue
            for kw in node.keywords:
                if kw.arg != "task_type":
                    continue
                # Serialize the value subtree and check for the field name
                value_src = ast.unparse(kw.value)
                if "routed_task_type" in value_src:
                    hit = True
                    break
            if hit:
                break
        assert hit, (
            "task_type kwarg on a generate() call must source from "
            "state's 'routed_task_type' field."
        )
