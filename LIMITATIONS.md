# Limitations, source rights and safety

## Known gaps

- **Not yet run end to end.** This version was written against the published evaluation
  contract without network access; run the smoke test before submitting and compare the
  envelope against the starter kit's output contract (see `SUBMISSION.md`).
- **NAV job feed** follows NAV's public token + feed flow as documented at the time of
  writing. If NAV changes it, `run-report.json` shows `nav_feed.state` other than `ready`
  and hiring falls back to company careers pages. The feed is read from its start each run
  (bounded by `SIGNALPOST_NAV_MAX_PAGES` and 15% of the time budget).
- **Earlier annual accounts**: the open accounts API may serve only the latest year. Earlier
  years are added when served and are kept from previous runs as `historical_claims`.
- **JavaScript-only sites** are not rendered (no headless browser), so their careers and news
  sections may be missed. Identity checks still use the static HTML.
- **Reviews, ratings and sentiment** are not collected.

## Source rights

- Brønnøysundregistrene data is open public data (NLOD) served by official APIs.
- NAV Arbeidsplassen's job feed is a public API with a published public token. The index is
  built newest-first; `run-report.json` → `nav_feed.reached_feed_end` says whether the walk
  was complete (only a complete walk can retire a previously seen job as `closed_job`).
- Norid RDAP (`rdap.norid.no`) is the `.no` registry's public lookup service; it is queried
  at most once per candidate domain and only a positive registrant match is used.
- Wikidata is CC0; one SPARQL query per 200 companies.
- Company websites: robots.txt is honoured for every company-site request; 5xx on robots.txt
  is treated as disallow; at most 2 concurrent requests per host; identifying User-Agent.
- LinkedIn, Facebook, Instagram, X, YouTube and similar platforms are **never fetched**. Only
  the link published on the company's own verified site is recorded.
- Search APIs (optional) are used only to discover candidate domains; nothing is published
  from a search result.

## Secrets

- No credentials are required. Optional search keys are read from environment variables on
  the server only and are never written to envelopes, reports or logs.

## Outbound URL safety

- Only `http`/`https`; loopback, private, link-local and other non-public IP literals and
  internal hostnames (`localhost`, `.local`, `.internal`, …) are refused, including on every
  redirect hop.
- Responses are size-capped (2.5 MB pages, 30 MB NAV feed pages) and time-capped.
- Hostnames are not re-resolved to check for DNS rebinding.
