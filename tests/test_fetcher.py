"""Milestone 2 tests: request_url() httpx + Playwright fallback.

httpx tests hit example.com (a tiny, reliable public page). Playwright tests
are skipped automatically when Chrome is not running with remote debugging.
"""

import httpx
import pytest

import config
import fetcher
from fetcher import FetchError, request_url


def _chrome_available() -> bool:
    """True if a Chrome CDP endpoint answers at the configured URL."""
    try:
        r = httpx.get(config.CHROME_CDP_URL + "/json/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def test_httpx_success():
    """A reliable public URL returns non-empty HTML."""
    html, _, _ = request_url("https://example.com")
    assert isinstance(html, str)
    assert len(html) > 0


def test_httpx_returns_mode():
    """example.com is served by httpx with the success reason."""
    _, fetch_mode, reason = request_url("https://example.com")
    assert fetch_mode == "httpx"
    assert reason == "httpx_success"


def test_premium_source_uses_playwright(monkeypatch):
    """A domain on PREMIUM_SOURCES is routed straight to Playwright."""
    if not _chrome_available():
        pytest.skip("Chrome CDP not running on " + config.CHROME_CDP_URL)
    monkeypatch.setattr(config, "PREMIUM_SOURCES", ["example.com"])
    _, fetch_mode, reason = request_url("https://example.com")
    assert fetch_mode == "playwright"
    assert reason == "playwright_auth"


def test_fetch_error_on_bad_url():
    """A garbage URL that resolves nowhere raises FetchError."""
    with pytest.raises(FetchError):
        request_url("http://nonexistent-domain-zzz-9999.invalid/")
