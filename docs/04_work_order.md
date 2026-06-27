# Document 4: Claude Code Work Order
## WebFetch API — Build Instructions

Read these three documents before writing a single line of code:
- `01_return_spec.md` — immutable return value contracts
- `02_api_spec.md` — technical implementation specification  
- `03_overview_spec.md` — purpose and success criteria

Do not deviate from the return structures defined in `01_return_spec.md`. They are the contract between this API and Claude. Everything else in this document is implementation guidance — the return spec is not.

---

## Environment

- OS: Windows
- Shell: PowerShell or Command Prompt
- Python: use `python` (not `python3`)
- Chrome user data dir: `G:\chrome-bank\selenium.driver.python`
- Chrome CDP URL: `http://localhost:9222`
- MySQL: running locally, create database `webfetch`
- Apache: running locally, not used by this project
- All credentials go in `.env` (never hardcoded)

Start Chrome with remote debugging before running any Playwright code:
```
chrome.exe --remote-debugging-port=9222 --user-data-dir="G:\chrome-bank\selenium.driver.python"
```

---

## Project Setup (do this first, before any milestone)

```bash
mkdir webfetch
cd webfetch
git init
python -m venv venv
venv\Scripts\activate
pip install fastapi uvicorn httpx playwright trafilatura beautifulsoup4 lxml anthropic pymysql exa-py python-dotenv pytest pytest-asyncio
playwright install chromium
```

Create `.env`:
```
EXA_API_KEY=
ANTHROPIC_API_KEY=
DB_HOST=localhost
DB_PORT=3306
DB_USER=
DB_PASSWORD=
DB_NAME=webfetch
CHROME_CDP_URL=http://localhost:9222
CHROME_DATA_DIR=G:\chrome-bank\selenium.driver.python
```

Create `.gitignore`:
```
.env
venv/
__pycache__/
*.pyc
.pytest_cache/
```

Create the database:
```sql
CREATE DATABASE webfetch CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Then commit:
```bash
git add .
git commit -m "init: project structure and dependencies"
```

---

## Milestone 1 — Configuration and Database

**Files:** `config.py`, `db.py`, and the two MySQL tables from `02_api_spec.md`

### config.py
Load all values from `.env`. Expose as module-level constants. Include all lists from `02_api_spec.md` section 3: `PREMIUM_SOURCES`, `TIER1_DOMAINS`, `TIER2_DOMAINS`, `JUNK_PATTERNS`, all timeout and cache TTL values.

### db.py
- Connection pool using `pymysql` (pool size from config)
- `get_cached_page(url_hash)` — returns row or None
- `upsert_page(data_dict)` — insert or update on url_hash
- `get_cached_search(query_hash)` — returns row or None
- `upsert_search(data_dict)` — insert or update on query_hash
- `get_connection()` context manager for raw queries

Create both tables on first run if they don't exist (run CREATE TABLE IF NOT EXISTS on startup).

### Tests: `tests/test_db.py`
```
- test_connection: can connect to MySQL
- test_upsert_and_retrieve_page: write a fake page row, read it back, assert all fields match
- test_upsert_and_retrieve_search: same for searches table
- test_cache_age: write a row, verify cache_age_hours calculation is correct
```

### Commit
```bash
git add .
git commit -m "milestone-1: config, database schema, cache layer"
```

---

## Milestone 2 — URL Fetcher

**File:** `fetcher.py`

Implements `request_url(url)` as described in `02_api_spec.md` section 4.2.

### Logic (in order):
1. If domain is in `PREMIUM_SOURCES` → go directly to Playwright with Chrome session, set `fetch_mode_reason = "playwright_auth"`
2. Try httpx with configured headers and timeout
   - Status 200 AND body > 500 chars → return, `fetch_mode_reason = "httpx_success"`
   - Status 403 → fall through, reason = `"httpx_403"`
   - Status 429 → fall through, reason = `"httpx_429"`
   - Body < 500 chars → fall through, reason = `"httpx_empty_body"`
3. Playwright fallback:
   - Connect via CDP to `CHROME_CDP_URL`
   - Use `browser.contexts[0]` — the existing authenticated session
   - `page.goto(url, wait_until="networkidle")`
   - Wait `PLAYWRIGHT_WAIT_MS` after load
   - Return `page.content()`
4. If both fail: raise `FetchError(url, reason)`

**Returns:** `(html: str, fetch_mode: str, fetch_mode_reason: str)`

If Chrome is not running when Playwright is called: catch the connection error, log a warning, raise `FetchError` with reason `"playwright_unavailable"`. Do not crash the server.

### Tests: `tests/test_fetcher.py`
```
- test_httpx_success: fetch a reliable public URL (example.com), assert html is non-empty
- test_httpx_returns_mode: assert fetch_mode == "httpx" and reason == "httpx_success"
- test_premium_source_uses_playwright: mock PREMIUM_SOURCES to include example.com,
  assert fetch_mode == "playwright" (skip if Chrome not running)
