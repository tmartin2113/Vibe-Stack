"""
Skill Registry Index Persistence Mixin

Mixin providing index load/save operations with platform-specific file locking
for the SkillRegistry. Extracted from skill_registry.py to keep that file
under 500 lines as the mixin pattern is expanded.
"""

import json
import tempfile
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# Platform-specific file locking imports
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

try:
    import msvcrt
    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

logger = logging.getLogger(__name__)

__all__ = ["SkillRegistryIndexMixin"]


class SkillRegistryIndexMixin:
    """
    Mixin providing skill index load/save with platform-specific file locking.

    Expects the composing class to provide:
    - self.index_path: Path to the index JSON file
    - self.base_dir: Path to the base skills directory (for temp file placement)
    - self.index: dict holding the current in-memory index state
    - self.security: object with an export_state() method
    """

    def _load_index(self) -> Dict[str, Any]:
        """
        Load the skill index from disk with file locking.

        Uses shared lock to allow concurrent reads but prevent reads during writes.
        """
        if not self.index_path.exists():
            return {
                "version": "1.0",
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "tiers": {
                    "official": {"skills": {}},
                    "local": {"skills": {}},
                    "temp": {"skills": {}}
                }
            }

        if HAS_FCNTL:
            # Unix/Linux/Mac: Use shared lock for reading
            with open(self.index_path, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    return json.load(f)  # type: ignore[no-any-return]
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        elif HAS_MSVCRT:
            # Windows: Use shared lock for reading
            with open(self.index_path, 'r') as f:
                # Note: msvcrt doesn't have shared locks, use exclusive briefly
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
                try:
                    return json.load(f)  # type: ignore[no-any-return]
                finally:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            # Fallback: No locking
            with open(self.index_path, 'r') as f:
                return json.load(f)  # type: ignore[no-any-return]

    def _save_index(self):
        """
        Save the skill index to disk with file locking.

        Uses platform-specific file locking to prevent race conditions:
        - Unix/Linux: fcntl (exclusive lock)
        - Windows: msvcrt (file locking)
        - Fallback: Atomic write via temp file + rename

        This fixes Bug #7: Race condition on concurrent .index.json writes.
        """
        self.index["last_updated"] = datetime.utcnow().isoformat() + "Z"
        # Bug #1/#2 fix: Persist security state (hashes + pending promotions)
        self.index["security"] = self.security.export_state()

        if HAS_FCNTL:
            # Unix/Linux/Mac: Use fcntl for file locking
            self._save_index_with_fcntl()
        elif HAS_MSVCRT:
            # Windows: Use msvcrt for file locking
            self._save_index_with_msvcrt()
        else:
            # Fallback: Atomic write (no locking, but safer than direct write)
            self._save_index_atomic()

    def _save_index_with_fcntl(self):
        """Save index with fcntl file locking (Unix/Linux/Mac)."""
        # Open with 'r+' (or create with 'w' if missing) to avoid truncating
        # before the lock is acquired.  'w' truncates immediately on open(),
        # which races with concurrent readers/writers.
        try:
            f = open(self.index_path, 'r+')
        except FileNotFoundError:
            f = open(self.index_path, 'w')

        with f:
            # Acquire exclusive lock (blocks until available)
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.truncate()
                json.dump(self.index, f, indent=2)
                f.flush()  # Ensure data written to disk
            finally:
                # Release lock
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _save_index_with_msvcrt(self):
        """Save index with msvcrt file locking (Windows)."""
        # Use atomic write for Windows instead of in-place locking.
        # msvcrt.locking() only locks N bytes starting at the current
        # file position, which is fragile with 'w' mode (truncation
        # before lock) and doesn't cover the full file.
        self._save_index_atomic()

    def _save_index_atomic(self):
        """
        Save index atomically without file locking (fallback).

        Writes to a temporary file first, then renames it to the target.
        This prevents partial writes but doesn't prevent race conditions.
        """
        # Write to temp file in same directory (ensures same filesystem)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=self.base_dir,
            prefix=".index-",
            suffix=".json.tmp"
        )

        try:
            with open(temp_fd, 'w') as f:
                json.dump(self.index, f, indent=2)
                f.flush()

            # Atomic rename (replaces existing file)
            Path(temp_path).replace(self.index_path)

        except Exception as e:
            # Clean up temp file on error
            try:
                Path(temp_path).unlink()
            except OSError:
                pass
            raise e
