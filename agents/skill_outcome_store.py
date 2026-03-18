"""
Skill Outcome Store - JSONL-backed memory for skill reinforcement.

Records (skill_content, task_type, specification, score, feedback) tuples
after each workflow run, closing the reinforcement loop:

    Generate skill -> Use skill -> Score skill -> Record outcome
                ^                                      |
                |______ Retrieve top-K examples ________|

The outcome store is the CONSUMER of skill quality signals.
It is actively queried by skill_generator to produce better skills over time.
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SkillOutcomeStore:
    """
    Persists skill outcomes and retrieves top-scoring examples for RAG.

    Thread-safe: uses a lock for file writes.

    File format (JSONL):
        {
            "skill_name": "ephemeral-test-generation",
            "task_type": "test_generation",
            "specification_summary": "...",
            "skill_content": "...",
            "score": 92,
            "feedback": "...",
            "timestamp": "2026-02-23T..."
        }
    """

    # Only record outcomes above this score — low-scoring skills are noise
    # unless we're collecting negative examples.
    NEGATIVE_EXAMPLE_CEILING = 50
    POSITIVE_EXAMPLE_FLOOR = 70

    def __init__(
        self,
        store_path: Optional[str] = None,
        max_entries: int = 500,
    ):
        """
        Initialize the outcome store.

        Args:
            store_path: Path to the JSONL file.  Defaults to
                        vibe_skills/outcome_store.jsonl alongside the
                        skill registry index.
            max_entries: Maximum entries to keep (FIFO eviction).
        """
        if store_path is None:
            store_path = str(
                Path.home() / ".local" / "share"
                / "vibe_skills" / "outcome_store.jsonl"
            )
        self.store_path = Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record(
        self,
        skill_name: str,
        task_type: str,
        specification: str,
        skill_content: str,
        score: int,
        feedback: str = "",
    ) -> None:
        """
        Record a skill outcome.

        Only records if the score is informative:
        - score >= POSITIVE_EXAMPLE_FLOOR  (good example)
        - score <= NEGATIVE_EXAMPLE_CEILING (bad example)
        Mid-range scores are ambiguous and skipped.

        Args:
            skill_name:    Registry name of the skill
            task_type:     Classified task type (e.g. "test_generation")
            specification: The specification that triggered this skill
            skill_content: The SKILL.md content that was used
            score:         Critic quality score (0-100)
            feedback:      Critic feedback text
        """
        if score == 0:
            logger.debug(
                f"Skipping unevaluated outcome for {skill_name} (score=0)"
            )
            return

        if self.NEGATIVE_EXAMPLE_CEILING < score < self.POSITIVE_EXAMPLE_FLOOR:
            logger.debug(
                f"Skipping ambiguous outcome for {skill_name} "
                f"(score={score}, outside recording bands)"
            )
            return

        entry = {
            "skill_name": skill_name,
            "task_type": task_type,
            "specification_summary": specification[:300],
            "skill_content": skill_content,
            "score": score,
            "feedback": feedback[:500] if feedback else "",
            "is_positive": score >= self.POSITIVE_EXAMPLE_FLOOR,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        with self._lock:
            dedup_hit = self._append(entry)

        action = "dedup" if dedup_hit else "recorded"
        logger.info(
            f"📝 Skill outcome {action}: {skill_name} "
            f"score={score} ({'positive' if entry['is_positive'] else 'negative'})"
        )

    def _append(self, entry: Dict[str, Any]) -> bool:
        """Append an entry with dedup-on-write and FIFO eviction.

        Dedup strategy: at most one positive and one negative entry per
        skill_name.  If an entry with the same (skill_name, is_positive)
        already exists, the better-scoring entry wins:
          - Positive band: higher score wins (best example for RAG)
          - Negative band: lower score wins (most instructive anti-pattern)
        This prevents redundant RAG examples while preserving the most
        useful example in each band.

        Returns:
            True if dedup matched (replaced or kept existing),
            False if appended as new.
        """
        entries = self._read_all()

        # Dedup: find existing entry with same skill_name + band
        dedup_hit = False
        new_name = entry["skill_name"]
        new_band = entry["is_positive"]
        new_score = entry["score"]

        for i, existing in enumerate(entries):
            if (existing.get("skill_name") == new_name
                    and existing.get("is_positive") == new_band):
                old_score = existing.get("score", 0)

                # Keep the more useful example for each band:
                #   positive → higher score wins (best RAG example)
                #   negative → lower score wins (worst anti-pattern)
                if new_band:
                    should_replace = new_score > old_score
                else:
                    should_replace = new_score < old_score

                if should_replace:
                    entries[i] = entry
                else:
                    # Existing entry is better — nothing to write
                    return True

                dedup_hit = True
                break

        if not dedup_hit:
            entries.append(entry)

        # FIFO eviction
        if len(entries) > self.max_entries:
            entries = entries[-self.max_entries:]

        # Atomic-ish write: write to tmp then rename
        tmp_path = self.store_path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        tmp_path.rename(self.store_path)

        return dedup_hit

    # ------------------------------------------------------------------
    # Read path (used by skill_generator for RAG)
    # ------------------------------------------------------------------

    def retrieve_positive_examples(
        self,
        task_type: str,
        top_k: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve the top-K highest-scoring positive examples for a task type.

        Args:
            task_type: Task type to filter by
            top_k:     Maximum number of examples to return

        Returns:
            List of outcome dicts, sorted by score descending.
        """
        entries = self._read_all()

        # Filter: positive examples for this task type
        matches = [
            e for e in entries
            if e.get("task_type") == task_type and e.get("is_positive", False)
        ]

        # Sort by score descending, then by recency
        matches.sort(key=lambda e: (e["score"], e["timestamp"]), reverse=True)

        return matches[:top_k]

    def retrieve_negative_examples(
        self,
        task_type: str,
        top_k: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve the lowest-scoring negative examples for a task type.

        Used as "don't do this" examples in skill generation prompts.

        Args:
            task_type: Task type to filter by
            top_k:     Maximum number of examples to return

        Returns:
            List of outcome dicts, sorted by score ascending.
        """
        entries = self._read_all()

        # Filter: negative examples for this task type
        matches = [
            e for e in entries
            if e.get("task_type") == task_type and not e.get("is_positive", True)
        ]

        # Sort by score ascending (worst first)
        matches.sort(key=lambda e: (e["score"], e["timestamp"]))

        return matches[:top_k]

    def get_stats(self) -> Dict[str, Any]:
        """Return summary statistics about the outcome store."""
        entries = self._read_all()

        if not entries:
            return {"total": 0, "by_task_type": {}}

        by_type: Dict[str, Dict[str, Any]] = {}
        for e in entries:
            tt = e.get("task_type", "unknown")
            if tt not in by_type:
                by_type[tt] = {"count": 0, "positive": 0, "negative": 0, "scores": []}
            by_type[tt]["count"] += 1
            by_type[tt]["scores"].append(e.get("score", 0))
            if e.get("is_positive"):
                by_type[tt]["positive"] += 1
            else:
                by_type[tt]["negative"] += 1

        # Compute averages
        for tt, stats in by_type.items():
            scores = stats.pop("scores")
            stats["avg_score"] = sum(scores) / len(scores) if scores else 0

        return {"total": len(entries), "by_task_type": by_type}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_all(self) -> List[Dict[str, Any]]:
        """Read all entries from the JSONL file."""
        if not self.store_path.exists():
            return []

        entries = []
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            logger.debug(f"Skipping malformed line in outcome store")
        except OSError as e:
            logger.warning(f"Could not read outcome store: {e}")

        return entries
