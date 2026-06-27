"""URL fetcher: httpx first, authenticated Playwright/Chrome as fallback.

`request_url()` is the only public entry point. It is synchronous on purpose:
Playwright's sync API cannot run inside an asyncio event loop, so the whole
fetch pipeline stays synchronous and FastAPI runs it in a threadpool (routes
declared with `def`, not `async def`).

Flow per `02_api_spec.md` §4.2:
  1. Premium/auth-gated domains → straight to Playwright (signed-in profile).
  2. Otherwise try httpx; on 403/429/empty-body fall through to Playwright.
  3. Playwright connects to the already-running Chrome over CDP and reuses the
     existing authenticated context.
  4. If Playwright is unavailable or also fails, raise FetchError.
"""

import logging
import os
import threading
import time
from urllib.parse import urlparse

import httpx

import config

logger = logging.getLogger(__name__)

# Serializes on-demand Chrome launches so concurrent fetches don't spawn it twice.
_chrome_launch_lock = threading.Lock()


def _cdp_ready() -> bool:
    """True if Chrome's CDP endpoint answers at the configured URL."""
    try:
        r = httpx.get(config.CHROME_CDP_URL + "/json/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _ensure_chrome() -> bool:
    """Make sure Chrome's CDP is up, launching the shortcut on demand.

    Returns True if CDP is (or becomes) reachable. If it is already up, returns
    immediately. Otherwise — when CHROME_AUTOLAUNCH is set — launches
    CHROME_LAUNCH_SHORTCUT (which carries the debug-port + profile flags) and
    polls until the port answers or CHROME_LAUNCH_WAIT_S elapses.
    """
    if _cdp_ready():
        return True
    if not config.CHROME_AUTOLAUNCH:
        return False

    with _chrome_launch_lock:
        # Another thread may have launched it while we waited for the lock.
        if _cdp_ready():
            return True
        shortcut = config.CHROME_LAUNCH_SHORTCUT
        if not os.path.exists(shortcut):
            logger.warning("Chrome auto-launch shortcut not found: %s", shortcut)
            return False
        logger.info("CDP down — launching Chrome debug session: %s", shortcut)
        try:
            os.startfile(shortcut)  # Windows: resolves and runs the .lnk
        except Exception as exc:
            logger.warning("Failed to launch Chrome shortcut: %s", exc)
            return False

        deadline = time.time() + config.CHROME_LAUNCH_WAIT_S
        while time.time() < deadline:
            if _cdp_ready():
                logger.info("Chrome CDP is up.")
                return True
            time.sleep(config.CHROME_LAUNCH_POLL_S)
        logger.warning(
            "Chrome did not expose CDP within %ss after launch.",
            config.CHROME_LAUNCH_WAIT_S,
        )
        return False


class FetchError(Exception):
    """Raised when a URL cannot be retrieved by any available method."""

    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"fetch_failed for {url}: {reason}")


def domain_of(url: str) -> str:
    """Return the bare lowercase domain (no leading www.) for a URL."""
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Drop any port suffix.
    return netloc.split(":")[0]


def _is_premium(domain: str) -> bool:
    """True if the domain (or a parent) is on the premium/auth source list."""
    return any(domain == d or domain.endswith("." + d) for d in config.PREMIUM_SOURCES)


def _fetch_httpx(url: str) -> tuple[str | None, str | None]:
    """Attempt an httpx GET.

    Returns `(html, None)` on success, or `(None, fall_through_reason)` when the
    response indicates Playwright should take over. Network/transport errors are
    treated as a fall-through with reason "httpx_error".
    """
    try:
        with httpx.Client(
            timeout=config.HTTPX_TIMEOUT_S,
            headers=config.HTTPX_HEADERS,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("httpx error for %s: %s", url, exc)
        return None, "httpx_error"

    body = resp.text or ""
    if resp.status_code == 200 and len(body) > config.HTTPX_MIN_BODY_CHARS:
        return body, None
    if resp.status_code == 403:
        return None, "httpx_403"
    if resp.status_code == 429:
        return None, "httpx_429"
    if len(body) <= config.HTTPX_MIN_BODY_CHARS:
        return None, "httpx_empty_body"
    # Any other non-200 status: let Playwright try.
    return None, "httpx_error"


def _fetch_playwright(url: str) -> str:
    """Render `url` through the running authenticated Chrome over CDP.

    Reuses `browser.contexts[0]` — the existing signed-in session — and opens a
    fresh page in it (never closing the user's own tabs/context). Raises
    FetchError("playwright_unavailable") if Chrome's CDP endpoint is unreachable.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import TimeoutError as PWTimeoutError
    except ImportError as exc:  # pragma: no cover - playwright always installed
        raise FetchError(url, "playwright_unavailable") from exc

    # Bring Chrome up on demand if its CDP port isn't already answering.
    if not _ensure_chrome():
        raise FetchError(url, "playwright_unavailable")

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(config.CHROME_CDP_URL)
        except Exception as exc:
            logger.warning(
                "Playwright cannot connect to Chrome CDP at %s: %s",
                config.CHROME_CDP_URL,
                exc,
            )
            raise FetchError(url, "playwright_unavailable") from exc

        page = None
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                page.goto(
                    url,
                    wait_until="networkidle",
                    timeout=config.PLAYWRIGHT_TIMEOUT_MS,
                )
            except PWTimeoutError:
                # networkidle can never settle on chatty pages; fall back to a
                # weaker wait condition but still return whatever rendered.
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=config.PLAYWRIGHT_TIMEOUT_MS,
                )
            page.wait_for_timeout(config.PLAYWRIGHT_WAIT_MS)
            return page.content()
        except FetchError:
            raise
        except Exception as exc:
            logger.warning("Playwright render failed for %s: %s", url, exc)
            raise FetchError(url, f"playwright_failed: {exc}") from exc
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            # Disconnect the CDP session without killing the user's Chrome.
            try:
                browser.close()
            except Exception:
                pass


def request_url(url: str) -> tuple[str, str, str]:
    """Fetch `url` and return `(html, fetch_mode, fetch_mode_reason)`.

    fetch_mode is "httpx" or "playwright"; fetch_mode_reason is one of the
    values enumerated in `01_return_spec.md`. Raises FetchError if neither
    httpx nor Playwright can retrieve the page.
    """
    domain = domain_of(url)

    # 1. Premium/auth-gated sources skip httpx entirely.
    if _is_premium(domain):
        html = _fetch_playwright(url)
        return html, "playwright", "playwright_auth"

    # 2. Try httpx.
    html, fall_reason = _fetch_httpx(url)
    if html is not None:
        return html, "httpx", "httpx_success"

    # 3. Playwright fallback, preserving the reason httpx fell through with.
    #    Only the spec-listed fall-through reasons carry over verbatim; anything
    #    else is normalized to "playwright_default".
    carry = fall_reason if fall_reason in ("httpx_403", "httpx_429", "httpx_empty_body") else "playwright_default"
    try:
        html = _fetch_playwright(url)
    except FetchError as exc:
        # Re-raise but keep the httpx context in the reason for diagnostics.
        if exc.reason == "playwright_unavailable":
            raise FetchError(url, "playwright_unavailable") from exc
        raise FetchError(url, f"{carry}; {exc.reason}") from exc
    return html, "playwright", carry
