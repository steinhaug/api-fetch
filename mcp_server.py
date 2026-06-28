"""MCP server exposing `search` and `fetch` as tools.

Each tool is a thin wrapper that calls the running FastAPI server over httpx, so
the FastAPI server (server.py) must be running on API_BASE_URL before this MCP
server starts. Run with:  python mcp_server.py
"""

import os
import sys

# Ensure this project is importable when Claude Code spawns us from elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx

import config

mcp = None
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("webfetch")
except ImportError:  # pragma: no cover - mcp is a declared dependency
    mcp = None


def _get(path: str, params: dict) -> dict:
    """GET an endpoint on the local FastAPI server, dropping None params."""
    clean = {k: v for k, v in params.items() if v is not None}
    resp = httpx.get(config.API_BASE_URL + path, params=clean, timeout=60)
    return resp.json()


if mcp is not None:

    @mcp.tool()
    def search(
        terms: str,
        date_from: str = None,
        date_to: str = None,
        max_results: int = config.EXA_DEFAULT_RESULTS,
        domains: list[str] = None,
        exclude_domains: list[str] = None,
    ) -> dict:
        """Search the web and return ranked results with highlights.

        Results are cached. Use date_from/date_to for time-sensitive queries.
        Returns results with source_tier and published_date per result.
        """
        return _get(
            "/search",
            {
                "terms": terms,
                "date_from": date_from,
                "date_to": date_to,
                "max_results": max_results,
                "domains": domains,
                "exclude_domains": exclude_domains,
            },
        )

    @mcp.tool()
    def fetch(
        url: str,
        return_type: str = "summary",
        cache_reload: bool = False,
        max_age_hours: int = config.CACHE_DEFAULT_MAX_AGE,
    ) -> dict:
        """Fetch a URL and return cleaned content.

        return_type options: "summary", "text", "text+links".
        Use cache_reload=True or a lower max_age_hours for breaking news.
        Handles paywalled sites via the authenticated browser session.
        """
        return _get(
            "/fetch",
            {
                "url": url,
                "return_type": return_type,
                "cache_reload": cache_reload,
                "max_age_hours": max_age_hours,
            },
        )


if __name__ == "__main__":
    if mcp is None:
        raise SystemExit("mcp package not available")

    # Default: stdio (Claude Desktop / Claude Code spawn us this way).
    # With --http: run the streamable-http transport on MCP_HTTP_HOST:PORT,
    # endpoint /mcp — for Ngrok / remote / Chat custom-connector POCs.
    # Same code, same tools, just a different transport. Both can run at once
    # (stdio has no port; http binds MCP_HTTP_PORT). The backend (server.py,
    # 8765) must be running either way — these tools proxy to it.
    if "--http" in sys.argv:
        mcp.settings.host = config.MCP_HTTP_HOST
        mcp.settings.port = config.MCP_HTTP_PORT
        print(
            f"webfetch MCP (streamable-http) on "
            f"http://{config.MCP_HTTP_HOST}:{config.MCP_HTTP_PORT}/mcp",
            flush=True,
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
