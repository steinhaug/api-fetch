"""The main fetch() function: wires fetcher, parser, summarizer, and cache.

fetch() normalizes the URL, checks the page cache, retrieves and cleans the
page, extracts metadata and links, decides verbatim-vs-summary, persists, and
assembles the response defined in `docs/task-20-webfetch_change_request.md` §2
(which supersedes Doc 1 §2 for fetch). It never raises to the caller: fetch
failures come back as the full error shape with `error` set.

Two orthogonal controls (task-20 §1):
  verbosity      "summary" (default) | "full" — how much text.
  include_links  bool — return outbound links (from cache) or not.

Content is a single `content` field plus a load-bearing `content_kind`
discriminator ("verbatim" = exact source text, safe to quote; "summary" =
Haiku paraphrase, triage only). Pages at/under SUMMARY_THRESHOLD_TOKENS are
returned verbatim without a Haiku call.
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
from tokenizer import count_tokens

logger = logging.getLogger(__name__)

VALID_VERBOSITY = ("summary", "full")

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


def _iso(value) -> str | None:
    """Render a datetime as ISO-8601 with Z, or pass through strings/None."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _resolve_content(verbosity, verbatim_tokens, stripped_text, summary_cached, url):
    """Decide content + content_kind per the threshold short-circuit (task-20 §3).

    Returns `(content, content_kind, summary_to_persist, generated_summary)`.

    - Pages at/under SUMMARY_THRESHOLD_TOKENS are always verbatim — the Haiku
      call is skipped entirely.
    - verbosity="full" is always verbatim.
    - Otherwise a summary is returned; if none is cached it is generated now
      (and flagged so the caller can backfill the row).
    """
    if verbatim_tokens <= config.SUMMARY_THRESHOLD_TOKENS:
        return stripped_text, "verbatim", summary_cached, False
    if verbosity == "full":
        return stripped_text, "verbatim", summary_cached, False
    # summary wanted on a long page
    summary = summary_cached
    generated = False
    if summary is None:
        summary = summarize(stripped_text, url)
        generated = True
    return summary, "summary", summary, generated


def _apply_hard_cap(content, content_kind):
    """Cap verbatim content at the hard safety limit. Returns (content, truncated).

    Verbatim is never truncated silently; `truncated` is True only if the cap is
    actually hit. Summaries are short and never capped.
    """
    if content_kind == "verbatim" and content and len(content) > config.VERBATIM_HARD_CAP_CHARS:
        return content[: config.VERBATIM_HARD_CAP_CHARS], True
    return content, False


def _assemble(
    *, url, domain, title, published_date, author, content, content_kind,
    verbatim_size_chars, verbatim_size_tokens, truncated, links, fetch_mode,
    cached, cached_at, cache_age_hours, source_tier, is_premium, fetch_mode_reason,
) -> dict:
    """Build the task-20 §2 response. Single shape for fresh + cached paths."""
    return {
        "url": url,
        "domain": domain,
        "title": title,
        "published_date": published_date,
        "author": author,
        "content": content,
        "content_kind": content_kind,
        "verbatim_size_chars": verbatim_size_chars,
        "verbatim_size_tokens": verbatim_size_tokens,
        "truncated": truncated,
        "links": links,
        "fetch_mode": fetch_mode,
        "cached": cached,
        "cached_at": cached_at,
        "cache_age_hours": cache_age_hours,
        "meta": {
            "source_tier": source_tier,
            "is_premium_source": is_premium,
            "fetch_mode_reason": fetch_mode_reason,
        },
        "error": None,
    }


def _response_from_row(row, url, verbosity, include_links):
    """Assemble a response from a cached `pages` row, applying task-20 logic.

    Serves links from the stored `links_json` when `include_links` — never
    re-fetches. Lazily generates/backfills a summary or token count if the
    cached row predates this code or was never summarized.
    """
    domain = row.get("domain") or domain_of(url)
    stripped_text = row.get("stripped_text") or ""
    url_hash = row.get("url_hash")

    verbatim_chars = row.get("page_size_chars")
    if verbatim_chars is None:
        verbatim_chars = len(stripped_text)
    verbatim_tokens = row.get("page_size_tokens")
    if verbatim_tokens is None:
        verbatim_tokens = count_tokens(stripped_text)
        if url_hash:
            try:
                db.update_page(url_hash, page_size_tokens=verbatim_tokens)
            except Exception as exc:
                logger.warning("Token backfill failed: %s", exc)

    content, content_kind, summary_persist, generated = _resolve_content(
        verbosity, verbatim_tokens, stripped_text, row.get("summary"), url
    )
    if generated and summary_persist is not None and url_hash:
        try:
            db.update_page(url_hash, summary=summary_persist)
        except Exception as exc:
            logger.warning("Summary backfill failed: %s", exc)

    content, truncated = _apply_hard_cap(content, content_kind)
    published = row.get("published_date")
    return _assemble(
        url=row.get("url") or url,
        domain=domain,
        title=row.get("title"),
        published_date=str(published) if published else None,
        author=row.get("author"),
        content=content,
        content_kind=content_kind,
        verbatim_size_chars=verbatim_chars,
        verbatim_size_tokens=verbatim_tokens,
        truncated=truncated,
        links=row.get("links_json") if include_links else None,
        fetch_mode=row.get("fetch_mode"),
        cached=True,
        cached_at=_iso(row.get("cached_at")),
        cache_age_hours=row.get("cache_age_hours"),
        source_tier=row.get("source_tier") or _source_tier(domain),
        is_premium=bool(row.get("is_premium_source")),
        fetch_mode_reason=row.get("fetch_mode_reason"),
    )


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


