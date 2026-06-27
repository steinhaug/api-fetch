"""Milestone 3 tests: strip_markup() and extract_links()."""

from parser import extract_links, strip_markup

_ARTICLE_HTML = """
<html><head><title>Tesla Board News</title></head>
<body>
  <nav><a href="/tag/business">Business</a></nav>
  <article>
    <h1>Tesla board member resigns amid shareholder pressure</h1>
    <p>John Doe, a long-serving member of the Tesla board of directors,
       submitted his resignation on June 25th 2026, citing disagreements
       over executive compensation and governance. The decision follows
       months of shareholder pressure during the annual meeting season.</p>
    <p>Analysts said the departure could reshape the board's oversight of
       the company's artificial intelligence strategy in the coming year.</p>
  </article>
  <footer><a href="https://facebook.com/tesla">Follow us</a></footer>
</body></html>
"""


def test_trafilatura_extracts_text():
    """Known article body text appears in the stripped output."""
    text, _ = strip_markup(_ARTICLE_HTML)
    assert "submitted his resignation" in text
    # Markup must be gone.
    assert "<p>" not in text and "<article>" not in text


def test_bs4_fallback():
    """HTML trafilatura yields nothing for still produces text via bs4."""
    html = "<html><body><div>UNIQUE_FALLBACK_SNIPPET_42</div></body></html>"
    text, _ = strip_markup(html)
    assert "UNIQUE_FALLBACK_SNIPPET_42" in text


def test_junk_links_filtered():
    """Nav, social, and subscribe links never survive extraction."""
    html = """
    <body>
      <a href="/tag/politics">tag</a>
      <a href="https://facebook.com/x">fb</a>
      <a href="https://example.com/subscribe">subscribe</a>
      <a href="https://reuters.com/world/real-story">Real story</a>
    </body>"""
    _, raw = strip_markup(html)
    links = extract_links("https://example.com/news/", raw)
    urls = [link["url"] for link in links]
    assert "https://reuters.com/world/real-story" in urls
    assert not any("facebook.com" in u for u in urls)
    assert not any("/tag/" in u for u in urls)
    assert not any("subscribe" in u for u in urls)


def test_link_quality_tier1():
    """A reuters.com link is classified high quality."""
    html = '<a href="https://reuters.com/business/story-x">Story</a>'
    _, raw = strip_markup(html)
    links = extract_links("https://example.com/", raw)
    reuters = [link for link in links if link["domain"] == "reuters.com"]
    assert reuters and reuters[0]["source_quality"] == "high"


def test_link_quality_unknown():
    """An unknown blog link is classified low quality."""
    html = '<a href="https://some-random-blog-xyz.net/post/1">Post</a>'
    _, raw = strip_markup(html)
    links = extract_links("https://example.com/", raw)
    assert links and links[0]["source_quality"] == "low"


def test_relative_urls_resolved():
    """Relative hrefs resolve to absolute URLs against base_url."""
    html = '<a href="article/1">A relative link</a>'
    _, raw = strip_markup(html)
    links = extract_links("https://example.com/news/", raw)
    assert any(
        link["url"] == "https://example.com/news/article/1" for link in links
    )


def test_link_cap():
    """100 valid links are capped at 50 in the output."""
    anchors = "".join(
        f'<a href="https://site.com/story/{i}">story {i}</a>' for i in range(100)
    )
    _, raw = strip_markup(f"<body>{anchors}</body>")
    links = extract_links("https://base.com/", raw)
    assert len(links) == 50
