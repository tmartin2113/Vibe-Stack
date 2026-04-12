# DeerFlow Memory Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port 6 upstream memory PRs to the Paperclip DeerFlow fork, adding storage abstraction, CRUD/import-export APIs, correction detection, and dedup with reinforcement.

**Architecture:** Extract file I/O into a `MemoryStorage` ABC → build CRUD/import-export endpoints on top → add correction/reinforcement detection in the middleware layer. Each task builds on the previous. All `paperclip_ctx` plumbing preserved.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest, LangChain

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| CREATE | `deerflow/agents/memory/storage.py` | Storage ABC + FileMemoryStorage |
| MODIFY | `deerflow/agents/memory/updater.py` | Delegate I/O to storage, add CRUD/import/export, dedup, correction hint |
| MODIFY | `deerflow/agents/memory/__init__.py` | Export new public symbols |
| MODIFY | `deerflow/config/memory_config.py` | Add `storage_class` field |
| MODIFY | `deerflow/agents/memory/queue.py` | Add correction_detected, reinforcement_detected fields |
| MODIFY | `deerflow/agents/memory/prompt.py` | Add correction category, correction hint text |
| MODIFY | `deerflow/agents/middlewares/memory_middleware.py` | Add correction/reinforcement detection |
| MODIFY | `deerflow/gateway/routers/memory.py` | Add CRUD, import/export endpoints |
| MODIFY | `deerflow/client.py` | Add matching client methods |
| CREATE | `tests/test_memory_storage.py` | Storage layer tests |
| CREATE | `tests/test_memory_crud.py` | CRUD + import/export tests |
| CREATE | `tests/test_memory_correction.py` | Correction detection tests |
| CREATE | `tests/test_memory_dedup.py` | Dedup + reinforcement tests |
| MODIFY | `tests/test_client.py` | Gateway conformance for new methods |

---

### Task 1: Storage Abstraction Layer

**Files:**
- Create: `deerflow/backend/deerflow/agents/memory/storage.py`
- Modify: `deerflow/backend/deerflow/agents/memory/updater.py`
- Modify: `deerflow/backend/deerflow/config/memory_config.py`
- Modify: `deerflow/backend/deerflow/agents/memory/__init__.py`
- Create: `deerflow/backend/tests/test_memory_storage.py`

- [ ] **Step 1: Write failing tests for storage abstraction**

Create `tests/test_memory_storage.py`:

```python
"""Tests for memory storage abstraction layer."""

import json
import tempfile
from pathlib import Path

import pytest

from deerflow.agents.memory.storage import FileMemoryStorage, MemoryStorage, get_memory_storage


class TestMemoryStorageABC:
    """Test that MemoryStorage is a proper abstract class."""

    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            MemoryStorage()


class TestFileMemoryStorage:
    """Tests for FileMemoryStorage implementation."""

    def test_load_nonexistent_returns_empty(self, tmp_path):
        storage = FileMemoryStorage(base_dir=tmp_path)
        data = storage.load()
        assert data["version"] == "1.0"
        assert data["facts"] == []
        assert "user" in data
        assert "history" in data

    def test_save_and_load_roundtrip(self, tmp_path):
        storage = FileMemoryStorage(base_dir=tmp_path)
        data = {"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": [{"id": "f1", "content": "test"}]}
        assert storage.save(data) is True
        loaded = storage.load()
        assert loaded["facts"][0]["content"] == "test"

    def test_save_updates_last_updated(self, tmp_path):
        storage = FileMemoryStorage(base_dir=tmp_path)
        data = {"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": []}
        storage.save(data)
        loaded = storage.load()
        assert loaded["lastUpdated"] != ""

    def test_exists_false_initially(self, tmp_path):
        storage = FileMemoryStorage(base_dir=tmp_path)
        assert storage.exists() is False

    def test_exists_true_after_save(self, tmp_path):
        storage = FileMemoryStorage(base_dir=tmp_path)
        storage.save({"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": []})
        assert storage.exists() is True

    def test_mtime_cache_invalidation(self, tmp_path):
        storage = FileMemoryStorage(base_dir=tmp_path)
        data = {"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": [{"id": "f1", "content": "v1"}]}
        storage.save(data)
        loaded1 = storage.load()

        # Modify file externally
        file_path = tmp_path / "memory.json"
        external_data = json.loads(file_path.read_text())
        external_data["facts"][0]["content"] = "v2"
        file_path.write_text(json.dumps(external_data))

        loaded2 = storage.load()
        assert loaded2["facts"][0]["content"] == "v2"

    def test_per_agent_isolation(self, tmp_path):
        storage = FileMemoryStorage(base_dir=tmp_path)
        global_data = {"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": [{"id": "g1", "content": "global"}]}
        agent_data = {"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": [{"id": "a1", "content": "agent"}]}
        storage.save(global_data)
        storage.save(agent_data, agent_name="coder")
        assert storage.load()["facts"][0]["content"] == "global"
        assert storage.load(agent_name="coder")["facts"][0]["content"] == "agent"

    def test_atomic_write_survives_crash(self, tmp_path):
        """Ensure no .tmp files left after successful save."""
        storage = FileMemoryStorage(base_dir=tmp_path)
        storage.save({"version": "1.0", "lastUpdated": "", "user": {}, "history": {}, "facts": []})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []


class TestGetMemoryStorage:
    """Test singleton accessor."""

    def test_returns_file_storage_by_default(self):
        storage = get_memory_storage()
        assert isinstance(storage, FileMemoryStorage)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_storage.py -v`
Expected: ImportError — `storage` module doesn't exist yet

- [ ] **Step 3: Implement `storage.py`**

Create `deerflow/agents/memory/storage.py`:

