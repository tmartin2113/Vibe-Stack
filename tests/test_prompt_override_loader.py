"""Tests for agents/prompt_library — prompt override loader and schema."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agents.prompt_library import (
    OverrideEntry,
    OverrideSchemaError,
    PromptOverrideLoader,
    validate_override_dict,
)


VALID_MINIMAL = {
    "id": "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X",
    "task_type": "code_generation",
    "append": "When handling code_generation: always include type hints.",
    "signal_refs": ["sig_abc"],
    "author_agent_id": "backend-engineer",
    "author_run_id": "run_01HZK",
    "created_at": "2026-04-09T17:23:00Z",
}


class TestValidateOverrideDict:
    def test_valid_minimal_passes(self):
        validate_override_dict(VALID_MINIMAL, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_missing_id_rejected(self):
        d = {k: v for k, v in VALID_MINIMAL.items() if k != "id"}
        with pytest.raises(OverrideSchemaError, match="missing required field: id"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_invalid_id_format_rejected(self):
        d = dict(VALID_MINIMAL, id="override_1")
        with pytest.raises(OverrideSchemaError, match="id must match"):
            validate_override_dict(d, filename="override_1.yaml")

    def test_filename_mismatch_rejected(self):
        with pytest.raises(OverrideSchemaError, match="filename does not match id"):
            validate_override_dict(VALID_MINIMAL, filename="some_other.yaml")

    def test_empty_append_rejected(self):
        d = dict(VALID_MINIMAL, append="")
        with pytest.raises(OverrideSchemaError, match="append must be non-empty"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_append_over_500_chars_rejected(self):
        d = dict(VALID_MINIMAL, append="x" * 501)
        with pytest.raises(OverrideSchemaError, match="append exceeds 500"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_append_with_nul_byte_rejected(self):
        d = dict(VALID_MINIMAL, append="a\x00b")
        with pytest.raises(OverrideSchemaError, match="NUL byte"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_append_with_triple_backtick_rejected(self):
        d = dict(VALID_MINIMAL, append="try ```python code```")
        with pytest.raises(OverrideSchemaError, match="triple backtick"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_empty_signal_refs_rejected(self):
        d = dict(VALID_MINIMAL, signal_refs=[])
        with pytest.raises(OverrideSchemaError, match="signal_refs must be non-empty"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_invalid_created_at_rejected(self):
        d = dict(VALID_MINIMAL, created_at="yesterday")
        with pytest.raises(OverrideSchemaError, match="created_at must be ISO 8601"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_extra_top_level_key_rejected(self):
        d = dict(VALID_MINIMAL, rogue_field="oops")
        with pytest.raises(OverrideSchemaError, match="unexpected field: rogue_field"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")

    def test_id_with_forbidden_crockford_char_rejected(self):
        # ULID alphabet excludes I, L, O, U. Include one to verify.
        d = dict(VALID_MINIMAL, id="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3I")
        with pytest.raises(OverrideSchemaError, match="id must match"):
            validate_override_dict(d, filename="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3I.yaml")


VALID_YAML_TEXT = """\
id: ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X
task_type: code_generation
append: |
  When handling code_generation: always include type hints.
signal_refs:
  - sig_abc
  - sig_def
