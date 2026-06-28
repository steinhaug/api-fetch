# WebFetch — Change Request for Claude Code

Endringer mot eksisterende spek (Doc 1 = return contract, Doc 2 = API/impl, Doc 3 = overview).
Dette er en **breaking change** på `fetch()`-signaturen og return-strukturen. Det er villet —
hele stacken er vår, ingen ekstern konsument å beholde bakoverkompatibilitet for.

Konsumenten (Claude) har spesifisert formatet under. Bygg backend til å matche det; det interne
står du fritt på så lenge return-kontrakten holder.

---

## 1. Ny `fetch()`-signatur

Erstatt `return_type` (enum `summary|text|text+links`) med to **ortogonale** akser:

```python
fetch(
    url: str,
    verbosity: str = "summary",   # "summary" | "full"
    include_links: bool = False,
    cache_reload: bool = False,
    max_age_hours: int = 24,
)
```

Begrunnelse: med både threshold-kortslutning og "kun lenker"-behov eksploderer enum-varianten
kombinatorisk (`summary+links`? `verbatim+links`?). To bool-/enum-akser holder det flatt:
*hvor mye tekst* (`verbosity`) og *lenker eller ikke* (`include_links`) er uavhengige valg.

`text+links`-variantens rare bieffekt (returnerte både summary OG text) forsvinner med dette.

---

## 2. Ny `fetch()` return-kontrakt (erstatter Doc 1 §2)

```json
{
  "url": "https://reuters.com/business/tesla-board-2026-06-25/",
  "domain": "reuters.com",
  "title": "Tesla board member resigns amid shareholder pressure",
  "published_date": "2026-06-25",
  "author": "Jane Smith",

  "content": "Full verbatim article text here…",
  "content_kind": "verbatim",
  "verbatim_size_chars": 7360,
  "verbatim_size_tokens": 1840,
  "truncated": false,

  "links": null,

  "fetch_mode": "httpx",
  "cached": true,
  "cached_at": "2026-06-27T14:35:00Z",
  "cache_age_hours": 0.5,

  "meta": {
    "source_tier": "tier1",
    "is_premium_source": false,
    "fetch_mode_reason": "httpx_success"
  },
  "error": null
}
```

### Feltendringer mot Doc 1 §2

| Endring | Detalj |
|---|---|
| **Fjern** `content.{summary,text,links}` (tre nullable slots) | Erstattes av ett `content`-felt + diskriminator. Jeg skal ikke gjette hvilken slot som er ikke-null. |
| **Ny:** `content` (string\|null, required) | Selve innholdet. Hva slags innhold sier `content_kind`. |
| **Ny:** `content_kind` (string, required) | `"verbatim"` \| `"summary"`. **Load-bearing felt.** `verbatim` = eksakt kildetekst, trygt å sitere. `summary` = Haiku-parafrase, kun triage — aldri siter. |
| **Rename + behold:** `page_size_chars` → `verbatim_size_chars` (int, required) | Størrelse på *full* tekst, **alltid satt** — også når `content_kind=summary`. Lar meg vite hva en eskalering koster. |
| **Ny:** `verbatim_size_tokens` (int, required) | Token-tall for full tekst, beregnet med ekte tokenizer serverside (ikke chars/4). Dette er tallet jeg budsjetterer mot. |
| **Ny:** `truncated` (bool, required) | `false` normalt. Verbatim trunkeres aldri stille. `true` kun hvis en hard sikkerhetstak ble truffet. |
| **Flytt:** `links` til toppnivå (array\|null, required) | Populeres kun når `include_links=true`. Ellers `null`. |
| **Fjern:** `return_type`-echo | Jeg vet hva jeg ba om. Jeg trenger å vite hva jeg *fikk* → det er `content_kind`. |

`title`, `published_date`, `author`, `fetch_mode`, `cached`, `cached_at`, `cache_age_hours`,
`meta.*`, `error` — uendret fra Doc 1.

`is_premium_source` beholdes, men reframes (se §6).

---

## 3. Threshold-kortslutning (ny intern logikk)

Ny config-konstant:

```python
# config.py
SUMMARY_THRESHOLD_TOKENS = 2000   # under dette: summer aldri, lever verbatim
```

Ny execution-flow for `fetch()` (erstatter Doc 2 §4.1 steg 7–9):

```
6. extract_links(html) → lagre links_json
7. verbatim_size_tokens = tokenize(stripped_text)   # ekte tokenizer, lagres på raden
8. Bestem content + content_kind:
     if verbatim_size_tokens <= SUMMARY_THRESHOLD_TOKENS:
         content = stripped_text
         content_kind = "verbatim"
         # HOPP OVER Haiku-kallet helt — sparer et modellkall + latency
     elif verbosity == "full":
         content = stripped_text
         content_kind = "verbatim"
     else:  # verbosity == "summary"
         content = summary   # Haiku, caches i pages.summary
         content_kind = "summary"
9. links = links_json if include_links else None
10. verbatim_size_chars og verbatim_size_tokens settes ALLTID, uansett gren
```