```python
"""Memory storage abstraction layer.

Provides an ABC for memory persistence and a default file-based implementation.
"""

import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from deerflow.config.memory_config import get_memory_config
from deerflow.config.paths import get_paths


def _create_empty_memory() -> dict[str, Any]:
    """Create an empty memory structure."""
    return {
        "version": "1.0",
        "lastUpdated": datetime.utcnow().isoformat() + "Z",
        "user": {
            "workContext": {"summary": "", "updatedAt": ""},
            "personalContext": {"summary": "", "updatedAt": ""},
            "topOfMind": {"summary": "", "updatedAt": ""},
        },
        "history": {
            "recentMonths": {"summary": "", "updatedAt": ""},
            "earlierContext": {"summary": "", "updatedAt": ""},
            "longTermBackground": {"summary": "", "updatedAt": ""},
        },
        "facts": [],
    }


class MemoryStorage(ABC):
    """Abstract base class for memory persistence."""

    @abstractmethod
    def load(self, agent_name: str | None = None) -> dict[str, Any]:
        """Load memory data.

        Args:
            agent_name: If provided, loads per-agent memory. None = global.

        Returns:
            Memory data dictionary.
        """

    @abstractmethod
    def save(self, data: dict[str, Any], agent_name: str | None = None) -> bool:
        """Save memory data.

        Args:
            data: Memory data to save.
            agent_name: If provided, saves per-agent memory. None = global.

        Returns:
            True if successful.
        """

    @abstractmethod
    def exists(self, agent_name: str | None = None) -> bool:
        """Check if memory data exists.

        Args:
            agent_name: If provided, checks per-agent memory. None = global.

        Returns:
            True if memory data exists.
        """


class FileMemoryStorage(MemoryStorage):
    """File-based memory storage with mtime caching and atomic writes."""

    def __init__(self, base_dir: Path | None = None):
        self._base_dir = base_dir
        # Cache: keyed by agent_name -> (data, file_mtime)
        self._cache: dict[str | None, tuple[dict[str, Any], float | None]] = {}

    def _get_file_path(self, agent_name: str | None = None) -> Path:
        """Get the memory file path for global or per-agent memory."""
        if self._base_dir is not None:
            # Explicit base_dir (used in tests)
            if agent_name is not None:
                return self._base_dir / "agents" / agent_name / "memory.json"
            return self._base_dir / "memory.json"

        if agent_name is not None:
            return get_paths().agent_memory_file(agent_name)

        config = get_memory_config()
        if config.storage_path:
            p = Path(config.storage_path)
            return p if p.is_absolute() else get_paths().base_dir / p
        return get_paths().memory_file

    def load(self, agent_name: str | None = None) -> dict[str, Any]:
        file_path = self._get_file_path(agent_name)

        try:
            current_mtime = file_path.stat().st_mtime if file_path.exists() else None
        except OSError:
            current_mtime = None

        cached = self._cache.get(agent_name)
        if cached is not None and cached[1] == current_mtime and current_mtime is not None:
            return cached[0]

        data = self._load_from_file(file_path)
        self._cache[agent_name] = (data, current_mtime)
        return data

    def save(self, data: dict[str, Any], agent_name: str | None = None) -> bool:
        file_path = self._get_file_path(agent_name)

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            data["lastUpdated"] = datetime.utcnow().isoformat() + "Z"

            temp_path = file_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            temp_path.replace(file_path)

            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                mtime = None

            self._cache[agent_name] = (data, mtime)
            print(f"Memory saved to {file_path}")
            return True
        except OSError as e:
            print(f"Failed to save memory file: {e}")
            return False

    def exists(self, agent_name: str | None = None) -> bool:
        return self._get_file_path(agent_name).exists()

    def invalidate_cache(self, agent_name: str | None = None) -> None:
        """Force cache invalidation for next load."""
        self._cache.pop(agent_name, None)

    def _load_from_file(self, file_path: Path) -> dict[str, Any]:
        if not file_path.exists():
            return _create_empty_memory()
        try:
            with open(file_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Failed to load memory file: {e}")
            return _create_empty_memory()


# Singleton
_storage: MemoryStorage | None = None


def get_memory_storage() -> MemoryStorage:
    """Get the global memory storage singleton."""
    global _storage
    if _storage is None:
        _storage = FileMemoryStorage()
    return _storage


def set_memory_storage(storage: MemoryStorage) -> None:
    """Set the global memory storage (for testing or custom backends)."""
    global _storage
    _storage = storage
```

- [ ] **Step 4: Refactor `updater.py` to use storage**

In `updater.py`:
- Remove `_get_memory_file_path()`, `_load_memory_from_file()`, `_save_memory_to_file()`, `_memory_cache`, and `_create_empty_memory()`
- Import `get_memory_storage` and `_create_empty_memory` from `storage`
- Rewrite `get_memory_data()` to delegate to `get_memory_storage().load(agent_name)`
- Rewrite `reload_memory_data()` to invalidate cache and reload via storage
- Replace `_save_memory_to_file()` calls with `get_memory_storage().save()`

The key changes in `updater.py`:

```python
# At top of file, replace file I/O imports with:
from deerflow.agents.memory.storage import _create_empty_memory, get_memory_storage

# Remove: _get_memory_file_path, _load_memory_from_file, _save_memory_to_file, _memory_cache, _create_empty_memory

# Replace get_memory_data:
def get_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    return get_memory_storage().load(agent_name)

# Replace reload_memory_data:
def reload_memory_data(agent_name: str | None = None) -> dict[str, Any]:
    storage = get_memory_storage()
    if hasattr(storage, 'invalidate_cache'):
        storage.invalidate_cache(agent_name)
    return storage.load(agent_name)

# In update_memory(), replace _save_memory_to_file(updated_memory, agent_name) with:
saved = get_memory_storage().save(updated_memory, agent_name)
```

- [ ] **Step 5: Update `__init__.py` exports**

Add `storage` exports to `deerflow/agents/memory/__init__.py`:

```python
from deerflow.agents.memory.storage import (
    FileMemoryStorage,
    MemoryStorage,
    get_memory_storage,
    set_memory_storage,
)
```

And add to `__all__`:
```python
"MemoryStorage",
"FileMemoryStorage",
"get_memory_storage",
"set_memory_storage",
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_storage.py tests/test_memory_upload_filtering.py -v`
Expected: All pass. Upload filtering tests confirm no regression.

- [ ] **Step 7: Commit**

```bash
git add deerflow/agents/memory/storage.py deerflow/agents/memory/updater.py deerflow/agents/memory/__init__.py deerflow/config/memory_config.py tests/test_memory_storage.py
git commit -m "feat(memory): add storage abstraction layer

Extract file I/O from updater.py into MemoryStorage ABC with
FileMemoryStorage implementation. Mtime caching and atomic writes
preserved. Upstream PR #1353."
```

---

### Task 2: Clear and Delete APIs

**Files:**
- Modify: `deerflow/backend/deerflow/agents/memory/updater.py`
- Modify: `deerflow/backend/deerflow/gateway/routers/memory.py`
- Modify: `deerflow/backend/deerflow/client.py`
- Create: `deerflow/backend/tests/test_memory_crud.py`

- [ ] **Step 1: Write failing tests for clear/delete**

Create `tests/test_memory_crud.py`:

