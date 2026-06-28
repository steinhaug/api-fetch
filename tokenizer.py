"""Server-side token counting for the verbatim size contract.

`verbatim_size_tokens` in the fetch contract is the number Claude budgets
against, so it must be a real tokenizer count — not chars/4. We use tiktoken's
`cl100k_base` (a stable, offline-cacheable BPE) computed once per page and
stored on the row. If tiktoken is somehow unavailable, we fall back to a coarse
chars/4 estimate rather than failing the fetch.
"""

import logging

logger = logging.getLogger(__name__)

_ENCODER = None
_ENCODER_FAILED = False


def _encoder():
    """Return a cached tiktoken encoder, or None if it cannot be loaded."""
    global _ENCODER, _ENCODER_FAILED
    if _ENCODER is not None:
        return _ENCODER
    if _ENCODER_FAILED:
        return None
    try:
        import tiktoken

        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # pragma: no cover - exercised only when tiktoken breaks
        logger.warning("tiktoken unavailable, falling back to estimate: %s", exc)
        _ENCODER_FAILED = True
        return None
    return _ENCODER


def count_tokens(text: str) -> int:
    """Return the real token count of `text` (cl100k_base), or a chars/4 estimate."""
    if not text:
        return 0
    enc = _encoder()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text))