- test_fetch_error_on_bad_url: assert FetchError raised on garbage URL
```

### Commit
```bash
git add .
git commit -m "milestone-2: url fetcher, httpx + playwright fallback"
```

---

## Milestone 3 — Parser

**File:** `parser.py`

Implements `strip_markup(html)` and `extract_links(base_url, raw_links)` as described in `02_api_spec.md` sections 4.3 and 4.4.

### strip_markup(html)
1. Try `trafilatura.extract(html, include_links=False, include_tables=False)`
2. If result is None or empty string: fall back to BeautifulSoup4
   - Remove tags: `script, style, nav, footer, header, aside, form, iframe`
   - `get_text(separator="\n", strip=True)`
3. Always use BeautifulSoup4 separately to extract raw links:
   - All `<a href="...">` tags → list of `{href, anchor_text}`
4. Return `(stripped_text: str, raw_links: list)`

### extract_links(base_url, raw_links)
1. Resolve all relative hrefs to absolute URLs using `base_url`
2. Filter out any URL matching `JUNK_PATTERNS`
3. For each remaining link:
   - Extract domain
   - Assign `source_quality`: `high` if domain in TIER1, `medium` if TIER2, else `low`
   - Assign `link_type`:
     - `primary_source` if domain matches `sec.gov`, `federalreserve.gov`, or similar official sources
     - `cross_reference` if domain is TIER1 and different from base_url domain
     - `background` if domain matches base_url domain
     - `external` otherwise
4. Sort: high quality first, then medium, then low
5. Cap at 50 results
6. Return list of link objects per `01_return_spec.md` section 3

### Tests: `tests/test_parser.py`
```
- test_trafilatura_extracts_text: pass sample HTML with known article text, assert text present in output
- test_bs4_fallback: pass HTML that trafilatura returns empty for, assert bs4 fallback produces text
- test_junk_links_filtered: pass HTML with nav/social/subscribe links, assert none appear in extracted links
- test_link_quality_tier1: pass HTML with reuters.com link, assert source_quality == "high"
- test_link_quality_unknown: pass HTML with unknown blog link, assert source_quality == "low"
- test_relative_urls_resolved: pass HTML with relative href, assert output contains absolute URL
- test_link_cap: pass HTML with 100 links, assert output contains max 50
```

### Commit
```bash
git add .
git commit -m "milestone-3: parser, markup stripping, link extraction and classification"
```

---

## Milestone 4 — Summarizer

**File:** `summarizer.py`

Implements `summarize(stripped_text, url)` using Claude Haiku via Anthropic API.

### Logic:
1. Truncate `stripped_text` to 8000 characters
2. Call Anthropic API with model `claude-haiku-4-5-20251001`, max_tokens 512
3. System prompt:
```
You are summarizing a web page for a research assistant.
Write a factual, dense summary in 3-5 sentences.
Focus on: who, what, when, where, and key claims or data points.
Do not editorialize. Do not include meta-commentary about the article itself.
```
4. User message: `Source URL: {url}\n\nPage content:\n{truncated_text}`
5. Return the response text as a plain string
6. On API error: return `None` (caller handles gracefully — summary field becomes null)

### Tests: `tests/test_summarizer.py`
```
- test_returns_string: pass a 200-word text block, assert return value is a non-empty string
- test_truncation: pass a 20000-char string, assert Haiku is called with max 8000 chars
  (mock the API call, inspect the message content)
