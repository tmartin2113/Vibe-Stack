"""
LLM-based Semantic Classifier for Task Routing

Uses the base Vibe model to classify tasks via structured prompting.
Supports both single-label and multi-label classification with
caching and fallback extraction.
"""

from typing import Dict, Any, List, Tuple, Optional
import json
import hashlib
import logging

logger = logging.getLogger(__name__)


class LLMClassifier:
    """
    LLM-based semantic classifier for task routing.

    Uses the base Vibe model to classify tasks via structured prompting.
    Supports both single-label and multi-label classification.
    """

    def __init__(self, base_model, cache_size: int = 100,
                 task_descriptions: Optional[Dict[str, str]] = None):
        """
        Initialize LLM classifier.

        Args:
            base_model: Base model adapter (Vibe) for generation
            cache_size: Maximum number of classifications to cache
            task_descriptions: Optional dict of {task_type: description}.
                If provided, used instead of hardcoded defaults.
        """
        self.model = base_model
        self.classification_cache: Dict[str, Any] = {}
        self.cache_size = cache_size

        # Task type descriptions for prompt — use registry-provided or defaults
        if task_descriptions is not None:
            self.task_descriptions = dict(task_descriptions)
        else:
            self.task_descriptions = {
                "test_generation": "Writing unit tests, test cases, test suites (pytest, jest, mocha, etc.)",
                "security_audit": "Security analysis, vulnerability scanning, penetration testing, security reviews",
                "documentation": "Writing docs, docstrings, README files, API documentation, code comments",
                "performance_optimization": "Performance tuning, profiling, bottleneck analysis, optimization",
                "debugging": "Bug fixing, troubleshooting, error investigation, debugging",
                "refactoring": "Code restructuring, cleaning, improving architecture, removing code smells",
                "code_generation": "Writing new code, implementing features, scaffolding, creating functions",
                "data_processing": "ETL pipelines, data transformation, pandas, data cleaning, CSV/JSON parsing",
                "api_development": "REST/GraphQL APIs, endpoint creation, API design, FastAPI, Flask",
                "database_operations": "SQL, schema design, query optimization, migrations, database tuning",
                "code_review": "Code review, explanation, reverse engineering, documentation generation, static analysis",
                "general": "General tasks that don't fit specific categories"
            }

    def classify_task(self, specification: str) -> Tuple[str, float, List[str]]:
        """
        Classify task using LLM with structured output.

        Args:
            specification: Task specification text

        Returns:
            Tuple of (primary_category, confidence, secondary_categories)
        """
        # BUG FIX #2: Handle None/empty specification
        if not specification:
            logger.warning("Empty or None specification provided to LLM classifier")
            return "general", 0.3, []

        # BUG FIX #5: Use hash for cache key to prevent collisions
        # Hash the full specification to avoid collisions from identical prefixes
        cache_key = hashlib.sha256(specification.encode('utf-8')).hexdigest()
        if cache_key in self.classification_cache:
            logger.debug("LLM classification cache hit")
            return self.classification_cache[cache_key]  # type: ignore[no-any-return]

        # Build classification prompt
        prompt = self._build_classification_prompt(specification)

        try:
            # Generate with low temperature for consistency
            messages = [{"role": "user", "content": prompt}]
            gen_kwargs = dict(temperature=0.1, max_tokens=600)
            # enable_thinking is Qwen 3-specific; only send it for Qwen models
            model_name = getattr(self.model, "model_name", "")
            if "qwen" in model_name.lower():
                gen_kwargs["chat_template_kwargs"] = {"enable_thinking": False}
            response = self.model.generate(messages, **gen_kwargs)

            # Parse JSON response
            result = self._parse_classification_response(response)

            primary = result["primary_category"]
            confidence = result["confidence"]
            secondary = result.get("secondary_categories", [])

            # Cache the result (limit cache size)
            if len(self.classification_cache) >= self.cache_size:
                # Remove oldest entry (FIFO)
                self.classification_cache.pop(next(iter(self.classification_cache)))

            self.classification_cache[cache_key] = (primary, confidence, secondary)

            logger.info(f"LLM classified as: {primary} (confidence: {confidence:.2f})")
            if secondary:
                logger.info(f"Secondary categories: {secondary}")

            return primary, confidence, secondary

        except Exception as e:
            logger.error(f"LLM classification failed: {e}", exc_info=True)
            # Fallback to general with low confidence
            return "general", 0.3, []

    def _build_classification_prompt(self, specification: str) -> str:
        """Build structured classification prompt"""

        # Format task descriptions
        categories_list = "\n".join([
            f"{i+1}. {task_type} - {desc}"
            for i, (task_type, desc) in enumerate(self.task_descriptions.items())
        ])

        return f"""Classify this task into ONE primary category. Output ONLY valid JSON, no reasoning or thinking.

Task: {specification}

Categories:
{categories_list}

Output ONLY this JSON (nothing else before or after):
{{"primary_category": "<name>", "confidence": <0.0-1.0>, "reasoning": "<brief>", "secondary_categories": []}}"""

    def _parse_classification_response(self, response: str) -> Dict[str, Any]:
        """Parse JSON classification response with fallback"""

        # Try to extract JSON from response
        response = response.strip()

        # BUG FIX #3: Use balanced brace matching instead of fragile regex
        # Find JSON with properly balanced braces (handles nested objects)
        start = response.find('{')
        if start != -1:
            depth = 0
            for i in range(start, len(response)):
                if response[i] == '{':
                    depth += 1
                elif response[i] == '}':
                    depth -= 1
                    if depth == 0:
                        # Found matching closing brace
                        json_str = response[start:i+1]
                        try:
                            result = json.loads(json_str)

                            # Validate required fields
                            if "primary_category" in result and "confidence" in result:
                                # Ensure confidence is float
                                result["confidence"] = float(result["confidence"])
                                # Ensure secondary_categories is list
                                if "secondary_categories" not in result:
                                    result["secondary_categories"] = []
                                elif not isinstance(result["secondary_categories"], list):
                                    result["secondary_categories"] = []

                                return result  # type: ignore[no-any-return]
                        except (json.JSONDecodeError, ValueError) as e:
                            logger.warning(f"JSON parse error: {e}")
                            break

        # Fallback: try to extract category name from text
        logger.warning("Failed to parse JSON, attempting text extraction")
        return self._fallback_extraction(response)

    def _fallback_extraction(self, response: str) -> Dict[str, Any]:
        """Extract category if JSON parsing fails.

        When multiple task types appear in the response, picks the one
        that occurs earliest (closest to where a JSON value would be)
        or, if ambiguous, the one with the most mentions.
        """
        response_lower = response.lower()

        # Collect all mentioned task types with their first occurrence position
        candidates: list[tuple[int, str]] = []
        for task_type in self.task_descriptions.keys():
            pos = response_lower.find(task_type)
            if pos != -1:
                candidates.append((pos, task_type))

        if candidates:
            # Prefer the task type that appears earliest (closest to
            # "primary_category": in a malformed JSON response)
            candidates.sort(key=lambda x: x[0])
            best = candidates[0][1]
            logger.info(
                f"Fallback extraction found: {best} "
                f"(from {len(candidates)} candidates)"
            )
            return {
                "primary_category": best,
                "confidence": 0.5,
                "reasoning": "Extracted from text (JSON parse failed)",
                "secondary_categories": []
            }

        # Ultimate fallback
        return {
            "primary_category": "general",
            "confidence": 0.3,
            "reasoning": "Unable to parse classification response",
            "secondary_categories": []
        }
