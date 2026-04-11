"""Bridge between Vibe Stack's MemoryStore and MemPalace.

All functions are wrapped in try/except — MemPalace is additive and must
never block the existing workflow.  If mempalace is not installed or the
palace volume is unavailable, every function silently returns.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PALACE_PATH = os.environ.get("MEMPALACE_PALACE_PATH", "")
_IDENTITIES_DIR = os.path.join(
    os.path.dirname(_PALACE_PATH), "identities"
) if _PALACE_PATH else ""


def _wing_name(agent_id: str) -> str:
    """Convert an agent_id like 'backend-engineer' to 'wing_backend_engineer'."""
    slug = agent_id.lower().replace("-", "_").replace(" ", "_")
    if not slug.startswith("wing_"):
        slug = f"wing_{slug}"
    return slug


def _drawer_id(wing: str, room: str, content: str) -> str:
    """Deterministic drawer ID for idempotent upserts."""
    h = hashlib.sha256(f"{wing}:{room}:{content}".encode()).hexdigest()[:12]
    return f"vibe_{wing}_{room}_{h}"


# ── Persist ──────────────────────────────────────────────────────────────────

def palace_persist(state: Dict[str, Any]) -> None:
    """File completed-run artifacts into the MemPalace palace.

    Called after persist_memory_node writes to the existing MemoryStore.
    """
    try:
        import chromadb
        from mempalace.knowledge_graph import KnowledgeGraph
    except ImportError:
        return

    if not _PALACE_PATH:
        return

    agent_id = (state.get("agent_id") or "").strip()
    task_id = (state.get("task_id") or "").strip()
    routed = state.get("routed_task_type") or state.get("task_type") or "general"
    output = state.get("final_output") or state.get("specialist_output") or ""
    spec = state.get("specification") or state.get("user_request") or ""
    score = state.get("final_score") or state.get("output_critic_score") or 0

    if not output and not spec:
        return

    wing = _wing_name(agent_id) if agent_id else "wing_vibe"
    room = routed.lower().replace(" ", "_").replace("-", "_")
    now = datetime.now(timezone.utc).isoformat()

    try:
        client = chromadb.PersistentClient(path=_PALACE_PATH)
        col = client.get_or_create_collection("mempalace_drawers")

        if spec:
            col.upsert(
                ids=[_drawer_id(wing, room, spec)],
                documents=[spec[:4000]],
                metadatas=[{
                    "wing": wing,
                    "room": room,
                    "hall": "hall_facts",
                    "source_file": f"paperclip:{task_id}",
                    "chunk_index": 0,
                    "added_by": "vibe_bridge",
                    "filed_at": now,
                }],
            )

        if output:
            col.upsert(
                ids=[_drawer_id(wing, room, output)],
                documents=[output[:4000]],
                metadatas=[{
                    "wing": wing,
                    "room": room,
                    "hall": "hall_discoveries",
                    "source_file": f"paperclip:{task_id}",
                    "chunk_index": 0,
                    "added_by": "vibe_bridge",
                    "filed_at": now,
                }],
            )

        logger.info(
            "palace_persist: filed to %s/%s (task=%s score=%s)",
            wing, room, task_id or "-", score,
        )
    except Exception as e:
        logger.debug("palace_persist: ChromaDB write failed: %s", e)

    # Knowledge graph triple
    try:
        kg_path = os.path.join(os.path.dirname(_PALACE_PATH), "knowledge_graph.sqlite3")
        kg = KnowledgeGraph(db_path=kg_path)
        kg.add_triple(
            subject=agent_id or "vibe",
            predicate="completed_task",
            object=routed,
            valid_from=now[:10],
        )
    except Exception as e:
        logger.debug("palace_persist: KG write failed: %s", e)


# ── Inject ───────────────────────────────────────────────────────────────────

def palace_inject(state: Dict[str, Any]) -> str:
    """Supplement inject_memory with palace search results."""
    try:
        from mempalace.searcher import search_memories
    except ImportError:
        return ""

    if not _PALACE_PATH:
        return ""

    user_request = state.get("user_request", "")
    if not user_request:
        return ""

    agent_id = (state.get("agent_id") or "").strip()
    wing = _wing_name(agent_id) if agent_id else None

    try:
        result = search_memories(
            query=user_request,
            palace_path=_PALACE_PATH,
            wing=wing,
            n_results=3,
        )
        hits = result.get("results", [])
        if not hits:
            return ""

        sections = []
        for hit in hits:
            text = hit.get("text", "")[:200]
            room = hit.get("room", "")
            sections.append(f"- {text} (palace: {room})")

        return (
            "\n\n## Palace Memory (Long-Term)\n\n"
            + "\n".join(sections)
        )
    except Exception as e:
        logger.debug("palace_inject: search failed: %s", e)
        return ""


# ── Identity ─────────────────────────────────────────────────────────────────

_SEED_IDENTITIES: Dict[str, str] = {
    "cto": (
        "I am the CTO agent for Vibe Stack.\n"
        "Traits: strategic, delegating, architecture-focused.\n"
        "Specialities: system design, code review, team coordination.\n"
        "Team: Backend, Frontend, QA, DevOps, Security engineers report to me."
    ),
    "backend_engineer": (
        "I am the Backend Engineer agent for Vibe Stack.\n"
        "Traits: precise, test-driven, security-conscious.\n"
        "Specialities: Python, TypeScript, API design, database schema, Docker.\n"
        "Team: CTO (reviewer), Frontend (UI counterpart), QA (validator)."
    ),
    "frontend_engineer": (
        "I am the Frontend Engineer agent for Vibe Stack.\n"
        "Traits: user-focused, component-driven, accessibility-first.\n"
        "Specialities: React, Next.js, Tailwind, shadcn/ui, Playwright.\n"
        "Team: CTO (reviewer), Backend (API counterpart), UX (design)."
    ),
    "qa_engineer": (
        "I am the QA Engineer agent for Vibe Stack.\n"
        "Traits: thorough, systematic, detail-oriented.\n"
        "Specialities: Vitest, Playwright, regression testing, coverage analysis.\n"
        "Team: CTO (reviewer), Backend + Frontend (code authors)."
    ),
    "devops_engineer": (
        "I am the DevOps Engineer agent for Vibe Stack.\n"
        "Traits: reliable, automation-focused, security-conscious.\n"
        "Specialities: Docker, CI/CD, GitHub Actions, monitoring, infrastructure.\n"
        "Team: CTO (reviewer), all engineers (deploy support)."
    ),
    "security_engineer": (
        "I am the Security Engineer agent for Vibe Stack.\n"
        "Traits: cautious, defense-in-depth, audit-focused.\n"
        "Specialities: OWASP, Zod validation, auth flows, dependency scanning.\n"
        "Team: CTO (reviewer), Backend (primary audit target)."
    ),
}


def ensure_agent_identity(agent_id: str) -> None:
    """Create a palace wing + identity file for an agent on first run."""
    try:
        import chromadb
    except ImportError:
        return

    if not _PALACE_PATH or not _IDENTITIES_DIR:
        return

    wing = _wing_name(agent_id)
    identity_path = os.path.join(_IDENTITIES_DIR, f"{wing}.txt")

    if os.path.exists(identity_path):
        return  # Already initialized

    try:
        os.makedirs(_IDENTITIES_DIR, exist_ok=True)

        # Find a matching seed identity
        slug = agent_id.lower().replace("-", "_").replace(" ", "_")
        identity_text = None
        for key, text in _SEED_IDENTITIES.items():
            if key in slug:
                identity_text = text
                break
        if not identity_text:
            identity_text = f"I am the {agent_id} agent for Vibe Stack."

        with open(identity_path, "w") as f:
            f.write(identity_text)

        # Create wing marker in ChromaDB
        client = chromadb.PersistentClient(path=_PALACE_PATH)
        col = client.get_or_create_collection("mempalace_drawers")
        col.upsert(
            ids=[f"identity_{wing}"],
            documents=[identity_text],
            metadatas=[{
                "wing": wing,
                "room": "identity",
                "hall": "hall_facts",
                "source_file": "vibe_bridge",
                "chunk_index": 0,
                "added_by": "vibe_bridge",
                "filed_at": datetime.now(timezone.utc).isoformat(),
            }],
        )

        logger.info("ensure_agent_identity: created %s", wing)
    except Exception as e:
        logger.debug("ensure_agent_identity: failed: %s", e)


# ── Wake-up ──────────────────────────────────────────────────────────────────

def palace_wakeup(agent_id: str) -> str:
    """Return L0+L1 context for an agent (~600-900 tokens)."""
    try:
        from mempalace.layers import MemoryStack
    except ImportError:
        return ""

    if not _PALACE_PATH:
        return ""

    wing = _wing_name(agent_id) if agent_id else None

    try:
        stack = MemoryStack(palace_path=_PALACE_PATH)
        # Set identity path if it exists
        identity_path = os.path.join(_IDENTITIES_DIR, f"{wing}.txt") if wing else ""
        if identity_path and os.path.exists(identity_path):
            stack.identity_path = identity_path

        context = stack.wake_up(wing=wing)
        if context:
            logger.info(
                "palace_wakeup: loaded %d chars for %s",
                len(context), wing or "all",
            )
        return context or ""
    except Exception as e:
        logger.debug("palace_wakeup: failed: %s", e)
        return ""
