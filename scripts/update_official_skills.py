#!/usr/bin/env python3
"""
Sync official skills from vetted remote sources.

Downloads SKILL.md files and supporting assets for each skill,
caches them locally under vibe_skills/official/, and updates
the .index.json so the SkillRegistry can discover them.

Supports 3 locked-down sources:
  1. anthropics/skills    — Anthropic's official skill collection
  2. obra/superpowers     — Development methodology skills
  3. vercel-labs/agent-skills — Open standard skill ecosystem

Usage:
    python scripts/update_official_skills.py --all            # Sync all sources
    python scripts/update_official_skills.py --source anthropics --list
    python scripts/update_official_skills.py --source vercel --only react-best-practices
    python scripts/update_official_skills.py --dry-run --all  # Preview without writing
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Add project root to path for config import
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SKILLS_DIR = PROJECT_ROOT / "vibe_skills" / "official"
INDEX_PATH = PROJECT_ROOT / "vibe_skills" / ".index.json"

# Rate-limit-safe retry settings
MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2  # seconds

logger = logging.getLogger("update_official_skills")


def _get_sources():
    """Get the locked-down skill source configurations."""
    try:
        from agents.config import SkillsConfig
        return SkillsConfig().sources
    except ImportError:
        # Fallback if agents package not importable
        from dataclasses import dataclass

        @dataclass
        class _FallbackSource:
            name: str
            repo: str
            branch: str = "main"
            skills_path: str = "skills"
            trust_level: str = "standard"
            enabled: bool = True

        return [
            _FallbackSource(name="anthropics", repo="anthropics/skills", trust_level="high"),
            _FallbackSource(name="superpowers", repo="obra/superpowers", trust_level="standard"),
            _FallbackSource(name="vercel", repo="vercel-labs/agent-skills", trust_level="standard"),
        ]


def _github_token() -> Optional[str]:
    """Get GitHub token from environment for authenticated requests."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _make_request(url: str, accept: str = "application/json") -> bytes:
    """Make an HTTP request with retry and optional auth."""
    token = _github_token()
    headers = {"Accept": accept, "User-Agent": "vibe-skill-sync/1.0"}
    if token:
        headers["Authorization"] = f"token {token}"

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 403:
                # Rate limit — back off
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning(f"Rate limited (403), retrying in {wait}s...")
                time.sleep(wait)
                continue
            elif e.code == 404:
                raise  # Don't retry 404s
            else:
                wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                logger.warning(f"HTTP {e.code}, retrying in {wait}s...")
                time.sleep(wait)
        except (urllib.error.URLError, OSError) as e:
            last_error = e
            wait = RETRY_BACKOFF_BASE ** (attempt + 1)
            logger.warning(f"Network error: {e}, retrying in {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {last_error}")


def list_remote_skills(source) -> List[str]:
    """Fetch the list of skill directories from a remote source."""
    api_base = f"https://api.github.com/repos/{source.repo}"
    url = f"{api_base}/contents/{source.skills_path}"
    data = json.loads(_make_request(url))
    return sorted(
        item["name"] for item in data
        if item["type"] == "dir"
    )


def list_skill_files(skill_name: str, source) -> List[Dict]:
    """List all files in a skill directory (recursive via tree API)."""
    api_base = f"https://api.github.com/repos/{source.repo}"
    url = f"{api_base}/contents/{source.skills_path}/{skill_name}"
    try:
        data = json.loads(_make_request(url))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise

    files = []
    if isinstance(data, list):
        for item in data:
            if item["type"] == "file":
                files.append({
                    "path": item["name"],
                    "download_url": item.get("download_url"),
                    "size": item.get("size", 0)
                })
            elif item["type"] == "dir":
                sub_files = _list_subdir_files(
                    skill_name, item["name"], source
                )
                files.extend(sub_files)

    return files


def _list_subdir_files(skill_name: str, subdir: str, source) -> List[Dict]:
    """Recursively list files in a subdirectory of a skill."""
    api_base = f"https://api.github.com/repos/{source.repo}"
    url = f"{api_base}/contents/{source.skills_path}/{skill_name}/{subdir}"
    try:
        data = json.loads(_make_request(url))
    except urllib.error.HTTPError:
        return []

    files = []
    if isinstance(data, list):
        for item in data:
            rel_path = f"{subdir}/{item['name']}"
            if item["type"] == "file":
                files.append({
                    "path": rel_path,
                    "download_url": item.get("download_url"),
                    "size": item.get("size", 0)
                })
            elif item["type"] == "dir":
                files.extend(_list_subdir_files(skill_name, rel_path, source))

    return files


