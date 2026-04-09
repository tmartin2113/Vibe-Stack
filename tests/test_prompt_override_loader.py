"""Tests for agents/prompt_library — prompt override loader and schema."""

import pytest

from agents.prompt_library import (
    OverrideSchemaError,
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