- test_api_error_returns_none: mock API to raise an exception, assert function returns None
```

### Commit
```bash
git add .
git commit -m "milestone-4: haiku summarizer"
```

---

## Milestone 5 — Search

**File:** `search.py`

Implements `search(terms, date_from, date_to, max_results, domains, exclude_domains)` using Exa API.

### Logic:
1. Normalize all parameters
2. Compute `query_hash = SHA-256(terms + date_from + date_to + max_results + sorted(domains or []) + sorted(exclude_domains or []))` — include every parameter that changes the result set, or different `max_results`/`exclude_domains` will collide on a stale cache entry
3. Check `searches` table — return cached result if within `CACHE_SEARCH_MAX_AGE` hours
4. Call Exa main search:
   - `query = terms`
   - `numResults = max_results`
   - `startPublishedDate = date_from` (if set)
   - `endPublishedDate = date_to` (if set)
   - `includeDomains = domains` (if set)
   - `excludeDomains = exclude_domains` (if set)
   - `contents = {"highlights": True}`
5. Call Exa premium source search in parallel (using `asyncio.gather` or `ThreadPoolExecutor`):
   - Same query
   - `numResults = EXA_PREMIUM_RESULTS` (5)
   - `includeDomains = PREMIUM_SOURCES`
   - Skip this call if `domains` parameter is already set (user has specified their own domain filter)
6. Merge results:
   - Deduplicate: if same domain AND same title (case-insensitive), keep the higher-ranked one
   - Main results ranked 1–N, premium results appended after if not duplicate
   - Assign `source_tier` from `TIER1_DOMAINS` / `TIER2_DOMAINS`
   - Assign `is_premium_source = True` if domain in `PREMIUM_SOURCES`
   - Set `fetch_available = True` for all results
7. Write merged result to `searches` table
8. Return structure per `01_return_spec.md` section 1

### Tests: `tests/test_search.py`
```
- test_returns_correct_structure: call search("python programming"), assert all required
  fields present per 01_return_spec.md
- test_cache_hit: call same search twice, assert second call returns cached=True
  and does not call Exa API again (mock Exa)
- test_date_filter_passed_to_exa: call with date_from set, assert Exa receives
  startPublishedDate (mock Exa, inspect call args)
- test_premium_sources_merged: mock Exa to return results, assert is_premium_source
  is True for washingtonpost.com result
- test_deduplication: mock Exa to return same URL from both calls, assert it appears
  only once in output
- test_source_tier_assigned: assert reuters.com result has source_tier == "tier1"
```

### Commit
```bash
git add .
git commit -m "milestone-5: exa search integration with caching and premium source merge"
```

---

## Milestone 6 — Fetch Orchestrator

**File:** `fetch_orchestrator.py`

This is the main `fetch()` function that wires together all previous modules.

### Logic:
1. Normalize URL (strip common tracking params: `utm_*`, `fbclid`, `gclid`, `ref`)
2. Compute `url_hash = SHA-256(normalized_url)`
3. Check cache:
   - If `cache_reload=False`: look up `pages` table by `url_hash`
   - If found AND `cache_age_hours <= max_age_hours`: return cached data in requested `return_type`
4. Call `request_url(url)` → `(html, fetch_mode, fetch_mode_reason)`
5. Call `strip_markup(html)` → `(stripped_text, raw_links)`
6. Call `extract_links(url, raw_links)` → `link_objects`
7. Call `summarize(stripped_text, url)` → `summary` (may be None)
8. Extract metadata from HTML using BeautifulSoup4:
   - `title`: from `<title>` or `<og:title>`
   - `author`: from `<meta name="author">` or byline patterns
   - `published_date`: from `<meta property="article:published_time">` or `<time>` tag
9. Write to `pages` table (upsert)
10. Assemble and return response per `01_return_spec.md` section 2
11. On `FetchError`: return the **full** response shape from `01_return_spec.md` §4 — every required field present (`null`/`0`/empty where unknown), `error` set to the failure reason. Do not return a minimal `{url, error, content}` dict; the contract shape must hold on failure too.

### return_type handling:
```
"summary"    → content.summary = summary, content.text = null, content.links = null
"text"       → content.summary = null, content.text = stripped_text, content.links = null
"text+links" → content.summary = summary, content.text = stripped_text, content.links = link_objects
```

### Tests: `tests/test_fetch_orchestrator.py`
```
- test_cache_miss_fetches_and_caches: mock request_url, assert result written to DB
- test_cache_hit_returns_cached: write a page to DB, call fetch, assert request_url not called
- test_max_age_bypass: write old cache entry, call with max_age_hours=1, assert fresh fetch triggered
- test_cache_reload_bypasses_cache: write fresh cache entry, call with cache_reload=True,
  assert fresh fetch triggered