```python
"""Tests for memory CRUD operations (clear, delete, create, update, import, export)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from deerflow.agents.memory.storage import FileMemoryStorage, set_memory_storage
from deerflow.agents.memory.updater import (
    clear_memory_data,
    delete_memory_fact,
    get_memory_data,
)


@pytest.fixture(autouse=True)
def _use_tmp_storage(tmp_path):
    """Use a temp-dir storage for every test."""
    storage = FileMemoryStorage(base_dir=tmp_path)
    set_memory_storage(storage)
    yield
    set_memory_storage(None)


def _seed_memory(facts=None):
    """Save memory with given facts."""
    from deerflow.agents.memory.storage import get_memory_storage, _create_empty_memory

    data = _create_empty_memory()
    if facts:
        data["facts"] = facts
    get_memory_storage().save(data)
    return data


class TestClearMemory:
    def test_clear_resets_to_empty(self):
        _seed_memory([{"id": "f1", "content": "test", "category": "knowledge", "confidence": 0.9, "createdAt": "", "source": "t1"}])
        result = clear_memory_data()
        assert result is True
        data = get_memory_data()
        assert data["facts"] == []
        assert data["user"]["workContext"]["summary"] == ""

    def test_clear_when_already_empty(self):
        result = clear_memory_data()
        assert result is True


class TestDeleteFact:
    def test_delete_existing_fact(self):
        _seed_memory([
            {"id": "f1", "content": "keep", "category": "knowledge", "confidence": 0.9, "createdAt": "", "source": "t1"},
            {"id": "f2", "content": "delete me", "category": "knowledge", "confidence": 0.9, "createdAt": "", "source": "t1"},
        ])
        result = delete_memory_fact("f2")
        assert result is True
        data = get_memory_data()
        assert len(data["facts"]) == 1
        assert data["facts"][0]["id"] == "f1"

    def test_delete_nonexistent_fact_returns_false(self):
        _seed_memory([{"id": "f1", "content": "test", "category": "knowledge", "confidence": 0.9, "createdAt": "", "source": "t1"}])
        result = delete_memory_fact("nonexistent")
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_crud.py::TestClearMemory tests/test_memory_crud.py::TestDeleteFact -v`
Expected: ImportError — `clear_memory_data` and `delete_memory_fact` don't exist

- [ ] **Step 3: Implement clear/delete in `updater.py`**

Add to `updater.py`:

```python
def clear_memory_data(agent_name: str | None = None) -> bool:
    """Clear all memory data, resetting to empty structure.

    Args:
        agent_name: If provided, clears per-agent memory. None = global.

    Returns:
        True if successful.
    """
    empty = _create_empty_memory()
    return get_memory_storage().save(empty, agent_name)


def delete_memory_fact(fact_id: str, agent_name: str | None = None) -> bool:
    """Delete a single fact by ID.

    Args:
        fact_id: The fact ID to delete.
        agent_name: If provided, operates on per-agent memory. None = global.

    Returns:
        True if fact was found and deleted, False if not found.
    """
    storage = get_memory_storage()
    data = storage.load(agent_name)
    original_count = len(data.get("facts", []))
    data["facts"] = [f for f in data.get("facts", []) if f.get("id") != fact_id]
    if len(data["facts"]) == original_count:
        return False
    storage.save(data, agent_name)
    return True
```

- [ ] **Step 4: Add gateway endpoints**

In `gateway/routers/memory.py`, add:

```python
from deerflow.agents.memory.updater import clear_memory_data, delete_memory_fact

@router.delete(
    "/memory",
    response_model=MemoryResponse,
    summary="Clear Memory",
    description="Clear all memory data, resetting to empty structure.",
)
async def clear_memory() -> MemoryResponse:
    clear_memory_data()
    return MemoryResponse(**get_memory_data())


@router.delete(
    "/memory/facts/{fact_id}",
    response_model=MemoryResponse,
    summary="Delete Memory Fact",
    description="Delete a specific fact by ID.",
)
async def delete_fact(fact_id: str) -> MemoryResponse:
    deleted = delete_memory_fact(fact_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Fact '{fact_id}' not found")
    return MemoryResponse(**get_memory_data())
```

- [ ] **Step 5: Add client methods**

In `client.py`, in the memory section, add:

```python
def clear_memory(self) -> dict:
    """Clear all memory data."""
    from deerflow.agents.memory.updater import clear_memory_data
    clear_memory_data()
    return self.get_memory()

def delete_memory_fact(self, fact_id: str) -> dict:
    """Delete a specific fact by ID.

    Args:
        fact_id: The fact ID to delete.

    Returns:
        Updated memory data dict.

    Raises:
        ValueError: If fact not found.
    """
    from deerflow.agents.memory.updater import delete_memory_fact
    if not delete_memory_fact(fact_id):
        raise ValueError(f"Fact '{fact_id}' not found")
    return self.get_memory()
```

- [ ] **Step 6: Update `__init__.py` exports**

Add `clear_memory_data` and `delete_memory_fact` to `__init__.py` imports and `__all__`.

- [ ] **Step 7: Run tests**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_crud.py tests/test_memory_storage.py tests/test_memory_upload_filtering.py -v`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add deerflow/agents/memory/updater.py deerflow/agents/memory/__init__.py deerflow/gateway/routers/memory.py deerflow/client.py tests/test_memory_crud.py
git commit -m "feat(memory): add clear and delete memory APIs

DELETE /api/memory clears all data. DELETE /api/memory/facts/{id}
removes a single fact. Matching client methods added.
Upstream PR #1467."
```

---

### Task 3: Manual Fact Create/Update

**Files:**
- Modify: `deerflow/backend/deerflow/agents/memory/updater.py`
- Modify: `deerflow/backend/deerflow/gateway/routers/memory.py`
- Modify: `deerflow/backend/deerflow/client.py`
- Modify: `deerflow/backend/tests/test_memory_crud.py`

- [ ] **Step 1: Add failing tests to `test_memory_crud.py`**

Append to `tests/test_memory_crud.py`:

