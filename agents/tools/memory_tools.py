"""
Memory tools: MemoryStoreTool, MemoryRecallTool, and shared store singleton.

Provides persistent long-term memory with citation tracking for agents.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import logging
import threading

from .base import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)


class MemoryStoreTool(Tool):
    """Store a fact, decision, or insight in persistent long-term memory.

    Every entry carries a citation tracking where the information came from.
    Memories persist across sessions and can be recalled later via memory_recall.
    """

    def __init__(self):
        super().__init__(
            name="memory_store",
            description=(
                "Store a fact, decision, insight, or learned context in persistent memory. "
                "Include the source of the information for citation tracking."
            ),
            category=ToolCategory.SPECIALIZED,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact, decision, or insight to remember.",
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Where this information came from. "
                        "Conventions: 'user' (user statement), 'url:<url>' (web page), "
                        "'file:<path>' (local file), 'tool:<name>' (tool output), "
                        "'agent' (inferred). Default: 'agent'"
                    ),
                },
                "tags": {
                    "type": "string",
                    "description": (
                        "Space-separated tags for categorization. "
                        "e.g. 'architecture decision python'"
                    ),
                },
            },
            "required": ["content"],
        }

    def execute(self, content: str, source: str = "agent", tags: str = "", **kwargs) -> ToolResult:
        if not content or not content.strip():
            return ToolResult(success=False, output="", error="Content cannot be empty")

        try:
            from ..memory_store import MemoryStore

            import agents.tools.registry as _reg
            store = _reg._get_shared_memory_store()
            memory_id = store.store(content=content, source=source, tags=tags)
            return ToolResult(
                success=True,
                output=f"Stored memory #{memory_id}: {content[:200]}",
                metadata={"memory_id": memory_id, "source": source, "tags": tags},
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to store memory: {e}")


class MemoryRecallTool(Tool):
    """Search persistent memory for relevant facts, decisions, and context.

    Returns results ranked by relevance (BM25) with citation information
    showing where each memory originally came from.
    """

    def __init__(self):
        super().__init__(
            name="memory_recall",
            description=(
                "Search persistent memory for relevant facts, decisions, insights, "
                "and context. Returns results with citations showing the original source."
            ),
            category=ToolCategory.SPECIALIZED,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query -- keywords or natural language.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (1-20, default 5).",
                    "default": 5,
                },
                "tag_filter": {
                    "type": "string",
                    "description": "Only return memories with tags containing this substring.",
                },
                "source_filter": {
                    "type": "string",
                    "description": (
                        "Only return memories whose source starts with this prefix. "
                        "e.g. 'url:' for web sources, 'file:' for file sources."
                    ),
                },
            },
            "required": ["query"],
        }

    def execute(self, query: str, max_results: int = 5, tag_filter: str = "", source_filter: str = "", **kwargs) -> ToolResult:
        if not query or not query.strip():
            return ToolResult(success=False, output="", error="Query cannot be empty")

        max_results = max(1, min(max_results, 20))

        try:
            from ..memory_store import MemoryStore

            import agents.tools.registry as _reg
            store = _reg._get_shared_memory_store()
            results = store.recall(
                query=query,
                max_results=max_results,
                tag_filter=tag_filter,
                source_filter=source_filter,
            )

            if not results:
                return ToolResult(
                    success=True,
                    output="No relevant memories found.",
                    metadata={"query": query, "results": 0},
                )

            # Format results with citations
            sections = []
            for i, entry in enumerate(results, 1):
                section = f"## Memory #{entry.memory_id} (score: {entry.score:.2f})\n"
                section += f"{entry.content}\n"
                section += f"\n**Source:** {entry.citation}\n"
                if entry.tags:
                    section += f"**Tags:** {entry.tags}\n"
                section += f"**Stored:** {entry.created_at}\n"
                sections.append(section)

            combined = "\n---\n".join(sections)
            header = f"Found {len(results)} relevant memories:\n\n"

            return ToolResult(
                success=True,
                output=header + combined,
                metadata={
                    "query": query,
                    "results": len(results),
                    "memory_ids": [e.memory_id for e in results],
                },
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Memory recall failed: {e}")


# Shared MemoryStore singleton (lazy-initialized).
# Tests may inject a store by setting this directly on
# this module OR on agents.tools.registry (backward compat).
_shared_memory_store = None
_memory_store_lock = threading.Lock()


def _get_shared_memory_store():
    """Get or create the shared MemoryStore singleton.

    Tests can inject a store by setting
    ``agents.tools.registry._shared_memory_store = my_store``
    (the original location before the split).  This function checks
    that location first so existing test fixtures continue to work.
    """
    global _shared_memory_store

    # Backward compat: registry module is the authoritative
    # injection point for tests.  Always check it first.
    try:
        import agents.tools.registry as _reg
        injected = vars(_reg).get("_shared_memory_store")
        if injected is not None:
            return injected
    except ImportError:
        pass

    if _shared_memory_store is not None:
        return _shared_memory_store

    with _memory_store_lock:
        if _shared_memory_store is None:
            from ..memory_store import MemoryStore
            _shared_memory_store = MemoryStore()
    return _shared_memory_store


__all__ = [
    "MemoryStoreTool",
    "MemoryRecallTool",
    "_get_shared_memory_store",
]
