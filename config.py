"""Central configuration for the WebFetch API.

All values are loaded from the project root `.env` via python-dotenv. No other
module reads `.env` directly — they import the constants defined here. Secrets
are never hardcoded; only non-secret defaults (lists, timeouts) live in source.
"""

import os

from dotenv import load_dotenv

# Load .env from this file's directory, not the process CWD — the MCP server is
# spawned by Claude Code from an arbitrary working directory, so a bare
# load_dotenv() would miss the credentials.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def _env(key: str, default: str | None = None) -> str | None:
    """Read an environment variable, returning `default` when unset/blank."""
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return value


# ── Exa search ──────────────────────────────────────────────────────────────
EXA_API_KEY = _env("EXA_API_KEY")
EXA_DEFAULT_RESULTS = 10
EXA_PREMIUM_RESULTS = 5  # extra results pulled from the premium source list

# ── Haiku summarizer (Anthropic) ────────────────────────────────────────────
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_MAX_TOKENS = 512
SUMMARY_MAX_INPUT_CHARS = 8000  # truncate stripped text before sending to Haiku

# ── Cache TTL defaults (hours) ──────────────────────────────────────────────
CACHE_DEFAULT_MAX_AGE = 24
CACHE_NEWS_MAX_AGE = 6  # for tier1 news domains
CACHE_SEARCH_MAX_AGE = 12

# ── Playwright / Chrome CDP ─────────────────────────────────────────────────
CHROME_CDP_URL = _env("CHROME_CDP_URL", "http://localhost:9222")
CHROME_DATA_DIR = _env("CHROME_DATA_DIR")
PLAYWRIGHT_TIMEOUT_MS = 30000
PLAYWRIGHT_WAIT_MS = 1500  # wait after page load for late JS

# On-demand Chrome launch: when a Playwright fetch is needed and the CDP port is
# not answering, the fetcher launches this shortcut (which carries the
# --remote-debugging-port=9222 + --user-data-dir flags) and waits for the port
# to come up. Lets the operator avoid keeping a Chrome window open permanently.
CHROME_AUTOLAUNCH = True
CHROME_LAUNCH_SHORTCUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "chrome-profile.lnk"
)
CHROME_LAUNCH_WAIT_S = 20    # max seconds to wait for CDP after launching
CHROME_LAUNCH_POLL_S = 0.5   # poll interval while waiting

# ── httpx ───────────────────────────────────────────────────────────────────
HTTPX_TIMEOUT_S = 15
HTTPX_MIN_BODY_CHARS = 500  # below this, treat as empty/JS-only → Playwright
HTTPX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── MySQL ───────────────────────────────────────────────────────────────────
DB_HOST = _env("DB_HOST", "localhost")
DB_PORT = int(_env("DB_PORT", "3306"))
DB_USER = _env("DB_USER")
DB_PASSWORD = _env("DB_PASSWORD")
# Honor the DB name from .env (the live env uses `agentic_webfetch`, while the
# docs example used `webfetch`). .env is the source of truth.
DB_NAME = _env("DB_NAME", "webfetch")
DB_POOL_SIZE = 5

# ── Server ──────────────────────────────────────────────────────────────────
API_HOST = "127.0.0.1"
API_PORT = 8765
API_BASE_URL = f"http://{API_HOST}:{API_PORT}"

# ── MCP server (HTTP transport) ─────────────────────────────────────────────
# Used only when mcp_server.py is started with --http (streamable-http
# transport). The default stdio transport ignores these. For Ngrok/remote POC,
# bind 0.0.0.0; for local-only, keep 127.0.0.1. Endpoint path is /mcp.
MCP_HTTP_HOST = _env("MCP_HTTP_HOST", "127.0.0.1")
MCP_HTTP_PORT = int(_env("MCP_HTTP_PORT", "8766"))
# FastMCP's DNS-rebinding protection rejects any Host header not in its allowlist
# (defaults to localhost), which returns 421/400 when reached through an Ngrok
# tunnel. For a POC behind Ngrok, allow any host. SECURITY: only do this for a
# temporary tunnel — it removes the rebinding guard. Set to "1" to re-enable.
MCP_HTTP_DNS_REBINDING_PROTECTION = _env("MCP_HTTP_DNS_REBINDING_PROTECTION", "0") == "1"