```python
from deerflow.agents.memory.updater import create_memory_fact, update_memory_fact

VALID_CATEGORIES = {"preference", "knowledge", "context", "behavior", "goal"}


class TestCreateFact:
    def test_create_fact_appends(self):
        _seed_memory()
        fact = create_memory_fact("User likes Python", "preference", 0.9)
        assert fact["content"] == "User likes Python"
        assert fact["category"] == "preference"
        assert fact["confidence"] == 0.9
        assert fact["id"].startswith("fact_")
        data = get_memory_data()
        assert len(data["facts"]) == 1

    def test_create_fact_invalid_category_raises(self):
        _seed_memory()
        with pytest.raises(ValueError, match="category"):
            create_memory_fact("test", "invalid_cat", 0.9)

    def test_create_fact_empty_content_raises(self):
        _seed_memory()
        with pytest.raises(ValueError, match="content"):
            create_memory_fact("", "knowledge", 0.9)

    def test_create_fact_confidence_out_of_range_raises(self):
        _seed_memory()
        with pytest.raises(ValueError, match="confidence"):
            create_memory_fact("test", "knowledge", 1.5)


class TestUpdateFact:
    def test_update_content(self):
        _seed_memory([{"id": "f1", "content": "old", "category": "knowledge", "confidence": 0.9, "createdAt": "", "source": "t1"}])
        result = update_memory_fact("f1", content="new")
        assert result is not None
        assert result["content"] == "new"
        assert result["category"] == "knowledge"  # unchanged

    def test_update_category(self):
        _seed_memory([{"id": "f1", "content": "test", "category": "knowledge", "confidence": 0.9, "createdAt": "", "source": "t1"}])
        result = update_memory_fact("f1", category="preference")
        assert result["category"] == "preference"

    def test_update_nonexistent_returns_none(self):
        _seed_memory()
        result = update_memory_fact("nonexistent", content="x")
        assert result is None

    def test_update_invalid_category_raises(self):
        _seed_memory([{"id": "f1", "content": "test", "category": "knowledge", "confidence": 0.9, "createdAt": "", "source": "t1"}])
        with pytest.raises(ValueError, match="category"):
            update_memory_fact("f1", category="bad")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_crud.py::TestCreateFact tests/test_memory_crud.py::TestUpdateFact -v`
Expected: ImportError

- [ ] **Step 3: Implement create/update in `updater.py`**

Add to `updater.py`:

```python
VALID_CATEGORIES = {"preference", "knowledge", "context", "behavior", "goal", "correction"}


def create_memory_fact(
    content: str,
    category: str = "knowledge",
    confidence: float = 0.9,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Create a new memory fact manually.

    Args:
        content: Fact content (non-empty, max 500 chars).
        category: One of preference, knowledge, context, behavior, goal, correction.
        confidence: 0.0-1.0.
        agent_name: Per-agent or global memory.

    Returns:
        The created fact dict.

    Raises:
        ValueError: If validation fails.
    """
    if not content or not content.strip():
        raise ValueError("Fact content must be non-empty")
    if len(content) > 500:
        raise ValueError("Fact content must be 500 characters or fewer")
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"Confidence must be between 0.0 and 1.0, got {confidence}")

    storage = get_memory_storage()
    data = storage.load(agent_name)
    now = datetime.utcnow().isoformat() + "Z"
    fact = {
        "id": f"fact_{uuid.uuid4().hex[:8]}",
        "content": content.strip(),
        "category": category,
        "confidence": confidence,
        "createdAt": now,
        "source": "manual",
    }
    data.setdefault("facts", []).append(fact)
    storage.save(data, agent_name)
    return fact


def update_memory_fact(
    fact_id: str,
    content: str | None = None,
    category: str | None = None,
    confidence: float | None = None,
    agent_name: str | None = None,
) -> dict[str, Any] | None:
    """Update an existing fact.

    Args:
        fact_id: ID of the fact to update.
        content: New content (if provided).
        category: New category (if provided).
        confidence: New confidence (if provided).
        agent_name: Per-agent or global memory.

    Returns:
        Updated fact dict, or None if not found.

    Raises:
        ValueError: If provided values fail validation.
    """
    if content is not None and not content.strip():
        raise ValueError("Fact content must be non-empty")
    if content is not None and len(content) > 500:
        raise ValueError("Fact content must be 500 characters or fewer")
    if category is not None and category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")
    if confidence is not None and not (0.0 <= confidence <= 1.0):
        raise ValueError(f"Confidence must be between 0.0 and 1.0, got {confidence}")

    storage = get_memory_storage()
    data = storage.load(agent_name)
    for fact in data.get("facts", []):
        if fact.get("id") == fact_id:
            if content is not None:
                fact["content"] = content.strip()
            if category is not None:
                fact["category"] = category
            if confidence is not None:
                fact["confidence"] = confidence
            storage.save(data, agent_name)
            return fact
    return None
```

- [ ] **Step 4: Add gateway endpoints for create/update**

In `gateway/routers/memory.py`, add request models and endpoints:

```python
from fastapi import HTTPException

class CreateFactRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=500)
    category: str = Field(default="knowledge")
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)

class UpdateFactRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=500)
    category: str | None = Field(default=None)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

@router.post("/memory/facts", response_model=Fact, summary="Create Memory Fact")
async def create_fact(request: CreateFactRequest) -> Fact:
    from deerflow.agents.memory.updater import create_memory_fact
    try:
        fact = create_memory_fact(request.content, request.category, request.confidence)
        return Fact(**fact)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

@router.patch("/memory/facts/{fact_id}", response_model=Fact, summary="Update Memory Fact")
async def update_fact(fact_id: str, request: UpdateFactRequest) -> Fact:
    from deerflow.agents.memory.updater import update_memory_fact
    try:
        result = update_memory_fact(fact_id, request.content, request.category, request.confidence)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail=f"Fact '{fact_id}' not found")
    return Fact(**result)
```

- [ ] **Step 5: Add client methods**

In `client.py`, add:

```python
def create_memory_fact(self, content: str, category: str = "knowledge", confidence: float = 0.9) -> dict:
    """Create a new memory fact."""
    from deerflow.agents.memory.updater import create_memory_fact
    return create_memory_fact(content, category, confidence)

def update_memory_fact(self, fact_id: str, content: str | None = None, category: str | None = None, confidence: float | None = None) -> dict:
    """Update an existing memory fact.

    Raises:
        ValueError: If fact not found.
    """
    from deerflow.agents.memory.updater import update_memory_fact
    result = update_memory_fact(fact_id, content, category, confidence)
    if result is None:
        raise ValueError(f"Fact '{fact_id}' not found")
    return result
```

- [ ] **Step 6: Run all memory tests**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_crud.py tests/test_memory_storage.py tests/test_memory_upload_filtering.py -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add deerflow/agents/memory/updater.py deerflow/gateway/routers/memory.py deerflow/client.py tests/test_memory_crud.py
git commit -m "feat(memory): add manual fact create and update APIs

POST /api/memory/facts creates a fact with validation.
PATCH /api/memory/facts/{id} updates specific fields.
Upstream PR #1538."
```

---

### Task 4: Import/Export

**Files:**
- Modify: `deerflow/backend/deerflow/agents/memory/updater.py`
- Modify: `deerflow/backend/deerflow/gateway/routers/memory.py`
- Modify: `deerflow/backend/deerflow/client.py`
- Create: `deerflow/backend/tests/test_memory_import_export.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_memory_import_export.py`:

```python
"""Tests for memory import/export."""

