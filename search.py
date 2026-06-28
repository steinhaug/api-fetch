"""Exa-backed web search with caching and premium-source merging.

`search()` runs the main Exa query and (unless the caller already constrained
domains) a parallel query restricted to the premium/authenticated sources, then
merges, deduplicates, ranks, and caches the combined result set. The return
shape matches `01_return_spec.md` §1 and never raises to the caller — failures
come back in the `error` field.
"""

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config
import db
from fetcher import domain_of

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_iso(value) -> str:
    """Render a datetime/date/string as an ISO string for the response."""
    if value is None:
        return _now_iso()
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _query_hash(terms, date_from, date_to, max_results, domains, exclude_domains) -> str:
    """SHA-256 over every parameter that changes the result set.

    Including max_results and exclude_domains is required so a call with
    different paging/filters never collides on a stale cache entry built for
    different parameters (see 02_api_spec.md §4.6 step 2).
    """
    parts = [
        terms or "",
        date_from or "",
        date_to or "",
        str(max_results),
        ",".join(sorted(domains or [])),
        ",".join(sorted(exclude_domains or [])),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _source_tier(domain: str) -> str:
    """Map a domain to tier1 / tier2 / unknown."""
    if domain in config.TIER1_DOMAINS:
        return "tier1"
    if domain in config.TIER2_DOMAINS:
        return "tier2"
    return "unknown"


def _is_premium(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in config.PREMIUM_SOURCES)


def _get_exa():
    """Return an Exa client. Isolated so tests can mock it."""
    from exa_py import Exa

    return Exa(config.EXA_API_KEY)


def _run_exa(query, num_results, date_from, date_to, include_domains, exclude_domains):
    """Execute one Exa search_and_contents call and return its results list."""
    client = _get_exa()
    kwargs = {"num_results": num_results, "highlights": True}
    if date_from:
        kwargs["start_published_date"] = date_from
    if date_to:
        kwargs["end_published_date"] = date_to
    if include_domains:
        kwargs["include_domains"] = include_domains
    if exclude_domains:
        kwargs["exclude_domains"] = exclude_domains
    resp = client.search_and_contents(query, **kwargs)
    return resp.results or []


def _result_to_dict(r) -> dict:
    """Convert an Exa result object into a spec result dict (rank set later)."""
    url = getattr(r, "url", "") or ""
    domain = domain_of(url)
    highlights = getattr(r, "highlights", None) or []
    highlight = highlights[0] if highlights else None
    published = getattr(r, "published_date", None)
    published_date = published[:10] if isinstance(published, str) and published else None
    return {
        "rank": None,
        "url": url,
        "domain": domain,
        "title": getattr(r, "title", None) or "",
        "published_date": published_date,
        "highlight": highlight,
        "source_tier": _source_tier(domain),
        "is_premium_source": _is_premium(domain),
        "fetch_available": True,
    }


def _merge(main_results, premium_results) -> list[dict]:
    """Merge main + premium results, dedup by (domain, lowercased title).

    Dedup is by domain+title only — never by highlight. A premium result with a
    thin/empty Exa snippet still survives (task-20 §7.3): the highlight is triage
    signal, not a quality filter, and on a premium source a thin highlight means
    "the snippet is limited, fetch for the full article" (task-20 §6).
    """
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for r in list(main_results) + list(premium_results):
        item = _result_to_dict(r)
        key = (item["domain"], item["title"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)

    for i, item in enumerate(merged, start=1):
        item["rank"] = i
    return merged


def search(
    terms: str,
    date_from: str | None = None,
    date_to: str | None = None,
    max_results: int = config.EXA_DEFAULT_RESULTS,
    domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> dict:
    """Search the web via Exa and return ranked, cached results.

    Returns the structure defined in `01_return_spec.md` §1. On any failure the
    same shape is returned with `results` empty, `result_count` 0, and a
    non-null `error`.
    """
    terms = (terms or "").strip()
    max_results = int(max_results) if max_results else config.EXA_DEFAULT_RESULTS
    domains = domains or None
    exclude_domains = exclude_domains or None

    q_hash = _query_hash(terms, date_from, date_to, max_results, domains, exclude_domains)

    # 1. Cache lookup.
    try:
        cached = db.get_cached_search(q_hash)
    except Exception as exc:
        logger.warning("Search cache read failed: %s", exc)
        cached = None
    if cached and (cached.get("cache_age_hours") or 0) <= config.CACHE_SEARCH_MAX_AGE:
        results = cached.get("results_json") or []
        return {
            "query": terms,
            "cached": True,
            "cached_at": _to_iso(cached.get("cached_at")),
            "result_count": len(results),
            "results": results,
            "error": None,
        }

    # 2/3. Run the main and (conditionally) premium searches in parallel.
    #      The premium search is skipped when the caller already set `domains`.
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            main_future = pool.submit(
                _run_exa, terms, max_results, date_from, date_to, domains, exclude_domains
            )
            premium_future = None
            if not domains:
                premium_future = pool.submit(
                    _run_exa,
                    terms,
                    config.EXA_PREMIUM_RESULTS,
                    date_from,
                    date_to,
                    config.PREMIUM_SOURCES,
                    exclude_domains,
                )
            # The main search is required; its failure fails the call.
            main_results = main_future.result()
            # The premium search is a best-effort enhancement — some Exa plans
            # cannot includeDomains for premium sources (403 SOURCE_NOT_AVAILABLE).
            # Swallow its failure and fall back to main results only.
            premium_results = []
            if premium_future:
                try:
                    premium_results = premium_future.result()
                except Exception as exc:
                    logger.warning("Premium Exa search failed (ignored): %s", exc)
    except Exception as exc:
        logger.warning("Exa search failed for %r: %s", terms, exc)
        return {
            "query": terms,
            "cached": False,
            "cached_at": _now_iso(),
            "result_count": 0,
            "results": [],
            "error": f"search_failed: {exc}",
        }

    # 4. Merge + rank.
    results = _merge(main_results, premium_results)

    # 5. Persist to cache (best-effort; a cache write failure must not fail the
    #    call — the freshly computed results are still returned).
    try:
        db.upsert_search(
            {
                "query_hash": q_hash,
                "query_text": terms[:1000],
                "date_from": date_from,
                "date_to": date_to,
                "max_results": max_results,
                "domains": domains,
                "results": results,
                "result_count": len(results),
            }
        )
    except Exception as exc:
        logger.warning("Search cache write failed: %s", exc)

    return {
        "query": terms,
        "cached": False,
        "cached_at": _now_iso(),
        "result_count": len(results),
        "results": results,
        "error": None,
    }
