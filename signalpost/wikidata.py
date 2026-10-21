"""Wikidata: organisation number (P2333) -> official website (P856).

One SPARQL query per 200 companies at the start of the run. Coverage is limited to
notable companies, but the link is keyed by the exact organisation number, so it is a
strong website candidate. The site itself must still pass the identity probe.
Set SIGNALPOST_WIKIDATA=0 to disable.
"""
from __future__ import annotations

import os

from .http import Fetcher
from .identity import normalize_site_url

SPARQL_URL = "https://query.wikidata.org/sparql"


async def website_candidates(f: Fetcher, orgnrs: list[str], budget_s: float = 60.0) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if os.environ.get("SIGNALPOST_WIKIDATA", "1") == "0" or not orgnrs:
        return out
    chunks = [orgnrs[i: i + 200] for i in range(0, len(orgnrs), 200)]
    per_chunk = max(8.0, min(20.0, budget_s / max(1, len(chunks))))
    for chunk in chunks:
        values = " ".join(f'"{o}"' for o in chunk)
        query = ("SELECT ?org ?site WHERE { VALUES ?org { " + values + " } "
                 "?item wdt:P2333 ?org . ?item wdt:P856 ?site . }")
        r = await f.get(SPARQL_URL, source_class="wikidata", params={"query": query, "format": "json"},
                        headers={"Accept": "application/sparql-results+json"}, check_robots=False,
                        keep=False, timeout=per_chunk)
        if not r.ok:
            continue
        try:
            rows = r.json().get("results", {}).get("bindings", [])
        except (ValueError, AttributeError):
            continue
        for b in rows:
            org = (b.get("org") or {}).get("value")
            site = normalize_site_url((b.get("site") or {}).get("value"))
            if org and site:
                out.setdefault(org, [])
                if site not in out[org]:
                    out[org].append(site)
    return out