import pytest

from deerflow.agents.memory.storage import FileMemoryStorage, _create_empty_memory, set_memory_storage
from deerflow.agents.memory.updater import get_memory_data, import_memory_data


@pytest.fixture(autouse=True)
def _use_tmp_storage(tmp_path):
    storage = FileMemoryStorage(base_dir=tmp_path)
    set_memory_storage(storage)
    yield
    set_memory_storage(None)


def _make_memory(facts=None, work_summary=""):
    data = _create_empty_memory()
    if facts:
        data["facts"] = facts
    if work_summary:
        data["user"]["workContext"]["summary"] = work_summary
        data["user"]["workContext"]["updatedAt"] = "2026-01-01T00:00:00Z"
    return data


class TestImportReplace:
    def test_import_replaces_entirely(self):
        from deerflow.agents.memory.storage import get_memory_storage
        get_memory_storage().save(_make_memory([{"id": "f1", "content": "old"}]))
        new_data = _make_memory([{"id": "f2", "content": "new"}])
        result = import_memory_data(new_data, merge=False)
        assert result is True
        loaded = get_memory_data()
        assert len(loaded["facts"]) == 1
        assert loaded["facts"][0]["id"] == "f2"

    def test_import_empty_clears(self):
        from deerflow.agents.memory.storage import get_memory_storage
        get_memory_storage().save(_make_memory([{"id": "f1", "content": "test"}]))
        result = import_memory_data(_make_memory(), merge=False)
        assert result is True
        assert get_memory_data()["facts"] == []


class TestImportMerge:
    def test_merge_adds_new_facts(self):
        from deerflow.agents.memory.storage import get_memory_storage
        get_memory_storage().save(_make_memory([{"id": "f1", "content": "existing"}]))
        new_data = _make_memory([{"id": "f2", "content": "new"}])
        result = import_memory_data(new_data, merge=True)
        assert result is True
        loaded = get_memory_data()
        ids = [f["id"] for f in loaded["facts"]]
        assert "f1" in ids
        assert "f2" in ids

    def test_merge_deduplicates_by_id(self):
        from deerflow.agents.memory.storage import get_memory_storage
        get_memory_storage().save(_make_memory([{"id": "f1", "content": "v1"}]))
        new_data = _make_memory([{"id": "f1", "content": "v2"}])
        result = import_memory_data(new_data, merge=True)
        assert result is True
        loaded = get_memory_data()
        assert len(loaded["facts"]) == 1
        assert loaded["facts"][0]["content"] == "v2"  # newer wins

    def test_merge_keeps_newer_summaries(self):
        from deerflow.agents.memory.storage import get_memory_storage
        old = _make_memory(work_summary="old work")
        old["user"]["workContext"]["updatedAt"] = "2025-01-01T00:00:00Z"
        get_memory_storage().save(old)

        new = _make_memory(work_summary="new work")
        new["user"]["workContext"]["updatedAt"] = "2026-06-01T00:00:00Z"
        import_memory_data(new, merge=True)

        loaded = get_memory_data()
        assert loaded["user"]["workContext"]["summary"] == "new work"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_import_export.py -v`
Expected: ImportError — `import_memory_data` doesn't exist

- [ ] **Step 3: Implement import/export in `updater.py`**

Add to `updater.py`:

```python
def import_memory_data(
    data: dict[str, Any],
    merge: bool = False,
    agent_name: str | None = None,
) -> bool:
    """Import memory data, either replacing or merging.

    Args:
        data: Memory data to import.
        merge: If True, merge with existing data. If False, replace entirely.
        agent_name: Per-agent or global memory.

    Returns:
        True if successful.
    """
    storage = get_memory_storage()

    if not merge:
        return storage.save(data, agent_name)

    # Merge mode
    existing = storage.load(agent_name)

    # Merge user/history sections: keep whichever has a newer updatedAt
    for section_key in ("user", "history"):
        existing_section = existing.get(section_key, {})
        import_section = data.get(section_key, {})
        for sub_key in existing_section:
            if sub_key not in import_section:
                continue
            existing_updated = existing_section.get(sub_key, {}).get("updatedAt", "")
            import_updated = import_section.get(sub_key, {}).get("updatedAt", "")
            if import_updated > existing_updated:
                existing_section[sub_key] = import_section[sub_key]

    # Merge facts: import wins on ID conflict
    existing_facts = {f["id"]: f for f in existing.get("facts", []) if "id" in f}
    for fact in data.get("facts", []):
        if "id" in fact:
            existing_facts[fact["id"]] = fact
    existing["facts"] = list(existing_facts.values())

    return storage.save(existing, agent_name)
```

- [ ] **Step 4: Add gateway endpoints**

In `gateway/routers/memory.py`:

```python
from deerflow.agents.memory.updater import import_memory_data

class ImportMemoryRequest(BaseModel):
    data: dict = Field(..., description="Memory data to import")
    merge: bool = Field(default=False, description="If true, merge with existing; if false, replace")

@router.get("/memory/export", response_model=MemoryResponse, summary="Export Memory")
async def export_memory() -> MemoryResponse:
    return MemoryResponse(**get_memory_data())

@router.post("/memory/import", response_model=MemoryResponse, summary="Import Memory")
async def import_memory(request: ImportMemoryRequest) -> MemoryResponse:
    import_memory_data(request.data, merge=request.merge)
    return MemoryResponse(**get_memory_data())
```

- [ ] **Step 5: Add client methods**

```python
def export_memory(self) -> dict:
    """Export memory data."""
    return self.get_memory()

def import_memory(self, data: dict, merge: bool = False) -> dict:
    """Import memory data."""
    from deerflow.agents.memory.updater import import_memory_data
    import_memory_data(data, merge=merge)
    return self.get_memory()
```

- [ ] **Step 6: Run tests**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_import_export.py tests/test_memory_crud.py tests/test_memory_storage.py -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add deerflow/agents/memory/updater.py deerflow/agents/memory/__init__.py deerflow/gateway/routers/memory.py deerflow/client.py tests/test_memory_import_export.py
git commit -m "feat(memory): add import/export APIs

GET /api/memory/export and POST /api/memory/import with merge mode.
Upstream PR #1521."
```

---

### Task 5: Correction Detection

**Files:**
- Modify: `deerflow/backend/deerflow/agents/middlewares/memory_middleware.py`
- Modify: `deerflow/backend/deerflow/agents/memory/queue.py`
- Modify: `deerflow/backend/deerflow/agents/memory/updater.py`
- Modify: `deerflow/backend/deerflow/agents/memory/prompt.py`
- Create: `deerflow/backend/tests/test_memory_correction.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_memory_correction.py`:

