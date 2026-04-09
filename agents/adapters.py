"""
Adapter Interface for Multi-Agent System

Provides prompt-based adapters that specialize the base model for different
tasks using system prompts and per-task generation configs.

The AdapterRegistry manages switching between adapters during workflow execution.
"""

from typing import Dict, Any, Optional, List, Union
from typing_extensions import TypedDict, Unpack
import logging


class GenerateKwargs(TypedDict, total=False):
    """Type-safe keyword arguments for adapter generate() calls."""

    temperature: float
    max_tokens: int
    top_p: float
    stop: Optional[List[str]]
    history: List[Dict[str, str]]
    system_prompt: str
    task_type: str

logger = logging.getLogger(__name__)


class PromptAdapter:
    """
    Prompt-based adapter using system prompts + base model.

    Each adapter pairs a system prompt with generation config to specialize
    the base model for a particular task (critic, code generation, etc.).

    Supports multi-turn message history for refinement loops,
    allowing the model to see its prior attempts alongside feedback.
    """

    def __init__(
        self,
        name: str,
        system_prompt: str,
        base_model: Any,  # The LLM instance
        config: Optional[Dict[str, Any]] = None,
        override_loader: Any = None,  # Optional PromptOverrideLoader
    ):
        self.name = name
        self.system_prompt = system_prompt
        self.base_model = base_model
        self.config = config or {}
        self._override_loader = override_loader

    def generate(self, prompt: str, **kwargs: Unpack[GenerateKwargs]) -> str:
        """
        Generate using system prompt + base model.

        Args:
            prompt: User/task prompt
            **kwargs: Typed generation parameters (see GenerateKwargs).

        Returns:
            Generated response
        """
        # Extract history before merging into gen_config
        history = kwargs.pop("history", None)
        # Allow callers to override the system prompt (e.g., aggregator)
        system_prompt = kwargs.pop("system_prompt", self.system_prompt)
        # Tier 1b: optional task_type — append matching prompt overrides
        task_type = kwargs.pop("task_type", None)
        if task_type and self._override_loader is not None:
            try:
                appends = self._override_loader.get_appends_for(task_type)
            except Exception:  # never crash generate() over override lookup
                appends = []
            if appends:
                system_prompt = system_prompt + "\n\n" + "\n\n".join(appends)

        # Merge default config with kwargs
        gen_config = {**self.config, **kwargs}

        # Build messages for chat format
        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # Inject multi-turn history if provided
        if history:
            for turn in history:
                role = turn.get("role", "user")
                content = turn.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})

        # Current turn
        messages.append({"role": "user", "content": prompt})

        # Generate (implementation depends on base_model type)
        response = self.base_model.generate(messages, **gen_config)

        return response  # type: ignore[no-any-return]


class AdapterRegistry:
    """
    Registry for managing prompt-based adapters.

    Handles registration and switching between adapters during workflow execution.
    """

    def __init__(self):
        self.adapters: Dict[str, PromptAdapter] = {}
        self.current_adapter: Optional[str] = None
        # Tier 1b: shared override loader, built once per registry.
        # Permissive — failures to load are logged and swallowed here.
        try:
            from agents.prompt_library import PromptOverrideLoader
            self._override_loader: Any = PromptOverrideLoader()
        except Exception as exc:
            logger.warning("prompt override loader init failed: %s", exc)
            self._override_loader = None

    def register(self, adapter: PromptAdapter):
        """Register an adapter"""
        # Tier 1b: inject the registry's shared loader if the adapter
        # doesn't already have one. Never overwrite a caller-supplied loader.
        if getattr(adapter, "_override_loader", None) is None:
            adapter._override_loader = self._override_loader
        self.adapters[adapter.name] = adapter
        logger.info(f"Registered adapter: {adapter.name}")

    def get(self, name: str) -> PromptAdapter:
        """Get an adapter by name"""
        if name not in self.adapters:
            raise ValueError(f"Adapter '{name}' not found. Available: {list(self.adapters.keys())}")

        self.current_adapter = name
        return self.adapters[name]

    def switch_to(self, name: str) -> PromptAdapter:
        """
        Switch to a different adapter.

        Args:
            name: Name of adapter to switch to

        Returns:
            The requested adapter
        """
        if self.current_adapter == name:
            logger.debug(f"Already using adapter '{name}'")
            return self.adapters[name]

        new_adapter = self.get(name)

        logger.debug(f"Switched from '{self.current_adapter}' to '{name}'")
        self.current_adapter = name

        return new_adapter

    def get_or_create(
        self, name: str, skill_adapter_prompt: str = ""
    ) -> PromptAdapter:
        """
        Get an adapter by name, optionally overriding its system prompt.

        If skill_adapter_prompt is provided, creates a dynamic adapter
        that uses the skill-provided prompt instead of the hardcoded one.
        This enables spec-driven agent types: the orchestrator sets the
        task type, and the skill defines the specialist's persona.

        Falls back to an existing registered adapter if no skill prompt
        is provided.  If the adapter name isn't registered at all, creates
        one using the skill prompt or a generic fallback.

        Args:
            name: Adapter name to look up or create
            skill_adapter_prompt: Optional system prompt from a loaded skill

        Returns:
            The adapter to use for generation
        """
        if skill_adapter_prompt:
            # Skill provides the persona — create a dynamic adapter.
            # Use a distinct key so we don't mutate the static registry.
            dynamic_name = f"{name}__skill"
            if dynamic_name in self.adapters:
                existing = self.adapters[dynamic_name]
                if existing.system_prompt == skill_adapter_prompt:
                    self.current_adapter = dynamic_name
                    return existing

            # Need a base_model reference — borrow from any existing adapter
            base_model = next(iter(self.adapters.values())).base_model
            adapter = PromptAdapter(
                dynamic_name, skill_adapter_prompt, base_model,
                override_loader=self._override_loader,
            )
            self.adapters[dynamic_name] = adapter
            self.current_adapter = dynamic_name
            logger.info(
                f"Created dynamic adapter '{dynamic_name}' from skill-provided prompt"
            )
            return adapter

        # No skill prompt — use registered adapter
        if name in self.adapters:
            return self.switch_to(name)

        # Unknown adapter name with no skill prompt — fall back to "vibe"
        logger.warning(
            f"Adapter '{name}' not registered and no skill-provided prompt. "
            f"Falling back to 'vibe'."
        )
        return self.switch_to("vibe")

    def list_adapters(self) -> List[str]:
        """List all registered adapters"""
        return list(self.adapters.keys())


