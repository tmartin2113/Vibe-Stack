import json
import tempfile
from pathlib import Path

import pytest

from agents.self_upgrade_trigger import SelfUpgradeTrigger, UpgradeSignal


def test_new_signals_get_id_and_null_artifact_ref(tmp_path):
    store = tmp_path / "signals.jsonl"
    trigger = SelfUpgradeTrigger(signal_store_path=str(store))

    sig = UpgradeSignal(
        category="low_score",
        task_type="code_generation",
        detail="test",
        score=40,
        source_node="critic",
    )
    trigger._persist_signals([sig], "code_generation")

    entries = [json.loads(line) for line in store.read_text().splitlines() if line]
    assert len(entries) == 1
    assert entries[0]["id"].startswith("sig_")
    assert entries[0]["artifact_ref"] is None


def test_load_persisted_signals_tolerates_missing_new_fields(tmp_path):
    store = tmp_path / "signals.jsonl"
    # Legacy-format entry (wipe should have caught these, but load should be tolerant)
    store.write_text(json.dumps({
        "category": "low_score",
        "task_type": "code_generation",
        "detail": "legacy",
        "score": 40,
        "source_node": "critic",
        "timestamp": "2026-03-28T00:00:00Z",
    }) + "\n")

    trigger = SelfUpgradeTrigger(signal_store_path=str(store))
    assert trigger.get_signal_count("code_generation") == 1


def test_mark_signal_with_artifact_ref(tmp_path):
    store = tmp_path / "signals.jsonl"
    trigger = SelfUpgradeTrigger(signal_store_path=str(store))
    trigger._persist_signals([
        UpgradeSignal(category="low_score", task_type="t", detail="d", score=40),
    ], "t")

    # Grab the first signal id
    entries = [json.loads(line) for line in store.read_text().splitlines() if line]
    sig_id = entries[0]["id"]

    trigger.mark_artifact_ref([sig_id], artifact_ref="note_abc")

    entries = [json.loads(line) for line in store.read_text().splitlines() if line]
    assert entries[0]["artifact_ref"] == "note_abc"