def _error_response(url, verbosity, reason) -> dict:
    """Full error shape: every required task-20 §2 field present, `error` set."""
    domain = domain_of(url)
    premium = _is_premium(domain)
    return {
        "url": url,
        "domain": domain,
        "title": None,
        "published_date": None,
        "author": None,
        "content": None,
        # content is null; content_kind echoes the verbosity that was requested.
        "content_kind": "verbatim" if verbosity == "full" else "summary",
        "verbatim_size_chars": 0,
        "verbatim_size_tokens": 0,
        "truncated": False,
        "links": None,
        "fetch_mode": "playwright" if premium or "playwright" in reason else "httpx",
        "cached": False,
        "cached_at": None,
        "cache_age_hours": None,
        "meta": {
            "source_tier": _source_tier(domain),
            "is_premium_source": premium,
            "fetch_mode_reason": reason,
        },
        "error": f"fetch_failed: {reason}",
    }


def fetch(
    url: str,
    verbosity: str = "summary",
    include_links: bool = False,
    cache_reload: bool = False,
    max_age_hours: int = config.CACHE_DEFAULT_MAX_AGE,
) -> dict:
    """Fetch a URL and return clean content per task-20 §2.

    verbosity is "summary" (default, Haiku paraphrase — triage only) or "full"
    (verbatim source text — citable). include_links adds outbound links from
    cache. Short pages (<= SUMMARY_THRESHOLD_TOKENS) always return verbatim.
    All failures are returned in-band via the `error` field.
    """
    if verbosity not in VALID_VERBOSITY:
        verbosity = "summary"

    normalized = normalize_url(url)
    url_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    domain = domain_of(normalized)

    # Cache check.
    if not cache_reload:
        try:
            row = db.get_cached_page(url_hash)
        except Exception as exc:
            logger.warning("Page cache read failed: %s", exc)
            row = None
        if row is not None:
            age = row.get("cache_age_hours")
            if age is not None and age <= max_age_hours:
                return _response_from_row(row, normalized, verbosity, include_links)

    # Retrieve.
    try:
        html, fetch_mode, fetch_mode_reason = request_url(normalized)
    except FetchError as exc:
        return _error_response(normalized, verbosity, exc.reason)
    except Exception as exc:
        logger.warning("Unexpected fetch error for %s: %s", normalized, exc)
        return _error_response(normalized, verbosity, str(exc))

    # Clean + links.
    stripped_text, raw_links = strip_markup(html)
    link_objects = extract_links(normalized, raw_links)

    # Sizes — always computed on the FULL verbatim text, regardless of branch.
    verbatim_chars = len(stripped_text or "")
    verbatim_tokens = count_tokens(stripped_text)

    # Content + content_kind via the threshold short-circuit. On a fresh fetch
    # there is no cached summary, so a long-page summary request summarizes here;
    # short pages skip Haiku entirely.
    content, content_kind, summary_persist, _ = _resolve_content(
        verbosity, verbatim_tokens, stripped_text, None, normalized
    )
    content, truncated = _apply_hard_cap(content, content_kind)

    # Metadata. Fall back to a body-text byline when no structured author tag is
    # present (e.g. Reuters puts "Reporting by ..." in the article body).
    title, author, published_date = _extract_metadata(html)
    if not author:
        author = _byline_from_text(stripped_text)

    source_tier = _source_tier(domain)
    is_premium = _is_premium(domain)

    # Login/auth-wall detection — logged for the operator, never alters the shape.
    markers = _detect_login_wall(html, verbatim_chars)
    if markers:
        _log_login_wall(
            normalized, domain, fetch_mode, fetch_mode_reason,
            verbatim_chars, is_premium, markers,
        )

    # Persist (best-effort). Always stores both size measures and the links so a
    # later include_links request reads them from cache without re-fetching.
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
                "summary": summary_persist,
                "page_size_chars": verbatim_chars,
                "page_size_tokens": verbatim_tokens,
                "fetch_mode": fetch_mode,
                "fetch_mode_reason": fetch_mode_reason,
                "source_tier": source_tier,
                "is_premium_source": is_premium,
            }
        )
    except Exception as exc:
        logger.warning("Page cache write failed for %s: %s", normalized, exc)

    return _assemble(
        url=normalized,
        domain=domain,
        title=title,
        published_date=published_date,
        author=author,
        content=content,
        content_kind=content_kind,
        verbatim_size_chars=verbatim_chars,
        verbatim_size_tokens=verbatim_tokens,
        truncated=truncated,
        links=link_objects if include_links else None,
        fetch_mode=fetch_mode,
        cached=False,
        cached_at=_iso(datetime.now(timezone.utc)),
        cache_age_hours=0.0,
        source_tier=source_tier,
        is_premium=is_premium,
        fetch_mode_reason=fetch_mode_reason,
    )
