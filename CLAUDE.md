# CLAUDE.md
## WebFetch API — Project Context

This file is the persistent orientation for Claude Code.
Read it at the start of every session before touching any code.
Update the Build Status section after completing each milestone.

---

## What This Project Is

A locally-hosted Python API that gives Claude clean, token-efficient access to web content.
Two endpoints: `search()` and `fetch()`. Full spec in the `docs/` folder.

The return value contract in `docs/01_return_spec.md` is immutable.
Do not change the structure of what the functions return under any circumstances.

---

## Project Structure

```
webfetch/
├── CLAUDE.md                  ← this file, update after each milestone
├── .env                       ← credentials, never commit
├── .gitignore
├── requirements.txt
│
├── docs/
│   ├── 01_return_spec.md      ← IMMUTABLE return value contract
│   ├── 02_api_spec.md         ← technical implementation spec
│   ├── 03_overview_spec.md    ← purpose and success criteria
│   └── 04_work_order.md       ← milestone build instructions
│
├── config.py                  ← all constants loaded from .env
├── db.py                      ← mysql connection pool and cache queries
├── fetcher.py                 ← request_url(): httpx + playwright logic
├── parser.py                  ← strip_markup(), extract_links()
├── summarizer.py              ← haiku api summarization
├── search.py                  ← exa api search integration
├── fetch_orchestrator.py      ← main fetch() function, wires all modules
├── server.py                  ← fastapi app, /fetch and /search endpoints
├── mcp_server.py              ← mcp tool definitions
│
└── tests/
    ├── test_db.py
    ├── test_fetcher.py
    ├── test_parser.py
    ├── test_summarizer.py
    ├── test_search.py
    ├── test_fetch_orchestrator.py
    └── test_server.py
```

---

## Environment

- OS: Windows
- Shell: PowerShell or Command Prompt
- Python: `python` (not `python3`)
- Virtual env: `venv\Scripts\activate`
- Run server: `python server.py` → listens on `http://127.0.0.1:8765`
- Run tests: `pytest tests/ -v`

**Chrome must be running before starting the server:**
```
chrome.exe --remote-debugging-port=9222 --user-data-dir="G:\chrome-bank\selenium.driver.python"
```
If Chrome is not running, the server starts in httpx-only mode and logs a warning.
Playwright calls will fail gracefully with `fetch_mode_reason: "playwright_unavailable"`.

---

## Credentials (.env)

All secrets live in `.env` in the project root. Never hardcode. Never commit.

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

`config.py` loads these at startup via `python-dotenv`. All other modules import from `config.py` — never from `.env` directly.

---

## Key Technical Decisions

- **MySQL over SQLite**: already running locally, enables cross-session queries on cached content
- **httpx first, Playwright fallback**: speed where possible, authenticated session where needed
- **Trafilatura first, BeautifulSoup4 fallback**: trafilatura is better at article extraction; bs4 always handles link extraction regardless
- **Haiku for summaries only**: summarization is the only AI step. Fetch and search pipeline works without it (summary field returns null on API error)
- **Exa for search**: semantic search quality + highlights in same call. 20,000 free requests/month
- **Premium sources get Playwright directly**: no httpx attempt for washingtonpost.com, nytimes.com, ft.com, wsj.com — these require the authenticated Chrome session
- **Links capped at 50**: sorted by source quality (tier1 first), junk filtered before cap applied
- **FastAPI + MCP as separate processes**: FastAPI runs on 8765, MCP server calls it via httpx. Both must be running for Claude Code integration

---

## Build Status

Update this section after each milestone is committed.

```
[x] Project setup       — venv (uv), git, .env, .gitignore, requirements.txt
[x] Milestone 1         — config.py, db.py, mysql tables
[x] Milestone 2         — fetcher.py
[x] Milestone 3         — parser.py
[x] Milestone 4         — summarizer.py
[x] Milestone 5         — search.py
[x] Milestone 6         — fetch_orchestrator.py
[x] Milestone 7         — server.py, mcp_server.py
[x] Milestone 8         — integration verified, all tests passing
[x] Post-build          — login-wall logging, link-noise filter, byline extraction
```

Test suite: **42 passed** (Chrome up → 0 skipped). Run: `pytest tests/ -v`.

### Build notes & decisions (2026-06-27)

- **Env manager**: built the venv with `uv` (`uv venv` + `uv pip install`) per
  operator preference; `python -m venv` still works identically.
- **DB name**: `.env` is the source of truth → `agentic_webfetch` (not the docs'
  `webfetch`). config.py reads it from `.env`. DB + tables auto-created.
- **Whole pipeline is synchronous.** Playwright's sync API can't run inside an
  asyncio loop, so fetcher/parser/orchestrator/search are sync and FastAPI
  routes are `def` (run in threadpool). Search parallelism uses ThreadPoolExecutor.
- **Connection pool**: hand-rolled queue-based pool (pymysql has none).
  `cache_age_hours` is computed in SQL (`TIMESTAMPDIFF`) to avoid client/server
  timezone skew.
- **TIER2_DOMAINS / PRIMARY_SOURCE_DOMAINS**: docs only enumerate tier1; curated
  starter lists added in config.py (documented inline).
- **Premium Exa search is best-effort.** This Exa plan returns 403
  SOURCE_NOT_AVAILABLE for includeDomains on wapo/nytimes/wsj; that failure is
  now swallowed and the main results are returned regardless.