```python
"""Tests for correction detection in memory pipeline."""

from langchain_core.messages import AIMessage, HumanMessage

from deerflow.agents.middlewares.memory_middleware import (
    _extract_message_text,
    detect_correction,
)


def _human(text: str) -> HumanMessage:
    return HumanMessage(content=text)


def _ai(text: str) -> AIMessage:
    return AIMessage(content=text)


class TestExtractMessageText:
    def test_string_content(self):
        assert _extract_message_text(_human("hello")) == "hello"

    def test_list_content(self):
        msg = _human([{"type": "text", "text": "hello"}, {"type": "image_url", "image_url": "..."}])
        # Should handle gracefully — list content extraction
        text = _extract_message_text(msg)
        assert "hello" in text


class TestDetectCorrection:
    def test_no_correction(self):
        msgs = [_human("Tell me about Python"), _ai("Python is a language")]
        assert detect_correction(msgs) is False

    def test_actually_correction(self):
        msgs = [_human("Actually, I meant JavaScript not Python")]
        assert detect_correction(msgs) is True

    def test_no_i_meant(self):
        msgs = [_human("No, I meant the other one")]
        assert detect_correction(msgs) is True

    def test_thats_wrong(self):
        msgs = [_human("That's wrong, it should be 42")]
        assert detect_correction(msgs) is True

    def test_not_x_but_y(self):
        msgs = [_human("Not Python, but Ruby")]
        assert detect_correction(msgs) is True

    def test_correction_only_checks_recent(self):
        """Only last 3 human messages are checked."""
        old_msgs = [_human(f"Message {i}") for i in range(10)]
        old_msgs.append(_human("Actually, correction here"))
        # The correction is in the last message, so it should be detected
        assert detect_correction(old_msgs) is True

    def test_correction_ignores_old_messages(self):
        """Correction in old messages (beyond last 3 human) should be ignored."""
        msgs = [
            _human("Actually, I meant something else"),  # old — beyond window
            _ai("OK"),
            _human("What is 1+1"),
            _ai("2"),
            _human("And 2+2"),
            _ai("4"),
            _human("Thanks"),
        ]
        # Only last 3 human messages: "What is 1+1", "And 2+2", "Thanks" — no correction
        assert detect_correction(msgs) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_correction.py -v`
Expected: ImportError

- [ ] **Step 3: Implement correction detection in `memory_middleware.py`**

Add to `memory_middleware.py` before the `MemoryMiddleware` class:

```python
import re

# Correction patterns — user is correcting or contradicting prior information
_CORRECTION_PATTERNS = [
    re.compile(r"\bactually\b[,.]?\s", re.IGNORECASE),
    re.compile(r"\bno[,.]?\s+I\s+meant\b", re.IGNORECASE),
    re.compile(r"\bthat'?s\s+(wrong|incorrect|not\s+right)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+\w+[,.]?\s+(but|rather)\b", re.IGNORECASE),
    re.compile(r"\bI\s+(was\s+wrong|made\s+a\s+mistake)\b", re.IGNORECASE),
    re.compile(r"\bcorrection\b", re.IGNORECASE),
    re.compile(r"\blet\s+me\s+correct\b", re.IGNORECASE),
    re.compile(r"\bI\s+should\s+have\s+said\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+I\s+meant\s+(was|is)\b", re.IGNORECASE),
    re.compile(r"\bsorry[,.]?\s+I\s+meant\b", re.IGNORECASE),
    re.compile(r"\u4e0d\u5bf9", re.IGNORECASE),  # 不对 (Chinese: "not right")
    re.compile(r"\u6211\u8bf4\u9519\u4e86", re.IGNORECASE),  # 我说错了 (Chinese: "I misspoke")
]

# Number of recent human messages to check for correction/reinforcement
_DETECTION_WINDOW = 3


def _extract_message_text(msg: Any) -> str:
    """Extract text content from a message object."""
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
        return " ".join(parts)
    return str(content)


def detect_correction(messages: list[Any]) -> bool:
    """Detect if recent human messages contain correction patterns.

    Only checks the last _DETECTION_WINDOW human messages.
    """
    human_texts = [
        _extract_message_text(m)
        for m in messages
        if getattr(m, "type", None) == "human"
    ]
    # Only check last N human messages
    recent = human_texts[-_DETECTION_WINDOW:]
    for text in recent:
        for pattern in _CORRECTION_PATTERNS:
            if pattern.search(text):
                return True
    return False
```

- [ ] **Step 4: Add `correction_detected` to queue**

In `queue.py`, update `ConversationContext`:

```python
@dataclass
class ConversationContext:
    thread_id: str
    messages: list[Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    agent_name: str | None = None
    paperclip_ctx: dict[str, str] | None = None
    correction_detected: bool = False
```

Update `add()` signature:

```python
def add(self, thread_id: str, messages: list[Any], agent_name: str | None = None,
        paperclip_ctx: dict[str, str] | None = None, correction_detected: bool = False) -> None:
```

In the `add()` method, when replacing a pending update, OR-merge the flag:

```python
with self._lock:
    # OR-merge correction_detected if replacing an existing entry
    for existing in self._queue:
        if existing.thread_id == thread_id:
            correction_detected = correction_detected or existing.correction_detected
            break
    self._queue = [c for c in self._queue if c.thread_id != thread_id]
    self._queue.append(context)
```

Update the `ConversationContext` construction to include `correction_detected=correction_detected`.

- [ ] **Step 5: Add correction hint to updater**

In `updater.py`, update `update_memory()` to accept and use `correction_hint`:

```python
def update_memory(self, messages, thread_id=None, agent_name=None,
                  paperclip_ctx=None, correction_hint=False):
```

Before calling the LLM, if `correction_hint` is True, append to the prompt:

```python
if correction_hint:
    prompt += "\n\nIMPORTANT: The user has corrected or contradicted previous information in this conversation. Pay special attention to identifying which existing facts should be removed (via factsToRemove) and replaced with corrected versions in newFacts."
```

Update `_process_queue()` in `queue.py` to pass `correction_hint`:

```python
success = updater.update_memory(
    messages=context.messages,
    thread_id=context.thread_id,
    agent_name=context.agent_name,
    paperclip_ctx=context.paperclip_ctx,
    correction_hint=context.correction_detected,
)
```

- [ ] **Step 6: Wire correction detection in middleware**

In `MemoryMiddleware.after_agent()`, after the existing message filtering, add:

