"""Optional search-API candidate discovery. Results are candidates only, never evidence.

Enabled only when a server-side key is present in the environment:
  SIGNALPOST_BRAVE_API_KEY   -> Brave Search API
  SIGNALPOST_SERPER_API_KEY  -> Serper (Google results API)
Budget: SIGNALPOST_SEARCH_MAX_QUERIES (default 2000 per run).
"""
from __future__ import annotations

import json
import os

import httpx

from .http import Ctx
from .util import spaced_orgnr, strip_legal_form


class SearchClient:
    def __init__(self, provider: str, key: str, max_queries: int):
        self.provider = provider
        self.key = key
        self.max_queries = max_queries
        self.queries = 0
        self._client = httpx.AsyncClient(timeout=10.0)

    @classmethod
    def from_env(cls) -> "SearchClient | None":
        maxq = int(os.environ.get("SIGNALPOST_SEARCH_MAX_QUERIES", "2000"))
        if os.environ.get("SIGNALPOST_BRAVE_API_KEY"):
            return cls("brave", os.environ["SIGNALPOST_BRAVE_API_KEY"], maxq)
        if os.environ.get("SIGNALPOST_SERPER_API_KEY"):
            return cls("serper", os.environ["SIGNALPOST_SERPER_API_KEY"], maxq)
        return None

    async def aclose(self):
        await self._client.aclose()

    async def find(self, ctx: Ctx, orgnr: str, legal_name: str, enhet: dict) -> list[str]:
        queries = [f'"{spaced_orgnr(orgnr)}" OR "{orgnr}"']
        addr = enhet.get("forretningsadresse")
        city = ((addr.get("poststed") if isinstance(addr, dict) else "") or "").title()
        base = strip_legal_form(legal_name)
        if base:
            queries.append(f'"{base}" {city}'.strip())
        urls: list[str] = []
        for q in queries:
            if self.queries >= self.max_queries or ctx.expired(10.0):
                break
            self.queries += 1
            ctx.requests += 1
            try:
                urls += await (self._brave(q) if self.provider == "brave" else self._serper(q))
            except (httpx.HTTPError, ValueError, KeyError) as e:
                ctx.error("search", "provider_error", str(e))
            if urls:
                break
        return urls[:8]

    async def _brave(self, q: str) -> list[str]:
        r = await self._client.get("https://api.search.brave.com/res/v1/web/search",
                                   params={"q": q, "country": "no", "count": "10"},
                                   headers={"X-Subscription-Token": self.key, "Accept": "application/json"})
        r.raise_for_status()
        return [x.get("url") for x in (r.json().get("web") or {}).get("results", []) if x.get("url")]

    async def _serper(self, q: str) -> list[str]:
        r = await self._client.post("https://google.serper.dev/search",
                                    content=json.dumps({"q": q, "gl": "no", "num": 10}),
                                    headers={"X-API-KEY": self.key, "Content-Type": "application/json"})
        r.raise_for_status()
        return [x.get("link") for x in r.json().get("organic", []) if x.get("link")]