- test_return_type_summary: assert text and links are null when return_type="summary"
- test_return_type_text: assert summary is null when return_type="text"
- test_return_type_text_plus_links: assert all three content fields populated
- test_error_response_structure: mock request_url to raise FetchError,
  assert error field is set, content fields are null, AND all required fields
  from 01_return_spec.md §4 are present (no missing keys)
```

### Commit
```bash
git add .
git commit -m "milestone-6: fetch orchestrator, wires all modules together"
```

---

## Milestone 7 — FastAPI Server + MCP

**Files:** `server.py`, `mcp_server.py`

### server.py
- FastAPI app with two routes: `GET /fetch` and `GET /search`
- Both routes call the orchestrator functions from milestone 5 and 6
- Return JSON directly (FastAPI serializes the dict)
- Global exception handler: catch all unhandled exceptions, return `{"error": str(e)}` with HTTP 500
- On startup: run `db.py` table creation, attempt Playwright CDP connection (warn if unavailable)
- Startup log should print: `WebFetch API running on http://127.0.0.1:8765`

```bash
python server.py
# or
uvicorn server:app --host 127.0.0.1 --port 8765
```

### mcp_server.py
MCP server exposing `search` and `fetch` as tools per `02_api_spec.md` section 5.
Tools call the FastAPI endpoints via `httpx` on `http://127.0.0.1:8765`.
This means the FastAPI server must be running before the MCP server is started.

### Tests: `tests/test_server.py`
```
- test_fetch_endpoint_returns_200: GET /fetch?url=https://example.com, assert 200 and valid JSON
- test_search_endpoint_returns_200: GET /search?terms=python, assert 200 and valid JSON
- test_fetch_missing_url_returns_error: GET /fetch with no url param, assert 422
- test_response_matches_spec: fetch a URL, assert all required fields from 01_return_spec.md present
```

Use `fastapi.testclient.TestClient` for server tests — no live server needed.

### Commit
```bash
git add .
git commit -m "milestone-7: fastapi server and mcp integration"
```

---

## Milestone 8 — Integration Test & Verification

Run the full pipeline end-to-end and verify all success criteria from `03_overview_spec.md` section 5.

### Checklist (run manually, document results):

```
[ ] search("Elon Musk Tesla") returns >= 2 tier1 sources
[ ] fetch(reuters_url, return_type="summary") returns non-empty summary, no HTML tags in content
[ ] fetch(auth_gated_url) via your logged-in session returns page_size_chars
    substantially larger than a logged-out fetch of the same URL (confirms the
    authenticated Chrome session is being used, not a logged-out stub)
[ ] fetch(any_url, cache_reload=True) returns cached=False and fresh cached_at timestamp
[ ] fetch(any_url) called twice: second call returns cached=True
[ ] fetch(url, return_type="text+links") returns links with source_quality field
[ ] search with date_from returns no results older than that date
[ ] Token comparison: fetch raw URL with httpx, count chars. Compare to stripped_text. 
    Document reduction percentage.
```

### Run all tests:
```bash
pytest tests/ -v
```

All tests must pass before final commit.

### Final commit
```bash
git add .
git commit -m "milestone-8: integration verified, all tests passing"
```

---

## General rules for Claude Code

- One milestone at a time. Complete and commit before starting the next.
- Never modify `01_return_spec.md`. It is the immutable contract.
- All credentials from `.env` via `python-dotenv`. Never hardcode.
- All functions must have docstrings.
- If a module dependency is not yet built, mock it in tests — don't skip the test.
- If something in the spec is ambiguous, make a decision, implement it, and add a comment explaining the choice. Do not ask — decide and document.
- Update `CLAUDE.md` after each milestone with what was built and any decisions made.
