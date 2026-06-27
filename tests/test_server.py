"""Milestone 7 tests: FastAPI endpoints via TestClient (no live server)."""

import pytest
from fastapi.testclient import TestClient

import server

client = TestClient(server.app)


def test_fetch_endpoint_returns_200():
    """GET /fetch on a reliable URL returns 200 and valid JSON."""
    resp = client.get("/fetch", params={"url": "https://example.com"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"].startswith("https://example.com")


def test_search_endpoint_returns_200():
    """GET /search returns 200 and a results list."""
    resp = client.get("/search", params={"terms": "python", "max_results": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert isinstance(data["results"], list)


def test_fetch_missing_url_returns_error():
    """GET /fetch without the required url param is a 422."""
    resp = client.get("/fetch")
    assert resp.status_code == 422


def test_response_matches_spec():
    """A fetch response carries every required field from spec §2."""
    resp = client.get(
        "/fetch", params={"url": "https://example.com", "return_type": "text"}
    )
    assert resp.status_code == 200
    data = resp.json()
    for field in (
        "url", "domain", "title", "published_date", "author", "fetch_mode",
        "cached", "cached_at", "cache_age_hours", "page_size_chars",
        "return_type", "content", "meta", "error",
    ):
        assert field in data
    for field in ("summary", "text", "links"):
        assert field in data["content"]
    for field in ("source_tier", "is_premium_source", "fetch_mode_reason"):
        assert field in data["meta"]
