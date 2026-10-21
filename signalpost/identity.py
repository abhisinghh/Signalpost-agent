"""Exact-entity website resolution.

A domain is only a candidate until it is tied back to the organisation number.
Order of candidates (most authoritative first):

1. website declared in Enhetsregisteret (`hjemmeside`)
2. website declared on one of the company's own registered subunits
3. the domain of the email address declared in the register
4. name-derived domain guesses (only accepted with org-number proof or name+address/phone proof)
5. optional search API results (same proof required)

Acceptance never relies on name similarity alone.
"""
from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from rapidfuzz import fuzz

from .http import Ctx, Fetcher, FetchResult
from .util import digits, find_orgnrs, fold, is_generic_name, slug_variants, strip_legal_form

FREE_MAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.no",
    "live.com", "msn.com", "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "online.no",
    "frisurf.no", "broadpark.no", "start.no", "getmail.no", "c2i.net", "lyse.net", "altibox.no",
    "haugnett.no", "tele2.no", "chello.no", "netcom.no", "telia.com", "telenor.no", "aol.com",
    "protonmail.com", "proton.me", "mail.com", "gmx.com", "gmx.net", "ebnett.no", "enivest.net",
    "hesbynett.no", "sensewave.com", "post.com", "epost.no", "bluezone.no", "loqal.no", "vikenfiber.no",
}

DIRECTORY_DOMAINS = {
    "proff.no", "purehelp.no", "gulesider.no", "1881.no", "brreg.no", "regnskapstall.no", "forvalt.no",
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "tiktok.com",
    "finn.no", "kompass.com", "bedriftsdatabasen.no", "allabolag.se", "dnb.com", "opencorporates.com",
    "northdata.com", "northdata.de", "infobel.com", "norgesregisteret.no", "krafttak.no", "hvemer.no",
    "firmasok.no", "nettbutikk.no", "wix.com", "wixsite.com", "squarespace.com", "google.com",
    "wikipedia.org", "bing.com", "duckduckgo.com", "yelp.com", "tripadvisor.com", "tripadvisor.no",
    "booking.com", "eniro.no", "arbeidsplassen.nav.no", "nav.no", "kunngjoringer.no",
    "domeneshop.no", "domene.shop", "one.com", "sedo.com", "dan.com", "hugedomains.com", "godaddy.com",
}

PARKED_RE = re.compile(
    r"(domain (name )?is for sale|domenet er til salgs|this domain may be for sale|buy this domain|"
    r"parkingcrew|sedoparking|domain parking|is parked free|denne domenen er parkert|"
    r"domenet er registrert hos|this domain is registered at|hugedomains|domain has expired|"
    r"website is under construction|account suspended|default web site page|it works!)",
    re.I,
)

VERIFY_LINK_RE = re.compile(
    r"(kontakt|contact|om-?oss|about|personvern|privacy|vilk[åa]r|terms|impressum|juridisk|legal|"
    r"firma|selskap|company|info)", re.I)

TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "com.au", "co.nz", "co.za", "com.br", "co.jp", "com.tr", "com.cn"}


def registrable(host: str | None) -> str:
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def is_directory(host: str | None) -> bool:
    r = registrable(host)
    h = (host or "").lower()
    return r in DIRECTORY_DOMAINS or any(h == d or h.endswith("." + d) for d in DIRECTORY_DOMAINS)


