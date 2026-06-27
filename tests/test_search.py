"""Milestone 5 tests: Exa search integration, caching, premium merge."""

import uuid

import pytest

import config
import db
import search


class FakeResult:
    """Minimal stand-in for an exa_py Result."""

    def __init__(self, url, title, published_date=None, highlights=None):
        self.url = url
        self.title = title
        self.published_date = published_date
        self.highlights = highlights or ["highlight excerpt"]


class FakeResp:
    def __init__(self, results):
        self.results = results


class FakeExa:
    """Records call kwargs and returns canned results per call."""

    def __init__(self, main, premium, counter):
        self._main = main
        self._premium = premium
        self._counter = counter
        self.calls = []

    def search_and_contents(self, query, **kwargs):
        self._counter[0] += 1
        self.calls.append(kwargs)
        if kwargs.get("include_domains") == config.PREMIUM_SOURCES:
            return FakeResp(list(self._premium))
        return FakeResp(list(self._main))


def _install_fake(monkeypatch, main, premium=None):
    """Patch search._get_exa to hand out a FakeExa; return (counter, holder)."""
    counter = [0]
    holder = {}

    def factory():
        exa = FakeExa(main, premium or [], counter)
        holder["exa"] = exa
        return exa

    monkeypatch.setattr(search, "_get_exa", factory)
    return counter, holder


def _cleanup(q_hash):
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM searches WHERE query_hash = %s", (q_hash,))


def test_returns_correct_structure():
    """A real search returns every required field from spec §1."""
    if not config.EXA_API_KEY:
        pytest.skip("EXA_API_KEY not configured")
    result = search.search("python programming language", max_results=3)
    for field in ("query", "cached", "cached_at", "result_count", "results", "error"):
        assert field in result
    assert isinstance(result["results"], list)
    if result["results"]:
        r = result["results"][0]
        for field in (
            "rank", "url", "domain", "title", "published_date", "highlight",
            "source_tier", "is_premium_source", "fetch_available",
        ):
            assert field in r


def test_cache_hit(monkeypatch):
    """A repeated query is served from cache without re-calling Exa."""
    terms = "unique query " + uuid.uuid4().hex
    q_hash = search._query_hash(terms, None, None, config.EXA_DEFAULT_RESULTS, None, None)
    _cleanup(q_hash)
    counter, _ = _install_fake(
        monkeypatch, main=[FakeResult("https://reuters.com/a", "A")]
    )
    try:
        first = search.search(terms)
        assert first["cached"] is False
        calls_after_first = counter[0]
        assert calls_after_first > 0
        second = search.search(terms)
        assert second["cached"] is True
        # No further Exa calls on the cache hit.
        assert counter[0] == calls_after_first
    finally:
        _cleanup(q_hash)


def test_date_filter_passed_to_exa(monkeypatch):
    """date_from reaches Exa as start_published_date."""
    terms = "dated query " + uuid.uuid4().hex
    q_hash = search._query_hash(terms, "2026-06-01", None, config.EXA_DEFAULT_RESULTS, None, None)
    _cleanup(q_hash)
    counter, holder = _install_fake(
        monkeypatch, main=[FakeResult("https://reuters.com/a", "A")]
    )
    try:
        search.search(terms, date_from="2026-06-01")
        # The main call must have carried the date filter.
        assert any(
            c.get("start_published_date") == "2026-06-01" for c in holder["exa"].calls
        )
    finally:
        _cleanup(q_hash)


def test_premium_sources_merged(monkeypatch):
    """A premium-source result is flagged is_premium_source=True."""
    terms = "premium query " + uuid.uuid4().hex
    q_hash = search._query_hash(terms, None, None, config.EXA_DEFAULT_RESULTS, None, None)
    _cleanup(q_hash)
    _install_fake(
        monkeypatch,
        main=[FakeResult("https://reuters.com/story", "Main story")],
        premium=[FakeResult("https://washingtonpost.com/x", "WaPo story")],
    )
    try:
        result = search.search(terms)
        wapo = [r for r in result["results"] if r["domain"] == "washingtonpost.com"]
        assert wapo and wapo[0]["is_premium_source"] is True
    finally:
        _cleanup(q_hash)


def test_deduplication(monkeypatch):
    """The same URL returned by both searches appears only once."""
    terms = "dedup query " + uuid.uuid4().hex
    q_hash = search._query_hash(terms, None, None, config.EXA_DEFAULT_RESULTS, None, None)
    _cleanup(q_hash)
    dup = FakeResult("https://washingtonpost.com/dup", "Same Title")
    _install_fake(monkeypatch, main=[dup], premium=[dup])
    try:
        result = search.search(terms)
        urls = [r["url"] for r in result["results"]]
        assert urls.count("https://washingtonpost.com/dup") == 1
    finally:
        _cleanup(q_hash)


def test_source_tier_assigned(monkeypatch):
    """A reuters.com result is tagged source_tier tier1."""
    terms = "tier query " + uuid.uuid4().hex
    q_hash = search._query_hash(terms, None, None, config.EXA_DEFAULT_RESULTS, None, None)
    _cleanup(q_hash)
    _install_fake(monkeypatch, main=[FakeResult("https://reuters.com/t", "T")])
    try:
        result = search.search(terms)
        reuters = [r for r in result["results"] if r["domain"] == "reuters.com"]
        assert reuters and reuters[0]["source_tier"] == "tier1"
    finally:
        _cleanup(q_hash)
