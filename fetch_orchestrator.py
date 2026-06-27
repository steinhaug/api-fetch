"""The main fetch() function: wires fetcher, parser, summarizer, and cache.

fetch() normalizes the URL, checks the page cache, retrieves and cleans the
page, extracts metadata and links, summarizes, persists, and assembles the
response defined in `01_return_spec.md` §2. It never raises to the caller:
fetch failures come back as the full §4 error shape with `error` set.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

import config
import db
from fetcher import FetchError, domain_of, request_url
from parser import extract_links, strip_markup
from summarizer import summarize

logger = logging.getLogger(__name__)

VALID_RETURN_TYPES = ("summary", "text", "text+links")

# Query-param keys stripped during normalization (tracking noise).
_TRACKING_KEYS = {"fbclid", "gclid", "ref"}


def normalize_url(url: str) -> str:
    """Strip tracking params (utm_*, fbclid, gclid, ref) and normalize.

    Removes a trailing slash on non-root paths for cache-key consistency so
    `/x` and `/x/` resolve to the same cache entry.
    """
    parsed = urlparse(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_KEYS
    ]
    query = urlencode(kept)
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse(
        (parsed.scheme, parsed.netloc, path, parsed.params, query, "")
    )


def _source_tier(domain: str) -> str:
    if domain in config.TIER1_DOMAINS:
        return "tier1"
    if domain in config.TIER2_DOMAINS:
        return "tier2"
    return "unknown"


def _is_premium(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in config.PREMIUM_SOURCES)


def _clean_name(value) -> str | None:
    """Trim a candidate author string; reject empties, URLs, and over-long blobs."""
    if not value or not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name.startswith("http") or len(name) > 120:
        return None
    return name


def _ld_author(data) -> str | None:
    """Pull an author name out of a JSON-LD object (dict, list, or @graph)."""
    if isinstance(data, list):
        for item in data:
            found = _ld_author(item)
            if found:
                return found
        return None
    if not isinstance(data, dict):
        return None
    if "@graph" in data:
        found = _ld_author(data["@graph"])
        if found:
            return found
    author = data.get("author")
    if isinstance(author, dict):
        return _clean_name(author.get("name"))
    if isinstance(author, list) and author:
        names = [
            a.get("name") if isinstance(a, dict) else a for a in author
        ]
        names = [_clean_name(n) for n in names]
        names = [n for n in names if n]
        if names:
            return ", ".join(names)
    if isinstance(author, str):
        return _clean_name(author)
    return None


# "By Jane Smith" / "Reporting by Thomas Seythal and Christina Amann".
# Note: no IGNORECASE — the name class must stay strictly capitalized so a
# lowercase word like "and" cannot be swallowed into the captured name.
_BYLINE_RE = re.compile(
    r"(?:^|\n)\s*(?:[Rr]eporting\s+by|[Bb]y)\s+"
    r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,3})"
)


def _byline_from_text(stripped_text: str) -> str | None:
    """Last-resort byline: scan the cleaned text for a 'By <Name>' pattern."""
    if not stripped_text:
        return None
    # Look near the top and bottom where bylines live.
    window = stripped_text[:600] + "\n" + stripped_text[-600:]
    m = _BYLINE_RE.search(window)
    if m:
        return _clean_name(m.group(1))
    return None


def _extract_author(soup) -> str | None:
    """Best-effort author extraction across the common HTML conventions."""
    # 1. <meta name="author"> / <meta property="article:author"> / NYT <meta name="byl">
    for attrs in (
        {"name": "author"},
        {"property": "article:author"},
        {"name": "byl"},
        {"property": "og:article:author"},
    ):
        tag = soup.find("meta", attrs=attrs)
        name = _clean_name(tag.get("content")) if tag else None
        if name:
            return name

    # 2. rel="author" link/anchor.
    rel = soup.find(attrs={"rel": lambda v: v and "author" in (v if isinstance(v, list) else [v])})
    if rel:
        name = _clean_name(rel.get_text(strip=True))
        if name:
            return name

    # 3. JSON-LD (schema.org Article/NewsArticle).
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        name = _ld_author(data)
        if name:
            return name

    # 4. A byline-classed element.
    byline = soup.find(attrs={"class": lambda c: c and "byline" in c.lower()})
    if byline:
        return _clean_name(byline.get_text(strip=True))

    return None


def _extract_metadata(html: str) -> tuple[str | None, str | None, str | None]:
    """Pull (title, author, published_date) from HTML via BeautifulSoup."""
    soup = BeautifulSoup(html or "", "lxml")

    title = None
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()

    author = _extract_author(soup)

    published_date = None
    pub_meta = soup.find("meta", attrs={"property": "article:published_time"})
    if pub_meta and pub_meta.get("content"):
        published_date = pub_meta["content"].strip()[:10]
    else:
        time_tag = soup.find("time")
        if time_tag:
            dt = time_tag.get("datetime") or time_tag.get_text(strip=True)
            if dt:
                published_date = dt.strip()[:10]

    return title, author, published_date


def _content_block(return_type: str, summary, text, links) -> dict:
    """Build the `content` block honoring the requested return_type."""
    if return_type == "text":
        return {"summary": None, "text": text, "links": None}
    if return_type == "text+links":
        return {"summary": summary, "text": text, "links": links}
    # default + "summary"
    return {"summary": summary, "text": None, "links": None}


def _iso(value) -> str | None:
    """Render a datetime as ISO-8601 with Z, or pass through strings/None."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _response_from_row(row: dict, url: str, return_type: str) -> dict:
    """Assemble a response dict from a cached `pages` row."""
    domain = row.get("domain") or domain_of(url)
    summary = row.get("summary")
    text = row.get("stripped_text")
    links = row.get("links_json")
    published = row.get("published_date")
    return {
        "url": row.get("url") or url,
        "domain": domain,
        "title": row.get("title"),
        "published_date": str(published) if published else None,
        "author": row.get("author"),
        "fetch_mode": row.get("fetch_mode"),
        "cached": True,
        "cached_at": _iso(row.get("cached_at")),
        "cache_age_hours": row.get("cache_age_hours"),
        "page_size_chars": row.get("page_size_chars") or 0,
        "return_type": return_type,
        "content": _content_block(return_type, summary, text, links),
        "meta": {
            "source_tier": row.get("source_tier") or _source_tier(domain),
            "is_premium_source": bool(row.get("is_premium_source")),
            "fetch_mode_reason": row.get("fetch_mode_reason"),
        },
        "error": None,
    }


