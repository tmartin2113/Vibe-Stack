"""
Training Data Collector for SFT/DPO Pipeline

Captures (prompt, response, score) tuples flowing through the pipeline
and logs them in formats suitable for SFT fine-tuning and DPO preference training.

Output files (JSONL):
  - sft_examples.jsonl: High-scoring outputs (85+) as positive SFT examples
  - dpo_pairs.jsonl: Refinement deltas as (chosen, rejected) preference pairs

SFT format (HuggingFace messages):
    {"messages": [...], "score": N, "metadata": {...}}

DPO format (TRL DPOTrainer):
    {"prompt": "...", "chosen": "...", "rejected": "...", "metadata": {...}}
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .state import AgentState

logger = logging.getLogger(__name__)


class TrainingDataCollector:
    """
    Collects training data from pipeline execution for SFT/DPO training.

    Hooks into critic evaluation nodes to capture every (prompt, response, score)
    tuple. High-scoring outputs become SFT examples; refinement loops produce
    DPO preference pairs.

    Thread-safe: uses a lock for file writes.
    """

    def __init__(
        self,
        output_dir: str = "training/data/pipeline",
        sft_threshold: int = 85,
        enabled: bool = True,
    ):
        self.output_dir = Path(output_dir)
        self.sft_threshold = sft_threshold
        self.enabled = enabled
        self._lock = threading.Lock()

        # Buffer previous iterations for DPO pair extraction.
        # Key: (session_id, stage, sub_index) → {"output": str, "score": int, "prompt": str}
        # Needed because conversation_history truncates outputs to 300 chars.
        self._iteration_buffer: Dict[str, Dict[str, Any]] = {}

        # Always define paths so get_stats() works regardless of enabled state.
        self.sft_path = self.output_dir / "sft_examples.jsonl"
        self.dpo_path = self.output_dir / "dpo_pairs.jsonl"

        if self.enabled:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                f"Training data collector initialized: "
                f"sft_threshold={sft_threshold}, output_dir={self.output_dir}"
            )

    # ===== PUBLIC COLLECTION HOOKS =====

    def collect_spec_evaluation(self, state: AgentState) -> None:
        """
        Collect after genesia specification evaluation (critic_spec node).

        Captures the (user_request → specification, score) tuple.
        Tracks refinement iterations for DPO pairs.
        """
        if not self.enabled:
            return

        session_id = state.get("session_id", "unknown")
        prompt = state.get("user_request", "")
        response = state.get("specification", "")
        score = state.get("spec_critic_score") or 0
        iteration = state.get("iteration_count", 0)

        if not prompt or not response:
            return

        metadata = {
            "stage": "specification",
            "adapter": "genesia",
            "task_type": "specification_building",
            "iteration": iteration,
            "session_id": session_id,
            "scores": state.get("spec_critic_scores", {}),
            "timestamp": datetime.now().isoformat(),
        }

        # SFT: high-scoring specifications
        if score >= self.sft_threshold:
            self._write_sft(prompt, response, score, metadata)

        # DPO: refinement pairs from iteration loops
        self._check_and_write_dpo(
            session_id=session_id,
            stage="specification",
            sub_index=0,
            prompt=prompt,
            response=response,
            score=score,
            iteration=iteration,
            metadata=metadata,
        )

    def collect_output_evaluation(self, state: AgentState) -> None:
        """
        Collect after specialist output evaluation (critic_output node).

        This is the richest data source: specialist outputs with multi-dimensional
        scores, plus refinement loops producing DPO preference pairs.
        """
        if not self.enabled:
            return

        session_id = state.get("session_id", "unknown")
        prompt = state.get("specification", "")
        response = state.get("specialist_output", "")
        score = state.get("output_critic_score") or 0
        iteration = state.get("specialist_iteration_count", 0)
        specialist = state.get("specialist_adapter", "unknown")
        task_type = state.get("routed_task_type", "general")

        if not prompt or not response:
            return

        metadata = {
            "stage": "specialist_output",
            "adapter": specialist,
            "task_type": task_type,
            "iteration": iteration,
            "session_id": session_id,
            "scores": state.get("output_critic_scores", {}),
            "feedback": state.get("output_critic_feedback", ""),
            "timestamp": datetime.now().isoformat(),
        }

        # SFT: high-scoring specialist outputs
        if score >= self.sft_threshold:
            self._write_sft(prompt, response, score, metadata)

        # DPO: refinement pairs
        self._check_and_write_dpo(
            session_id=session_id,
            stage="specialist",
            sub_index=0,
            prompt=prompt,
            response=response,
            score=score,
            iteration=iteration,
            metadata=metadata,
        )

    def collect_sub_spec_evaluation(self, state: AgentState) -> None:
        """
        Collect after sub-task specification evaluation (sub_critic_spec node).

        Tracks per-sub-task specification refinement loops for DPO pairs.
        """
        if not self.enabled:
            return

        session_id = state.get("session_id", "unknown")
        sub_idx = state.get("current_sub_task_index", 0)
        sub_tasks = state.get("sub_tasks", [])

        if sub_idx >= len(sub_tasks):
            return

        sub = sub_tasks[sub_idx]
        prompt = state.get("user_request", "")
        response = sub.get("specification", "")
        score = sub.get("spec_score") or 0
        iteration = sub.get("spec_iteration_count", 0)

        if not prompt or not response:
            return

        metadata = {
            "stage": "sub_specification",
            "adapter": "genesia",
            "task_type": sub.get("task_type", "general"),
            "sub_task_index": sub_idx,
            "iteration": iteration,
            "session_id": session_id,
            "feedback": sub.get("spec_feedback", ""),
            "timestamp": datetime.now().isoformat(),
        }

        if score >= self.sft_threshold:
            self._write_sft(prompt, response, score, metadata)

        # DPO: refinement pairs from sub-spec iteration loops
        self._check_and_write_dpo(
            session_id=session_id,
            stage="sub_spec",
            sub_index=sub_idx,
            prompt=prompt,
            response=response,
            score=score,
            iteration=iteration,
            metadata=metadata,
        )

    def collect_sub_output_evaluation(self, state: AgentState) -> None:
        """
        Collect after sub-task output evaluation (sub_critic_output node).

        Tracks per-sub-task refinement loops for DPO pairs.
        """
        if not self.enabled:
            return

        session_id = state.get("session_id", "unknown")
        sub_idx = state.get("current_sub_task_index", 0)
        sub_tasks = state.get("sub_tasks", [])

        if sub_idx >= len(sub_tasks):
            return

        sub = sub_tasks[sub_idx]
        prompt = sub.get("specification", "")
        response = sub.get("output", "")
        score = sub.get("output_score") or 0
        iteration = sub.get("iteration_count", 0)
        specialist = sub.get("specialist_adapter", "unknown")

        if not prompt or not response:
            return

        metadata = {
            "stage": "sub_task_output",
            "adapter": specialist,
            "task_type": sub.get("task_type", "general"),
            "sub_task_index": sub_idx,
            "iteration": iteration,
            "session_id": session_id,
            "timestamp": datetime.now().isoformat(),
        }

        # SFT: high-scoring sub-task outputs
        if score >= self.sft_threshold:
            self._write_sft(prompt, response, score, metadata)

        # DPO: refinement pairs per sub-task
        self._check_and_write_dpo(
            session_id=session_id,
            stage="sub_task",
            sub_index=sub_idx,
            prompt=prompt,
            response=response,
            score=score,
            iteration=iteration,
            metadata=metadata,
        )

    def collect_final_evaluation(self, state: AgentState) -> None:
        """
        Collect after aggregated output evaluation (final_critic node).

        Captures the final synthesized output quality.
        """
        if not self.enabled:
            return

        session_id = state.get("session_id", "unknown")
        prompt = state.get("specification", "")
        response = state.get("aggregated_output", "")
        score = state.get("output_critic_score") or 0

        if not prompt or not response:
            return

        metadata = {
            "stage": "aggregated_output",
            "adapter": "aggregator",
            "task_type": "aggregation",
            "session_id": session_id,
            "scores": state.get("output_critic_scores", {}),
            "sub_task_count": state.get("completed_sub_tasks", 0),
            "aggregation_strategy": state.get("aggregation_strategy", "merge"),
            "timestamp": datetime.now().isoformat(),
        }

        if score >= self.sft_threshold:
            self._write_sft(prompt, response, score, metadata)

        # Final evaluation is the last hook per workflow — clear session buffer
        # to prevent unbounded growth in daemon mode.
        self.clear_iteration_buffer(session_id)

    # ===== INTERNAL: SFT WRITING =====

    def _write_sft(
        self,
        prompt: str,
        response: str,
        score: int,
        metadata: Dict[str, Any],
    ) -> None:
        """
        Write a high-scoring example in HuggingFace messages format for SFT.

        Format matches TRL SFTTrainer convention.
        """
        record = {
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response},
            ],
            "score": score,
            "metadata": metadata,
        }

        self._append_jsonl(self.sft_path, record)
        logger.debug(
            f"SFT example logged: stage={metadata.get('stage')}, "
            f"score={score}, adapter={metadata.get('adapter')}"
        )

    # ===== INTERNAL: DPO PAIR EXTRACTION =====

    def _check_and_write_dpo(
        self,
        session_id: str,
        stage: str,
        sub_index: int,
        prompt: str,
        response: str,
        score: int,
        iteration: int,
        metadata: Dict[str, Any],
    ) -> None:
        """
        Compare current iteration with buffered previous iteration.

        If the current score improved over the previous iteration's score,
        write a DPO preference pair: previous = rejected, current = chosen.
        The feedback stored is from the PREVIOUS iteration — i.e., the critic's
        explanation of why the rejected output was inadequate, which is the
        signal that motivated the improvement.
        Then update the buffer for the next potential iteration.
        """
        buf_key = f"{session_id}:{stage}:{sub_index}"

        if buf_key in self._iteration_buffer:
            prev = self._iteration_buffer[buf_key]
            prev_score = prev["score"]

            # Only create a pair when the score actually improved
            if score > prev_score:
                dpo_record = {
                    "prompt": prompt,
                    "chosen": response,
                    "rejected": prev["output"],
                    "metadata": {
                        **metadata,
                        "chosen_score": score,
                        "rejected_score": prev_score,
                        "iteration_from": prev.get("iteration", iteration - 1),
                        "iteration_to": iteration,
                        # Use the PREVIOUS iteration's feedback: explains why
                        # the rejected output was inadequate, not what's still
                        # wrong with the chosen output.
                        "feedback": prev.get("feedback", ""),
                    },
                }
                self._append_jsonl(self.dpo_path, dpo_record)
                logger.debug(
                    f"DPO pair logged: stage={stage}, "
                    f"rejected_score={prev_score} → chosen_score={score}"
                )

        # Always buffer current iteration for next comparison,
        # including feedback so the NEXT DPO pair can reference it.
        self._iteration_buffer[buf_key] = {
            "output": response,
            "score": score,
            "prompt": prompt,
            "iteration": iteration,
            "feedback": metadata.get("feedback", ""),
        }

    # ===== INTERNAL: FILE I/O =====

    def _append_jsonl(self, path: Path, record: Dict[str, Any]) -> None:
        """Thread-safe append of a JSON record to a JSONL file."""
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ===== UTILITY =====

    def get_stats(self) -> Dict[str, int]:
        """Return counts of collected SFT examples and DPO pairs."""
        stats = {"sft_examples": 0, "dpo_pairs": 0}

        if self.sft_path.exists():
            with open(self.sft_path, "r") as f:
                stats["sft_examples"] = sum(1 for _ in f)

        if self.dpo_path.exists():
            with open(self.dpo_path, "r") as f:
                stats["dpo_pairs"] = sum(1 for _ in f)

        return stats

    def clear_iteration_buffer(self, session_id: Optional[str] = None) -> None:
        """
        Clear the iteration buffer, optionally for a specific session.

        Call this at the end of a workflow run to free memory.
        """
        if session_id is None:
            self._iteration_buffer.clear()
        else:
            keys_to_remove = [
                k for k in self._iteration_buffer if k.startswith(f"{session_id}:")
            ]
            for k in keys_to_remove:
                del self._iteration_buffer[k]
