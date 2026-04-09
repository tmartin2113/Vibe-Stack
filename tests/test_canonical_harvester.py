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