def normalize_site_url(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip("/").replace(" ", "")
    if not raw or "@" in raw:
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
    except ValueError:
        return None
    if not p.hostname or "." not in p.hostname:
        return None
    path = p.path if p.path and p.path != "/" else ""
    return f"{p.scheme.lower()}://{p.hostname.lower()}{path}"


@dataclass
class Candidate:
    url: str
    source: str  # registry_website | subunit_website | registry_email_domain | name_guess | search
    note: str | None = None


@dataclass
class Verdict:
    candidate: Candidate
    decision: str  # accepted | ambiguous | rejected | blocked | failed (transient)
    basis: str | None = None
    relationship: str = "exact"
    extract_site_facts: bool = False
    home: FetchResult | None = None
    pages: list[FetchResult] = field(default_factory=list)
    proof: FetchResult | None = None
    proof_excerpt: str | None = None
    foreign_orgnrs: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    reason: str = ""

    def summary(self) -> dict:
        return {
            "url": self.candidate.url, "source": self.candidate.source, "decision": self.decision,
            "basis": self.basis, "relationship": self.relationship, "reason": self.reason,
            "final_url": self.home.where if self.home else None, "signals": self.signals,
            "foreign_orgnrs": self.foreign_orgnrs[:5],
        }


@dataclass
class Resolution:
    state: str  # available | not_available | ambiguous | blocked
    verdict: Verdict | None
    tried: list[dict]
    reason: str
    checked: bool  # True when at least one source was examined successfully or no candidates exist


class WebsiteResolver:
    def __init__(self, fetcher: Fetcher, search=None, max_probes: int = 7):
        self.f = fetcher
        self.search = search
        self.max_probes = max_probes
        self.dns_enabled = True

    async def calibrate_dns(self) -> None:
        """Disable the DNS pre-check when local resolution does not work (e.g. proxy-only egress)."""
        self.dns_enabled = await self._resolves("data.brreg.no", force=True)

    async def _resolves(self, host: str, force: bool = False) -> bool:
        if not self.dns_enabled and not force:
            return True
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), 4.0)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ candidates
    def candidates(self, enhet: dict, subunits: list[dict], legal_name: str, guesses: bool,
                   wikidata_urls: list[str] | None = None) -> list[Candidate]:
        out: list[Candidate] = []
        seen: set[str] = set()

        def push(url, source, note=None):
            u = normalize_site_url(url)
            if not u:
                return
            host = urlsplit(u).hostname
            r = registrable(host)
            if r in seen or is_directory(host):
                return
            seen.add(r)
            out.append(Candidate(u, source, note))

        push(enhet.get("hjemmeside"), "registry_website")
        for u in wikidata_urls or []:
            push(u, "wikidata_orgnr")  # Wikidata item carrying this exact organisation number
        for u in subunits:
            push(u.get("hjemmeside"), "subunit_website", u.get("organisasjonsnummer"))
        email = (enhet.get("epostadresse") or "").strip().lower()
        if "@" in email:
            dom = email.split("@", 1)[1].strip(". ")
            if dom and dom not in FREE_MAIL and registrable(dom) not in FREE_MAIL:
                push(dom, "registry_email_domain")
        if guesses:
            base = strip_legal_form(legal_name)
            if base and not is_generic_name(base):
                for slug in slug_variants(base)[:4]:
                    push(f"{slug}.no", "name_guess")
                for slug in slug_variants(base)[:1]:
                    push(f"{slug}.com", "name_guess")
            # Registered workplaces often carry the public brand ("Hansen Holding AS" owns
            # "Kafé Solsiden"). Guess from the most common distinct subunit names too; the
            # same proof gates apply, so this only adds candidates, never trust.
            seen_names: dict[str, int] = {}
            for u in subunits:
                b = strip_legal_form(u.get("navn") or "")
                if b and fold(b) != fold(base) and not is_generic_name(b):
                    seen_names[b] = seen_names.get(b, 0) + 1
            for b, _ in sorted(seen_names.items(), key=lambda kv: -kv[1])[:2]:
                for slug in slug_variants(b)[:2]:
                    push(f"{slug}.no", "subunit_name_guess", b)
        return out

    # ------------------------------------------------------------------ resolve
    async def resolve(self, ctx: Ctx, orgnr: str, enhet: dict, subunits: list[dict],
                      wikidata_urls: list[str] | None = None) -> Resolution:
        legal_name = enhet.get("navn") or ""
        cands = self.candidates(enhet, subunits, legal_name, guesses=not ctx.registry_only,
                                wikidata_urls=wikidata_urls)
        tried: list[dict] = []
        verdicts: list[Verdict] = []
        accepted: Verdict | None = None
        probes = 0

        async def run(cs: list[Candidate]):
            nonlocal accepted, probes
            for c in cs:
                if probes >= self.max_probes or ctx.expired(8.0):
                    break
                if any(registrable(urlsplit(c.url).hostname) == registrable(urlsplit(t["url"]).hostname) for t in tried):
                    continue
                probes += 1
                v = await self.probe(ctx, orgnr, enhet, c)
                verdicts.append(v)
                tried.append(v.summary())
                if v.decision == "accepted":
                    if accepted is None or _rank(v) > _rank(accepted):
                        accepted = v
                    if v.basis in ("orgnr_on_site", "domain_registrant_orgnr"):
                        return

        await run(cands)
        if (accepted is None or accepted.basis != "orgnr_on_site") and self.search and not ctx.registry_only \
                and not ctx.expired(15.0) and accepted is None:
            try:
                urls = await self.search.find(ctx, orgnr, legal_name, enhet)
            except Exception as e:  # search is optional; never fatal
                ctx.error("website_search", "search_failed", str(e))
                urls = []
            more = []
            for u in urls:
                n = normalize_site_url(u)
                if n and not is_directory(urlsplit(n).hostname):
                    p = urlsplit(n)
                    more.append(Candidate(f"{p.scheme}://{p.hostname}", "search"))
            await run(more[:3])

        if accepted is not None:
            return Resolution("available", accepted, tried, accepted.reason, True)
        if any(v.decision == "ambiguous" for v in verdicts):
            return Resolution("ambiguous", None, tried,
                              "candidate site found but could not be tied to this organisation number",
                              not any(v.decision in ("failed", "blocked") for v in verdicts))
        if verdicts and all(v.decision == "blocked" for v in verdicts):
            return Resolution("blocked", None, tried, "candidate sites refused automated access", False)
        if verdicts and all(v.decision in ("failed", "blocked") for v in verdicts) and \
                any(v.candidate.source in ("registry_website", "subunit_website") for v in verdicts):
            return Resolution("failed", None, tried, "declared website could not be reached this run", False)
        if not cands:
            return Resolution("not_available", None, tried, "no website declared in the register and no usable name-based candidate", True)
        # Only a complete, proof-based check may count as "checked" (it can trigger a removal on refresh).
        checked = bool(verdicts) and not any(v.decision in ("failed", "blocked") for v in verdicts)
        return Resolution("not_available", None, tried, "no candidate site could be verified for this company", checked)

    # ------------------------------------------------------------------ probe one candidate
    async def probe(self, ctx: Ctx, orgnr: str, enhet: dict, c: Candidate) -> Verdict:
        host = urlsplit(c.url).hostname or ""
        url = c.url
        if not await self._resolves(host):
            alt = "www." + host if not host.startswith("www.") else None
            if alt and await self._resolves(alt):
                url = c.url.replace(f"//{host}", f"//{alt}", 1)
            else:
                return Verdict(c, "failed", reason="dns_unresolved")
        home = await self.f.get(url, ctx=ctx, source_class="company_site")
        if not home.ok and home.error in ("network", "timeout") and url.startswith("https://"):
            home = await self.f.get("http://" + url[len("https://"):], ctx=ctx, source_class="company_site")
        if not home.ok and home.error in ("network", "timeout") and not urlsplit(url).hostname.startswith("www."):
            h = urlsplit(url).hostname
            home = await self.f.get(url.replace(f"//{h}", f"//www.{h}", 1), ctx=ctx, source_class="company_site")
        if not home.ok:
            if home.error in ("blocked", "robots"):
                return Verdict(c, "blocked", home=home, reason=f"homepage {home.error}")
            # not_found is a real answer; network/timeout/5xx/budget are transient
            return Verdict(c, "rejected" if home.error == "not_found" else "failed", home=home,
                           reason=f"homepage {home.error}")
        if not home.is_html:
            return Verdict(c, "rejected", home=home, reason="not an html page")
        final_host = urlsplit(home.where).hostname
        if is_directory(final_host):
            return Verdict(c, "rejected", home=home, reason=f"redirects to directory/platform {final_host}")
        text = home.soup().get_text(" ", strip=True)
        if len(text) < 80 and not home.soup().find("script"):
            return Verdict(c, "rejected", home=home, reason="empty page")
        if PARKED_RE.search(text[:5000]):
            return Verdict(c, "rejected", home=home, reason="parked or placeholder domain")

        pages = [home]
        ours, foreign, proof, proof_ex = self._scan(home, orgnr)
        if not ours:
            for link in pick_links(home, VERIFY_LINK_RE, limit=3):
                if ctx.expired(6.0):
                    break
                pg = await self.f.get(link, ctx=ctx, source_class="company_site")
                if not pg.ok or not pg.is_html:
                    continue
                pages.append(pg)
                o2, f2, p2, e2 = self._scan(pg, orgnr)
                foreign |= f2
                if o2:
                    ours, proof, proof_ex = True, p2, e2
                    break

        sig = self._signals(enhet, pages)
        sig["orgnr_on_site"] = ours
        sig["foreign_orgnr_on_site"] = bool(foreign)
        v = Verdict(c, "rejected", home=home, pages=pages, foreign_orgnrs=sorted(foreign), signals=sig)
        name_ok = sig["legal_name_exact"] or sig["brand_match"]
        contact_ok = sig["address_match"] or sig["phone_match"]
        declared = c.source in ("registry_website", "subunit_website", "wikidata_orgnr")

        # Independent proof for .no domains: the domain registry's RDAP record names the
        # registrant's organisation number. Only a positive match counts (registrar numbers
        # also appear in the record, so a foreign number there proves nothing).
        rdap = None
        brand_guess = c.source == "subunit_name_guess" and c.note and fold(c.note) in fold(text[:20000])
        if not ours and not foreign and (name_ok or declared or brand_guess) and not ctx.expired(6.0):
            rdap = await self._rdap_proof(ctx, orgnr, urlsplit(home.where).hostname)
            sig["domain_registrant_orgnr"] = rdap is not None

        if ours:
            v.decision, v.basis, v.extract_site_facts = "accepted", "orgnr_on_site", True
            v.proof, v.proof_excerpt = proof, proof_ex
            v.reason = "organisation number shown on the site"
        elif rdap is not None:
            v.decision, v.basis, v.extract_site_facts = "accepted", "domain_registrant_orgnr", True
            v.proof, v.proof_excerpt = rdap[0], rdap[1]
            v.reason = "domain is registered to this organisation number (Norid RDAP)"
        elif declared:
            v.basis = {"registry_website": "registry_declared", "subunit_website": "subunit_registry_declared",
                       "wikidata_orgnr": "wikidata_orgnr_link"}[c.source]
            v.proof = home
            if foreign and name_ok:
                # e.g. a subsidiary declaring the group's site: publish the link, label it, extract nothing
                v.decision, v.relationship = "accepted", "shared_or_group_site"
                v.reason = "declared for this company, but the site shows another organisation number (group/shared site)"
            elif foreign:
                v.decision, v.reason = "rejected", "declared site now shows a different organisation number and not this company's name"
            elif name_ok or contact_ok:
                v.decision, v.extract_site_facts = "accepted", True
                v.reason = "declared for this company and the site confirms its name/address/phone"
            else:
                # Declared domains lapse and get re-registered; without any on-site confirmation
                # this is not safe to publish under the company.
                v.decision, v.reason = "ambiguous", "declared for this company, but the site shows no trace of it (name, address, phone or org. number)"
        elif foreign:
            v.decision, v.reason = "rejected", "site shows a different organisation number"
        elif c.source == "registry_email_domain" and name_ok:
            v.decision, v.basis, v.extract_site_facts = "accepted", "registry_email_domain_and_name", True
            v.proof, v.reason = home, "domain of the registered email address and the site carries the company name"
        elif sig["legal_name_exact"] and (sig["address_match"] or sig["phone_match"]):
            v.decision, v.basis, v.extract_site_facts = "accepted", "legal_name_and_address_or_phone", True
            v.proof, v.reason = home, "full legal name plus registered address/phone on the site"
        elif sig["brand_match"] and sig["address_match"] and sig["phone_match"]:
            v.decision, v.basis, v.extract_site_facts = "accepted", "brand_address_and_phone", True
            v.proof, v.reason = home, "brand name plus registered address and phone on the site"
        elif name_ok:
            v.decision, v.reason = "ambiguous", "name matches but no organisation number, address or phone proof"
        else:
            v.decision, v.reason = "rejected", "no identity signal"
        return v

    async def _rdap_proof(self, ctx: Ctx, orgnr: str, host: str | None):
        """Return (FetchResult, excerpt) when the .no domain's RDAP record carries this org. number."""
        dom = registrable(host)
        if not dom.endswith(".no"):
            return None
        r = await self.f.get(f"https://rdap.norid.no/domain/{dom}", ctx=ctx, source_class="domain_registry",
                             headers={"Accept": "application/rdap+json, application/json"},
                             check_robots=False, timeout=8, max_bytes=300_000)
        if not r.ok:
            return None
        hits = [h for h in find_orgnrs(r.text) if h["orgnr"] == orgnr]
        if not hits:
            return None
        s = max(0, hits[0]["start"] - 80)
        return r, r.text[s: hits[0]["end"] + 20]

    @staticmethod
    def _scan(page: FetchResult, orgnr: str):
        text = page.soup().get_text(" ", strip=True)
        raw_extra = " ".join(m.get("content", "") for m in page.soup().find_all("meta") if m.get("content"))
        hits = find_orgnrs(text + " " + raw_extra)
        ours = next((h for h in hits if h["orgnr"] == orgnr), None)
        foreign = {h["orgnr"] for h in hits if h["orgnr"] != orgnr and h["context"]}
        if ours:
            s = max(0, ours["start"] - 60)
            return True, foreign, page, (text + " " + raw_extra)[s: ours["end"] + 40]
        return False, foreign, None, None

    @staticmethod
    def _signals(enhet: dict, pages: list[FetchResult]) -> dict:
        legal = enhet.get("navn") or ""
        base = fold(strip_legal_form(legal))
        full = fold(legal)
        home = pages[0].soup()
        title = fold(home.title.get_text(" ", strip=True) if home.title else "")
        og = home.find("meta", attrs={"property": "og:site_name"})
        site_name = fold(og.get("content")) if og and og.get("content") else ""
        h1 = home.find("h1")
        h1t = fold(h1.get_text(" ", strip=True)) if h1 else ""
        texts = [p.soup().get_text(" ", strip=True) for p in pages]
        folded = " " + " ".join(fold(t) for t in texts) + " "
        dig = " ".join(digits(t) for t in texts)
        legal_exact = len(full) >= 5 and len(full.split()) >= 2 and f" {full} " in folded
        brand = False
        if base and len(base) >= 4:
            for t in (title, site_name, h1t):
                if t and (f" {base} " in f" {t} " or fuzz.partial_ratio(base, t) >= 93 and len(t) <= len(base) * 4):
                    brand = True
                    break
        addr = enhet.get("forretningsadresse") or enhet.get("postadresse") or {}
        if not isinstance(addr, dict):
            addr = {}
        street = fold(((addr.get("adresse") or [""]) or [""])[0] if isinstance(addr.get("adresse"), list) else addr.get("adresse") or "")
        postnr = addr.get("postnummer") or ""
        address_match = bool(street and postnr and f" {street} " in folded and postnr in dig)
        phones = [digits(enhet.get(k))[-8:] for k in ("telefon", "mobil") if enhet.get(k)]
        phone_match = any(len(p) == 8 and p in dig.replace(" ", "") for p in phones)
        return {"legal_name_exact": legal_exact, "brand_match": brand,
                "address_match": address_match, "phone_match": phone_match}