def download_skill(skill_name: str, source, dry_run: bool = False) -> bool:
    """
    Download a skill's files from a remote source into vibe_skills/official/.

    Returns True if the skill was successfully downloaded.
    """
    logger.info(f"Syncing skill: {skill_name} (from {source.name})")

    raw_base = (
        f"https://raw.githubusercontent.com/"
        f"{source.repo}/{source.branch}"
    )

    files = list_skill_files(skill_name, source)
    if not files:
        logger.warning(f"  No files found for skill: {skill_name}")
        return False

    # Check SKILL.md exists
    has_skill_md = any(f["path"] == "SKILL.md" for f in files)
    if not has_skill_md:
        logger.warning(f"  Skipping {skill_name}: no SKILL.md found")
        return False

    skill_dir = SKILLS_DIR / skill_name

    if dry_run:
        logger.info(f"  [dry-run] Would download {len(files)} files to {skill_dir}")
        for f in files:
            size_kb = f["size"] / 1024
            logger.info(f"    {f['path']} ({size_kb:.1f}KB)")
        return True

    # Download each file
    skill_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    skipped_large = 0

    for file_info in files:
        file_path = skill_dir / file_info["path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Skip large binary files (fonts, images > 500KB) to keep cache light
        if file_info["size"] > 512_000 and not file_info["path"].endswith(".md"):
            skipped_large += 1
            continue

        download_url = file_info.get("download_url")
        if not download_url:
            download_url = (
                f"{raw_base}/{source.skills_path}"
                f"/{skill_name}/{file_info['path']}"
            )

        try:
            content = _make_request(download_url, accept="*/*")
            file_path.write_bytes(content)
            downloaded += 1
        except Exception as e:
            logger.warning(f"  Failed to download {file_info['path']}: {e}")

    logger.info(
        f"  Downloaded {downloaded} files"
        + (f" (skipped {skipped_large} large binaries)" if skipped_large else "")
    )
    return downloaded > 0


def parse_skill_frontmatter(skill_dir: Path) -> Dict:
    """Parse YAML frontmatter from a SKILL.md file to extract metadata."""
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return {}

    content = skill_file.read_text(encoding="utf-8")

    # Extract YAML frontmatter between --- markers
    if not content.startswith("---"):
        return {"description": "", "name": skill_dir.name}

    try:
        end_marker = content.index("---", 3)
    except ValueError:
        return {"description": "", "name": skill_dir.name}

    frontmatter = content[3:end_marker].strip()

    metadata = {"name": skill_dir.name}
    current_key = None
    current_value_lines = []

    for line in frontmatter.split("\n"):
        stripped = line.strip()

        # Top-level key: value (not indented)
        if ":" in line and not line[0].isspace():
            # Save previous key
            if current_key in ("name", "description"):
                metadata[current_key] = " ".join(current_value_lines).strip()

            key, _, value = stripped.partition(":")
            current_key = key.strip()
            value = value.strip()

            # Skip YAML block scalar indicators
            if value in (">-", ">", "|", "|-"):
                current_value_lines = []
            else:
                current_value_lines = [value]

        elif current_key is not None and line[0:1].isspace() and stripped:
            # Continuation line for multiline value
            current_value_lines.append(stripped)

    # Save the last key
    if current_key in ("name", "description"):
        metadata[current_key] = " ".join(current_value_lines).strip()

    return metadata


def _infer_task_types(skill_name: str, description: str) -> List[str]:
    """Infer task types from skill name and description for index matching."""
    text = f"{skill_name} {description}".lower()

    type_keywords = {
        "test_generation": ["testing", "playwright", "pytest", "unit test"],
        "security_audit": ["security", "audit", "vulnerability"],
        "documentation": ["doc coauthoring", "documentation", "writing", "internal comms"],
        "code_generation": ["code", "build", "create", "generate", "develop"],
        "code_review": ["review", "lint", "quality"],
        "api_development": ["api", "endpoint", "rest", "graphql"],
        "data_processing": ["csv", "xlsx", "spreadsheet", "tabular", "excel"],
        "database_operations": ["database", "sql", "query", "schema"],
        "performance_optimization": ["performance", "optimize", "speed", "profil"],
        "debugging": ["debug", "fix", "troubleshoot"],
        "frontend_development": ["frontend", "ui", "design", "css", "html", "react"],
        "pdf_processing": ["pdf"],
        "presentation": ["presentation", "slides", "deck", "pptx", "powerpoint"],
        "document_processing": ["docx"],
        "mcp_development": ["mcp", "model context protocol", "tool server"],
        "messaging": ["slack", "gif"],
    }

    matched = []
    for task_type, keywords in type_keywords.items():
        if any(kw in text for kw in keywords):
            matched.append(task_type)

    return matched or ["general"]


def update_index(synced_skills: List[str], source):
    """Update the .index.json with metadata for all synced official skills."""
    # Load existing index or create new
    if INDEX_PATH.exists():
        with open(INDEX_PATH, "r") as f:
            index = json.load(f)
    else:
        index = {
            "version": "1.0",
            "last_updated": "",
            "tiers": {
                "official": {"skills": {}},
                "local": {"skills": {}},
                "temp": {"skills": {}}
            }
        }

    official = index["tiers"]["official"]["skills"]

    for skill_name in synced_skills:
        skill_dir = SKILLS_DIR / skill_name
        meta = parse_skill_frontmatter(skill_dir)
        description = meta.get("description", "")
        task_types = _infer_task_types(skill_name, description)

        official[skill_name] = {
            "description": description,
            "task_types": task_types,
            "path": str(skill_dir),
            "usage_count": official.get(skill_name, {}).get("usage_count", 0),
            "avg_score": official.get(skill_name, {}).get("avg_score", 0.0),
            "created_at": official.get(skill_name, {}).get(
                "created_at", datetime.utcnow().isoformat() + "Z"
            ),
            "source": f"https://github.com/{source.repo}",
            "source_name": source.name,
            "trust_level": source.trust_level,
            "synced_at": datetime.utcnow().isoformat() + "Z",
        }

    index["last_updated"] = datetime.utcnow().isoformat() + "Z"

    with open(INDEX_PATH, "w") as f:
        json.dump(index, f, indent=2)

    logger.info(f"Updated index with {len(synced_skills)} skills from {source.name}")


SKILL_SOURCES_DIR = Path(os.environ.get(
    "VIBE_SKILL_SOURCES", "/home/prime/Repos/skill-sources"
))

# Mapping from clone directory names to source metadata
_SOURCE_DIR_MAP = {
    "anthropics-skills": ("anthropics", "anthropics/skills", "high"),
    "obra-superpowers": ("superpowers", "obra/superpowers", "standard"),
    "vercel-agent-skills": ("vercel", "vercel-labs/agent-skills", "standard"),
}


def _build_source_skill_map() -> Dict[str, Tuple[str, str, str]]:
    """
    Build a mapping of skill_name -> (source_name, repo, trust_level)
    by scanning the local clones to see which skills came from where.
    """
    skill_map: Dict[str, Tuple[str, str, str]] = {}
    for clone_dir, (source_name, repo, trust_level) in _SOURCE_DIR_MAP.items():
        skills_path = SKILL_SOURCES_DIR / clone_dir / "skills"
        if not skills_path.is_dir():
            continue
        for entry in skills_path.iterdir():
            if entry.is_dir() and entry.name != "template":
                skill_map[entry.name] = (source_name, repo, trust_level)
    return skill_map


def reindex_from_disk():
    """
    Re-index official skills from disk without making any API calls.

    Scans vibe_skills/official/ for skill directories with SKILL.md,
    parses frontmatter, and updates .index.json. Preserves existing
    usage_count, scores, and avg_score for skills already in the index.
    """
    if not SKILLS_DIR.exists():
        logger.error(f"Skills directory not found: {SKILLS_DIR}")
        sys.exit(1)

    # Load existing index
    if INDEX_PATH.exists():
        with open(INDEX_PATH, "r") as f:
            index = json.load(f)
    else:
        index = {
            "version": "1.0",
            "last_updated": "",
            "tiers": {
                "official": {"skills": {}},
                "local": {"skills": {}},
                "temp": {"skills": {}}
            }
        }

    official = index["tiers"]["official"]["skills"]

    # Build source mapping from local clones
    source_map = _build_source_skill_map()

    # Scan skill directories on disk
    synced = 0
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue

        skill_name = skill_dir.name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            logger.warning(f"  Skipping {skill_name}: no SKILL.md")
            continue

        meta = parse_skill_frontmatter(skill_dir)
        description = meta.get("description", "")
        task_types = _infer_task_types(skill_name, description)

        # Determine source
        source_name, repo, trust_level = source_map.get(
            skill_name, ("unknown", "unknown/unknown", "standard")
        )

        # Preserve existing usage data
        existing = official.get(skill_name, {})

        official[skill_name] = {
            "description": description,
            "task_types": task_types,
            "path": str(skill_dir),
            "usage_count": existing.get("usage_count", 0),
            "avg_score": existing.get("avg_score", 0.0),
            "scores": existing.get("scores", []),
            "created_at": existing.get(
                "created_at", datetime.utcnow().isoformat() + "Z"
            ),
            "last_used": existing.get("last_used", ""),
            "source": f"https://github.com/{repo}",
            "source_name": source_name,
            "trust_level": trust_level,
            "synced_at": datetime.utcnow().isoformat() + "Z",
        }
        synced += 1
        logger.info(f"  Indexed: {skill_name} ({source_name}) -> {task_types}")

    index["last_updated"] = datetime.utcnow().isoformat() + "Z"

    with open(INDEX_PATH, "w") as f:
        json.dump(index, f, indent=2)

    logger.info(f"\nRe-indexed {synced} official skills from disk")
    return synced


def main():
    parser = argparse.ArgumentParser(
        description="Sync official skills from vetted remote sources"
    )
    parser.add_argument(
        "--source", metavar="NAME",
        help="Sync from a specific source (anthropics, superpowers, vercel)"
    )
    parser.add_argument(
        "--all", action="store_true", dest="sync_all",
        help="Sync from all enabled sources"
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Re-index skills from disk (no API calls)"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available skills without downloading"
    )
    parser.add_argument(
        "--only", nargs="*", metavar="SKILL",
        help="Only sync specific skills"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be downloaded without writing files"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s"
    )

    if args.local:
        count = reindex_from_disk()
        print(f"\nRe-indexed {count} skills from disk.")
        return

    if not args.source and not args.sync_all:
        parser.error("Specify --source NAME, --all, or --local")

    token = _github_token()
    if token:
        logger.info("Using authenticated GitHub API (higher rate limit)")
    else:
        logger.info(
            "Using unauthenticated GitHub API (60 req/hr limit). "
            "Set GITHUB_TOKEN for higher limits."
        )

    # Determine which sources to process
    all_sources = _get_sources()
    if args.source:
        sources = [s for s in all_sources if s.name == args.source]
        if not sources:
            valid = ", ".join(s.name for s in all_sources)
            logger.error(f"Unknown source: {args.source}. Valid: {valid}")
            sys.exit(1)
    else:
        sources = [s for s in all_sources if s.enabled]

    total_synced = []
    total_failed = []

    for source in sources:
        print(f"\n--- Source: {source.name} ({source.repo}) ---\n")

        try:
            remote_skills = list_remote_skills(source)
        except Exception as e:
            logger.error(f"Failed to fetch skill list from {source.name}: {e}")
            continue

        if args.list:
            print(f"Available skills ({len(remote_skills)} total):\n")
            local_skills = set()
            if SKILLS_DIR.exists():
                local_skills = {d.name for d in SKILLS_DIR.iterdir() if d.is_dir()}

            for skill in remote_skills:
                status = "  [cached]" if skill in local_skills else ""
                print(f"  {skill}{status}")
            print()
            continue

        # Determine which skills to sync
        if args.only:
            to_sync = [s for s in args.only if s in remote_skills]
            unknown = [s for s in args.only if s not in remote_skills]
            if unknown:
                logger.warning(f"Unknown skills (not in {source.name}): {', '.join(unknown)}")
        else:
            to_sync = remote_skills

        if not to_sync:
            logger.info("No skills to sync.")
            continue

        logger.info(f"Syncing {len(to_sync)} skills from {source.name}...\n")

        synced = []
        failed = []
        for skill_name in to_sync:
            try:
                success = download_skill(skill_name, source, dry_run=args.dry_run)
                if success:
                    synced.append(skill_name)
                else:
                    failed.append(skill_name)
            except Exception as e:
                logger.error(f"  Error syncing {skill_name}: {e}")
                failed.append(skill_name)

        if synced and not args.dry_run:
            update_index(synced, source)

        total_synced.extend(synced)
        total_failed.extend(failed)

    # Summary
    if not args.list:
        print(f"\n{'=' * 50}")
        print(f"Sync complete: {len(total_synced)} synced, {len(total_failed)} failed")
        if total_synced:
            print(f"  Synced: {', '.join(total_synced)}")
        if total_failed:
            print(f"  Failed: {', '.join(total_failed)}")
        print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
