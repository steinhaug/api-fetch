"""Milestone 1 tests: MySQL connectivity and the cache layer.

These hit the live `agentic_webfetch` database configured in `.env`. They write
rows with disposable hashes and clean them up so the cache is not polluted.
"""

import hashlib

import pytest

import db


@pytest.fixture(scope="module", autouse=True)
def _schema():
    """Ensure tables exist before any test in this module runs."""
    db.init_db()


def _cleanup_page(url_hash: str) -> None:
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pages WHERE url_hash = %s", (url_hash,))


def _cleanup_search(query_hash: str) -> None:
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM searches WHERE query_hash = %s", (query_hash,))


def test_connection():
    """A pooled connection can round-trip a trivial query."""
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            assert cur.fetchone()["ok"] == 1


def test_upsert_and_retrieve_page():
    """A written page row reads back with all fields intact."""
    url_hash = hashlib.sha256(b"test-page-roundtrip").hexdigest()
    _cleanup_page(url_hash)
    data = {
        "url": "https://example.com/article",
        "url_hash": url_hash,
        "domain": "example.com",
        "title": "Test Title",
        "author": "Jane Smith",
        "published_date": "2026-06-25",
        "raw_html": "<html>raw</html>",
        "stripped_text": "clean article text",
        "links": [{"url": "https://sec.gov/x", "anchor_text": "filing",
                   "domain": "sec.gov", "source_quality": "high",
                   "link_type": "primary_source"}],
        "summary": "A short summary.",
        "page_size_chars": 18,
        "fetch_mode": "httpx",
        "fetch_mode_reason": "httpx_success",
        "source_tier": "unknown",
        "is_premium_source": False,
    }
    try:
        db.upsert_page(data)
        row = db.get_cached_page(url_hash)
        assert row is not None
        assert row["url"] == data["url"]
        assert row["domain"] == "example.com"
        assert row["title"] == "Test Title"
        assert row["author"] == "Jane Smith"
        assert str(row["published_date"]) == "2026-06-25"
        assert row["stripped_text"] == "clean article text"
        assert row["summary"] == "A short summary."
        assert row["page_size_chars"] == 18
        assert row["fetch_mode"] == "httpx"
        assert row["fetch_mode_reason"] == "httpx_success"
        assert row["links_json"][0]["domain"] == "sec.gov"
        # upsert path: a second write updates rather than duplicating.
        data["title"] = "Updated Title"
        db.upsert_page(data)
        row2 = db.get_cached_page(url_hash)
        assert row2["title"] == "Updated Title"
    finally:
        _cleanup_page(url_hash)


def test_upsert_and_retrieve_search():
    """A written search row reads back with results decoded."""
    query_hash = hashlib.sha256(b"test-search-roundtrip").hexdigest()
    _cleanup_search(query_hash)
    results = [
        {"rank": 1, "url": "https://reuters.com/x", "domain": "reuters.com",
         "title": "Headline", "published_date": "2026-06-25",
         "highlight": "excerpt", "source_tier": "tier1",
         "is_premium_source": False, "fetch_available": True},
    ]
    data = {
        "query_hash": query_hash,
        "query_text": "python programming",
        "date_from": None,
        "date_to": None,
        "max_results": 10,
        "domains": ["reuters.com"],
        "results": results,
        "result_count": 1,
    }
    try:
        db.upsert_search(data)
        row = db.get_cached_search(query_hash)
        assert row is not None
        assert row["query_text"] == "python programming"
        assert row["result_count"] == 1
        assert row["results_json"][0]["domain"] == "reuters.com"
        assert row["domains_filter"] == ["reuters.com"]
    finally:
        _cleanup_search(query_hash)


def test_cache_age():
    """cache_age_hours reflects the row's age. A row aged 5h reads back ~5.0."""
    url_hash = hashlib.sha256(b"test-cache-age").hexdigest()
    _cleanup_page(url_hash)
    data = {
        "url": "https://example.com/age",
        "url_hash": url_hash,
        "domain": "example.com",
        "title": "Age Test",
        "author": None,
        "published_date": None,
        "raw_html": None,
        "stripped_text": "x",
        "links": None,
        "summary": None,
        "page_size_chars": 1,
        "fetch_mode": "httpx",
        "fetch_mode_reason": "httpx_success",
        "source_tier": "unknown",
        "is_premium_source": False,
    }
    try:
        db.upsert_page(data)
        # Fresh write: age is near zero.
        fresh = db.get_cached_page(url_hash)
        assert fresh["cache_age_hours"] is not None
        assert 0 <= fresh["cache_age_hours"] < 0.1
        # Backdate the row 5 hours and confirm the computed age tracks it.
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pages SET cached_at = NOW() - INTERVAL 5 HOUR "
                    "WHERE url_hash = %s",
                    (url_hash,),
                )
        aged = db.get_cached_page(url_hash)
        assert abs(aged["cache_age_hours"] - 5.0) < 0.05
    finally:
        _cleanup_page(url_hash)
