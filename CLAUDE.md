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
[ ] Project setup       — venv, git init, .env, .gitignore, requirements.txt
[ ] Milestone 1         — config.py, db.py, mysql tables
[ ] Milestone 2         — fetcher.py
[ ] Milestone 3         — parser.py
[ ] Milestone 4         — summarizer.py
[ ] Milestone 5         — search.py
[ ] Milestone 6         — fetch_orchestrator.py
[ ] Milestone 7         — server.py, mcp_server.py
[ ] Milestone 8         — integration verified, all tests passing
```

---

## What Not To Do

- Do not modify anything in `docs/01_return_spec.md`
- Do not add fields to return structures not defined in `01_return_spec.md`
- Do not hardcode credentials anywhere
- Do not skip tests to move faster
- Do not start the next milestone before committing the current one
- Do not install packages not in `requirements.txt` without adding them there first
