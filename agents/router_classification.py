"""
Router Classification — Task Type Detection

Mixin providing regex, LLM, and hybrid classification modes for
the RouterNode. Extracted for readability; not intended for
standalone use.
"""

import re
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

__all__ = ["ClassificationMixin"]


class ClassificationMixin:
    """Classification methods for RouterNode.

    Expects the host class to have:
        self.classification_mode: str
        self.task_patterns: Dict[str, List[str]]
        self.pattern_weights: Dict[str, Dict[str, float]]
        self.hybrid_thresholds: Dict[str, float]
        self.llm_confidence_threshold: float
        self.llm_classifier: Optional[LLMClassifier]
        self._last_secondary_categories: List[str]
    """

    def _classify_task(self, specification: str, force_regex: bool = False) -> Tuple[str, float]:
        """
        Classify task using configured mode (regex/llm/hybrid).

        Args:
            specification: The specification text to classify
            force_regex: If True, always use regex-only (fast-path tier)

        Returns:
            Tuple of (task_type, confidence_score)
        """
        # Clear stale secondary categories from any previous classification call.
        # Without this, a prior LLM call's secondary categories could bleed into
        # a subsequent regex-path classification and incorrectly trigger decomposition.
        self._last_secondary_categories: List[str] = []

        # Fast-tier override: never spend an LLM call on classification
        if force_regex:
            return self._classify_task_regex(specification)

        if self.classification_mode == "regex":
            # Fast path: regex only
            return self._classify_task_regex(specification)

        elif self.classification_mode == "llm":
            # LLM path: semantic classification only
            return self._classify_task_llm(specification)

        elif self.classification_mode == "hybrid":
            # Hybrid path: try regex first, LLM if low confidence
            task_type, confidence = self._classify_task_regex(specification)

            # Use per-task threshold if available, otherwise fall back to global
            threshold = self.hybrid_thresholds.get(
                task_type, self.llm_confidence_threshold
            )

            # If regex is confident enough for this task type, use it (fast path)
            if confidence >= threshold:
                logger.debug(
                    f"Regex classification confident ({confidence:.2f} >= "
                    f"{threshold:.2f} for {task_type}), using regex result"
                )
                return task_type, confidence

            # Low confidence for this task type, use LLM for better accuracy
            logger.info(
                f"Regex confidence low ({confidence:.2f} < {threshold:.2f} "
                f"for {task_type}), using LLM classification"
            )
            return self._classify_task_llm(specification)

        else:
            logger.error(f"Unknown classification mode: {self.classification_mode}")
            return self._classify_task_regex(specification)

    def _classify_task_regex(self, specification: str) -> Tuple[str, float]:
        """
        Classify task using weighted regex pattern matching (fast, keyword-based).

        Uses pattern_weights to give stronger signals higher scores.
        A single "pytest" match (weight 3.0) now produces higher confidence
        than a single "test.*code" match (weight 1.0).

        Args:
            specification: The specification text to classify

        Returns:
            Tuple of (task_type, confidence_score)
        """
        # BUG FIX #2: Handle None/empty specification
        if not specification:
            return "general", 0.5

        spec_lower = specification.lower()

        # Score each task type using weighted pattern matching
        scores = {}
        for task_type, patterns in self.task_patterns.items():
            if not patterns:  # Skip empty pattern lists (like "general")
                continue

            weights = self.pattern_weights.get(task_type, {})
            weighted_score = 0.0
            max_possible = 0.0

            for pattern in patterns:
                weight = weights.get(pattern, 1.0)  # Default weight 1.0
                max_possible += weight

                if re.search(pattern, spec_lower):
                    weighted_score += weight

            # Normalized weighted score (0.0 to 1.0)
            score = weighted_score / max_possible if max_possible > 0 else 0
            scores[task_type] = score

        # Find highest scoring task type
        if scores:
            best_task = max(scores.items(), key=lambda x: x[1])
            task_type, confidence = best_task

            # If confidence is too low, default to general
            if confidence < 0.15:
                return "general", confidence

            return task_type, confidence

        # Default to general
        return "general", 0.0

    def _classify_task_llm(self, specification: str) -> Tuple[str, float]:
        """
        Classify task using LLM semantic understanding.

        Args:
            specification: The specification text to classify

        Returns:
            Tuple of (task_type, confidence_score)
        """
        if self.llm_classifier is None:
            logger.warning("LLM classifier not initialized, falling back to regex")
            return self._classify_task_regex(specification)

        # Use LLM classifier
        primary, confidence, secondary = self.llm_classifier.classify_task(specification)

        # BUG FIX #4: Removed redundant if-else (both branches did same thing)
        # Store secondary categories for potential decomposition use
        self._last_secondary_categories = secondary

        return primary, confidence
