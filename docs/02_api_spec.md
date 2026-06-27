# Document 2: API, Functions, Cache & Database Specification
## WebFetch API — Technical Implementation Specification

---

## 1. Project Structure

```
webfetch/
├── server.py              # FastAPI application entry point
├── config.py              # All configuration constants
├── db.py                  # MySQL connection pool and queries
├── cache.py               # Cache read/write logic
├── fetcher.py             # request_url() — httpx + Playwright logic
├── parser.py              # strip_markup(), extract_links()
├── summarizer.py          # Haiku API calls
├── search.py              # Exa API integration
├── sources.py             # PREMIUM_SOURCES, SOURCE_TIERS, FILTER_RULES
├── mcp_server.py          # MCP tool definitions wrapping the API
└── requirements.txt
```

---

## 2. Database Schema (MySQL)

### Table: `pages`
Stores fetched page content. One row per URL.

```sql
CREATE TABLE pages (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    url             TEXT NOT NULL,
    url_hash        CHAR(64) NOT NULL,          -- SHA-256 of normalized URL
    domain          VARCHAR(255) NOT NULL,
    title           VARCHAR(1000),
    author          VARCHAR(500),
    published_date  DATE,
    raw_html        LONGTEXT,
    stripped_text   LONGTEXT,
    links_json      JSON,                        -- array of link objects
    summary         TEXT,
    page_size_chars INT UNSIGNED,
    fetch_mode      ENUM('httpx','playwright') NOT NULL,
    fetch_mode_reason VARCHAR(100) NOT NULL,
    source_tier     ENUM('tier1','tier2','unknown') NOT NULL DEFAULT 'unknown',
    is_premium_source TINYINT(1) NOT NULL DEFAULT 0,
    cached_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_url_hash (url_hash),
    INDEX idx_domain (domain),
    INDEX idx_cached_at (cached_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### Table: `searches`
Stores search results. One row per unique query.

```sql
CREATE TABLE searches (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    query_hash      CHAR(64) NOT NULL,           -- SHA-256 of normalized query+params
    query_text      VARCHAR(1000) NOT NULL,
    date_from       DATE,
    date_to         DATE,
    max_results     TINYINT UNSIGNED NOT NULL DEFAULT 10,
    domains_filter  JSON,                        -- included domains if specified
    results_json    JSON NOT NULL,               -- full result array (doc 1 structure)
    result_count    TINYINT UNSIGNED NOT NULL,
    cached_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_query_hash (query_hash),
    INDEX idx_cached_at (cached_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

## 3. Configuration (`config.py`)

```python
# Exa API
EXA_API_KEY         = "..."
EXA_DEFAULT_RESULTS = 10
EXA_PREMIUM_RESULTS = 5       # extra results from premium sources

# Haiku API
ANTHROPIC_API_KEY   = "..."
HAIKU_MODEL         = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS    = 512

# Cache TTL defaults (hours)
CACHE_DEFAULT_MAX_AGE   = 24
CACHE_NEWS_MAX_AGE      = 6     # for tier1 news domains
CACHE_SEARCH_MAX_AGE    = 12

# Playwright
CHROME_CDP_URL          = "http://localhost:9222"
PLAYWRIGHT_TIMEOUT_MS   = 30000
PLAYWRIGHT_WAIT_MS      = 1500  # wait after page load for JS

# httpx
HTTPX_TIMEOUT_S         = 15
HTTPX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# MySQL
DB_HOST     = "localhost"
DB_PORT     = 3306
DB_USER     = "..."
DB_PASSWORD = "..."
DB_NAME     = "webfetch"
DB_POOL_SIZE = 5

# Server
API_HOST    = "127.0.0.1"
API_PORT    = 8765

# Sites rendered via your own authenticated Chrome session.
# These are login-gated or JS-heavy for you, so skip httpx and go
# straight to Playwright using the profile you're already signed in to.
# (is_premium_source in the return contract flags results from this list.)
PREMIUM_SOURCES = [
    "washingtonpost.com",
    "nytimes.com",
    "ft.com",
    "wsj.com",
]

# Source tiers
TIER1_DOMAINS = [
    "reuters.com", "apnews.com", "ft.com", "bloomberg.com",
    "wsj.com", "washingtonpost.com", "nytimes.com", "bbc.com",
    "economist.com", "sec.gov", "federalreserve.gov", "europa.eu",
]
```

---

## 4. Function Specifications

### 4.1 `fetch(url, return_type, cache_reload, max_age_hours)`

**Endpoint:** `GET /fetch`

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | URL to fetch |
| `return_type` | string | `"summary"` | `summary`, `text`, or `text+links` |
| `cache_reload` | bool | `false` | Force fresh fetch even if cached |
| `max_age_hours` | int | `24` | Reject cache older than N hours |

**Execution flow:**

```
1. Normalize URL (strip tracking params, trailing slash consistency)
2. Compute SHA-256 of normalized URL
3. If not cache_reload:
     Check pages table by url_hash
     If found AND cached_at > NOW() - max_age_hours:
       Return cached data in requested return_type
4. Call request_url(url)
5. Call strip_markup(html) → stripped_text, raw_links
6. Call extract_links(html, raw_links) → filtered link objects
7. Call summarizer(stripped_text) → summary
8. Write to pages table (upsert on url_hash)
9. Return response per Document 1 spec
```

---

### 4.2 `request_url(url)`

**Not exposed externally. Called by fetch() only.**

**Logic:**

```
1. If domain in PREMIUM_SOURCES:
     Use Playwright with Chrome session
     fetch_mode_reason = "playwright_auth"
     return html, fetch_mode

2. Try httpx GET with HTTPX_HEADERS:
     If status 200 AND len(body) > 500:
       fetch_mode_reason = "httpx_success"
       return html, fetch_mode="httpx"
     If status 403:
       fetch_mode_reason = "httpx_403" → fall through to Playwright
     If status 429:
       fetch_mode_reason = "httpx_429" → fall through to Playwright
     If body < 500 chars (JS-rendered page):
       fetch_mode_reason = "httpx_empty_body" → fall through to Playwright

3. Playwright fallback:
     Connect to Chrome CDP at CHROME_CDP_URL
     Use context = browser.contexts[0]  (existing authenticated session)
     page.goto(url, wait_until="networkidle")
     Wait PLAYWRIGHT_WAIT_MS
     html = page.content()
     fetch_mode_reason = as set above (or "playwright_default")
     return html, fetch_mode="playwright"

4. If Playwright also fails:
     raise FetchError with reason
```

---

### 4.3 `strip_markup(html)`

**Returns:** `stripped_text` (string), `raw_links` (list of raw href+anchor pairs)

**Logic:**

```
1. Try trafilatura.extract(html):
     If returns non-empty string: use as stripped_text
     
2. Fallback to BeautifulSoup4:
     Remove tags: script, style, nav, footer, header, aside, form
     Extract remaining text with get_text(separator="\n", strip=True)
     Use as stripped_text

3. Always use BeautifulSoup4 for link extraction:
     Find all <a href="..."> tags
     Return list of {href, anchor_text} before filtering
```

---

### 4.4 `extract_links(base_url, raw_links)`

**Returns:** filtered list of link objects per Document 1 spec

**Logic:**

```
1. Resolve relative URLs to absolute using base_url
2. Filter out links matching JUNK_PATTERNS (nav, social, ads etc.)
3. For each remaining link:
     Determine domain
     Assign source_quality: high/medium/low
     Assign link_type: primary_source/cross_reference/background/external
4. Return sorted: high quality first, then medium, then low
5. Cap at 50 links total
```

**JUNK_PATTERNS (regex, applied to full URL):**

```python
JUNK_PATTERNS = [
    r'/tag/', r'/category/', r'/author/', r'/search', r'/archive',
    r'\?.*utm_', r'#[a-z\-]+nav',
    r'subscribe', r'newsletter', r'login', r'signin', r'register',
    r'facebook\.com', r'twitter\.com', r'x\.com', r'instagram\.com',
    r'linkedin\.com', r'youtube\.com',
    r'javascript:', r'mailto:', r'tel:',
]
```

---

### 4.5 `summarizer(stripped_text, url)`

**Returns:** summary string

**Haiku prompt:**

```
You are summarizing a web page for a research assistant.
Write a factual, dense summary in 3-5 sentences.
Focus on: who, what, when, where, and key claims or data points.
Do not editorialize. Do not include meta-commentary about the article itself.
Source URL: {url}

Page content:
{stripped_text[:8000]}
```

**Truncate input to 8000 chars before sending to Haiku.**

---

### 4.6 `search(terms, date_from, date_to, max_results, domains, exclude_domains)`

**Endpoint:** `GET /search`

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `terms` | string | required | Search query |
| `date_from` | date | `null` | ISO date — only results after this date |
| `date_to` | date | `null` | ISO date — only results before this date |
| `max_results` | int | `10` | Results from general web |
| `domains` | string[] | `null` | Restrict to these domains |
| `exclude_domains` | string[] | `null` | Exclude these domains |

**Execution flow:**

```
1. Normalize parameters
2. Compute query_hash = SHA-256 of (terms + date_from + date_to + max_results + sorted domains + sorted exclude_domains)
   — every parameter that changes the result set MUST be in the hash, or a
   call with different max_results/exclude_domains will return a stale cached
   result built for different parameters.
3. Check searches table by query_hash
   If found AND cached_at > NOW() - CACHE_SEARCH_MAX_AGE:
     Return cached result

4. Call Exa API — main search:
     query = terms
     numResults = max_results
     startPublishedDate = date_from (if set)
     endPublishedDate = date_to (if set)
     includeDomains = domains (if set)
     excludeDomains = exclude_domains (if set)
     contents.highlights = true

5. Call Exa API — premium source search (parallel):
     Same query
     numResults = EXA_PREMIUM_RESULTS
     includeDomains = PREMIUM_SOURCES
     contents.highlights = true

6. Merge results:
     Deduplicate by domain+title similarity
     Assign rank (main results first, premium appended if not duplicate)
     Assign source_tier and is_premium_source per sources.py

7. Write to searches table
8. Return response per Document 1 spec
```

---

## 5. MCP Server (`mcp_server.py`)

Exposes two MCP tools wrapping the FastAPI endpoints.

```python
@mcp.tool()
def search(
    terms: str,
    date_from: str = None,
    date_to: str = None,
    max_results: int = 10,
    domains: list[str] = None,
    exclude_domains: list[str] = None
) -> dict:
    """
    Search the web and return ranked results with highlights.
    Results are cached. Use date_from/date_to for time-sensitive queries.
    Returns list of results with source_tier and published_date per result.
    """
    return requests.get("http://127.0.0.1:8765/search", params={...}).json()


@mcp.tool()
def fetch(
    url: str,
    return_type: str = "summary",
    cache_reload: bool = False,
    max_age_hours: int = 24
) -> dict:
    """
    Fetch a URL and return cleaned content.
    return_type options: "summary", "text", "text+links"
    Use cache_reload=True or lower max_age_hours for breaking news.
    Handles paywalled sites automatically via authenticated browser session.
    """
    return requests.get("http://127.0.0.1:8765/fetch", params={...}).json()
```

---

## 6. Server startup

FastAPI runs as a local Windows service or background process:

```bash
python server.py
# Listening on http://127.0.0.1:8765
```

Playwright connects to Chrome on startup and keeps the CDP connection alive for the session duration. If Chrome is not running with `--remote-debugging-port=9222`, Playwright falls back gracefully and logs a warning — httpx-only mode continues to work.

---

## 7. Requirements

```
fastapi
uvicorn
httpx
playwright
trafilatura
beautifulsoup4
lxml
anthropic
pymysql
exa-py
python-dotenv
```