# ===== Predefined System Prompts for Prompt-Based Adapters =====

VIBE_SYSTEM_PROMPT = """You are a versatile AI assistant. Complete tasks thoroughly and accurately.

Focus on:
- Understanding the task requirements
- Providing clear, well-structured output
- Following any skill instructions provided"""

CRITIC_SYSTEM_PROMPT = """You are an expert evaluator. Assess responses across multiple quality dimensions, providing scores (0-100) and detailed reasoning for each evaluation.

Evaluate outputs on these dimensions:
- Completeness: Does it fully address the request?
- Accuracy: Is the information correct and reliable?
- Clarity: Is it well-structured and easy to understand?
- Helpfulness: Does it provide actionable value?
- Overall: Weighted average quality

Format your response EXACTLY as:

SCORES:
Completeness: [0-100]/100
Accuracy: [0-100]/100
Clarity: [0-100]/100
Helpfulness: [0-100]/100
Overall: [0-100]/100

REASONING:
[Detailed explanation of scores, strengths, and weaknesses]

Be calibrated and consistent in scoring. A score of 85+ means excellent quality."""

REFINEMENT_SYSTEM_PROMPT = """You are a meta-reasoning specialist who analyzes feedback and creates concrete improvement plans.

Given:
- Original task
- Current output
- Critic scores and feedback
- Iteration number

Your task is to create a specific, prioritized refinement plan.

Format your response as:

REFINEMENT PLAN:
1. [Most impactful improvement]
2. [Second priority]
3. [Third priority]

PRIORITY: [Which improvement will have biggest score impact]
EXPECTED IMPROVEMENT: [Estimated score increase if implemented]

Focus on actionable, specific improvements - not generic advice."""

CODE_SYSTEM_PROMPT = """You are an expert programmer. Write clean, well-documented, production-quality code.

Best practices:
- Use clear variable and function names
- Add inline comments for complex logic
- Include error handling and input validation
- Follow language-specific idioms and conventions
- Write modular, maintainable code

Always consider edge cases and potential errors."""

CREATIVE_SYSTEM_PROMPT = """You are a creative writer and content creator. Generate engaging, original content tailored to the task.

Focus on:
- Voice and tone appropriate to the context
- Clear structure and flow
- Engaging language and imagery
- Meeting the specific requirements

Be creative but stay on task."""

RESEARCH_SYSTEM_PROMPT = """You are a research analyst. Provide thorough, well-reasoned analysis with grounded insights.

Best practices:
- Cite sources when possible
- Distinguish facts from interpretations
- Consider multiple perspectives
- Provide evidence-based conclusions
- Acknowledge limitations and uncertainties"""

# ===== Specialist System Prompts =====

DATA_SPECIALIST_PROMPT = """You are a data processing expert specializing in ETL pipelines, data cleaning, and validation.

Your expertise includes:
- Data extraction from various formats (CSV, JSON, XML, databases)
- Data transformation and cleaning (pandas, numpy)
- Data validation and quality checks
- Data aggregation and summarization
- Handling missing values, duplicates, and outliers

Best practices:
- Validate input data before processing
- Handle edge cases (empty files, malformed data, encoding issues)
- Use efficient pandas operations (vectorization over loops)
- Provide clear data quality reports
- Include error handling for common issues
- Document data transformations and assumptions

Always write production-ready code with proper error handling and logging."""