# ── Source lists ────────────────────────────────────────────────────────────
# Sites rendered via the operator's own authenticated Chrome session: login or
# JS gated, so httpx is skipped and Playwright drives the signed-in profile.
# `is_premium_source` in the return contract flags results from this list.
PREMIUM_SOURCES = [
    "washingtonpost.com",
    "nytimes.com",
    "ft.com",
    "wsj.com",
]

# tier1: wire services, major papers, and official/government sources.
TIER1_DOMAINS = [
    "reuters.com",
    "apnews.com",
    "ft.com",
    "bloomberg.com",
    "wsj.com",
    "washingtonpost.com",
    "nytimes.com",
    "bbc.com",
    "economist.com",
    "sec.gov",
    "federalreserve.gov",
    "europa.eu",
]

# tier2: other recognized news outlets/publications. The spec ("Document 1")
# defines tier2 as "all other recognized news outlets" without enumerating
# them, so this is a curated starter set — decision documented per work order.
TIER2_DOMAINS = [
    "theguardian.com",
    "cnn.com",
    "nbcnews.com",
    "cbsnews.com",
    "abcnews.go.com",
    "npr.org",
    "politico.com",
    "axios.com",
    "thehill.com",
    "forbes.com",
    "businessinsider.com",
    "cnbc.com",
    "marketwatch.com",
    "theverge.com",
    "techcrunch.com",
    "arstechnica.com",
    "wired.com",
    "aljazeera.com",
    "time.com",
    "newsweek.com",
    "usatoday.com",
    "latimes.com",
    "theatlantic.com",
    "vox.com",
    "independent.co.uk",
    "telegraph.co.uk",
    "nature.com",
    "sciencemag.org",
]

# Official/primary-source domains → link_type "primary_source".
PRIMARY_SOURCE_DOMAINS = [
    "sec.gov",
    "federalreserve.gov",
    "europa.eu",
    "treasury.gov",
    "whitehouse.gov",
    "supremecourt.gov",
    "uscourts.gov",
    "congress.gov",
    "govinfo.gov",
]

# Regex patterns applied to the full (absolute) URL during link filtering.
JUNK_PATTERNS = [
    r"/tag/",
    r"/category/",
    r"/author/",
    r"/search",
    r"/archive",
    r"\?.*utm_",
    r"#[a-z\-]+nav",
    r"subscribe",
    r"newsletter",
    r"login",
    r"signin",
    r"register",
    r"facebook\.com",
    r"twitter\.com",
    r"x\.com",
    r"instagram\.com",
    r"linkedin\.com",
    r"youtube\.com",
    r"javascript:",
    r"mailto:",
    r"tel:",
]

# Cap on link objects returned per fetch (after filtering + sorting).
MAX_LINKS = 50

# ── Login / auth-wall detection ─────────────────────────────────────────────
# When a fetch returns a suspiciously small body AND the raw HTML carries
# login/paywall markers, we log it so the operator can fix the Chrome session.
# A fully rendered article is typically 4000–15000 chars; a stub/login wall is
# usually under 2000 (see 03_overview_spec.md §5.3).
LOGIN_STUB_MAX_CHARS = 2000
LOGIN_WALL_MARKERS = [
    "sign in",
    "signin",
    "log in",
    "login",
    "create an account",
    "create a free account",
    "subscribe",
    "subscription",
    "register to read",
    "register to continue",
    "metered",
    "paywall",
    "you have reached your",
    "to continue reading",
]
# Path to the append-only log of suspected login walls (one JSON line each).
LOGIN_WALL_LOG = os.path.join(os.path.dirname(__file__), "login_required.log")
