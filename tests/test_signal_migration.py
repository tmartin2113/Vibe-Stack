import json
from pathlib import Path

from scripts.migrate_upgrade_signals import migrate_signals_file


def test_migration_wipes_legacy_signals(tmp_path):
    legacy = tmp_path / "upgrade_signals.jsonl"
    # Seed with a typical stale entry (no id, no artifact_ref)
    legacy.write_text(json.dumps({
        "category": "low_score",
        "task_type": "code_generation",
        "detail": "Score 40/100",
        "score": 40,
        "source_node": "critic",
        "timestamp": "2026-03-28T13:47:53.116358Z",
    }) + "\n")

    result = migrate_signals_file(legacy, wipe=True)

    assert result.wiped_count == 1
    assert result.remaining_count == 0
    assert legacy.read_text() == ""


def test_migration_preserves_new_format_entries(tmp_path):
    legacy = tmp_path / "upgrade_signals.jsonl"
    legacy.write_text(json.dumps({
        "id": "sig_01HZ123",
        "artifact_ref": None,
        "category": "low_score",
        "task_type": "code_generation",
        "detail": "Score 40/100",
        "score": 40,
        "source_node": "critic",
        "timestamp": "2026-04-06T00:00:00Z",
    }) + "\n")

    result = migrate_signals_file(legacy, wipe=False)

    assert result.wiped_count == 0
    assert result.remaining_count == 1
