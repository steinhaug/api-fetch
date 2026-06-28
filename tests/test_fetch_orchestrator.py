"""Milestone 6 + task-20 tests: fetch() orchestration, new contract, threshold."""

import hashlib
import json
import uuid

import pytest

import config
import db
import fetch_orchestrator as fo
from fetcher import FetchError

# Short article — comfortably under SUMMARY_THRESHOLD_TOKENS → always verbatim.
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
  <a href="https://reuters.com/related-story-2026-06-26">Related coverage</a>
</body></html>
"""

# Long article — over the token threshold so summary logic kicks in.
_LONG_HTML = (
    "<html><head><title>Long Piece</title></head><body><article>"
    + "<p>" + ("The committee deliberated at length over the policy. " * 600) + "</p>"
    + "</article></body></html>"
)

_REQUIRED_FIELDS = (
    "url", "domain", "title", "published_date", "author", "content",
    "content_kind", "verbatim_size_chars", "verbatim_size_tokens", "truncated",
    "links", "fetch_mode", "cached", "cached_at", "cache_age_hours", "meta",
    "error",
)


def _url():
    return f"https://example.com/article-{uuid.uuid4().hex}"


def _hash(url):
    return hashlib.sha256(fo.normalize_url(url).encode("utf-8")).hexdigest()


def _cleanup(url):
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pages WHERE url_hash = %s", (_hash(url),))


def _mock_fetch(monkeypatch, html=_HTML, counter=None, summary_calls=None):
    """Patch request_url + summarize so no network/API is touched."""
    def fake_request(url):
        if counter is not None:
            counter[0] += 1
        return html, "httpx", "httpx_success"

    def fake_summary(text, url):
        if summary_calls is not None:
            summary_calls[0] += 1
        return "FAKE SUMMARY"

    monkeypatch.setattr(fo, "request_url", fake_request)
    monkeypatch.setattr(fo, "summarize", fake_summary)


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
        "links": [{"url": "https://sec.gov/x", "anchor_text": "filing",
                   "domain": "sec.gov", "source_quality": "high",
                   "link_type": "primary_source"}],
        "summary": "cached summary",
        "page_size_chars": 16,
        "page_size_tokens": 4,
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
        result = fo.fetch(url, verbosity="full")
        assert result["cached"] is False
        assert result["error"] is None
        row = db.get_cached_page(_hash(url))
        assert row is not None
        assert row["stripped_text"]
        assert row["page_size_tokens"] is not None and row["page_size_tokens"] > 0
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
        result = fo.fetch(url, verbosity="summary")
        assert result["cached"] is True
        # Short cached page (4 tokens) short-circuits to verbatim.
        assert result["content_kind"] == "verbatim"
        assert result["content"] == "cached body text"
    finally:
        _cleanup(url)


def test_max_age_bypass(monkeypatch):
    url = _url()
    _cleanup(url)
    _write_cache(url)
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
        assert counter[0] == 1
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


def test_short_page_short_circuits_to_verbatim(monkeypatch):
    """A short page returns verbatim even with verbosity=summary, no Haiku call."""
    url = _url()
    _cleanup(url)
    summary_calls = [0]
    _mock_fetch(monkeypatch, summary_calls=summary_calls)
    try:
        result = fo.fetch(url, verbosity="summary")
        assert result["content_kind"] == "verbatim"
        assert "central bank" in result["content"]
        assert summary_calls[0] == 0  # Haiku skipped entirely
        assert result["verbatim_size_tokens"] <= config.SUMMARY_THRESHOLD_TOKENS
    finally:
        _cleanup(url)


def test_long_page_full_is_verbatim(monkeypatch):
    url = _url()
    _cleanup(url)
    summary_calls = [0]
    _mock_fetch(monkeypatch, html=_LONG_HTML, summary_calls=summary_calls)
    try:
        result = fo.fetch(url, verbosity="full")
        assert result["content_kind"] == "verbatim"
        assert result["verbatim_size_tokens"] > config.SUMMARY_THRESHOLD_TOKENS
        assert summary_calls[0] == 0  # full never summarizes
    finally:
        _cleanup(url)


def test_long_page_summary_calls_haiku(monkeypatch):
    url = _url()
    _cleanup(url)
    summary_calls = [0]
    _mock_fetch(monkeypatch, html=_LONG_HTML, summary_calls=summary_calls)
    try:
        result = fo.fetch(url, verbosity="summary")
        assert result["content_kind"] == "summary"
        assert result["content"] == "FAKE SUMMARY"
        assert summary_calls[0] == 1
        # verbatim_size_* still reflect the FULL text, not the summary.
        assert result["verbatim_size_tokens"] > config.SUMMARY_THRESHOLD_TOKENS
        assert result["verbatim_size_chars"] > len("FAKE SUMMARY")
    finally:
        _cleanup(url)


def test_include_links_toggle(monkeypatch):
    url = _url()
    _cleanup(url)
    _mock_fetch(monkeypatch)
    try:
        without = fo.fetch(url, verbosity="full", include_links=False)
        assert without["links"] is None
        with_links = fo.fetch(url, verbosity="full", include_links=True, cache_reload=True)
        assert isinstance(with_links["links"], list) and len(with_links["links"]) > 0
        assert "source_quality" in with_links["links"][0]
    finally:
        _cleanup(url)


def test_include_links_from_cache_no_refetch(monkeypatch):
    """include_links on a cached page serves links_json without re-fetching."""
    url = _url()
    _cleanup(url)
    _write_cache(url)

    def explode(u):
        raise AssertionError("must not re-fetch just for links")

    monkeypatch.setattr(fo, "request_url", explode)
    try:
        result = fo.fetch(url, verbosity="summary", include_links=True)
        assert result["cached"] is True
        assert isinstance(result["links"], list)
        assert result["links"][0]["domain"] == "sec.gov"
    finally:
        _cleanup(url)


def test_lazy_summary_backfill_on_cache_hit(monkeypatch):
    """A long cached page with no stored summary summarizes lazily and backfills."""
    url = _url()
    _cleanup(url)
    long_text = "The committee deliberated at length over the policy. " * 600
    _write_cache(
        url,
        stripped_text=long_text,
        summary=None,
        page_size_chars=len(long_text),
        page_size_tokens=5000,
    )
    monkeypatch.setattr(fo, "summarize", lambda t, u: "LAZY SUMMARY")
    try:
        result = fo.fetch(url, verbosity="summary")
        assert result["cached"] is True
        assert result["content_kind"] == "summary"
        assert result["content"] == "LAZY SUMMARY"
        # Backfilled into the row.
        row = db.get_cached_page(_hash(url))
        assert row["summary"] == "LAZY SUMMARY"
    finally:
        _cleanup(url)


def test_return_contract_fields_present(monkeypatch):
    url = _url()
    _cleanup(url)
    _mock_fetch(monkeypatch)
    try:
        result = fo.fetch(url, verbosity="full", include_links=True)
        for field in _REQUIRED_FIELDS:
            assert field in result, f"missing {field}"
        for field in ("source_tier", "is_premium_source", "fetch_mode_reason"):
            assert field in result["meta"]
        assert result["truncated"] is False
        # Old contract fields must be gone.
        assert "return_type" not in result
        assert not isinstance(result.get("content"), dict)
    finally:
        _cleanup(url)


def test_error_response_structure(monkeypatch):
    url = _url()
    _cleanup(url)

    def boom(u):
        raise FetchError(u, "httpx_403")

    monkeypatch.setattr(fo, "request_url", boom)
    result = fo.fetch(url, verbosity="summary")

    assert result["error"] is not None
    assert result["content"] is None
    assert result["content_kind"] in ("summary", "verbatim")
    assert result["verbatim_size_chars"] == 0
    assert result["verbatim_size_tokens"] == 0
    assert result["truncated"] is False
    assert result["links"] is None
    for field in _REQUIRED_FIELDS:
        assert field in result
    for field in ("source_tier", "is_premium_source", "fetch_mode_reason"):
        assert field in result["meta"]


def test_author_from_meta_byl():
    html = '<html><head><meta name="byl" content="By Jane Reporter"></head><body><p>x</p></body></html>'
    _, author, _ = fo._extract_metadata(html)
    assert author == "By Jane Reporter"


def test_author_from_json_ld():
    html = """
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"NewsArticle",
     "author":{"@type":"Person","name":"Maria Author"}}
    </script></head><body><p>x</p></body></html>"""
    _, author, _ = fo._extract_metadata(html)
    assert author == "Maria Author"


def test_author_from_body_text_fallback():
    text = (
        "BERLIN, June 26 (Reuters) - Some article body here about cars.\n"
        "Reporting by Thomas Seythal and Christina Amann; Editing by Nick Carey"
    )
    assert fo._byline_from_text(text) == "Thomas Seythal"


def test_login_wall_logged_for_stub(monkeypatch, tmp_path):
    logfile = tmp_path / "login.log"
    monkeypatch.setattr(config, "LOGIN_WALL_LOG", str(logfile))
    stub = (
        "<html><body><h1>Sign in to continue</h1>"
        "<form><input type='password' name='pw'></form>"
        "<p>Subscribe to read this article.</p></body></html>"
    )
    url = _url()
    _cleanup(url)
    _mock_fetch(monkeypatch, html=stub)
    try:
        result = fo.fetch(url, verbosity="full")
        assert result["error"] is None
        assert logfile.exists()
        entry = json.loads(logfile.read_text(encoding="utf-8").strip())
        assert "password_field" in entry["markers"]
    finally:
        _cleanup(url)


def test_login_wall_not_logged_for_full_article(monkeypatch, tmp_path):
    logfile = tmp_path / "login.log"
    monkeypatch.setattr(config, "LOGIN_WALL_LOG", str(logfile))
    body = "<p>" + ("Real article sentence. " * 200) + "Subscribe to our newsletter.</p>"
    html = f"<html><body><article>{body}</article></body></html>"
    url = _url()
    _cleanup(url)
    _mock_fetch(monkeypatch, html=html)
    try:
        fo.fetch(url, verbosity="full")
        assert not logfile.exists()
    finally:
        _cleanup(url)
