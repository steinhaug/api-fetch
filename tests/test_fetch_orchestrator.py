"""Milestone 6 tests: fetch() orchestration, caching, return types, errors."""

import hashlib
import uuid

import pytest

import db
import fetch_orchestrator as fo
from fetcher import FetchError

_HTML = """
<html><head>
  <title>Sample Article Title</title>
  <meta name="author" content="Jane Reporter">
  <meta property="article:published_time" content="2026-06-25T10:00:00Z">
</head><body>
  <article>
    <p>The central bank announced a major policy shift on Wednesday, raising
       benchmark rates by half a point in response to stubborn inflation.
       Officials signaled further tightening could follow if prices keep rising.</p>
  </article>
  <a href="https://sec.gov/filing/x">Primary filing</a>
  <a href="https://reuters.com/related">Related coverage</a>
</body></html>
"""


def _url():
    return f"https://example.com/article-{uuid.uuid4().hex}"


def _hash(url):
    return hashlib.sha256(fo.normalize_url(url).encode("utf-8")).hexdigest()


def _cleanup(url):
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pages WHERE url_hash = %s", (_hash(url),))


def _mock_fetch(monkeypatch, html=_HTML, counter=None):
    """Patch request_url + summarize so no network/API is touched."""
    def fake_request(url):
        if counter is not None:
            counter[0] += 1
        return html, "httpx", "httpx_success"

    monkeypatch.setattr(fo, "request_url", fake_request)
    monkeypatch.setattr(fo, "summarize", lambda text, url: "FAKE SUMMARY")


def _write_cache(url, **overrides):
    data = {
        "url": fo.normalize_url(url),
        "url_hash": _hash(url),
        "domain": "example.com",
        "title": "Cached Title",
        "author": "Cached Author",
        "published_date": "2026-06-20",
        "raw_html": "<html></html>",
        "stripped_text": "cached body text",
        "links": [],
        "summary": "cached summary",
        "page_size_chars": 16,
        "fetch_mode": "httpx",
        "fetch_mode_reason": "httpx_success",
        "source_tier": "unknown",
        "is_premium_source": False,
    }
    data.update(overrides)
    db.upsert_page(data)


def test_cache_miss_fetches_and_caches(monkeypatch):
    url = _url()
    _cleanup(url)
    _mock_fetch(monkeypatch)
    try:
        result = fo.fetch(url, return_type="text")
        assert result["cached"] is False
        assert result["error"] is None
        # Row must now be persisted.
        row = db.get_cached_page(_hash(url))
        assert row is not None
        assert row["stripped_text"]
    finally:
        _cleanup(url)


def test_cache_hit_returns_cached(monkeypatch):
    url = _url()
    _cleanup(url)
    _write_cache(url)

    def explode(u):
        raise AssertionError("request_url should not be called on a cache hit")

    monkeypatch.setattr(fo, "request_url", explode)
    try:
        result = fo.fetch(url, return_type="summary")
        assert result["cached"] is True
        assert result["content"]["summary"] == "cached summary"
    finally:
        _cleanup(url)


def test_max_age_bypass(monkeypatch):
    url = _url()
    _cleanup(url)
    _write_cache(url)
    # Backdate the cached row to 5 hours old.
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pages SET cached_at = NOW() - INTERVAL 5 HOUR WHERE url_hash = %s",
                (_hash(url),),
            )
    counter = [0]
    _mock_fetch(monkeypatch, counter=counter)
    try:
        result = fo.fetch(url, max_age_hours=1)
        assert counter[0] == 1  # fresh fetch triggered
        assert result["cached"] is False
    finally:
        _cleanup(url)


def test_cache_reload_bypasses_cache(monkeypatch):
    url = _url()
    _cleanup(url)
    _write_cache(url)
    counter = [0]
    _mock_fetch(monkeypatch, counter=counter)
    try:
        result = fo.fetch(url, cache_reload=True)
        assert counter[0] == 1
        assert result["cached"] is False
    finally:
        _cleanup(url)


def test_return_type_summary(monkeypatch):
    url = _url()
    _cleanup(url)
    _mock_fetch(monkeypatch)
    try:
        c = fo.fetch(url, return_type="summary")["content"]
        assert c["summary"] is not None
        assert c["text"] is None and c["links"] is None
    finally:
        _cleanup(url)


def test_return_type_text(monkeypatch):
    url = _url()
    _cleanup(url)
    _mock_fetch(monkeypatch)
    try:
        c = fo.fetch(url, return_type="text")["content"]
        assert c["summary"] is None
        assert c["text"] is not None
        assert c["links"] is None
    finally:
        _cleanup(url)


def test_return_type_text_plus_links(monkeypatch):
    url = _url()
    _cleanup(url)
    _mock_fetch(monkeypatch)
    try:
        c = fo.fetch(url, return_type="text+links")["content"]
        assert c["summary"] is not None
        assert c["text"] is not None
        assert isinstance(c["links"], list) and len(c["links"]) > 0
    finally:
        _cleanup(url)


def test_error_response_structure(monkeypatch):
    url = _url()
    _cleanup(url)

    def boom(u):
        raise FetchError(u, "httpx_403")

    monkeypatch.setattr(fo, "request_url", boom)
    result = fo.fetch(url, return_type="summary")

    assert result["error"] is not None
    assert result["content"] == {"summary": None, "text": None, "links": None}
    # Every required top-level field from spec §2/§4 must be present.
    for field in (
        "url", "domain", "title", "published_date", "author", "fetch_mode",
        "cached", "cached_at", "cache_age_hours", "page_size_chars",
        "return_type", "content", "meta", "error",
    ):
        assert field in result
    for field in ("source_tier", "is_premium_source", "fetch_mode_reason"):
        assert field in result["meta"]