def _rank(v: Verdict) -> int:
    order = {"orgnr_on_site": 8, "domain_registrant_orgnr": 7, "registry_declared": 6, "wikidata_orgnr_link": 5,
             "subunit_registry_declared": 4, "registry_email_domain_and_name": 3,
             "legal_name_and_address_or_phone": 2, "brand_address_and_phone": 1}
    bonus = 1 if v.relationship == "exact" else 0
    return order.get(v.basis or "", 0) * 2 + bonus


def same_site(a: str, b: str) -> bool:
    return registrable(urlsplit(a).hostname) == registrable(urlsplit(b).hostname)


def pick_links(page: FetchResult, pattern: re.Pattern, limit: int = 3, same: bool = True) -> list[str]:
    out: list[str] = []
    base = page.where
    for a in page.soup().find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            url = urljoin(base, href).split("#")[0]
        except ValueError:
            continue
        if not url.startswith(("http://", "https://")):
            continue
        if same and not same_site(url, base):
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|svg|zip|docx?|xlsx?|mp4)(\?|$)", url, re.I):
            continue
        label = a.get_text(" ", strip=True)
        if pattern.search(urlsplit(url).path) or pattern.search(label):
            if url.rstrip("/") != base.rstrip("/") and url not in out:
                out.append(url)
        if len(out) >= limit:
            break
    return out
