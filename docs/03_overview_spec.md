# Document 3: Project Overview & Success Criteria
## WebFetch API — Purpose, Goals, and Definition of Done

---

## 1. What This Is

WebFetch is a locally-hosted API that acts as a clean web intelligence layer between Claude and the raw internet.

It handles fetching, parsing, caching, and summarizing web content — so Claude never touches raw HTML, never burns tokens on markup, and always reasons from actual article text rather than noise.

Two endpoints. That's it. The complexity lives inside the API, not inside Claude's context window.

---

## 2. The Problem Being Solved

### The token trap
When an AI model fetches a web page directly, it receives the full raw response: HTML tags, navigation menus, cookie banners, ad containers, footer links, JavaScript snippets, and tracking parameters — wrapped around the actual content the model needs.

A typical news article is 800 words of content buried inside 40,000 characters of markup. The model pays for all 40,000.

For expensive frontier models (Opus, GPT-4 class), this is not a minor inefficiency. It is a structural cost multiplier on every research task.

### The quality trap
Even when raw content is retrieved, the model has no signal about source credibility. A Reuters wire report and a random blog post arrive identically formatted. The model must either guess or burn more tokens evaluating source quality inline.

### The freshness trap
Cached or stale data served without metadata means the model cannot know whether it is reasoning from yesterday's article or last year's. For financial data, political events, and breaking news, this is a correctness risk.

---

## 3. How WebFetch Solves This

| Problem | Solution |
|---|---|
| Token waste on markup | trafilatura + BeautifulSoup4 strips all markup before content reaches Claude |
| Poor source quality signal | Every result carries `source_tier`, `domain`, and `published_date` |
| Stale data without metadata | Every response carries `cached_at`, `cache_age_hours`, `published_date` |
| Paywalled premium sources | Authenticated Chrome session handles login state transparently |
| Cookie walls and JS-heavy pages | Playwright fallback via persistent Chrome profile |
| Repeated fetches of same content | MySQL cache eliminates redundant network calls |
| Summary vs full text decision | Claude chooses `verbosity`; short pages always deliver verbatim |

---

## 4. Goals

### Primary goal
Enable Claude to research any topic — including content behind paywalls — using clean, source-attributed text, at a fraction of the token cost of raw web fetching.

### Secondary goals
- Make research output **structurally stable**: same query returns the same response *shape* every time, so Claude never branches on format. (Content is not deterministic — Exa re-ranks over time, the web changes, summaries vary — and the spec does not pretend otherwise.)
- Keep the API **invisible to Claude during use**: simple tool calls, structured responses, no parsing burden.
- Make the cache **a research asset**: cached pages accumulate over time into a local knowledge base that can be queried across sessions.

---

## 5. Success Criteria

### 5.1 Token efficiency
> **A research session using WebFetch must consume fewer tokens than the equivalent session using raw web fetching.**

Baseline comparison: fetching 5 articles raw vs. fetching 5 articles via WebFetch with `verbosity="summary"`.

Expected result: 80-95% token reduction per article when using summary mode. Full text mode should still represent a significant reduction over raw HTML.

> **`summary` is for triage, not for verification.** Use `summary` to decide which of N results are worth reading. Once a specific factual claim matters — "did X actually say Y?", exact figures, anything you'd quote or cross-reference — fetch `text` and reason from the source words, not from a Haiku paraphrase. Never settle a payoff claim on a summary alone.

### 5.2 Source quality
> **Claude must be able to cross-reference a factual claim across tier1 sources without manual URL selection.**

A `search()` call on any significant news event must return at least 2 tier1 sources in the result set. Claude can then `fetch()` those specifically and cross-reference.

### 5.3 Authenticated session access
> **Sites you are already logged in to via your own Chrome profile must render their full content through that session, not a logged-out/JS-stub version.**

This is your own authenticated browser, used for sites you have legitimate access to — the goal is full rendering of login-gated or JS-heavy pages, not circumventing access you don't have. Verified by comparing `page_size_chars` for a known article against a logged-out fetch of the same URL: the session version should be substantially larger (a stub/login wall is typically under 2,000 characters; a fully rendered article 4,000–15,000).

### 5.4 Freshness control
> **Claude must be able to force a cache bypass for time-sensitive queries and receive content with explicit age metadata.**

Verified by calling `fetch(url, cache_reload=True)` and confirming `cached_at` is within the last 60 seconds and `cached=false` in the response.

### 5.5 Reliability
> **fetch() must succeed on at least 90% of URLs from tier1 domains.**

httpx handles the majority. Playwright with authenticated session handles the rest. A combined failure rate above 10% on tier1 sources indicates a configuration problem, not expected behavior.

---

## 6. What Claude Gets

When WebFetch is running and connected via MCP, Claude has access to two tools that together cover the full research workflow:

**For a typical research question like:**
> *"Is it true that Elon Musk said X, and that Y has sold their stake in Z?"*

Claude's workflow becomes:

```
1. search("Elon Musk X statement", date_from="2026-06-01")
   → 12 results, source_tier and published_date visible
   → Claude selects 2-3 tier1 results worth verifying

2. fetch(reuters_url, verbosity="full")
   → Clean article text, no markup, author and date confirmed

3. fetch(ft_url, verbosity="full")
   → Cross-reference confirmed or contradicted

4. fetch(sec_url, verbosity="full", include_links=True)
   → Primary source with further document links if needed
```

Total tokens spent: on the article content itself. Zero tokens spent on HTML, navigation, ads, or cookie notices.

**The result:** Claude reasons from real journalism and primary sources, with explicit source attribution and date context — at the cost of a fraction of what raw fetching would require.

This makes WebFetch a force multiplier for any frontier model used in research, fact-checking, financial analysis, or competitive intelligence tasks.

---

## 7. What This Is Not

- Not a general-purpose web browser for Claude
- Not a replacement for Claude's own reasoning — it delivers content, Claude does the thinking
- Not a scraping tool for bulk data collection
- Not dependent on any third-party AI service for core functionality (Haiku is used only for summaries — the fetch and search pipeline works without it)