author_agent_id: backend-engineer
author_run_id: run_01HZK
created_at: 2026-04-09T17:23:00Z
"""


def _write_override(dir_path: Path, filename: str, content: str = VALID_YAML_TEXT) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / filename
    p.write_text(content)
    return p


class TestPromptOverrideLoader:
    def test_loader_handles_missing_root(self, tmp_path):
        loader = PromptOverrideLoader(root=tmp_path / "does_not_exist")
        assert loader.get_appends_for("code_generation") == []

    def test_loader_handles_empty_root(self, tmp_path):
        (tmp_path / "overrides").mkdir()
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert loader.get_appends_for("code_generation") == []

    def test_loader_loads_valid_override(self, tmp_path):
        task_dir = tmp_path / "overrides" / "code_generation"
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        appends = loader.get_appends_for("code_generation")
        assert len(appends) == 1
        assert "type hints" in appends[0]

    def test_loader_skips_decayed_override(self, tmp_path):
        task_dir = tmp_path / "overrides" / "code_generation"
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")
        (task_dir / "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.decayed").write_text("rev on 2026-04-10\n")
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert loader.get_appends_for("code_generation") == []

    def test_loader_skips_superseded_override(self, tmp_path):
        task_dir = tmp_path / "overrides" / "code_generation"
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")
        (task_dir / "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.superseded").write_text(
            "replaced_by: ovr_01HZL5YF5N2P3Q8R9S0T1V2W3X\n"
        )
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert loader.get_appends_for("code_generation") == []

    def test_loader_skips_malformed_yaml(self, tmp_path, caplog):
        task_dir = tmp_path / "overrides" / "code_generation"
        task_dir.mkdir(parents=True)
        (task_dir / "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml").write_text("not: valid: yaml: [")
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert loader.get_appends_for("code_generation") == []

    def test_loader_skips_schema_violation(self, tmp_path, caplog):
        task_dir = tmp_path / "overrides" / "code_generation"
        # Replace the full append value with a whitespace-only string so that
        # validate_override_dict raises OverrideSchemaError ("append must be non-empty").
        bad = VALID_YAML_TEXT.replace(
            "append: |\n  When handling code_generation: always include type hints.",
            'append: "   "',
        )
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml", bad)
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert loader.get_appends_for("code_generation") == []

    def test_loader_sort_order_is_created_at_ascending(self, tmp_path):
        task_dir = tmp_path / "overrides" / "code_generation"
        later = VALID_YAML_TEXT
        earlier = VALID_YAML_TEXT.replace(
            "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X", "ovr_01HZJ4XF5N2P3Q8R9S0T1V2W3X"
        ).replace("2026-04-09T17:23:00Z", "2026-04-08T10:00:00Z").replace(
            "type hints", "OLDER override"
        )
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml", later)
        _write_override(task_dir, "ovr_01HZJ4XF5N2P3Q8R9S0T1V2W3X.yaml", earlier)
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        appends = loader.get_appends_for("code_generation")
        assert len(appends) == 2
        assert "OLDER" in appends[0]
        assert "type hints" in appends[1]

    def test_loader_ignores_non_yaml_files(self, tmp_path):
        task_dir = tmp_path / "overrides" / "code_generation"
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")
        (task_dir / "README.md").write_text("some notes")
        (task_dir / "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.baseline").write_text("2026-04-09T17:30:00Z 87.3\n")
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert len(loader.get_appends_for("code_generation")) == 1

    def test_loader_ignores_non_ovr_prefixed_files(self, tmp_path):
        task_dir = tmp_path / "overrides" / "code_generation"
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml")
        (task_dir / "something.yaml").write_text(VALID_YAML_TEXT)
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert len(loader.get_appends_for("code_generation")) == 1

    def test_override_entry_is_frozen_dataclass(self):
        entry = OverrideEntry(
            id="ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X",
            task_type="code_generation",
            append="test append",
            signal_refs=("sig_1",),
            author_agent_id="x",
            author_run_id="y",
            created_at="2026-04-09T17:23:00Z",
        )
        with pytest.raises(FrozenInstanceError):
            entry.append = "mutated"  # type: ignore[misc]

    def test_loader_skips_non_utc_created_at(self, tmp_path):
        """Override with non-UTC offset must be skipped, not silently mislabeled."""
        task_dir = tmp_path / "overrides" / "code_generation"
        bad = VALID_YAML_TEXT.replace(
            "2026-04-09T17:23:00Z",
            "2026-04-09T17:23:00+05:30",
        )
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml", bad)
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert loader.get_appends_for("code_generation") == []

    def test_loader_skips_naive_datetime_created_at(self, tmp_path):
        """Override with naive datetime (no Z) must be skipped."""
        task_dir = tmp_path / "overrides" / "code_generation"
        bad = VALID_YAML_TEXT.replace(
            "2026-04-09T17:23:00Z",
            "2026-04-09T17:23:00",
        )
        _write_override(task_dir, "ovr_01HZK4XF5N2P3Q8R9S0T1V2W3X.yaml", bad)
        loader = PromptOverrideLoader(root=tmp_path / "overrides")
        assert loader.get_appends_for("code_generation") == []