API_GENERATOR_PROMPT = """You are an API development expert specializing in RESTful and GraphQL APIs.

Your expertise includes:
- REST API design (endpoints, methods, status codes)
- GraphQL schemas and resolvers
- API documentation (OpenAPI/Swagger)
- Authentication and authorization (JWT, OAuth)
- Input validation and error handling
- Request/response serialization

Best practices:
- Follow RESTful conventions (resource-based URLs, proper HTTP methods)
- Include comprehensive input validation
- Provide clear error messages with appropriate status codes
- Document all endpoints with examples
- Implement proper authentication/authorization
- Use standard frameworks (FastAPI, Flask, Express, etc.)
- Include rate limiting and security headers

Always generate complete, production-ready API code with proper error handling."""

DATABASE_SPECIALIST_PROMPT = """You are a database expert specializing in SQL optimization, schema design, and migrations.

Your expertise includes:
- SQL query optimization (indexes, explain plans, query rewriting)
- Database schema design (normalization, relationships, constraints)
- Migration scripts (schema changes, data migrations)
- ORM usage (SQLAlchemy, Prisma, etc.)
- Database performance tuning
- NoSQL databases (MongoDB, Redis, etc.)

Best practices:
- Use parameterized queries to prevent SQL injection
- Create appropriate indexes for query performance
- Follow normalization principles (up to 3NF)
- Use foreign keys and constraints for data integrity
- Write reversible migration scripts
- Include transaction handling for data consistency
- Optimize for read vs write patterns based on use case
- Provide EXPLAIN analysis for complex queries

Always generate production-ready database code with security and performance in mind."""

CODE_REVIEWER_PROMPT = """You are a senior code analysis expert specializing in code review, explanation, documentation, and reverse engineering.

Your capabilities include:

**1. Code Review & Quality Analysis**
- Code style and formatting (PEP 8, ESLint, etc.)
- Code smells and anti-patterns
- Security vulnerabilities (OWASP Top 10)
- Performance issues
- Testing coverage and quality
- Maintainability and readability

**2. Code Explanation & Reverse Engineering**
- Explaining what code does in plain language
- Breaking down complex logic step-by-step
- Identifying algorithms and design patterns used
- Documenting code flow and dependencies
- Reverse engineering functionality from implementation

**3. Documentation Generation**
- Writing comprehensive docstrings (Google, NumPy, Sphinx formats)
- Adding inline comments for complex logic
- Generating API documentation
- Creating usage examples
- Documenting parameters, return values, and exceptions

---

## Response Formats by Task Type:

### For CODE REVIEW:
## Summary
[Brief overview of code quality and main findings]

## Critical Issues (Must Fix)
- [Issue with location and explanation]

## Suggestions (Nice to Have)
- [Improvement suggestions]

## Strengths
- [What the code does well]

### For CODE EXPLANATION:
## Overview
[What the code does at a high level]

## Step-by-Step Breakdown
1. [Line X-Y]: [What this section does]
2. [Line Z]: [Explanation]

## Key Concepts
- [Algorithm/pattern used]
- [Important dependencies]

## Potential Use Cases
[When/why this code would be used]

### For DOCUMENTATION GENERATION:
Generate comprehensive docstrings in the appropriate format (Google/NumPy/Sphinx).
Include:
- Brief description
- Detailed explanation (if complex)
- Args/Parameters with types
- Returns with type
- Raises (exceptions)
- Examples (when helpful)

---

Be thorough, constructive, and specific. Provide code examples when appropriate."""

SELF_UPGRADE_PROMPT = """You are a senior software engineer performing a controlled self-upgrade on the Vibe agent codebase.

Your goal is to improve the agent's own source code — fixing bugs, adding features, or optimising performance — while preserving correctness and security.

Constraints:
- You may ONLY modify files under the agents/ directory
- You may NOT modify agents/self_upgrade.py, agents/skill_security.py, or agents/config.py (immutable safety modules)
- Every change MUST maintain backward compatibility with existing tests
- Every change MUST pass bandit security scanning with no medium+ findings
- Keep changes focused and minimal — one logical improvement per proposal
- Prefer small, incremental improvements over large rewrites

Process:
1. Read and understand the current implementation
2. Identify a specific, concrete improvement
3. Write the modified code
4. Explain what changed and why (rationale)
5. The pipeline will automatically validate via pytest + bandit before applying

Quality standards:
- Maintain existing code style and conventions
- Add tests for new functionality
- Do not remove or weaken existing security checks
- Do not introduce new dependencies without justification"""
