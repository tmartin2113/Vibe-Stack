"""
Skill Registry Utilities

Standalone helper functions extracted from SkillRegistry for
frontmatter parsing and task-type inference.
"""

from typing import Dict, List, Optional


def parse_frontmatter(content: str) -> Dict[str, str]:
    """
    Extract key-value pairs from YAML frontmatter.

    Handles multiline values using YAML block scalar indicators
    (>-, >, |) by joining continuation lines (indented lines that
    follow a key).
    """
    if not content.startswith("---"):
        return {}
    try:
        end = content.index("---", 3)
    except ValueError:
        return {}

    metadata: Dict[str, str] = {}
    current_key: Optional[str] = None
    current_value_lines: list = []

    for line in content[3:end].strip().split("\n"):
        stripped = line.strip()

        # Top-level key: value (not indented)
        if ":" in line and not line[0].isspace():
            # Save previous key if any
            if current_key is not None:
                metadata[current_key] = " ".join(current_value_lines).strip()

            key, _, value = stripped.partition(":")
            current_key = key.strip()
            value = value.strip()

            # Skip YAML block scalar indicators (>-, >, |, |-)
            if value in (">-", ">", "|", "|-"):
                current_value_lines = []
            else:
                current_value_lines = [value]

        elif current_key is not None and line[0:1].isspace() and stripped:
            # Continuation line for a multiline value
            current_value_lines.append(stripped)

    # Save the last key
    if current_key is not None:
        metadata[current_key] = " ".join(current_value_lines).strip()

    return metadata


def infer_task_types_from_name(skill_name: str) -> List[str]:
    """Infer task types from a skill name for matching."""
    text = skill_name.replace("-", " ").lower()

    # Keywords are ordered from most specific to least specific.
    # Use multi-word phrases first to avoid short-token collisions
    # (e.g. "doc" previously matched both documentation and document_processing).
    type_keywords = {
        "test_generation": ["testing", "playwright", "webapp testing",
                            "test driven", "tdd"],
        "security_audit": ["security", "audit", "pentest", "vulnerability",
                           "exploit", "recon"],
        "documentation": ["doc coauthoring", "writing", "internal comms"],
        "code_generation": ["code", "builder", "creator"],
        "code_review": ["code review", "receiving code review",
                        "requesting code review"],
        "debugging": ["debugging", "systematic debugging"],
        "planning": ["planning", "brainstorming", "writing plans",
                     "executing plans"],
        "frontend_development": ["frontend", "design", "canvas",
                                 "theme factory", "web artifact", "art",
                                 "brand", "react", "web design"],
        "data_processing": ["xlsx", "spreadsheet", "excel", "csv"],
        "pdf_processing": ["pdf"],
        "presentation": ["pptx", "slides", "presentation"],
        "document_processing": ["docx"],
        "mcp_development": ["mcp"],
        "messaging": ["slack", "gif"],
        "devops": ["git worktree", "worktree", "development branch"],
    }

    matched = []
    for task_type, keywords in type_keywords.items():
        if any(kw in text for kw in keywords):
            matched.append(task_type)
    return matched or ["general"]