Konsekvens: ber jeg om `summary` men siden er liten, får jeg `content_kind="verbatim"` tilbake —
det *er* signalet om at kortslutningen slo til. Ingen egne flagg trengs, ingen bortkastet
Haiku-runde på korte sider.

---

## 4. Token-telling

`verbatim_size_tokens` må beregnes med en faktisk tokenizer, ikke estimat:
- bruk `tiktoken` (`cl100k_base`) eller Anthropics token-count.
- beregn én gang ved fetch, **lagre på `pages`-raden** (ny kolonne), server fra cache deretter.

DB-endring:

```sql
ALTER TABLE pages ADD COLUMN page_size_tokens INT UNSIGNED AFTER page_size_chars;
```

(Behold DB-kolonnenavn `page_size_chars`; map til `verbatim_size_chars` i response-builderen.
DB-navn er internt, kontrakt-navn er det jeg ser.)

---

## 5. Lenker uten å re-hente fulltekst

`include_links=true` skal lese fra `pages.links_json` (allerede ekstrahert og lagret ved første
fetch). En page som alt er hentet verbatim har lenkene i cache — `fetch(url, verbosity="summary",
include_links=true)` på den returnerer da cachet summary + cachet lenker, uten nettverkskall og
uten å sende hele verbatim-teksten på nytt. Det løser "dumt å hente alt igjen bare for lenkene".

---

## 6. `is_premium_source` — reframe (ikke fjern)

Den er **ikke** et troverdighetssignal — `source_tier` eier den aksen. Dokumentér den om til
*tilgangsmekanisme*:

> `is_premium_source=true` betyr at fulltekst er hentbar via den autentiserte sesjonen. I et
> **search-resultat** er dette signalet: en tynn highlight her betyr "snippet er begrenset, ikke
> artikkelen" — verdt å fetche.

Ingen kodeendring utover å oppdatere docstring/kommentar til denne betydningen.

---

## 7. `search()` + premium-inkludering (Q2)

To-fase-designet er korrekt og beholdes: billig search for triage → målrettet autentisert fetch
for fulltekst. **Ikke** dra autentisert fulltekst inn i search-highlights — det dreper
token-gevinsten.

Endringer:

1. **Avstem `PREMIUM_SOURCES` mot virkeligheten.** Lista er i dag `wapo, nyt, ft, wsj`, men
   reuters (som du regner som live premium) står *ikke* der. Verifiser hvilke domener som faktisk
   krever den innloggede sesjonen din:
   - er reuters httpx-hentbar uten login → la den være tier1, ikke premium (ingen endring).
   - krever den sesjonen → legg `reuters.com` i `PREMIUM_SOURCES`.
   Mekanismen treffer bare domener som faktisk står i lista.

2. **Behold det parallelle premium-søket** (Doc 2 §4.6 steg 5): `includeDomains = PREMIUM_SOURCES`,
   merget inn. Det er det som garanterer at premium-URLer overhodet dukker opp i resultatsettet.

3. **Merge/dedup må ikke droppe en relevant premium-treff pga. tynn highlight.** Premium-resultater
   med svak Exa-snippet skal fortsatt med — highlighten er triage, ikke kvalitetsfilter.

4. `is_premium_source` i search-resultatet beholdes med betydningen fra §6.

---

## 8. Nye MCP-docstrings (Doc 2 §5) — trappen MÅ stå her

Dette er teksten jeg faktisk handler på. Erstatt begge docstrings:

```python
@mcp.tool()
def fetch(url, verbosity="summary", include_links=False,
          cache_reload=False, max_age_hours=24) -> dict:
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


@mcp.tool()
def search(terms, date_from=None, date_to=None, max_results=10,
           domains=None, exclude_domains=None) -> dict:
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
```

---

## 9. Avledede oppdateringer i Doc 3 (overview)

- §5.1: erstatt `return_type=summary` / `return_type=text` med `verbosity="summary"` /
  `verbosity="full"`.
- §6 workflow-eksempel: `fetch(url, return_type="text")` → `fetch(url, verbosity="full")`;
  `fetch(sec_url, return_type="text+links")` → `fetch(sec_url, verbosity="full", include_links=True)`.
- §3-tabellen: rad "Summary vs full text decision" → "Claude velger `verbosity`; korte sider
  leverer alltid verbatim".

---

## Definition of done

- [ ] `fetch()` tar `verbosity` + `include_links`, ikke `return_type`.
- [ ] Return matcher §2 nøyaktig; `content_kind`, `verbatim_size_chars`, `verbatim_size_tokens`
      alltid satt.
- [ ] Sider <= `SUMMARY_THRESHOLD_TOKENS` returnerer verbatim uten Haiku-kall.
- [ ] `verbatim_size_tokens` beregnet med ekte tokenizer og cachet på raden.
- [ ] `include_links=true` leser fra `links_json`, utløser aldri re-fetch.
- [ ] `PREMIUM_SOURCES` avstemt mot faktisk sesjonstilgang.
- [ ] Premium-treff med tynn highlight overlever merge/dedup.
- [ ] Begge MCP-docstrings oppdatert med trappen.
- [ ] Error-in-band med uforanderlig shape beholdt fra Doc 1 §4.
