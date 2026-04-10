"""Tests for agents/canonical_harvester — captures fixtures + redacts secrets."""

import pytest

from agents.canonical_harvester import RedactionRefused, _redact


class TestRedaction:
    @pytest.mark.parametrize("text", [
        "The quick brown fox jumps over the lazy dog",
        "def add(a, b): return a + b",
        "When handling code_generation requests, include docstrings",
        "Review the pull request at file path src/main.py line 42",
    ])
    def test_safe_text_passes_through(self, text):
        assert _redact(text) == text

    def test_empty_string_passes_through(self):
        assert _redact("") == ""

    @pytest.mark.parametrize("secret_text", [
        "my key is sk-proj-abcdef1234567890abcdef1234567890",
        "OPENAI_API_KEY=sk-1234567890abcdef1234567890",
        "ANTHROPIC_API_KEY=sk-ant-abc-def-123456789",
        "Authorization: Bearer abc.def.ghi_jklmno-pqrstuv",
        "hello contact me at alice@example.com please",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",  # GitHub PAT prefix
    ])
    def test_secret_patterns_refuse_capture(self, secret_text):
        with pytest.raises(RedactionRefused):
            _redact(secret_text)

    def test_high_entropy_long_token_refuses(self):
        # 48-char alphanumeric that doesn't match a named pattern
        blob = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4"
        with pytest.raises(RedactionRefused):
            _redact(blob)

    def test_short_alphanumeric_is_safe(self):
        assert _redact("id=abc123") == "id=abc123"

    def test_refusal_reason_is_in_exception_message(self):
        try:
            _redact("contact alice@example.com")
        except RedactionRefused as exc:
            assert "email" in str(exc).lower() or "pattern" in str(exc).lower()


import json
from pathlib import Path
import re

from agents.canonical_harvester import (
    _count_fixtures,
    _extract_keywords,
    _new_ulid,
    _update_baseline,
    _utcnow_iso,
)


class TestNewUlid:
    def test_ulid_has_correct_prefix_and_length(self):
        uid = _new_ulid()
        assert uid.startswith("can_")
        assert len(uid) == len("can_") + 26
        # Crockford base32 alphabet
        body = uid[len("can_"):]
        assert re.match(r"^[0-9A-HJKMNP-TV-Z]{26}$", body)

    def test_ulids_are_unique(self):
        seen = {_new_ulid() for _ in range(20)}
        assert len(seen) == 20


class TestCountFixtures:
    def test_count_nonexistent_dir_is_zero(self, tmp_path):
        assert _count_fixtures(tmp_path / "nope") == 0

    def test_count_empty_dir_is_zero(self, tmp_path):
        (tmp_path / "vibe").mkdir()
        assert _count_fixtures(tmp_path / "vibe") == 0

    def test_count_ignores_non_json(self, tmp_path):
        d = tmp_path / "vibe"
        d.mkdir()
        (d / "a.json").write_text("{}")
        (d / "README.md").write_text("notes")
        (d / "baseline.json").write_text("{}")  # baseline is NOT a fixture
        assert _count_fixtures(d) == 1

    def test_count_ignores_baseline_file(self, tmp_path):
        d = tmp_path / "vibe"
        d.mkdir()
        (d / "can_01HZK4XF5N2P3Q8R9S0T1V2W3X.json").write_text("{}")
        (d / "baseline.json").write_text("{}")
        assert _count_fixtures(d) == 1


class TestExtractKeywords:
    def test_returns_list_of_strings(self):
        kws = _extract_keywords("the quick brown fox jumps over the lazy dog")
        assert isinstance(kws, list)
        assert all(isinstance(k, str) for k in kws)

    def test_filters_stopwords(self):
        kws = _extract_keywords("the and of a to in that it is")
        assert kws == []

    def test_extracts_content_words(self):
        kws = _extract_keywords(
            "FastAPI response_model decorator Pydantic BaseModel validation"
        )
        assert "fastapi" in kws or "FastAPI" in kws
        assert any("response_model" in k.lower() for k in kws)

    def test_caps_at_top_n(self):
        text = " ".join(f"word{i}" for i in range(200))
        kws = _extract_keywords(text, top_n=10)
        assert len(kws) <= 10


class TestUpdateBaseline:
    def test_writes_new_baseline_file(self, tmp_path):
        d = tmp_path / "vibe"
        d.mkdir()
        _update_baseline(d, fixture_id="can_01", score=92)
        baseline = json.loads((d / "baseline.json").read_text())
        assert baseline == {"can_01": 92.0}

    def test_updates_existing_baseline_with_ema(self, tmp_path):
        d = tmp_path / "vibe"
        d.mkdir()
        (d / "baseline.json").write_text(json.dumps({"can_01": 90.0}))
        _update_baseline(d, fixture_id="can_01", score=100)
        baseline = json.loads((d / "baseline.json").read_text())
        # EMA alpha=0.3: new = 0.3*100 + 0.7*90 = 30 + 63 = 93.0
        assert abs(baseline["can_01"] - 93.0) < 0.001

    def test_adds_new_fixture_to_existing_baseline(self, tmp_path):
        d = tmp_path / "vibe"
        d.mkdir()
        (d / "baseline.json").write_text(json.dumps({"can_01": 90.0}))
        _update_baseline(d, fixture_id="can_02", score=85)
        baseline = json.loads((d / "baseline.json").read_text())
        assert baseline["can_01"] == 90.0
        assert baseline["can_02"] == 85.0


