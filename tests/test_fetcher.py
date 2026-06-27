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


def test_ensure_chrome_autolaunch(monkeypatch):
    """_ensure_chrome launches the shortcut when CDP is down, then succeeds
    once the port comes up — without requiring a real Chrome."""
    calls = {"started": 0, "probes": 0}

    def fake_ready():
        calls["probes"] += 1
        # Down on the first probe, up after the "launch".
        return calls["started"] > 0

    def fake_startfile(path):
        calls["started"] += 1

    monkeypatch.setattr(fetcher, "_cdp_ready", fake_ready)
    monkeypatch.setattr(fetcher.os, "startfile", fake_startfile, raising=False)
    monkeypatch.setattr(config, "CHROME_AUTOLAUNCH", True)
    monkeypatch.setattr(config, "CHROME_LAUNCH_SHORTCUT", __file__)  # any existing file
    monkeypatch.setattr(config, "CHROME_LAUNCH_WAIT_S", 3)
    monkeypatch.setattr(config, "CHROME_LAUNCH_POLL_S", 0.01)

    assert fetcher._ensure_chrome() is True
    assert calls["started"] == 1


def test_ensure_chrome_disabled(monkeypatch):
    """With autolaunch off and CDP down, _ensure_chrome returns False."""
    monkeypatch.setattr(fetcher, "_cdp_ready", lambda: False)
    monkeypatch.setattr(config, "CHROME_AUTOLAUNCH", False)
    assert fetcher._ensure_chrome() is False


def test_fetch_error_on_bad_url():
    """A garbage URL that resolves nowhere raises FetchError."""
    with pytest.raises(FetchError):
        request_url("http://nonexistent-domain-zzz-9999.invalid/")
