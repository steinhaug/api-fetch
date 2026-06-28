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
        """
        Search the web; returns ranked results with source_tier, published_date,
        and a relevance highlight per result. Cached.

        source_tier ("tier1"|"tier2"|"unknown") is your credibility axis — prefer
        tier1 (wire services, papers of record, official/gov) for claims you'll
        rely on. A significant news event returns >=2 tier1 sources so you can
        cross-reference without hand-picking URLs.

        is_premium_source=true means full text is retrievable via the authenticated
        session — treat a thin highlight on such a result as "snippet is limited,
        the article is not": fetch it to get the whole thing.

        Use date_from/date_to to bound time-sensitive queries; domains /
        exclude_domains to scope the result set.
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
        verbosity: str = "summary",
        include_links: bool = False,
        cache_reload: bool = False,
        max_age_hours: int = config.CACHE_DEFAULT_MAX_AGE,
    ) -> dict:
        """
        Fetch one URL and return clean, source-attributed content.

        Two independent controls:
          verbosity="summary" (default) → 3-5 sentence summary. For triage:
              deciding which results are worth reading. NOT citable as the source.
          verbosity="full" → verbatim stripped article text. Cite from this.
          include_links=True → also return high-quality outbound links (primary
              sources, cross-references). Served from cache; never re-fetches.

        Escalation ladder:
          Every response carries verbatim_size_tokens — the FULL article's size —
          even on a summary. Use it to decide whether a "full" fetch is worth the
          tokens BEFORE paying for it.

          Short pages short-circuit: if the article is <= ~2000 tokens you always
          get content_kind="verbatim" (no summary step), because summarizing
          something that small wastes a model call and loses fidelity for no gain.

        ALWAYS check content_kind: "verbatim" = exact source text, safe to quote;
        "summary" = paraphrase, triage only.

        cache_reload=True or a lower max_age_hours forces fresh content for
        breaking news / time-sensitive data. Paywalled/login-gated sources render
        in full via the authenticated browser session.
        """
        return _get(
            "/fetch",
            {
                "url": url,
                "verbosity": verbosity,
                "include_links": include_links,
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
        # Allow the Ngrok (or other) Host header through FastMCP's DNS-rebinding
        # guard, which otherwise returns 421/400 for non-localhost hosts.
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=config.MCP_HTTP_DNS_REBINDING_PROTECTION
        )
        print(
            f"webfetch MCP (streamable-http) on "
            f"http://{config.MCP_HTTP_HOST}:{config.MCP_HTTP_PORT}/mcp",
            flush=True,
        )
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
