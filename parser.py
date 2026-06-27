"""Markup stripping and link extraction/classification.

`strip_markup()` turns raw HTML into clean article text (trafilatura first,
BeautifulSoup4 as fallback) and also returns every raw `<a>` href/anchor pair.
`extract_links()` then resolves, filters, classifies, sorts, and caps those
links into the link objects defined in `01_return_spec.md` §3.
"""

import re
from urllib.parse import urljoin, urlparse

import trafilatura
from bs4 import BeautifulSoup

import config
from fetcher import domain_of

# Tags whose text is never article content (used only in the bs4 fallback).
_BOILERPLATE_TAGS = ["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]

# Compiled once: JUNK_PATTERNS applied case-insensitively to the full URL.
_JUNK_RE = [re.compile(p, re.IGNORECASE) for p in config.JUNK_PATTERNS]

_QUALITY_RANK = {"high": 0, "medium": 1, "low": 2}


def strip_markup(html: str) -> tuple[str, list[dict]]:
    """Extract clean text and raw links from HTML.

    Returns `(stripped_text, raw_links)` where raw_links is a list of
    `{"href": ..., "anchor_text": ...}` for every `<a href>` found, before any
    filtering. trafilatura handles article extraction; bs4 is the fallback for
    pages it returns nothing for, and always does link extraction.
    """
    stripped_text = ""
    if html:
        extracted = trafilatura.extract(
            html, include_links=False, include_tables=False
        )
        if extracted:
            stripped_text = extracted

    soup = BeautifulSoup(html or "", "lxml")

    if not stripped_text:
        # Fallback: drop boilerplate tags, then take the visible text.
        for tag in soup(_BOILERPLATE_TAGS):
            tag.decompose()
        stripped_text = soup.get_text(separator="\n", strip=True)
        # Re-parse for links since we just mutated the tree above.
        soup = BeautifulSoup(html or "", "lxml")

    raw_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        raw_links.append({"href": href, "anchor_text": a.get_text(strip=True)})

    return stripped_text, raw_links


def _is_junk(url: str) -> bool:
    """True if the URL matches any JUNK_PATTERN (nav, social, ads, etc.)."""
    return any(rx.search(url) for rx in _JUNK_RE)


def _source_quality(domain: str) -> str:
    """Classify a domain as high / medium / low source quality."""
    if domain in config.TIER1_DOMAINS or domain in config.PRIMARY_SOURCE_DOMAINS:
        return "high"
    if domain in config.TIER2_DOMAINS:
        return "medium"
    return "low"


def _is_nav_noise(parsed, domain: str, base_domain: str, base_path: str) -> bool:
    """True if a same-domain link is a nav/section/menu entry, not an article.

    Cross-domain links are always kept (they carry cross-reference value). For
    same-domain links we keep only article-like URLs. Section landing pages use
    short hyphenated category slugs (``/business/autos-transportation``) with no
    digits, whereas real articles carry a date or numeric id somewhere in the
    path (``...-2026-06-26``, ``/2026/06/26/...``) or a long slug. Self-links to
    the current page are also dropped. This keeps nav chrome from filling the
    50-link cap and crowding out genuine cross-references.
    """
    if domain != base_domain:
        return False
    path = parsed.path.rstrip("/")
    if not path:
        return True  # the domain root itself
    if path == base_path:
        return True  # a link back to the page we are reading
    segments = [s for s in path.split("/") if s]
    last = segments[-1] if segments else ""
    has_digit = any(c.isdigit() for c in path)
    is_article = has_digit or len(last) >= 30
    return not is_article


def _link_type(domain: str, base_domain: str) -> str:
    """Classify the relationship of a link to the page it was found on."""
    if domain in config.PRIMARY_SOURCE_DOMAINS:
        return "primary_source"
    if domain == base_domain:
        return "background"
    if domain in config.TIER1_DOMAINS:
        return "cross_reference"
    return "external"


def extract_links(base_url: str, raw_links: list[dict]) -> list[dict]:
    """Resolve, filter, classify, sort, and cap raw links.

    Returns a list of link objects per `01_return_spec.md` §3, sorted by source
    quality (high → medium → low) and capped at `config.MAX_LINKS`. Junk links
    (navigation, social, ads) and non-http(s) schemes are dropped entirely.
    """
    base_domain = domain_of(base_url)
    base_path = urlparse(base_url).path.rstrip("/")
    seen: set[str] = set()
    links: list[dict] = []

    for raw in raw_links:
        href = (raw.get("href") or "").strip()
        if not href:
            continue
        # Pure in-page anchors ("#main-content") are not real links.
        if href.startswith("#"):
            continue
        anchor = (raw.get("anchor_text") or "").strip()
        # A link with no readable anchor text is low value (and usually chrome).
        if not anchor:
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if _is_junk(absolute):
            continue
        if absolute in seen:
            continue

        domain = domain_of(absolute)
        if not domain:
            continue
        if _is_nav_noise(parsed, domain, base_domain, base_path):
            continue
        seen.add(absolute)

        links.append(
            {
                "url": absolute,
                "anchor_text": anchor,
                "domain": domain,
                "source_quality": _source_quality(domain),
                "link_type": _link_type(domain, base_domain),
            }
        )

    # Stable sort keeps discovery order within each quality band.
    links.sort(key=lambda link: _QUALITY_RANK[link["source_quality"]])
    return links[: config.MAX_LINKS]
