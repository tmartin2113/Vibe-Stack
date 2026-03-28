"""
Unified task type registry — single source of truth for all task types.

Built-in types are registered as defaults.  Skills extend the registry
with custom types declared in SKILL.md frontmatter.  The router, orchestrator,
and workflow factory all read from this registry instead of maintaining
their own hardcoded lists.

This eliminates the split between "real" hardcoded types and "second-class"
skill-injected types.  Every type goes through the same path.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class TaskTypeEntry:
    """A single registered task type."""

    name: str
    description: str
    adapter: str  # specialist adapter name (e.g. "vibe", "test_generator")
    label: str  # human-readable label (e.g. "test generation")
    patterns: List[str] = field(default_factory=list)
    pattern_weights: Dict[str, float] = field(default_factory=dict)
    hybrid_threshold: float = 0.6
    source: str = "builtin"  # "builtin" or "skill"


class TaskTypeRegistry:
    """Central registry of all known task types.

    Provides a unified interface for the router, orchestrator, and
    workflow factory to discover task types and their metadata.
    """

    def __init__(self) -> None:
        self._types: Dict[str, TaskTypeEntry] = {}

    def register(self, entry: TaskTypeEntry) -> None:
        """Register a task type.  Skill entries do not overwrite builtins."""
        if entry.name in self._types and entry.source == "skill":
            existing = self._types[entry.name]
            if existing.source == "builtin":
                logger.debug(
                    "Skipping skill override for builtin type %r", entry.name
                )
                return
        self._types[entry.name] = entry

    def get(self, name: str) -> Optional[TaskTypeEntry]:
        return self._types.get(name)

    def all_types(self) -> Dict[str, TaskTypeEntry]:
        return dict(self._types)

    def adapter_mapping(self) -> Dict[str, str]:
        """Return {task_type: adapter_name} for all types."""
        return {name: e.adapter for name, e in self._types.items()}

    def task_descriptions(self) -> Dict[str, str]:
        """Return {task_type: description} for LLM classifier prompts."""
        return {name: e.description for name, e in self._types.items()}

    def task_labels(self) -> Dict[str, str]:
        """Return {task_type: human_label} for decomposition."""
        return {name: e.label for name, e in self._types.items()}

    def task_patterns(self) -> Dict[str, List[str]]:
        """Return {task_type: [regex_patterns]} for regex classification."""
        return {name: list(e.patterns) for name, e in self._types.items()}

    def pattern_weights(self) -> Dict[str, Dict[str, float]]:
        """Return {task_type: {pattern: weight}} for weighted scoring."""
        return {
            name: dict(e.pattern_weights)
            for name, e in self._types.items()
            if e.pattern_weights
        }

    def hybrid_thresholds(self) -> Dict[str, float]:
        """Return {task_type: threshold} for hybrid mode."""
        return {name: e.hybrid_threshold for name, e in self._types.items()}

    def type_names(self) -> List[str]:
        """Return sorted list of all type names."""
        return sorted(self._types.keys())

    def __len__(self) -> int:
        return len(self._types)

    def __contains__(self, name: str) -> bool:
        return name in self._types


# ── Built-in type definitions ─────────────────────────────────────

def _builtin_entry(
    name: str,
    description: str,
    adapter: str,
    label: str,
    patterns: List[str],
    pattern_weights: Dict[str, float],
    hybrid_threshold: float = 0.6,
) -> TaskTypeEntry:
    return TaskTypeEntry(
        name=name,
        description=description,
        adapter=adapter,
        label=label,
        patterns=patterns,
        pattern_weights=pattern_weights,
        hybrid_threshold=hybrid_threshold,
        source="builtin",
    )


# All 12 built-in types.  Patterns and weights are identical to the
# previously-hardcoded values in RouterNode.__init__.
BUILTIN_TYPES: List[TaskTypeEntry] = [
    _builtin_entry(
        name="test_generation",
        description="Writing unit tests, test cases, test suites (pytest, jest, mocha, etc.)",
        adapter="test_generator",
        label="test generation",
        patterns=[
            r"\bunit test", r"\btest case", r"\btest.*function", r"\btest.*code",
            r"\bpytest", r"\bjest", r"\btest coverage", r"\btest suite",
            r"\bmocha", r"\bvitest", r"\bjunit", r"\bcypress", r"\bplaywright",
            r"\bmock", r"\bstub", r"\bfixture", r"\bintegration test",
            r"\be2e test", r"\bassert",
        ],
        pattern_weights={
            r"\bpytest": 3.0, r"\bjest": 3.0, r"\bmocha": 3.0,
            r"\bvitest": 3.0, r"\bjunit": 3.0, r"\bcypress": 3.0,
            r"\bplaywright": 3.0, r"\btest coverage": 2.5,
            r"\btest suite": 2.5, r"\bintegration test": 2.5,
            r"\be2e test": 2.5, r"\bunit test": 2.0, r"\btest case": 2.0,
            r"\bmock": 1.5, r"\bstub": 1.5, r"\bfixture": 2.0,
            r"\bassert": 1.0, r"\btest.*function": 1.0, r"\btest.*code": 1.0,
        },
        hybrid_threshold=0.5,
    ),
    _builtin_entry(
        name="security_audit",
        description="Security analysis, vulnerability scanning, penetration testing, security reviews",
        adapter="security_auditor",
        label="security audit",
        patterns=[
            r"\bsecurity", r"\bvulnerabilit", r"\bXSS", r"\bSQL injection",
            r"\bCSRF", r"\bsanitize", r"\bvalidate input", r"\bsecurity audit",
            r"\bpenetration test", r"\bOAuth", r"\bJWT", r"\bauthenticat",
            r"\bauthoriz", r"\bencrypt", r"\bOWASP", r"\bCVE",
            r"\bsecret.*manag", r"\baccess.*control",
        ],
        pattern_weights={
            r"\bXSS": 3.0, r"\bSQL injection": 3.0, r"\bCSRF": 3.0,
            r"\bOWASP": 3.0, r"\bCVE": 3.0, r"\bpenetration test": 2.5,
            r"\bsecurity audit": 2.5, r"\bOAuth": 2.5, r"\bJWT": 2.5,
            r"\bvulnerabilit": 2.0, r"\bsanitize": 2.0,
            r"\bvalidate input": 2.0, r"\bencrypt": 2.0,
            r"\baccess.*control": 2.0, r"\bsecret.*manag": 2.0,
            r"\bauthenticat": 1.5, r"\bauthoriz": 1.5, r"\bsecurity": 1.0,
        },
        hybrid_threshold=0.55,
    ),
    _builtin_entry(
        name="documentation",
        description="Writing docs, docstrings, README files, API documentation, code comments",
        adapter="doc_generator",
        label="documentation",
        patterns=[
            r"\bdocument(?!.*(?:database|store|model))", r"\bdocstring", r"\bAPI doc", r"\bREADME",
            r"\bcomment.*code", r"\bexplain.*code", r"\bdocumentation",
            r"\bwrite.*docs", r"\bchangelog", r"\badd.*type hint", r"\bannotat.*exist",
            r"\bJSDoc", r"\bSphinx", r"\bMkDocs",
        ],
        pattern_weights={
            r"\bJSDoc": 3.0, r"\bSphinx": 3.0, r"\bMkDocs": 3.0,
            r"\bdocstring": 2.5, r"\bAPI doc": 2.5, r"\bREADME": 2.5,
            r"\bchangelog": 2.5, r"\bdocumentation": 2.0,
            r"\bwrite.*docs": 2.0, r"\badd.*type hint": 2.0,
            r"\bdocument(?!.*(?:database|store|model))": 1.5, r"\bcomment.*code": 1.5,
            r"\bexplain.*code": 1.5, r"\bannotat.*exist": 1.5,
        },
        hybrid_threshold=0.6,
    ),
    _builtin_entry(
        name="performance_optimization",
        description="Performance tuning, profiling, bottleneck analysis, optimization",
        adapter="performance_optimizer",
        label="performance optimization",
        patterns=[
            r"\boptimiz", r"\bperformance", r"\bspeed.*up", r"\bfaster",
            r"\bbottleneck", r"\bprofile", r"\befficiency",
            r"\bcomplexity.*O\(", r"\bbenchmark", r"\bcach(e|ing)",
            r"\blatency", r"\bthroughput", r"\bmemory.*leak",
            r"\bmemory.*usage", r"\bCPU.*usage", r"\blazy.*load",
        ],
        pattern_weights={
            r"\bbottleneck": 3.0, r"\bmemory.*leak": 3.0, r"\bprofile": 2.5,
            r"\bcomplexity.*O\(": 2.5, r"\bbenchmark": 2.5,
            r"\bperformance": 2.0, r"\boptimiz": 2.0, r"\blatency": 2.0,
            r"\bthroughput": 2.0, r"\bmemory.*usage": 2.0,
            r"\bCPU.*usage": 2.0, r"\bcach(e|ing)": 1.5,
            r"\blazy.*load": 1.5, r"\bspeed.*up": 1.5,
            r"\befficiency": 1.5, r"\bfaster": 1.0,
        },
        hybrid_threshold=0.65,
    ),
    _builtin_entry(
        name="debugging",
        description="Bug fixing, troubleshooting, error investigation, debugging",
        adapter="debugging_assistant",
        label="debugging",
        patterns=[
            r"\bdebug", r"\bfix.*bug", r"\berror(?! handl)", r"\bunhandled.*exception",
            r"\btroubleshoot", r"\broot cause", r"\bwhy.*not.*work",
            r"\bfailing test", r"\bstack.*trace", r"\bsegfault",
            r"\bcrash", r"\bbreakpoint", r"\blog.*error", r"\b500.*error",
        ],
        pattern_weights={
            r"\bsegfault": 3.0, r"\bstack.*trace": 2.5, r"\bdebug": 2.5,
            r"\bfix.*bug": 2.5, r"\broot cause": 2.5, r"\bbreakpoint": 2.5,
            r"\btroubleshoot": 2.0, r"\bfailing test": 2.0,
            r"\bwhy.*not.*work": 2.0, r"\blog.*error": 2.0,
            r"\b500.*error": 2.0, r"\bcrash": 1.5,
            r"\berror(?! handl)": 1.0, r"\bunhandled.*exception": 1.0,
        },
        hybrid_threshold=0.6,
    ),
    _builtin_entry(
        name="refactoring",
        description="Code restructuring, cleaning, improving architecture, removing code smells",
        adapter="vibe",
        label="refactoring",
        patterns=[
            r"\brefactor", r"\bclean.*code", r"\brestructure",
            r"\bimprove.*structure", r"\bcode smell", r"\bDRY", r"\bSOLID",
            r"\bextract.*method", r"\bextract.*class", r"\bdecouple",
            r"\bsimplif", r"\bdead.*code", r"\btechnical.*debt",
        ],
        pattern_weights={
            r"\bcode smell": 3.0, r"\bDRY": 3.0, r"\bSOLID": 3.0,
            r"\btechnical.*debt": 2.5, r"\bextract.*method": 2.5,
            r"\bextract.*class": 2.5, r"\brefactor": 2.5,
            r"\bclean.*code": 2.0, r"\brestructure": 2.0,
            r"\bdecouple": 2.0, r"\bdead.*code": 2.0,
            r"\bimprove.*structure": 1.5, r"\bsimplif": 1.5,
        },
        hybrid_threshold=0.65,
    ),
    _builtin_entry(
        name="code_generation",
        description="Writing new code, implementing features, scaffolding, creating functions",
        adapter="vibe",
        label="code generation",
        patterns=[
            r"\bcreate.*function", r"\bwrite.*(?:a |the )?(?:\w+ )?function",
            r"\bwrite.*code", r"\bimplement",
            r"\bbuild.*feature", r"\bgenerate.*code", r"\bscaffold",
            r"\bboilerplate", r"\bprototype", r"\bclass.*for", r"\bmodule.*for",
            r"\bfunction.*that\b",
        ],
        pattern_weights={
            r"\bscaffold": 2.5, r"\bboilerplate": 2.5, r"\bimplement": 2.5,
            r"\bgenerate.*code": 2.5, r"\bprototype": 2.0,
            r"\bcreate.*function": 2.0, r"\bwrite.*code": 2.0,
            r"\bwrite.*(?:a |the )?(?:\w+ )?function": 2.5,
            r"\bfunction.*that\b": 2.0,
            r"\bclass.*for": 1.5, r"\bmodule.*for": 1.5,
            r"\bbuild.*feature": 1.5,
        },
        hybrid_threshold=0.6,
    ),
    _builtin_entry(
        name="data_processing",
        description="ETL pipelines, data transformation, pandas, data cleaning, CSV/JSON parsing",
        adapter="data_specialist",
        label="data processing",
        patterns=[
            r"\bETL", r"\bdata.*pipeline", r"\bdata.*transform", r"\bpandas",
            r"\bdata.*clean", r"\bdata.*validat", r"\bparse.*CSV",
            r"\bparse.*JSON", r"\bdata.*aggregat", r"\bdata.*wrangl",
            r"\bnumpy", r"\bpolars", r"\bdataframe", r"\bserialization",
            r"\bdata.*migrat",
        ],
        pattern_weights={
            r"\bETL": 3.0, r"\bpandas": 3.0, r"\bnumpy": 3.0,
            r"\bpolars": 3.0, r"\bparse.*CSV": 2.5, r"\bparse.*JSON": 2.5,
            r"\bdata.*pipeline": 2.5, r"\bdataframe": 2.5,
            r"\bdata.*clean": 2.0, r"\bdata.*transform": 2.0,
            r"\bdata.*validat": 2.0, r"\bserialization": 2.0,
            r"\bdata.*migrat": 2.0, r"\bdata.*aggregat": 1.5,
            r"\bdata.*wrangl": 1.5,
        },
        hybrid_threshold=0.5,
    ),
    _builtin_entry(
        name="api_development",
        description="REST/GraphQL APIs, endpoint creation, API design, FastAPI, Flask",
        adapter="api_generator",
        label="API development",
        patterns=[
            r"\bAPI", r"\bREST", r"\bGraphQL", r"\bendpoint",
            r"\bGET.*POST.*PUT", r"\bOpenAPI", r"\bSwagger", r"\bAPI.*route",
            r"\bFastAPI", r"\bFlask.*route", r"\bDjango.*view",
            r"\bExpress.*route", r"\bmiddleware", r"\bHTTP.*status",
            r"\bwebhook", r"\brate.*limit", r"\bCORS",
        ],
        pattern_weights={
            r"\bREST": 3.0, r"\bGraphQL": 3.0, r"\bFastAPI": 3.0,
            r"\bDjango.*view": 3.0, r"\bExpress.*route": 3.0,
            r"\bOpenAPI": 2.5, r"\bSwagger": 2.5, r"\bAPI.*route": 2.5,
            r"\bFlask.*route": 2.5, r"\bwebhook": 2.5, r"\bCORS": 2.5,
            r"\bendpoint": 2.0, r"\bGET.*POST.*PUT": 2.0,
            r"\bmiddleware": 1.5, r"\bHTTP.*status": 1.5,
            r"\brate.*limit": 2.0, r"\bAPI": 1.0,
        },
        hybrid_threshold=0.55,
    ),
    _builtin_entry(
        name="database_operations",
        description="SQL, schema design, query optimization, migrations, database tuning",
        adapter="database_specialist",
        label="database operations",
        patterns=[
            r"\bSQL", r"\bdatabase", r"\bquery.*optimiz", r"\bindex",
            r"\bschema", r"\bmigration", r"\bPostgreSQL", r"\bMySQL",
            r"\bMongoDB", r"\bNoSQL", r"\bORM", r"\bSQLAlchemy",
            r"\bdatabase.*design", r"\bRedis", r"\bSQLite", r"\bAlembic",
            r"\bPrisma", r"\bforeign.*key", r"\bjoin.*query",
            r"\bstored.*procedure",
        ],
        pattern_weights={
            r"\bPostgreSQL": 3.0, r"\bMySQL": 3.0, r"\bMongoDB": 3.0,
            r"\bSQLAlchemy": 3.0, r"\bRedis": 3.0, r"\bSQLite": 3.0,
            r"\bAlembic": 3.0, r"\bPrisma": 3.0, r"\bORM": 2.5,
            r"\bmigration": 2.5, r"\bquery.*optimiz": 2.5,
            r"\bforeign.*key": 2.5, r"\bstored.*procedure": 2.5,
            r"\bjoin.*query": 2.5, r"\bschema": 2.0, r"\bindex": 2.0,
            r"\bdatabase.*design": 2.0, r"\bNoSQL": 2.0,
            r"\bSQL": 1.5, r"\bdatabase": 1.0,
        },
        hybrid_threshold=0.55,
    ),
    _builtin_entry(
        name="code_review",
        description="Code review, explanation, reverse engineering, documentation generation, static analysis",
        adapter="code_reviewer",
        label="code review",
        patterns=[
            # Traditional code review
            r"\bcode.*review", r"\bpull.*request", r"\bPR.*review",
            r"\breview.*code", r"\bcode.*quality", r"\bstatic.*analysis",
            r"\blint", r"\bcode.*style", r"\bbest.*practice",
            r"\bpylint", r"\bESLint", r"\bruff", r"\bflake8", r"\bmypy",
            r"\btype.*check", r"\bcode.*audit",
            # Code explanation
            r"\bexplain.*code", r"\bexplain.*function", r"\bexplain.*class",
            r"\bexplain.*method", r"\bwhat does.*do", r"\bhow does.*work",
            r"\bunderstand.*code", r"\bwalk.*through", r"\bbreak.*down",
            # Reverse engineering
            r"\breverse.*engineer", r"\banalyze.*code", r"\bfigure.*out",
            r"\bdecode.*logic", r"\binterpret.*code",
            # Documentation generation
            r"\badd.*docstring", r"\bgenerate.*docstring",
            r"\bdocument.*code", r"\bdocument.*function",
            r"\bdocument.*class", r"\badd.*comment", r"\bgenerate.*doc",
            r"\bwrite.*docstring", r"\bcomprehensive.*docstring",
        ],
        pattern_weights={
            # Traditional code review (strong signals)
            r"\bpull.*request": 3.0, r"\bPR.*review": 3.0,
            r"\bpylint": 3.0, r"\bESLint": 3.0, r"\bruff": 3.0,
            r"\bflake8": 3.0, r"\bmypy": 3.0, r"\bcode.*review": 2.5,
            r"\bstatic.*analysis": 2.5, r"\bcode.*audit": 2.5,
            r"\breview.*code": 2.0, r"\bcode.*quality": 2.0,
            r"\blint": 2.0, r"\btype.*check": 2.0,
            r"\bbest.*practice": 1.5, r"\bcode.*style": 1.5,
            # Code explanation (strong signals)
            r"\bexplain.*function": 3.0, r"\bexplain.*class": 3.0,
            r"\bwhat does.*do": 2.5, r"\bhow does.*work": 2.5,
            r"\bexplain.*code": 2.5, r"\bexplain.*method": 2.5,
            r"\bunderstand.*code": 2.0, r"\bwalk.*through": 2.0,
            r"\bbreak.*down": 1.5,
            # Reverse engineering (medium-strong signals)
            r"\breverse.*engineer": 3.0, r"\banalyze.*code": 2.5,
            r"\bdecode.*logic": 2.5, r"\binterpret.*code": 2.0,
            r"\bfigure.*out": 1.5,
            # Documentation generation (strong signals)
            r"\bgenerate.*docstring": 3.0, r"\bwrite.*docstring": 3.0,
            r"\bcomprehensive.*docstring": 3.0, r"\badd.*docstring": 2.5,
            r"\bdocument.*function": 2.5, r"\bdocument.*class": 2.5,
            r"\bdocument.*code": 2.0, r"\bgenerate.*doc": 2.0,
            r"\badd.*comment": 1.5,
        },
        hybrid_threshold=0.5,
    ),
    _builtin_entry(
        name="self_upgrade",
        description="Self-improvement of the agent's own source code — bug fixes, new features, optimisations",
        adapter="self_upgrade",
        label="self upgrade",
        patterns=[
            r"\bself.upgrade", r"\bself.improv", r"\bself.modif",
            r"\bupgrade.*agent", r"\bimprove.*agent", r"\bmodify.*agent",
            r"\bagent.*upgrade", r"\bagent.*improv", r"\bagent.*refactor",
            r"\bself.evolv", r"\bself.patch", r"\bauto.upgrade",
            r"\bbootstrap", r"\bself.heal",
        ],
        pattern_weights={
            r"\bself.upgrade": 3.0, r"\bself.improv": 3.0,
            r"\bself.modif": 3.0, r"\bself.evolv": 3.0,
            r"\bupgrade.*agent": 2.5, r"\bimprove.*agent": 2.5,
            r"\bagent.*upgrade": 2.5, r"\bagent.*refactor": 2.5,
            r"\bself.patch": 2.5, r"\bauto.upgrade": 2.5,
            r"\bmodify.*agent": 2.0, r"\bagent.*improv": 2.0,
            r"\bbootstrap": 1.5, r"\bself.heal": 1.5,
        },
        hybrid_threshold=0.7,
    ),
    _builtin_entry(
        name="general",
        description="General tasks that don't fit specific categories",
        adapter="vibe",
        label="general development",
        patterns=[],
        pattern_weights={},
        hybrid_threshold=0.7,
    ),
]


def create_default_registry() -> TaskTypeRegistry:
    """Create a registry pre-populated with all 12 built-in task types."""
    registry = TaskTypeRegistry()
    for entry in BUILTIN_TYPES:
        registry.register(entry)
    return registry


def populate_from_skill_registry(
    task_registry: TaskTypeRegistry,
    skill_registry: Any,
) -> int:
    """Inject custom task types from loaded skills into the task type registry.

    Args:
        task_registry: The task type registry to populate.
        skill_registry: A SkillRegistry instance.

    Returns:
        Number of new types injected.
    """
    custom_types = skill_registry.get_all_custom_task_types()
    injected = 0

    for task_type, description in custom_types.items():
        if task_type in task_registry:
            continue

        entry = TaskTypeEntry(
            name=task_type,
            description=description,
            adapter="vibe",  # skill's adapter_prompt overrides at execution
            label=task_type.replace("_", " "),
            patterns=[],  # LLM-only classification
            pattern_weights={},
            hybrid_threshold=0.6,
            source="skill",
        )
        task_registry.register(entry)
        injected += 1

    if injected > 0:
        logger.info("Injected %d custom task type(s) from skills", injected)

    return injected