```python
# Detect correction in recent messages
correction_detected = detect_correction(filtered_messages)

# Queue with correction flag
queue.add(
    thread_id=thread_id,
    messages=filtered_messages,
    agent_name=self._agent_name,
    paperclip_ctx=paperclip_ctx,
    correction_detected=correction_detected,
)
```

- [ ] **Step 7: Update prompt.py**

In `prompt.py`, add `correction` to the categories list in `MEMORY_UPDATE_PROMPT`:

Change the categories line from:
```
- Categories: preference, knowledge, context, behavior, goal
```
to:
```
- Categories: preference, knowledge, context, behavior, goal, correction
```

- [ ] **Step 8: Run all tests**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_correction.py tests/test_memory_crud.py tests/test_memory_storage.py tests/test_memory_upload_filtering.py -v`
Expected: All pass

- [ ] **Step 9: Commit**

```bash
git add deerflow/agents/middlewares/memory_middleware.py deerflow/agents/memory/queue.py deerflow/agents/memory/updater.py deerflow/agents/memory/prompt.py tests/test_memory_correction.py
git commit -m "feat(memory): add correction detection

Detect user corrections via 12 regex patterns (EN+CN).
correction_detected flag OR-merged in queue, correction hint
appended to LLM prompt. Upstream PR #1668."
```

---

### Task 6: Case-Insensitive Dedup + Reinforcement

**Files:**
- Modify: `deerflow/backend/deerflow/agents/memory/updater.py`
- Modify: `deerflow/backend/deerflow/agents/memory/queue.py`
- Modify: `deerflow/backend/deerflow/agents/middlewares/memory_middleware.py`
- Create: `deerflow/backend/tests/test_memory_dedup.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_memory_dedup.py`:

```python
"""Tests for case-insensitive dedup and reinforcement detection."""

import pytest
from langchain_core.messages import HumanMessage

from deerflow.agents.memory.storage import FileMemoryStorage, _create_empty_memory, set_memory_storage
from deerflow.agents.memory.updater import _fact_content_key
from deerflow.agents.middlewares.memory_middleware import detect_correction, detect_reinforcement


def _human(text: str) -> HumanMessage:
    return HumanMessage(content=text)


@pytest.fixture(autouse=True)
def _use_tmp_storage(tmp_path):
    storage = FileMemoryStorage(base_dir=tmp_path)
    set_memory_storage(storage)
    yield
    set_memory_storage(None)


class TestFactContentKey:
    def test_casefold(self):
        assert _fact_content_key("User likes Python") == _fact_content_key("user likes python")

    def test_strip_whitespace(self):
        assert _fact_content_key("  hello  ") == _fact_content_key("hello")

    def test_collapse_spaces(self):
        assert _fact_content_key("hello   world") == _fact_content_key("hello world")

    def test_different_content_different_key(self):
        assert _fact_content_key("Python") != _fact_content_key("Ruby")


class TestDetectReinforcement:
    def test_no_reinforcement(self):
        msgs = [_human("Tell me about Python")]
        assert detect_reinforcement(msgs) is False

    def test_as_i_mentioned(self):
        msgs = [_human("As I mentioned, I prefer Python")]
        assert detect_reinforcement(msgs) is True

    def test_like_i_said(self):
        msgs = [_human("Like I said before, use TypeScript")]
        assert detect_reinforcement(msgs) is True

    def test_remember_that(self):
        msgs = [_human("Remember that I always use dark mode")]
        assert detect_reinforcement(msgs) is True

    def test_i_always(self):
        msgs = [_human("I always write tests first")]
        assert detect_reinforcement(msgs) is True

    def test_mutual_exclusion_correction_wins(self):
        """When both correction and reinforcement detected, correction wins."""
        msgs = [_human("Actually, as I mentioned, I meant Ruby not Python")]
        # Both patterns match, but correction takes priority
        correction = detect_correction(msgs)
        reinforcement = detect_reinforcement(msgs)
        assert correction is True
        # After mutual exclusion, reinforcement should be suppressed
        if correction:
            reinforcement = False
        assert reinforcement is False


class TestDedupOnInsert:
    """Test that _apply_updates deduplicates facts by content key."""

    def test_exact_duplicate_skipped(self):
        from deerflow.agents.memory.updater import MemoryUpdater

        updater = MemoryUpdater()
        current = _create_empty_memory()
        current["facts"] = [{"id": "f1", "content": "User likes Python", "category": "preference", "confidence": 0.8, "createdAt": "", "source": "t1"}]
        update_data = {
            "newFacts": [{"content": "User likes Python", "category": "preference", "confidence": 0.9}],
            "factsToRemove": [],
        }
        result = updater._apply_updates(current, update_data)
        # Should not add duplicate; should update confidence
        assert len(result["facts"]) == 1
        assert result["facts"][0]["confidence"] == 0.9  # boosted

    def test_case_insensitive_duplicate(self):
        from deerflow.agents.memory.updater import MemoryUpdater

        updater = MemoryUpdater()
        current = _create_empty_memory()
        current["facts"] = [{"id": "f1", "content": "user likes python", "category": "preference", "confidence": 0.8, "createdAt": "", "source": "t1"}]
        update_data = {
            "newFacts": [{"content": "User Likes Python", "category": "preference", "confidence": 0.7}],
            "factsToRemove": [],
        }
        result = updater._apply_updates(current, update_data)
        assert len(result["facts"]) == 1
        # Confidence stays at 0.8 since new is lower
        assert result["facts"][0]["confidence"] == 0.8

    def test_reinforcement_boosts_confidence(self):
        from deerflow.agents.memory.updater import MemoryUpdater

        updater = MemoryUpdater()
        current = _create_empty_memory()
        current["facts"] = [{"id": "f1", "content": "User likes Python", "category": "preference", "confidence": 0.8, "createdAt": "", "source": "t1"}]
        update_data = {
            "newFacts": [{"content": "User likes Python", "category": "preference", "confidence": 0.9}],
            "factsToRemove": [],
        }
        result = updater._apply_updates(current, update_data, reinforcement_detected=True)
        assert len(result["facts"]) == 1
        assert result["facts"][0]["confidence"] == min(0.8 + 0.1, 1.0)  # reinforcement boost

    def test_new_unique_fact_added(self):
        from deerflow.agents.memory.updater import MemoryUpdater

        updater = MemoryUpdater()
        current = _create_empty_memory()
        current["facts"] = [{"id": "f1", "content": "User likes Python", "category": "preference", "confidence": 0.8, "createdAt": "", "source": "t1"}]
        update_data = {
            "newFacts": [{"content": "User works at Acme Corp", "category": "context", "confidence": 0.9}],
            "factsToRemove": [],
        }
        result = updater._apply_updates(current, update_data)
        assert len(result["facts"]) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_dedup.py -v`
Expected: ImportError

- [ ] **Step 3: Implement `_fact_content_key` and dedup in `updater.py`**

Add to `updater.py`:

```python
import re as _re