def _detect_login_wall(html: str, page_size_chars: int) -> list[str] | None:
    """Heuristic: does this look like a login/auth wall rather than content?

    Triggers when the extracted body is short (< LOGIN_STUB_MAX_CHARS) AND the
    raw HTML carries login/paywall markers, OR a password input is present on a
    short page. Returns the list of matched markers, or None if it looks fine.

    The length guard avoids false positives on full articles that merely host a
    metered-paywall overlay on top of fully-delivered content (e.g. Reuters):
    those have large bodies and are not flagged.
    """
    if page_size_chars >= config.LOGIN_STUB_MAX_CHARS:
        return None
    lowered = (html or "").lower()
    matched = [m for m in config.LOGIN_WALL_MARKERS if m in lowered]
    if 'type="password"' in lowered or "type='password'" in lowered:
        matched.append("password_field")
    return matched or None


def _log_login_wall(url: str, domain: str, fetch_mode: str, reason: str,
                    page_size_chars: int, is_premium: bool, markers: list[str]) -> None:
    """Append one JSON line describing a suspected login wall to the log file."""
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": url,
        "domain": domain,
        "fetch_mode": fetch_mode,
        "fetch_mode_reason": reason,
        "page_size_chars": page_size_chars,
        "is_premium_source": is_premium,
        "markers": markers,
    }
    try:
        with open(config.LOGIN_WALL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # logging must never break a fetch
        logger.warning("Could not write login-wall log: %s", exc)
    logger.warning(
        "Suspected login/auth wall on %s (%d chars, mode=%s, markers=%s). "
        "Check the Chrome session.",
        url, page_size_chars, fetch_mode, markers,
    )


def _error_response(url: str, return_type: str, reason: str) -> dict:
    """Full §4 error shape: every required field present, `error` set."""
    domain = domain_of(url)
    premium = _is_premium(domain)
    return {
        "url": url,
        "domain": domain,
        "title": None,
        "published_date": None,
        "author": None,
        "fetch_mode": "playwright" if premium or "playwright" in reason else "httpx",
        "cached": False,
        "cached_at": None,
        "cache_age_hours": None,
        "page_size_chars": 0,
        "return_type": return_type,
        "content": {"summary": None, "text": None, "links": None},
        "meta": {
            "source_tier": _source_tier(domain),
            "is_premium_source": premium,
            "fetch_mode_reason": reason,
        },
        "error": f"fetch_failed: {reason}",
    }


def fetch(
    url: str,
    return_type: str = "summary",
    cache_reload: bool = False,
    max_age_hours: int = config.CACHE_DEFAULT_MAX_AGE,
) -> dict:
    """Fetch a URL and return cleaned content per `01_return_spec.md` §2.

    return_type is one of "summary", "text", "text+links". When `cache_reload`
    is False and a cached page within `max_age_hours` exists, the cached content
    is returned. All failures are returned in-band via the `error` field.
    """
    if return_type not in VALID_RETURN_TYPES:
        return_type = "summary"

    normalized = normalize_url(url)
    url_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    domain = domain_of(normalized)

    # 3. Cache check.
    if not cache_reload:
        try:
            row = db.get_cached_page(url_hash)
        except Exception as exc:
            logger.warning("Page cache read failed: %s", exc)
            row = None
        if row is not None:
            age = row.get("cache_age_hours")
            if age is not None and age <= max_age_hours:
                return _response_from_row(row, normalized, return_type)

    # 4. Retrieve.
    try:
        html, fetch_mode, fetch_mode_reason = request_url(normalized)
    except FetchError as exc:
        return _error_response(normalized, return_type, exc.reason)
    except Exception as exc:
        logger.warning("Unexpected fetch error for %s: %s", normalized, exc)
        return _error_response(normalized, return_type, str(exc))

    # 5/6. Clean + links.
    stripped_text, raw_links = strip_markup(html)
    link_objects = extract_links(normalized, raw_links)

    # 7. Summarize (best-effort; may be None).
    summary = summarize(stripped_text, normalized)

    # 8. Metadata. Fall back to a body-text byline when no structured author tag
    #    is present (e.g. Reuters puts "Reporting by ..." in the article body).
    title, author, published_date = _extract_metadata(html)
    if not author:
        author = _byline_from_text(stripped_text)

    page_size_chars = len(stripped_text or "")
    source_tier = _source_tier(domain)
    is_premium = _is_premium(domain)

    # 8b. Login/auth-wall detection — logged for the operator, does not alter
    #     the response shape (the contract is immutable).
    markers = _detect_login_wall(html, page_size_chars)
    if markers:
        _log_login_wall(
            normalized, domain, fetch_mode, fetch_mode_reason,
            page_size_chars, is_premium, markers,
        )

    # 9. Persist (best-effort).
    try:
        db.upsert_page(
            {
                "url": normalized,
                "url_hash": url_hash,
                "domain": domain,
                "title": title,
                "author": author,
                "published_date": published_date,
                "raw_html": html,
                "stripped_text": stripped_text,
                "links": link_objects,
                "summary": summary,
                "page_size_chars": page_size_chars,
                "fetch_mode": fetch_mode,
                "fetch_mode_reason": fetch_mode_reason,
                "source_tier": source_tier,
                "is_premium_source": is_premium,
            }
        )
    except Exception as exc:
        logger.warning("Page cache write failed for %s: %s", normalized, exc)

    # 10. Assemble fresh response.
    return {
        "url": normalized,
        "domain": domain,
        "title": title,
        "published_date": published_date,
        "author": author,
        "fetch_mode": fetch_mode,
        "cached": False,
        "cached_at": _iso(datetime.now(timezone.utc)),
        "cache_age_hours": 0.0,
        "page_size_chars": page_size_chars,
        "return_type": return_type,
        "content": _content_block(return_type, summary, stripped_text, link_objects),
        "meta": {
            "source_tier": source_tier,
            "is_premium_source": is_premium,
            "fetch_mode_reason": fetch_mode_reason,
        },
        "error": None,
    }
