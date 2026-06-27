"""Haiku-based page summarization.

`summarize()` is the single AI step in the pipeline. It is intentionally
best-effort: any failure (missing API key, network error, API exception)
returns None so the caller can set the `summary` field to null and carry on —
the fetch/search pipeline never depends on summaries.
"""

import logging

import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are summarizing a web page for a research assistant.\n"
    "Write a factual, dense summary in 3-5 sentences.\n"
    "Focus on: who, what, when, where, and key claims or data points.\n"
    "Do not editorialize. Do not include meta-commentary about the article itself."
)


def _call_haiku(system: str, user: str) -> str:
    """Send one message to Haiku and return the text of the response.

    Isolated from `summarize()` so tests can mock the network call.
    """
    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.HAIKU_MODEL,
        max_tokens=config.HAIKU_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


def summarize(stripped_text: str, url: str) -> str | None:
    """Summarize page text with Haiku, or return None on any failure.

    The input is truncated to `config.SUMMARY_MAX_INPUT_CHARS` (8000) characters
    before being sent. Returns the summary string, or None if the API key is
    absent, the text is empty, or the API call fails.
    """
    if not config.ANTHROPIC_API_KEY:
        logger.info("No ANTHROPIC_API_KEY set; skipping summary.")
        return None
    if not stripped_text:
        return None

    truncated = stripped_text[: config.SUMMARY_MAX_INPUT_CHARS]
    user = f"Source URL: {url}\n\nPage content:\n{truncated}"
    try:
        summary = _call_haiku(SYSTEM_PROMPT, user)
    except Exception as exc:
        logger.warning("Haiku summarization failed for %s: %s", url, exc)
        return None
    return summary
