"""
Intent Classifier for Genesia

Determines user intent before routing to specialists. Distinguishes between:
- conversational: Questions, explanations, comparisons (no code needed)
- research: Documentation lookup, framework research
- planning: Architecture advice, design decisions
- code_generation: Actual code implementation (routes to RouterNode)
"""

from typing import Tuple
import re
import logging
from .state import AgentState

logger = logging.getLogger(__name__)


class IntentClassifier:
    """
    Classifies user intent to determine if code generation is needed.

    This is the first stage before RouterNode — determines if the user wants:
    - Conversational response (explain, compare, what is)
    - Research (find docs, look up)
    - Planning (architecture, design advice)
    - Code generation (write, implement, create)
    """

    def __init__(self):
        """Initialize intent classifier with detection patterns."""

        # Intent classification patterns
        # Each intent has patterns that strongly indicate that intent
        self.intent_patterns = {
            "conversational": [
                r"^what is\b",
                r"^what are\b",
                r"^what's\b",
                r"^how does\b",
                r"^how do\b",
                r"^why\b",
                r"^explain\b",
                r"^describe\b",
                r"^compare\b",
                r"^difference between\b",
                r"^when should I\b",
                r"^can you explain\b",
                r"^tell me about\b",
                r"^what.*mean\b",
                r"\bvs\b",  # "X vs Y"
                r"\bversus\b",
            ],
            "research": [
                r"^research\b",
                r"^find.*doc",
                r"^look up\b",
                r"^search for\b",
                r"^show me.*doc",
                r"^get.*doc",
                r"^fetch.*doc",
                r"\bdocumentation for\b",
                r"\bofficial.*doc",
                r"\blatest.*version",
                r"\brelease notes\b",
            ],
            "planning": [
                r"^design\b",
                r"^architect",
                r"^plan\b",
                r"^should i use\b",  # Lowercase 'i' to match after .lower()
                r"^which.*should\b",
                r"^recommend\b",
                r"^suggest\b",
                r"^advise\b",
                r"\bbest approach\b",
                r"\bbest way to\b",
                r"\btrade.*off",
                r"\bpros.*cons\b",
                r"\bwhen to use\b",
                r"\bchoose between\b",
                r"\barchitecture for\b",
                r"\bdesign pattern\b",
                r"\bdesign.*system\b",
                r"\bwhich.*should.*use\b",
                r"\btechnology stack\b",
                r"\bchoose.*technolog\b",
                r"\barchitecture.*pattern\b",
                r"\bsystem.*design\b",
            ],
            "code_generation": [
                r"\bwrite\b",
                r"\bimplement\b",
                r"\bcreate\b",
                r"\bgenerate\b",
                r"\bbuild\b",
                r"\badd\b",
                r"\bfix\b",
                r"\brefactor\b",
                r"\boptimiz",
                r"\btest\b",
                r"\bdebug\b",
                r"\bfunction\b",
                r"\bclass\b",
                r"\bmodule\b",
                r"\bAPI\b",
                r"\bendpoint\b",
            ]
        }

        # Pattern weights: stronger signals get higher weights
        self.pattern_weights = {
            "conversational": {
                r"^what is\b": 3.0,
                r"^what are\b": 3.0,
                r"^how does\b": 3.0,
                r"^explain\b": 3.0,
                r"^compare\b": 3.0,
                r"^difference between\b": 3.0,
                r"^describe\b": 2.5,
                r"^why\b": 2.0,
                r"^tell me about\b": 2.0,
                r"^can you explain\b": 2.5,
                r"^what's\b": 2.5,
                r"\bvs\b": 2.0,
                r"\bversus\b": 2.0,
                r"^when should I\b": 1.5,
                r"^what.*mean\b": 2.0,
                r"^how do\b": 2.0,
            },
            "research": {
                r"^research\b": 3.0,
                r"^find.*doc": 3.0,
                r"^look up\b": 2.5,
                r"^search for\b": 2.0,
                r"\bofficial.*doc": 2.5,
                r"\blatest.*version": 2.0,
                r"\brelease notes\b": 2.5,
                r"^show me.*doc": 2.0,
                r"^get.*doc": 2.0,
                r"^fetch.*doc": 2.0,
                r"\bdocumentation for\b": 2.0,
            },
            "planning": {
                r"^design\b": 2.5,
                r"^architect": 3.0,
                r"^should i use\b": 2.5,  # Lowercase 'i'
                r"^which.*should\b": 2.0,
                r"^recommend\b": 2.5,
                r"\barchitecture for\b": 3.0,
                r"\bdesign pattern\b": 2.5,
                r"\btrade.*off": 2.5,
                r"\bpros.*cons\b": 2.5,
                r"\bbest approach\b": 2.0,
                r"\bbest way to\b": 2.0,
                r"^plan\b": 2.0,
                r"^suggest\b": 2.0,
                r"^advise\b": 2.0,
                r"\bwhen to use\b": 1.5,
                r"\bchoose between\b": 2.0,
                r"\bdesign.*system\b": 2.5,
                r"\bwhich.*should.*use\b": 2.5,
                r"\btechnology stack\b": 3.0,
                r"\bchoose.*technolog\b": 2.5,
                r"\barchitecture.*pattern\b": 2.5,
                r"\bsystem.*design\b": 2.5,
            },
            "code_generation": {
                r"\bwrite\b": 2.0,
                r"\bimplement\b": 2.5,
                r"\bcreate\b": 2.0,
                r"\bgenerate\b": 2.5,
                r"\bbuild\b": 2.0,
                r"\bfix\b": 2.5,
                r"\brefactor\b": 2.5,
                r"\boptimiz": 2.0,
                r"\bfunction\b": 1.5,
                r"\bclass\b": 1.5,
                r"\bmodule\b": 1.5,
                r"\bAPI\b": 1.0,  # Weak (could be "explain API")
                r"\badd\b": 1.5,
                r"\btest\b": 1.5,
                r"\bdebug\b": 1.5,
                r"\bendpoint\b": 1.5,
            }
        }

        # Confidence thresholds for each intent
        # If confidence is below threshold, default to code_generation (safer)
        self.confidence_thresholds = {
            "conversational": 0.3,  # Low bar (conversational signals are strong)
            "research": 0.4,        # Medium bar
            "planning": 0.35,       # Low-medium bar
            "code_generation": 0.2  # Very low bar (default fallback)
        }

    def classify(self, user_request: str) -> Tuple[str, float]:
        """
        Classify user intent from their request.

        Args:
            user_request: Raw user input

        Returns:
            Tuple of (intent, confidence_score)
            - intent: "conversational", "research", "planning", or "code_generation"
            - confidence: 0.0 to 1.0
        """
        if not user_request or not user_request.strip():
            logger.warning("Empty user request, defaulting to code_generation")
            return "code_generation", 0.5

        request_lower = user_request.lower().strip()

        # Score each intent using max matched weight (not normalized)
        # This allows a single strong signal (e.g., "What is") to yield high confidence
        scores = {}
        for intent, patterns in self.intent_patterns.items():
            weights = self.pattern_weights.get(intent, {})
            max_matched_weight = 0.0

            for pattern in patterns:
                if re.search(pattern, request_lower):
                    weight = weights.get(pattern, 1.0)
                    max_matched_weight = max(max_matched_weight, weight)

            # Score is the strongest matched signal (0.0 to 3.0)
            # Normalize to 0.0-1.0 by dividing by max possible weight (3.0)
            score = max_matched_weight / 3.0
            scores[intent] = score

        # Find highest scoring intent
        if not scores:
            logger.warning("No intent patterns matched, defaulting to code_generation")
            return "code_generation", 0.5

        best_intent = max(scores.items(), key=lambda x: x[1])
        intent, confidence = best_intent

        # Check if confidence meets threshold
        threshold = self.confidence_thresholds.get(intent, 0.5)
        if confidence < threshold:
            logger.info(
                f"Intent '{intent}' confidence ({confidence:.2f}) below threshold "
                f"({threshold:.2f}), defaulting to code_generation"
            )
            return "code_generation", 0.6  # Default with medium confidence

        logger.info(f"Classified intent: {intent} (confidence: {confidence:.2f})")
        return intent, confidence

    def execute(self, state: AgentState) -> AgentState:
        """
        Execute intent classification on state.

        Args:
            state: Current agent state

        Returns:
            Updated state with intent classification
        """
        user_request = state.get("user_request", "")

        intent, confidence = self.classify(user_request)

        # Update state with intent classification
        state["intent"] = intent  # type: ignore[typeddict-item]
        state["intent_confidence"] = confidence

        # Add to debug info
        debug_info = state.get("debug_info", {})
        debug_info["intent_classification"] = {
            "intent": intent,
            "confidence": confidence
        }
        state["debug_info"] = debug_info

        logger.info(f"User intent: {intent} (confidence: {confidence:.2f})")

        return state


# Convenience function for graph integration
def classify_intent(state: AgentState) -> AgentState:
    """
    Classify user intent before routing.

    This should be the first node in the graph, before RouterNode.

    Args:
        state: Current agent state

    Returns:
        Updated state with intent classification
    """
    classifier = IntentClassifier()
    return classifier.execute(state)
