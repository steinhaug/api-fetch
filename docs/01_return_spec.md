# Document 1: Return Value Specification
## WebFetch API — From the Consumer's Perspective (Claude)

> This document defines the **immutable contract** for what `search()` and `fetch()` must return.
> Claude Code must not alter these structures. All fields marked **required** must always be present.
> Optional fields must be present as `null` if not populated — never omitted.
>
> **Revision note:** `fetch()` now uses a flat `content` field with a `content_kind`
> discriminator and orthogonal `verbosity` / `include_links` controls. This supersedes the
> earlier `return_type` / `content.{summary,text,links}` design. `search()` is unchanged except
> for the reframing of `is_premium_source` (§1).

---

## 1. `search(terms, date_from, date_to, max_results, domains, exclude_domains)`

### Return structure

```json
{
  "query": "Elon Musk Tesla board resignation",
  "cached": true,
  "cached_at": "2026-06-27T14:32:00Z",
  "result_count": 12,
  "results": [
    {
      "rank": 1,
      "url": "https://reuters.com/business/tesla-board-2026-06-25/",
      "domain": "reuters.com",
      "title": "Tesla board member resigns amid shareholder pressure",
      "published_date": "2026-06-25",
      "highlight": "Board member John Doe submitted resignation letter citing...",
      "source_tier": "tier1",
      "is_premium_source": false,
      "fetch_available": true
    }
  ],
  "error": null
}
```

### Field definitions

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | ✅ | The original search terms as submitted |
| `cached` | bool | ✅ | Whether result came from cache |
| `cached_at` | ISO datetime | ✅ | When this search was cached |
| `result_count` | int | ✅ | Total results returned |
| `results[].rank` | int | ✅ | Position in result set (1-based) |
| `results[].url` | string | ✅ | Full URL |
| `results[].domain` | string | ✅ | Bare domain (e.g. `reuters.com`) |
| `results[].title` | string | ✅ | Page title |
| `results[].published_date` | date or null | ✅ | ISO date. `null` if unknown |
| `results[].highlight` | string or null | ✅ | Excerpt most relevant to query |
| `results[].source_tier` | string | ✅ | `tier1`, `tier2`, or `unknown` — the **credibility** axis (see Source Tiers) |
| `results[].is_premium_source` | bool | ✅ | **Access mechanism, not credibility.** `true` = full text is retrievable via the authenticated session. A thin `highlight` on such a result means the snippet is limited, not the article — fetch it to get the whole thing. |
| `results[].fetch_available` | bool | ✅ | Whether `fetch()` can be called on this URL |
| `error` | string or null | ✅ | Error message if search failed, else `null` |

### Source Tiers

```
tier1: reuters.com, apnews.com, ft.com, bloomberg.com, wsj.com,
       washingtonpost.com, nytimes.com, bbc.com, economist.com,
       sec.gov, federalreserve.gov, europa.eu (official/government sources)

tier2: All other recognized news outlets and publications

unknown: Blogs, forums, aggregators, unrecognized domains
```

> `source_tier` is the credibility signal. `is_premium_source` is orthogonal to it — a tier1
> source may or may not be premium, and premium says nothing about trust, only about access.

---

## 2. `fetch(url, verbosity, include_links, cache_reload, max_age_hours)`

`verbosity` is `"summary"` (default) or `"full"`. `include_links` is a bool (default `false`).
These are independent: text verbosity and link inclusion are separate axes.

### Return structure

```json
{
  "url": "https://reuters.com/business/tesla-board-2026-06-25/",
  "domain": "reuters.com",
  "title": "Tesla board member resigns amid shareholder pressure",
  "published_date": "2026-06-25",
  "author": "Jane Smith",

  "content": "Tesla board member John Doe resigned on June 25th. <full verbatim stripped article text when content_kind is verbatim; the Haiku summary when content_kind is summary>",
  "content_kind": "verbatim",
  "verbatim_size_chars": 7360,
  "verbatim_size_tokens": 1840,
  "truncated": false,

  "links": null,

  "fetch_mode": "httpx",
  "cached": true,
  "cached_at": "2026-06-27T14:35:00Z",
  "cache_age_hours": 0.5,

  "meta": {
    "source_tier": "tier1",
    "is_premium_source": false,
    "fetch_mode_reason": "httpx_success"
  },
  "error": null
}
```

### Field definitions

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | ✅ | Canonical URL fetched |
| `domain` | string | ✅ | Bare domain |
| `title` | string or null | ✅ | Page title extracted from HTML |
| `published_date` | date or null | ✅ | ISO date. `null` if not found |
| `author` | string or null | ✅ | Byline if extractable, else `null` |
| `content` | string or null | ✅ | The returned content. What it *is* is given by `content_kind`. `null` only on error. |
| `content_kind` | string or null | ✅ | `"verbatim"` = exact stripped source text, **safe to quote**. `"summary"` = Haiku paraphrase, **triage only, never quote**. `null` only on error. |
| `verbatim_size_chars` | int | ✅ | Size of the **full** stripped text in characters. **Always present**, even when `content_kind="summary"`. |
| `verbatim_size_tokens` | int | ✅ | Tokenizer count of the full stripped text. **Always present.** Use it to decide whether escalating to `verbosity="full"` is worth the tokens *before* paying. |
| `truncated` | bool | ✅ | `true` only if verbatim exceeded the hard cap (`VERBATIM_HARD_CAP_CHARS`). Verbatim is never silently truncated otherwise. |
| `links` | array or null | ✅ | Populated only when `include_links=true`; read from cache, never triggers a re-fetch. `null` otherwise. |
| `fetch_mode` | string | ✅ | `httpx` or `playwright` |
| `cached` | bool | ✅ | Whether content came from cache |
| `cached_at` | ISO datetime or null | ✅ | When this page was cached (`null` on error) |
| `cache_age_hours` | float or null | ✅ | Age of cached content in hours (`null` on error) |
| `meta.source_tier` | string | ✅ | Same tier definition as `search()` |
| `meta.is_premium_source` | bool | ✅ | Access mechanism (see §1) — full text available via authenticated session |
| `meta.fetch_mode_reason` | string | ✅ | Why this fetch mode was chosen (see below) |
| `error` | string or null | ✅ | Error message if fetch failed, else `null` |