def _fact_content_key(content: str) -> str:
    """Normalize fact content for case-insensitive deduplication."""
    key = content.casefold().strip()
    key = _re.sub(r"\s+", " ", key)
    return key
```

Update `_apply_updates()` to accept `reinforcement_detected` and perform dedup:

```python
def _apply_updates(self, current_memory, update_data, thread_id=None, reinforcement_detected=False):
    # ... existing code for user/history updates and factsToRemove ...

    # Build content key index of existing facts
    existing_keys = {}
    for i, fact in enumerate(current_memory.get("facts", [])):
        key = _fact_content_key(fact.get("content", ""))
        existing_keys[key] = i

    # Add new facts with dedup
    new_facts = update_data.get("newFacts", [])
    for fact in new_facts:
        confidence = fact.get("confidence", 0.5)
        if confidence < config.fact_confidence_threshold:
            continue
        content = fact.get("content", "")
        key = _fact_content_key(content)
        if key in existing_keys:
            # Duplicate found — update confidence if higher or reinforcement
            idx = existing_keys[key]
            existing_fact = current_memory["facts"][idx]
            if reinforcement_detected:
                existing_fact["confidence"] = min(existing_fact.get("confidence", 0) + 0.1, 1.0)
            elif confidence > existing_fact.get("confidence", 0):
                existing_fact["confidence"] = confidence
            continue  # skip adding duplicate

        fact_entry = {
            "id": f"fact_{uuid.uuid4().hex[:8]}",
            "content": content,
            "category": fact.get("category", "context"),
            "confidence": confidence,
            "createdAt": now,
            "source": thread_id or "unknown",
        }
        current_memory["facts"].append(fact_entry)
        existing_keys[key] = len(current_memory["facts"]) - 1

    # ... existing max_facts enforcement ...
```

- [ ] **Step 4: Add reinforcement detection to middleware**

In `memory_middleware.py`, add:

```python
_REINFORCEMENT_PATTERNS = [
    re.compile(r"\bas\s+I\s+mentioned\b", re.IGNORECASE),
    re.compile(r"\blike\s+I\s+said\b", re.IGNORECASE),
    re.compile(r"\bremember\s+that\b", re.IGNORECASE),
    re.compile(r"\bI\s+always\b", re.IGNORECASE),
    re.compile(r"\bas\s+I\s+told\s+you\b", re.IGNORECASE),
    re.compile(r"\bI'?ve\s+(?:always|already)\s+said\b", re.IGNORECASE),
    re.compile(r"\bI\s+keep\s+(?:saying|telling)\b", re.IGNORECASE),
    re.compile(r"\bagain[,.]?\s", re.IGNORECASE),
    re.compile(r"\bonce\s+more\b", re.IGNORECASE),
    re.compile(r"\bI\s+(?:still|continue\s+to)\b", re.IGNORECASE),
    re.compile(r"\bmy\s+preference\s+(?:is|remains)\b", re.IGNORECASE),
    re.compile(r"\u6211\u8bf4\u8fc7", re.IGNORECASE),  # 我说过 (Chinese: "I've said")
    re.compile(r"\u6211\u4e00\u76f4", re.IGNORECASE),  # 我一直 (Chinese: "I always")
]


def detect_reinforcement(messages: list[Any]) -> bool:
    """Detect if recent human messages contain reinforcement patterns."""
    human_texts = [
        _extract_message_text(m)
        for m in messages
        if getattr(m, "type", None) == "human"
    ]
    recent = human_texts[-_DETECTION_WINDOW:]
    for text in recent:
        for pattern in _REINFORCEMENT_PATTERNS:
            if pattern.search(text):
                return True
    return False
```

- [ ] **Step 5: Add `reinforcement_detected` to queue**

In `queue.py`, add to `ConversationContext`:

```python
reinforcement_detected: bool = False
```

Update `add()` to accept and OR-merge it:

```python
def add(self, thread_id, messages, agent_name=None, paperclip_ctx=None,
        correction_detected=False, reinforcement_detected=False):
```

OR-merge both flags when replacing pending updates. Pass `reinforcement_detected` to updater in `_process_queue()`.

- [ ] **Step 6: Wire reinforcement in middleware and updater**

In `MemoryMiddleware.after_agent()`, after detecting correction:

```python
reinforcement_detected = detect_reinforcement(filtered_messages)
# Mutual exclusion: correction takes priority
if correction_detected:
    reinforcement_detected = False

queue.add(
    thread_id=thread_id,
    messages=filtered_messages,
    agent_name=self._agent_name,
    paperclip_ctx=paperclip_ctx,
    correction_detected=correction_detected,
    reinforcement_detected=reinforcement_detected,
)
```

In `_process_queue()`, pass `reinforcement_detected` to `update_memory()`, and in `update_memory()`, pass to `_apply_updates()`.

- [ ] **Step 7: Run all tests**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && PYTHONPATH=. uv run pytest tests/test_memory_dedup.py tests/test_memory_correction.py tests/test_memory_crud.py tests/test_memory_storage.py tests/test_memory_upload_filtering.py tests/test_memory_import_export.py -v`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add deerflow/agents/memory/updater.py deerflow/agents/memory/queue.py deerflow/agents/middlewares/memory_middleware.py tests/test_memory_dedup.py
git commit -m "feat(memory): add case-insensitive dedup and reinforcement

_fact_content_key() normalizes via casefold for dedup. Reinforcement
patterns boost existing fact confidence. Mutual exclusion with
correction detection. Upstream PR #1804."
```

---

### Task 7: Update CLAUDE.md + Final Test Run

**Files:**
- Modify: `deerflow/backend/CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md memory section**

Update the Memory System section to document:
- Storage abstraction (MemoryStorage ABC, FileMemoryStorage, get_memory_storage)
- New CRUD operations (clear, delete, create, update)
- Import/export functionality
- Correction detection (12 patterns, correction_detected flag, correction hint)
- Case-insensitive dedup (_fact_content_key, reinforcement boost)
- New gateway endpoints (DELETE, POST, PATCH, import/export)

- [ ] **Step 2: Run full test suite**

Run: `cd /home/prime/Repos/paperclip/deerflow/backend && make test`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for memory improvements"
```
