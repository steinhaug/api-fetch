"""Milestone 4 tests: Haiku summarizer.

The truncation and error tests mock the network call. test_returns_string
makes a real Haiku call and is skipped when no API key is configured.
"""

import pytest

import config
import summarizer


def test_returns_string():
    """A real Haiku call on a text block returns a non-empty string."""
    if not config.ANTHROPIC_API_KEY:
        pytest.skip("ANTHROPIC_API_KEY not configured")
    text = (
        "The Federal Reserve held interest rates steady on Wednesday, citing "
        "persistent inflation and a resilient labor market. Chair Jerome Powell "
        "said the committee needs more confidence that inflation is moving "
        "sustainably toward the two percent target before cutting rates. "
        "Markets had widely expected the decision. Analysts now anticipate the "
        "first rate cut could come later in the year if data softens. "
    ) * 3
    result = summarizer.summarize(text, "https://example.com/fed")
    assert isinstance(result, str)
    assert len(result) > 0


def test_truncation(monkeypatch):
    """Input longer than 8000 chars is truncated before reaching Haiku."""
    captured = {}

    def fake_call(system, user):
        captured["user"] = user
        return "ok"

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(summarizer, "_call_haiku", fake_call)

    summarizer.summarize("A" * 20000, "https://example.com/x")
    # Only the page-content portion is the 'A's; it must be capped at 8000.
    assert captured["user"].count("A") == config.SUMMARY_MAX_INPUT_CHARS


def test_api_error_returns_none(monkeypatch):
    """An exception from the API call results in None, not a raise."""
    def boom(system, user):
        raise RuntimeError("api down")

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(summarizer, "_call_haiku", boom)

    assert summarizer.summarize("some text", "https://example.com/x") is None