- **Success criterion 5.2 (≥2 tier1 per news event)** is Exa-ranking dependent.
  Verified strong for outlet-heavy queries (e.g. "Federal Reserve interest rate"
  → 4–6 tier1); some broad/regional topics return regional/aggregator sources
  Exa ranks higher. Classification itself is correct.
- **Token reduction**: apnews.com raw 2.04M chars → stripped 3.5K = **99.8%**
  reduction (criterion 5.1 met).

### Post-build enhancements (2026-06-27)

Added after live testing against real premium articles. All verified end-to-end
with the server running and Chrome connected.

- **Auth-session (5.3) verified live.** NYT via `playwright_auth` → 17.3K chars;
  WaPo opinion piece → full body (5.2K), ads stripped, encoding clean (0 U+FFFD,
  em-dash/accents intact — the `�` seen in terminals is just Windows cp1252).
- **Login/auth-wall logging** (`fetch_orchestrator._detect_login_wall`). When a
  fetch returns a short body (< `LOGIN_STUB_MAX_CHARS`, 2000) AND the raw HTML
  has login/paywall markers or a password field, one JSON line is appended to
  `login_required.log` (project root, gitignored) + a server WARNING. Lets the
  operator spot a dead Chrome/bugmenot session by domain. The length guard means
  full articles behind *metered* overlays (Reuters delivers full HTML under a JS
  paywall) are NOT flagged. Contract shape unchanged — log is a side channel.
- **Link-noise filter** (`parser`). Drops empty-anchor links, in-page `#`
  anchors, self-links, and same-domain nav/section pages (e.g. `/business/
  autos-transportation`). Same-domain links kept only when article-like (a
  date/id in the path or a ≥30-char slug); cross-domain links always kept. On a
  Reuters article this cut 50 nav-filled links → 36 real cross-references.
  Remaining stock-ticker links (digit codes) are a tolerated site-specific edge.
- **Byline extraction** (`fetch_orchestrator._extract_author`). Order:
  `meta[name=author]` → `article:author` → `meta[name=byl]` (NYT) → `rel=author`
  → schema.org JSON-LD `author.name` → byline-classed element → body-text
  fallback (`By …` / `Reporting by …`). Reuters now yields a name from the body
  credits; WaPo/NYT from structured tags.
- **On-demand Chrome launch** (`fetcher._ensure_chrome`). When a Playwright
  fetch is needed and CDP (9222) isn't answering, the fetcher launches
  `chrome-profile.lnk` (carries `--remote-debugging-port=9222` + the profile
  `--user-data-dir`) and polls until the port is up (≤ `CHROME_LAUNCH_WAIT_S`,
  20s). The operator no longer needs to keep a Chrome window open. Separate
  profile dir = isolated instance, so it coexists with normal Chrome. Live-
  verified: cold NYT fetch with Chrome closed auto-launched it and returned
  17.1K chars via `playwright_auth` in ~29s (first call only; Chrome then stays
  up and later calls are fast). Toggle with `config.CHROME_AUTOLAUNCH`.
- **Reuters note**: a metered paywall overlay ("pay $10") can appear in the
  browser while the full article is still delivered in the DOM — we extract the
  complete text regardless. A *hard*-paywalled stub (short body, no end-marker)
  would instead trip the login-wall log above.

### task-20: fetch contract redesign (2026-06-28) — BREAKING

`docs/01_return_spec.md` remains the **authoritative return contract** — it was
manually reconciled to this redesign, so 01 and the code now match.
`docs/task-20-webfetch_change_request.md` is the **applied change request** that
specified this owner-sanctioned breaking change (history/rationale), not a
second source of truth. When in doubt, 01 wins.

- **`fetch()` signature**: `return_type` removed → two orthogonal axes
  `verbosity` ("summary" | "full") + `include_links` (bool).
- **Flat content**: `content.{summary,text,links}` gone. Now top-level `content`
  (str|null) + load-bearing `content_kind` ("verbatim" = exact source, citable;
  "summary" = Haiku paraphrase, triage only). `links` is top-level (null unless
  `include_links`). `return_type` echo removed.
- **Sizes always set**: `verbatim_size_chars` + `verbatim_size_tokens` (real
  tiktoken `cl100k_base`, in `tokenizer.py`) reflect the FULL text even on a
  summary, so Claude can budget an escalation. `truncated` (bool) — verbatim is
  never truncated silently; true only if `VERBATIM_HARD_CAP_CHARS` (1M) is hit.
- **Threshold short-circuit**: pages ≤ `SUMMARY_THRESHOLD_TOKENS` (2000) return
  verbatim with NO Haiku call. So asking for "summary" on a short page returns
  `content_kind="verbatim"` — that's the signal the short-circuit fired.
- **DB**: `pages.page_size_tokens` column added via idempotent migration in
  `init_db()`. Token count stored on the row; `db.update_page()` lazily
  backfills tokens/summary on cache hits for old rows.
- **include_links from cache**: reads `links_json`, never re-fetches.
- **PREMIUM_SOURCES**: `reuters.com` added (httpx gets 401 → needs the session).
  `is_premium_source` reframed as an *access-mechanism* flag, not credibility.
- **search merge**: dedup never drops a premium hit for a thin highlight.
- Tests: 47 passed / 1 skipped (Chrome down).

---

## What Not To Do

- Do not modify anything in `docs/01_return_spec.md`
- Do not add fields to return structures not defined in `01_return_spec.md`
- Do not hardcode credentials anywhere
- Do not skip tests to move faster
- Do not start the next milestone before committing the current one
- Do not install packages not in `requirements.txt` without adding them there first
