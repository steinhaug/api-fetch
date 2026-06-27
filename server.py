"""FastAPI application exposing /fetch and /search.

Routes are declared with `def` (not `async def`) so FastAPI runs them in a
threadpool — required because the fetch pipeline uses Playwright's synchronous
API, which cannot run inside an asyncio event loop.

Run with:  python server.py   (or)   uvicorn server:app --host 127.0.0.1 --port 8765
"""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

import config
import db
from fetch_orchestrator import fetch as fetch_page
from search import search as run_search

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webfetch")


def _probe_chrome() -> bool:
    """Best-effort check that Chrome's CDP endpoint is reachable."""
    try:
        r = httpx.get(config.CHROME_CDP_URL + "/json/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure DB schema, probe Chrome, announce readiness."""
    try:
        db.init_db()
        logger.info("Database schema ready (%s).", config.DB_NAME)
    except Exception as exc:
        logger.error("Database init failed: %s", exc)

    if _probe_chrome():
        logger.info("Chrome CDP reachable at %s.", config.CHROME_CDP_URL)
    else:
        logger.warning(
            "Chrome CDP not reachable at %s — running in httpx-only mode. "
            "Playwright fetches will fail gracefully.",
            config.CHROME_CDP_URL,
        )

    logger.info("WebFetch API running on %s", config.API_BASE_URL)
    yield


app = FastAPI(title="WebFetch API", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """Catch-all: return the error in-band with HTTP 500."""
    logger.exception("Unhandled error on %s", request.url)
    return JSONResponse(status_code=500, content={"error": str(exc)})


@app.get("/fetch")
def fetch_endpoint(
    url: str,
    return_type: str = "summary",
    cache_reload: bool = False,
    max_age_hours: int = config.CACHE_DEFAULT_MAX_AGE,
):
    """Fetch a URL and return cleaned content (see 01_return_spec.md §2)."""
    return fetch_page(
        url=url,
        return_type=return_type,
        cache_reload=cache_reload,
        max_age_hours=max_age_hours,
    )


@app.get("/search")
def search_endpoint(
    terms: str,
    date_from: str | None = None,
    date_to: str | None = None,
    max_results: int = config.EXA_DEFAULT_RESULTS,
    domains: list[str] | None = Query(default=None),
    exclude_domains: list[str] | None = Query(default=None),
):
    """Search the web and return ranked results (see 01_return_spec.md §1)."""
    return run_search(
        terms=terms,
        date_from=date_from,
        date_to=date_to,
        max_results=max_results,
        domains=domains,
        exclude_domains=exclude_domains,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)