class TestUtcnowIso:
    def test_format_is_iso_z(self):
        ts = _utcnow_iso()
        # yyyy-mm-ddThh:mm:ssZ or yyyy-mm-ddThh:mm:ss.ffffffZ
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$", ts)


from agents.canonical_harvester import maybe_capture_canonical


class _StubRegistry:
    def __init__(self, mapping):
        self._mapping = mapping

    def adapter_mapping(self):
        return dict(self._mapping)


def _good_state(**overrides):
    base = {
        "routed_task_type": "code_generation",
        "critic_score": 95,
        "user_prompt": "Write a FastAPI endpoint for user login",
        "final_output": "from fastapi import FastAPI\napp = FastAPI()\n",
        "model_id": "vllm-local",
    }
    base.update(overrides)
    return base


class TestMaybeCaptureCanonical:
    def test_happy_path_writes_fixture(self, tmp_path):
        registry = _StubRegistry({"code_generation": "vibe"})
        result = maybe_capture_canonical(
            state=_good_state(),
            task_type_registry=registry,
            fixtures_root=tmp_path,
        )
        assert result is not None
        assert result.exists()
        assert result.parent == tmp_path / "vibe"
        data = json.loads(result.read_text())
        assert data["task_type"] == "code_generation"
        assert data["baseline_score"] == 95
        assert "expected_keywords" in data

    def test_low_score_not_captured(self, tmp_path):
        registry = _StubRegistry({"code_generation": "vibe"})
        result = maybe_capture_canonical(
            state=_good_state(critic_score=85),
            task_type_registry=registry,
            fixtures_root=tmp_path,
            score_threshold=90,
        )
        assert result is None

    def test_missing_task_type_not_captured(self, tmp_path):
        registry = _StubRegistry({})
        result = maybe_capture_canonical(
            state=_good_state(routed_task_type=""),
            task_type_registry=registry,
            fixtures_root=tmp_path,
        )
        assert result is None

    def test_unknown_adapter_not_captured(self, tmp_path):
        registry = _StubRegistry({})  # no mapping for code_generation
        result = maybe_capture_canonical(
            state=_good_state(),
            task_type_registry=registry,
            fixtures_root=tmp_path,
        )
        assert result is None

    def test_cap_reached_not_captured(self, tmp_path):
        registry = _StubRegistry({"code_generation": "vibe"})
        vibe_dir = tmp_path / "vibe"
        vibe_dir.mkdir()
        # Create 20 existing fixtures
        for i in range(20):
            (vibe_dir / f"can_0{i:025d}.json").write_text("{}")
        result = maybe_capture_canonical(
            state=_good_state(),
            task_type_registry=registry,
            fixtures_root=tmp_path,
            cap_per_adapter=20,
        )
        assert result is None

    def test_redaction_refusal_not_captured(self, tmp_path):
        registry = _StubRegistry({"code_generation": "vibe"})
        state = _good_state(user_prompt="API key is sk-proj-abcdef1234567890abcdef1234567890")
        result = maybe_capture_canonical(
            state=state,
            task_type_registry=registry,
            fixtures_root=tmp_path,
        )
        assert result is None

    def test_osfailure_swallowed_returns_none(self, tmp_path, monkeypatch):
        registry = _StubRegistry({"code_generation": "vibe"})

        def raising_write(*args, **kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(Path, "write_text", raising_write)
        # Should not raise — harvester swallows OSError
        result = maybe_capture_canonical(
            state=_good_state(),
            task_type_registry=registry,
            fixtures_root=tmp_path,
        )
        assert result is None

    def test_fixture_has_required_fields(self, tmp_path):
        registry = _StubRegistry({"code_generation": "vibe"})
        result = maybe_capture_canonical(
            state=_good_state(),
            task_type_registry=registry,
            fixtures_root=tmp_path,
        )
        assert result is not None
        data = json.loads(result.read_text())
        for required in ("id", "task_type", "prompt", "expected_keywords",
                         "baseline_score", "model_id", "captured_at"):
            assert required in data, f"missing {required}"

    def test_happy_path_updates_baseline(self, tmp_path):
        registry = _StubRegistry({"code_generation": "vibe"})
        maybe_capture_canonical(
            state=_good_state(),
            task_type_registry=registry,
            fixtures_root=tmp_path,
        )
        baseline_path = tmp_path / "vibe" / "baseline.json"
        assert baseline_path.exists()
        data = json.loads(baseline_path.read_text())
        assert len(data) == 1
        assert next(iter(data.values())) == 95.0