### Behavioral contract (verbosity, threshold, citability)

```
content_kind is the field the caller branches on for citability:
  "verbatim" → exact source words, quote/cross-reference freely
  "summary"  → paraphrase, use for triage only

verbosity="full"     → always content_kind="verbatim"
verbosity="summary"  → content_kind="summary" UNLESS the short-circuit fires:

  Short-circuit: if verbatim_size_tokens <= SUMMARY_THRESHOLD_TOKENS (2000),
  the page is returned as content_kind="verbatim" regardless of verbosity.
  No Haiku call is made — summarizing something that small wastes a model
  call and loses fidelity for nothing. The verbatim response IS the signal
  that the short-circuit fired.

verbatim_size_chars / verbatim_size_tokens are ALWAYS populated (both branches),
so the caller can compute the cost of escalating from summary to full.

include_links is independent of verbosity. Links come from the cached
links_json populated at first fetch — requesting them never re-fetches the page.
```

### `fetch_mode_reason` values

```
httpx_success         — httpx worked, no fallback needed
httpx_empty_body      — httpx returned empty/JS-only body, fell back to Playwright
httpx_403             — blocked, fell back to Playwright
httpx_429             — rate limited, fell back to Playwright
playwright_default    — domain is on forced-Playwright list
playwright_auth       — domain requires authenticated Chrome session
```

---

## 3. Link objects (when `include_links=true`)

```json
{
  "url": "https://sec.gov/Archives/edgar/data/...",
  "anchor_text": "Q1 2026 10-K Filing",
  "domain": "sec.gov",
  "source_quality": "high",
  "link_type": "primary_source"
}
```

### Link quality classification

| `source_quality` | Criteria |
|---|---|
| `high` | Domain is tier1, or link points to official document (SEC, government, court filing) |
| `medium` | Domain is tier2, or recognized news outlet |
| `low` | Unknown domain, blog, forum |
| `filtered` | Navigation, social media, ads — **never returned** |

### `link_type` values

```
primary_source    — SEC filings, government docs, official press releases
cross_reference   — Same story on another outlet
background        — Related article on same domain
external          — Outbound link to different domain, purpose unclear
```

### Links that are ALWAYS filtered (never appear in output)

```
- /tag/, /category/, /author/, /search, /archive
- ?utm_*, #section-anchors used for navigation
- subscribe, newsletter, login, signin, register
- facebook.com, twitter.com, x.com, instagram.com, linkedin.com
- javascript:, mailto:, tel:
- CDN domains, image hosts, analytics scripts
- Any URL where domain == same as fetched page AND path matches nav patterns
```

---

## 4. Error responses

Both functions return errors in-band (never raise exceptions to caller).

An error response still carries **every required field** defined in §1/§2 —
populated where known, `null` (or its typed equivalent) where not — plus a
non-null `error` string. The `error` field is the only signal the caller
branches on; the response *shape* never changes. This keeps the contract's
"required fields always present, never omitted" rule true even on failure.

Example — a failed `fetch()`:

```json
{
  "url": "https://example.com/article",
  "domain": "example.com",
  "title": null,
  "published_date": null,
  "author": null,
  "content": null,
  "content_kind": null,
  "verbatim_size_chars": 0,
  "verbatim_size_tokens": 0,
  "truncated": false,
  "links": null,
  "fetch_mode": "playwright",
  "cached": false,
  "cached_at": null,
  "cache_age_hours": null,
  "meta": {
    "source_tier": "unknown",
    "is_premium_source": false,
    "fetch_mode_reason": "httpx_403"
  },
  "error": "fetch_failed: httpx 403, playwright timeout after 30s"
}
```

A failed `search()` follows the same principle: all §1 fields present,
`results` as an empty array, `result_count` 0, and `error` set.

Possible error strings:

```
fetch_failed: [reason]
search_failed: [reason]
cache_read_error: [reason]
parse_failed: trafilatura and bs4 both returned empty
timeout: [mode] exceeded [N]s
```

---

## 5. What Claude will do with these structures

- **`search()` results**: Read `source_tier` and `published_date` to decide which URLs are worth
  calling `fetch()` on. Never fetch all results blindly. Treat `is_premium_source=true` as
  "full text available — a thin highlight here is not a thin article".
- **`fetch(verbosity="summary")`**: Quick triage and initial assessment. Not citable —
  unless `content_kind` comes back `verbatim` (short-circuit on a small page), in which case it is.
- **`fetch(verbosity="full")`**: Verbatim source text for analysis, quoting, and cross-referencing.
  Always `content_kind="verbatim"`.
- **`verbatim_size_tokens`**: Read on any summary response to decide whether a `full` fetch fits
  the budget before paying for it.
- **`content_kind`**: The citability switch. `verbatim` → safe to quote; `summary` → triage only.
- **`include_links=true`**: Follow the evidence trail — high-quality links become candidates for
  further `fetch()` calls. Served from cache; never costs a re-fetch.
- **`cache_age_hours`**: Request `cache_reload=true` or set `max_age_hours` tighter when freshness
  is critical (breaking news, financial data).
- **`error` field**: Handle gracefully — log it and try the next result. Never crash a research
  session on a single fetch failure.
