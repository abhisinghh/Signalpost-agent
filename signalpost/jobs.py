"""NAV Arbeidsplassen public job-vacancy feed (pam-stilling-feed).

Why it matters: NAV ads carry the employer's organisation number, so a job can be
attributed to the exact legal entity (or one of its registered subunits) with no
name guessing. Matching flow:

1. Build an index of ACTIVE feed entries keyed by normalised employer name
   (bounded by SIGNALPOST_NAV_MAX_PAGES / time budget).
2. For a company, look up its legal name, public brand and subunit names.
3. Fetch the full ad and publish it ONLY if employer.orgnr is the company's own
   organisation number or one of its registered subunits.

The feed endpoints and token flow follow NAV's public documentation at the time
of writing; if NAV changes them the index reports `state != "ready"` and the
hiring family falls back to company careers pages. Set SIGNALPOST_NAV=0 to disable.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import defaultdict
from urllib.parse import urljoin

from .claims import Claims
from .http import Ctx, Fetcher
from .util import fold, short_hash, strip_legal_form

TOKEN_URL = "https://pam-stilling-feed.nav.no/api/publicToken"
FEED_URL = "https://pam-stilling-feed.nav.no/api/v1/feed"
ENTRY_URL = "https://pam-stilling-feed.nav.no/api/v1/feedentry/{uuid}"
PUBLIC_AD_URL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
SRC = "public_job_feed"
_JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


def name_key(name: str | None) -> str:
    return fold(strip_legal_form(name or ""))


class NavJobIndex:
    def __init__(self, fetcher: Fetcher):
        self.f = fetcher
        self.enabled = os.environ.get("SIGNALPOST_NAV", "1") != "0"
        self.state = "not_built" if self.enabled else "disabled"
        self.error: str | None = None
        self.token: str | None = None
        self.by_name: dict[str, set[str]] = defaultdict(set)
        self.meta: dict[str, dict] = {}
        self.pages = 0
        self.complete = False
        self.ready = asyncio.Event()

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    async def build(self, budget_s: float) -> None:
        try:
            await self._build(budget_s)
        except Exception as e:  # never fatal for the run
            self.state, self.error = "failed", f"{type(e).__name__}: {e}"[:300]
        finally:
            self.ready.set()

    async def _build(self, budget_s: float) -> None:
        if not self.enabled:
            return
        deadline = time.monotonic() + budget_s
        tok = await self.f.get(TOKEN_URL, source_class=SRC, check_robots=False, keep=False, timeout=10)
        m = _JWT.search(tok.text) if tok.ok else None
        if not m:
            self.state, self.error = "unavailable", f"public token not obtained ({tok.error or 'no token in body'})"
            return
        self.token = m.group(0)
        max_pages = int(os.environ.get("SIGNALPOST_NAV_MAX_PAGES", "400"))
        self.state = "building"

        # The feed is chronological and years long. Active ads live at the newest end, so walk
        # backwards from the last page (`?last=true` -> previous_url). Entries are visited newest
        # first; the first status seen for a uuid is its latest status, so earlier (older) entries
        # for the same uuid are ignored.
        start = os.environ.get("SIGNALPOST_NAV_FEED_START")
        url, direction = (start, "next_url") if start else (FEED_URL + "?last=true", "previous_url")
        seen: set[str] = set()
        first_page = True
        while url and self.pages < max_pages and time.monotonic() < deadline:
            r = await self.f.get(url, source_class=SRC, headers=self.headers(), check_robots=False,
                                 keep=False, max_bytes=30_000_000, timeout=30)
            if not r.ok and first_page and direction == "previous_url":
                # `?last=true` unsupported: fall back to a forward walk from the start.
                url, direction, first_page = FEED_URL, "next_url", False
                continue
            first_page = False
            if not r.ok:
                self.error = f"feed page failed: {r.error} {r.status}"
                break
            try:
                d = r.json()
            except ValueError:
                self.error = "feed page was not JSON"
                break
            items = d.get("items") or []
            ordered = reversed(items) if direction == "previous_url" else items
            for it in ordered:
                fe = it.get("_feed_entry") or {}
                uuid = fe.get("uuid") or it.get("id")
                if not uuid:
                    continue
                status = (fe.get("status") or "").upper()
                if direction == "previous_url":
                    if uuid in seen:
                        continue  # an older entry for an ad whose latest state we already know
                    seen.add(uuid)
                    if status and status != "ACTIVE":
                        continue
                else:
                    if status and status != "ACTIVE":
                        old = self.meta.pop(uuid, None)
                        if old:
                            self.by_name[old["key"]].discard(uuid)
                        continue
                key = name_key(fe.get("businessName"))
                if not key:
                    continue
                old = self.meta.get(uuid)
                if old and old["key"] != key:
                    self.by_name[old["key"]].discard(uuid)
                self.meta[uuid] = {"key": key, "title": fe.get("title") or it.get("title"),
                                   "modified": it.get("date_modified") or fe.get("sistEndret"),
                                   "municipal": fe.get("municipal")}
                self.by_name[key].add(uuid)
            self.pages += 1
            nxt = d.get(direction)
            if not nxt or not items:
                self.complete = True  # reached the end of the feed in this direction
                break
            url = urljoin(FEED_URL, nxt)
        self.state = "ready" if self.meta else ("empty" if not self.error else "failed")

    def candidates(self, names: list[str]) -> list[str]:
        out: list[str] = []
        for n in names:
            k = name_key(n)
            if len(k) < 3:
                continue
            for u in self.by_name.get(k, ()):
                if u not in out:
                    out.append(u)
        return out

    async def company_jobs(self, ctx: Ctx, cl: Claims, orgnr: str, names: list[str],
                           subunit_orgnrs: set[str], max_details: int = 12) -> tuple[int, bool]:
        """Publish NAV ads whose employer orgnr is this company or one of its subunits.

        Returns (jobs_published, checked).
        """
        if self.state not in ("ready", "empty"):
            return 0, False
        allowed = {orgnr} | set(subunit_orgnrs)
        n = 0
        complete = self.complete  # a partial feed index cannot prove a job closed
        cands = self.candidates(names)
        if len(cands) > max_details:
            complete = False
        for uuid in cands[:max_details]:
            if ctx.expired(4.0):
                complete = False
                break
            r = await self.f.get(ENTRY_URL.format(uuid=uuid), ctx=ctx, source_class=SRC,
                                 headers=self.headers(), check_robots=False)
            if not r.ok:
                complete = False
                continue
            try:
                d = r.json()
            except ValueError:
                complete = False
                continue
            ad = d.get("ad_content") or d
            emp = ad.get("employer") or {}
            emp_org = re.sub(r"\D", "", str(emp.get("orgnr") or ""))
            if emp_org not in allowed:
                continue  # same name, different legal entity: never publish
            status = (d.get("status") or ad.get("status") or "ACTIVE").upper()
            if status != "ACTIVE":
                continue
            locs = ad.get("workLocations") or []
            loc = ", ".join(x for x in {(l.get("city") or l.get("municipal") or "").title() for l in locs if isinstance(l, dict)} if x)
            value = {
                "title": ad.get("title"),
                "url": PUBLIC_AD_URL.format(uuid=uuid),
                "apply_url": ad.get("applicationUrl") or ad.get("sourceurl"),
                "date_posted": (ad.get("published") or "")[:10] or None,
                "valid_through": (ad.get("expires") or "")[:10] or None,
                "application_due": ad.get("applicationDue"),
                "location": loc or None,
                "employer_name": emp.get("name"),
                "employer_orgnr": emp_org,
                "employer_is_subunit": emp_org != orgnr,
                "source": "NAV Arbeidsplassen",
            }
            if cl.add("hiring", "job_posting", value, fr=r, method="nav_feed:employer.orgnr",
                      locator="/ad_content/employer/orgnr", excerpt=f"{ad.get('title')} — orgnr {emp_org}",
                      key=f"hiring:job:nav:{uuid}", basis="orgnr_in_source",
                      period={"date_posted": value["date_posted"], "valid_through": value["valid_through"]}):
                n += 1
        return n, complete
