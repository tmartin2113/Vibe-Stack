"""One-shot migration for ~/.vibe/skills/upgrade_signals.jsonl.

Wipes stale test data from the March 28 burst and ensures any remaining entries
have the new `id` + `artifact_ref` fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MigrationResult:
    wiped_count: int
    remaining_count: int
    path: Path


def _is_legacy(entry: dict) -> bool:
    """Return True for any entry missing the new id+artifact_ref fields."""
    return "id" not in entry or "artifact_ref" not in entry


def migrate_signals_file(path: Path, wipe: bool = True) -> MigrationResult:
    """Read the JSONL file, optionally wipe legacy entries, rewrite in-place.

    Args:
        path:  Path to the upgrade_signals.jsonl file.
        wipe:  If True (default), legacy entries are dropped. If False, legacy
               entries are back-filled with generated ids and null artifact_ref.

    Returns:
        MigrationResult with counts.
    """
    if not path.exists():
        return MigrationResult(wiped_count=0, remaining_count=0, path=path)

    wiped = 0
    kept: list[dict] = []

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # Corrupt line — drop it
            wiped += 1
            continue

        if _is_legacy(entry):
            if wipe:
                wiped += 1
                continue
            # Backfill mode
            entry["id"] = f"sig_backfill_{len(kept)}"
            entry["artifact_ref"] = None

        kept.append(entry)

    path.write_text("".join(json.dumps(e) + "\n" for e in kept))

    return MigrationResult(
        wiped_count=wiped,
        remaining_count=len(kept),
        path=path,
    )


def main() -> None:
    default_path = Path.home() / ".vibe" / "skills" / "upgrade_signals.jsonl"
    result = migrate_signals_file(default_path, wipe=True)
    print(
        f"Migration complete: wiped={result.wiped_count} "
        f"remaining={result.remaining_count} path={result.path}"
    )


if __name__ == "__main__":
    main()
