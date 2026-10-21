"""Crawl a verified company site and extract company-owned facts.

Deterministic first: JSON-LD, then DOM attributes (links, <time>), then text rules.
Only called for sites accepted by identity.WebsiteResolver with extract_site_facts=True.
"""
from __future__ import annotations

import asyncio
import html as htmlmod
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from .claims import Claims
from .http import Ctx, Fetcher, FetchResult
from .identity import Verdict, pick_links, registrable, same_site
from .util import date_from_url, digits, fold, parse_any_datetime, parse_date, short_hash, strip_legal_form, trunc

CAREERS_RE = re.compile(
    r"(karriere|career|jobb|/jobs?\b|stilling|ledige|rekrutter|jobbe-hos|work-with-us|join-us|"
    r"bli-med|vacanc|join our team)", re.I)
NEWS_RE = re.compile(r"(nyhet|news|aktuelt|presse|press-?release|blogg?|artikler|/media|innsikt|insights|arkiv)", re.I)
INFO_RE = re.compile(r"(kontakt|contact|om-?oss|about|avdeling|kontor|locations?|finn-oss|lokasjon|ledelse|leadership)", re.I)

ATS_MARKERS = (
    "teamtailor.com", "jobylon.com", "webcruiter.com", "webcruiter.no", "recman.no", "recman.page",
    "hrmanager.no", "easycruit.com", "jobbnorge.no", "reachmee.com", "smartrecruiters.com", "lever.co",
    "greenhouse.io", "workable.com", "varbi.com", "emply.com", "personio.", "jobindex.", "cvpartner.",
    "talentech.", "career.", "careers.", "jobs.",
)
FINN_JOB_RE = re.compile(r"finn\.no/(job|jobb)", re.I)

JOB_LINK_RE = re.compile(
    r"(/jobs?/[^/?#]+|/stillinger?/[^/?#]+|/ledige-stillinger/[^/?#]+|/vacanc(y|ies)/[^/?#]+|"
    r"/positions?/[^/?#]+|/karriere/[^/?#]+/[^/?#]+|/careers?/[^/?#]+/[^/?#]+|/job/|/stilling/|"
    r"advertpublic|[?&](ad|finnkode|jobid|job_id|vacancyid|adid)=\w+)", re.I)
NAV_WORDS = {
    "se alle", "alle stillinger", "ledige stillinger", "se ledige stillinger", "les mer", "søk", "søk her",
    "apply", "see all", "read more", "karriere", "jobb", "jobs", "careers", "career", "stillinger",
    "open positions", "ledige jobber", "mer", "more", "her", "søk nå", "apply now", "view all", "vis alle",
    "se alle stillinger", "jobbe hos oss", "jobb hos oss", "ledige jobber hos oss",
}
OPEN_APPLICATION_RE = re.compile(r"(åpen søknad|open application|generell søknad|spontaneous application)", re.I)

SOCIAL = {
    "linkedin": re.compile(r"^https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(company|school|showcase)/([^/?#]+)", re.I),
    "facebook": re.compile(r"^https?://(?:www\.|m\.|nb-no\.|business\.)?(?:facebook|fb)\.com/(?!sharer|share|dialog|plugins|tr\b|login|l\.php|groups/?$|events/?$|hashtag|watch|profile\.php)([A-Za-z0-9.\-_]{2,})/?(?:$|[?#])", re.I),
    "instagram": re.compile(r"^https?://(?:www\.)?instagram\.com/(?!p/|reel/|explore/|accounts/|stories/)([A-Za-z0-9_.]{2,30})/?(?:$|[?#])", re.I),
    "x": re.compile(r"^https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(?!intent|share|home|search|hashtag|i/)([A-Za-z0-9_]{1,15})/?(?:$|[?#])", re.I),
    "youtube": re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/((?:channel|c|user)/[^/?#]+|@[^/?#]+)", re.I),
    "tiktok": re.compile(r"^https?://(?:www\.)?tiktok\.com/(@[^/?#]+)", re.I),
    "pinterest": re.compile(r"^https?://(?:[a-z]{2}\.|www\.)?pinterest\.[a-z.]+/([A-Za-z0-9_]{3,30})/?(?:$|[?#])", re.I),
    "github": re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9\-]{2,39})/?(?:$|[?#])", re.I),
}
CANON_HOST = {"linkedin": "www.linkedin.com", "facebook": "www.facebook.com", "instagram": "www.instagram.com",
              "x": "x.com", "youtube": "www.youtube.com", "tiktok": "www.tiktok.com",
              "pinterest": "www.pinterest.com", "github": "github.com"}

ORG_TYPES = {"organization", "corporation", "localbusiness", "store", "restaurant", "professionalservice",
             "medicalbusiness", "legalservice", "accountingservice", "autodealer", "homeandconstructionbusiness",
             "lodgingbusiness", "foodestablishment", "financialservice", "educationalorganization",
             "dentist", "realestateagent", "travelagency", "ngo", "onlinestore", "onlinebusiness"}
ARTICLE_TYPES = {"newsarticle", "blogposting", "article", "report", "pressrelease", "socialmediaposting"}


@dataclass
class Site:
    verdict: Verdict
    pages: list[FetchResult] = field(default_factory=list)
    careers_pages: list[FetchResult] = field(default_factory=list)
    news_pages: list[FetchResult] = field(default_factory=list)
    sitemap: FetchResult | None = None
    sitemap_entries: list[tuple[str, str | None]] = field(default_factory=list)
    feed: FetchResult | None = None
    careers_links_found: int = 0


# --------------------------------------------------------------------------- crawl


async def crawl(f: Fetcher, ctx: Ctx, v: Verdict, max_extra: int = 8) -> Site:
    site = Site(verdict=v, pages=list(v.pages))
    home = v.home
    fetched = {p.where.rstrip("/") for p in site.pages}

    async def get(url: str) -> FetchResult | None:
        key = url.rstrip("/")
        if key in fetched or ctx.expired(5.0):
            return None
        fetched.add(key)
        r = await f.get(url, ctx=ctx, source_class="company_site")
        if r.ok and r.is_html:
            site.pages.append(r)
            return r
        return None

    careers = pick_links(home, CAREERS_RE, limit=2)
    ats = [u for u in pick_links(home, re.compile(".*"), limit=200, same=False)
           if any(m in (urlsplit(u).hostname or "") for m in ATS_MARKERS) and not same_site(u, home.where)][:1]
    news = pick_links(home, NEWS_RE, limit=2)
    info = [u for u in pick_links(home, INFO_RE, limit=3) if u.rstrip("/") not in fetched][:2]
    site.careers_links_found = len(careers) + len(ats)

    plan = ([("careers", u) for u in careers + ats] + [("news", u) for u in news] + [("info", u) for u in info])[:max_extra]
    results = await asyncio.gather(*(get(u) for _, u in plan), return_exceptions=True)
    for (kind, _), r in zip(plan, results):
        if isinstance(r, FetchResult):
            (site.careers_pages if kind == "careers" else site.news_pages if kind == "news" else []).append(r)

    # A careers page that only links out to an ATS: follow one ATS listing.
    if site.careers_pages and not ctx.expired(10.0):
        for cp in list(site.careers_pages):
            outs = [u for u in pick_links(cp, re.compile(".*"), limit=300, same=False)
                    if any(m in (urlsplit(u).hostname or "") for m in ATS_MARKERS) and not same_site(u, cp.where)]
            if outs:
                r = await get(outs[0])
                if r:
                    site.careers_pages.append(r)
                    site.careers_links_found += 1
                break

    # Sitemap (dated URLs) and RSS/Atom feed
    if not ctx.expired(8.0):
        await _sitemap(f, ctx, site)
    if not ctx.expired(6.0):
        feed_url = _feed_link(home)
        if feed_url:
            r = await f.get(feed_url, ctx=ctx, source_class="company_site")
            if r.ok:
                site.feed = r
    return site


async def _sitemap(f: Fetcher, ctx: Ctx, site: Site) -> None:
    home = site.verdict.home
    p = urlsplit(home.where)
    origin = f"{p.scheme}://{p.netloc}"
    candidates = [u for u in f.sitemaps_for(home.where) if not u.endswith(".gz")][:2] or [origin + "/sitemap.xml"]
    for sm_url in candidates:
        r = await f.get(sm_url, ctx=ctx, source_class="company_site", max_bytes=4_000_000)
        if not r.ok:
            continue
        entries = parse_sitemap(r.text)
        subs = [loc for tag, loc, _ in entries if tag == "sitemap"]
        if subs:
            pref = [s for s in subs if re.search(r"(post|news|nyhet|artikl|blog|press|aktuelt)", s, re.I)]
            pick = (pref or subs)[:1]
            for s in pick:
                if s.endswith(".gz") or ctx.expired(6.0):
                    continue
                r2 = await f.get(s, ctx=ctx, source_class="company_site", max_bytes=4_000_000)
                if r2.ok:
                    site.sitemap = r2
                    site.sitemap_entries = [(loc, lm) for tag, loc, lm in parse_sitemap(r2.text) if tag == "url"]
                    return
        else:
            site.sitemap = r
            site.sitemap_entries = [(loc, lm) for tag, loc, lm in entries if tag == "url"]
            return


_BLOCK_RE = re.compile(r"<(url|sitemap)>(.*?)</\1>", re.S | re.I)
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)
_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)


def parse_sitemap(text: str) -> list[tuple[str, str, str | None]]:
    out = []
    for tag, body in _BLOCK_RE.findall(text or "")[:50000]:
        m = _LOC_RE.search(body)
        if not m:
            continue
        loc = htmlmod.unescape(m.group(1).replace("<![CDATA[", "").replace("]]>", "").strip())
        lm = _LASTMOD_RE.search(body)
        out.append((tag.lower(), loc, lm.group(1).strip() if lm else None))
    return out


def _feed_link(home: FetchResult) -> str | None:
    for link in home.soup().find_all("link", href=True):
        t = (link.get("type") or "").lower()
        rel = " ".join(link.get("rel") or []).lower()
        if "alternate" in rel and ("rss" in t or "atom" in t):
            href = link.get("href")
            if "comments" in href.lower():
                continue
            url = urljoin(home.where, href)
            if same_site(url, home.where):
                return url
    return None


# --------------------------------------------------------------------------- extraction helpers


def jsonld_nodes(page: FetchResult) -> list[tuple[dict, int]]:
    out: list[tuple[dict, int]] = []
    for i, s in enumerate(page.soup().find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})):
        raw = (s.string or s.get_text() or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            try:
                obj = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.replace("\n", " ")))
            except ValueError:
                continue
        for node in _flatten(obj):
            out.append((node, i))
    return out


def _flatten(o, depth: int = 0):
    if depth > 6:
        return
    if isinstance(o, list):
        for x in o:
            yield from _flatten(x, depth + 1)
    elif isinstance(o, dict):
        yield o
        for k in ("@graph", "itemListElement", "mainEntity", "item"):
            if k in o:
                yield from _flatten(o[k], depth + 1)


def node_types(n: dict) -> set[str]:
    t = n.get("@type")
    if isinstance(t, list):
        return {str(x).lower() for x in t}
    return {str(t).lower()} if t else set()


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or v.get("url")
        if v is None:
            return None
    if isinstance(v, list):
        v = v[0] if v else None
        return _s(v)
    v = str(v).strip()
    return v or None


def _jsonld_address(a) -> str | None:
    if isinstance(a, list):
        a = a[0] if a else None
    if isinstance(a, str):
        return a.strip() or None
    if not isinstance(a, dict):
        return None
    parts = [_s(a.get("streetAddress")), " ".join(x for x in (_s(a.get("postalCode")), _s(a.get("addressLocality"))) if x)]
    return ", ".join(p for p in parts if p) or None


def canon_social(url: str) -> tuple[str, str, str] | None:
    url = url.strip()
    for platform, rx in SOCIAL.items():
        m = rx.match(url)
        if not m:
            continue
        if platform == "linkedin":
            handle = f"{m.group(1).lower()}/{m.group(2)}"
        else:
            handle = m.group(1)
        if handle.lower() in {"home", "pages", "people", "about", "help", "policies", "watch", "feed"}:
            return None
        canon = f"https://{CANON_HOST[platform]}/{handle.strip('/')}"
        return platform, canon, handle
    return None


def date_near(el, max_up: int = 4):
    node = el
    for _ in range(max_up + 1):
        if node is None or getattr(node, "name", None) in (None, "[document]", "body", "html"):
            break
        if node is not el and len(node.find_all("a")) > 4:
            break
        t = node.find("time") if node.name != "time" else node
        if t is not None:
            d = parse_date(t.get("datetime") or t.get_text(" ", strip=True))
            if d:
                return d
        d = parse_date(node.get_text(" ", strip=True)[:400])
        if d:
            return d
        node = node.parent
    return None


def _anchor_title(a) -> str:
    h = a.find(["h1", "h2", "h3", "h4", "h5"])
    text = (h.get_text(" ", strip=True) if h else a.get_text(" ", strip=True)) or a.get("title") or a.get("aria-label") or ""
    return re.sub(r"\s+", " ", text).strip()


def _phone(raw: str) -> str | None:
    d = digits(raw)
    if d.startswith("0047"):
        d = d[4:]
    elif d.startswith("47") and len(d) == 10:
        d = d[2:]
    if len(d) == 8:
        return f"+47 {d[:2]} {d[2:4]} {d[4:6]} {d[6:]}" if d[0] in "49" else f"+47 {d[:3]} {d[3:5]} {d[5:]}"
    return None


# --------------------------------------------------------------------------- claims


def site_claims(cl: Claims, site: Site, enhet: dict) -> dict:
    v = site.verdict
    home = v.home
    basis, rel = v.basis or "verified_site", v.relationship
    kw = {"basis": basis, "relationship": rel}
    stats = {"social": 0, "jobs": 0, "careers_pages": 0, "news": 0, "contact": 0}
    legal_base = fold(strip_legal_form(enhet.get("navn") or ""))
    site_dom = registrable(urlsplit(home.where).hostname)

    # ---------------- brand + self description (company-owned copy)
    soup = home.soup()
    brand = None
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content"):
        brand = og.get("content").strip()
    for node, i in jsonld_nodes(home):
        if node_types(node) & ORG_TYPES and _s(node.get("name")):
            brand = brand or _s(node.get("name"))
    if brand and 2 <= len(brand) <= 80 and fold(brand) != legal_base and fold(brand) != fold(enhet.get("navn")):
        cl.add("identity", "public_brand", brand, fr=home, method="og:site_name|jsonld", locator="meta[property=og:site_name]",
               excerpt=brand, confidence="medium", **kw)
    desc = None
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content") and len(m.get("content").strip()) >= 30:
            desc = m.get("content").strip()
            break
    if desc:
        cl.add("identity", "self_description", trunc(desc, 400), fr=home, method="meta_description",
               locator="meta[name=description]", excerpt=desc, confidence="medium", **kw)

    # ---------------- socials, contact, locations, articles, job postings from every page
    socials: dict[str, list] = {}
    phones, emails = [], []
    for page in site.pages + site.careers_pages + site.news_pages:
        if not page.ok or not page.is_html:
            continue
        on_company_domain = same_site(page.where, home.where)
        for a in page.soup().find_all("a", href=True):
            href = a.get("href", "").strip()
            if on_company_domain:
                c = canon_social(href)
                if c:
                    socials.setdefault(c[0], [])
                    if c[1].lower() not in [x[1].lower() for x in socials[c[0]]]:
                        socials[c[0]].append((page, c[1], c[2], href, "link_on_verified_site"))
                if href.lower().startswith("tel:"):
                    p = _phone(href[4:])
                    if p and p not in [x[1] for x in phones]:
                        phones.append((page, p, href))
                elif href.lower().startswith("mailto:"):
                    e = href[7:].split("?")[0].strip().lower()
                    if "@" in e and registrable(e.split("@", 1)[1]) == site_dom and e not in [x[1] for x in emails]:
                        emails.append((page, e, href))
        for node, idx in jsonld_nodes(page):
            types = node_types(node)
            if types & ORG_TYPES and on_company_domain:
                same_as = node.get("sameAs") or []
                if isinstance(same_as, str):
                    same_as = [same_as]
                for u in same_as:
                    c = canon_social(str(u))
                    if c:
                        socials.setdefault(c[0], [])
                        if c[1].lower() not in [x[1].lower() for x in socials[c[0]]]:
                            socials[c[0]].append((page, c[1], c[2], str(u), "jsonld_sameAs"))
                addr = _jsonld_address(node.get("address"))
                if addr:
                    cl.add("workplaces", "site_location", {"name": _s(node.get("name")), "address": addr},
                           fr=page, method="jsonld", locator=f"script[type=ld+json][{idx}]/address",
                           excerpt=addr, key=f"workplaces:site_location:{fold(addr)}", confidence="medium", **kw)
            if "jobposting" in types:
                _add_job(cl, page, node, idx, stats, kw)
            if types & ARTICLE_TYPES:
                d = parse_date(_s(node.get("datePublished")) or _s(node.get("dateCreated")))
                title = _s(node.get("headline")) or _s(node.get("name"))
                url = _s(node.get("url")) or page.where
                if d and title:
                    _add_news(cl, page, title, urljoin(page.where, url), d, "published", "jsonld",
                              f"script[type=ld+json][{idx}]", stats, kw)

    for platform, items in socials.items():
        for page, canon, handle, href, method in items[:3]:
            if cl.add("social_profiles", platform, {"platform": platform, "url": canon, "handle": handle},
                      fr=page, method=method, locator=f'a[href="{trunc(href, 150)}"]', excerpt=href,
                      key=f"social_profiles:{platform}:{canon.lower()}", **kw):
                stats["social"] += 1
    for page, p, href in phones[:3]:
        if cl.add("contact", "site_phone", p, fr=page, method="tel_link", locator=f'a[href="{href}"]',
                  excerpt=href, key=f"contact:site_phone:{digits(p)}", **kw):
            stats["contact"] += 1
    for page, e, href in emails[:3]:
        if cl.add("contact", "site_email", e, fr=page, method="mailto_link", locator=f'a[href="{href}"]',
                  excerpt=href, key=f"contact:site_email:{e}", **kw):
            stats["contact"] += 1

    # ---------------- careers pages and job links
    for cp in site.careers_pages:
        if same_site(cp.where, home.where) or any(m in (urlsplit(cp.where).hostname or "") for m in ATS_MARKERS):
            if cl.add("hiring", "careers_page", {"url": cp.where}, fr=cp, method="careers_link_on_verified_site",
                      locator="url", excerpt=cp.where, key=f"hiring:careers_page:{cp.where.rstrip('/').lower()}", **kw):
                stats["careers_pages"] += 1
        for a in cp.soup().find_all("a", href=True):
            try:
                url = urljoin(cp.where, a.get("href").strip()).split("#")[0]
            except ValueError:
                continue
            host = urlsplit(url).hostname or ""
            allowed = same_site(url, home.where) or any(m in host for m in ATS_MARKERS) or FINN_JOB_RE.search(url)
            if not allowed or not JOB_LINK_RE.search(url) or url.rstrip("/") == cp.where.rstrip("/"):
                continue
            title = _anchor_title(a)
            if not (6 <= len(title) <= 160) or title.lower().strip(" ›»→") in NAV_WORDS or OPEN_APPLICATION_RE.search(title):
                continue
            d = date_near(a, 2)
            key = f"hiring:job:{short_hash(_norm_url(url), 16)}"
            if key in cl.items:
                continue
            if cl.add("hiring", "job_posting",
                      {"title": trunc(title, 140), "url": url, "date_seen": None, "date_posted": d.isoformat() if d else None,
                       "source": "careers_page"},
                      fr=cp, method="careers_page_link", locator=f'a[href="{trunc(a.get("href"), 150)}"]',
                      excerpt=title, key=key, confidence="medium", period={"date_posted": d.isoformat() if d else None}, **kw):
                stats["jobs"] += 1
            if stats["jobs"] >= 60:
                break

    # ---------------- dated news links on home and news pages
    for page in [home] + site.news_pages:
        section = urlsplit(page.where).path.rstrip("/") if page is not home else None
        for a in page.soup().find_all("a", href=True):
            try:
                url = urljoin(page.where, a.get("href").strip()).split("#")[0]
            except ValueError:
                continue
            if not same_site(url, home.where):
                continue
            path = urlsplit(url).path.rstrip("/")
            if section:
                if not path.startswith(section + "/") and not NEWS_RE.search(path):
                    continue
            elif not NEWS_RE.search(path):
                continue
            if path == section or path.count("/") < 2 and not date_from_url(url):
                continue
            title = _anchor_title(a)
            if not (15 <= len(title) <= 220) or title.lower() in NAV_WORDS:
                continue
            d = date_from_url(url) or date_near(a)
            if not d:
                continue
            _add_news(cl, page, title, url, d, "published", "dated_link", f'a[href="{trunc(a.get("href"), 150)}"]', stats, kw)

    # ---------------- RSS / Atom
    if site.feed is not None:
        _feed_items(cl, site.feed, stats, kw)

    # ---------------- sitemap news URLs with lastmod (weakest signal, labelled as modification date)
    if site.sitemap is not None and stats["news"] < 3:
        news_entries = [(loc, lm) for loc, lm in site.sitemap_entries
                        if lm and NEWS_RE.search(urlsplit(loc).path or "") and urlsplit(loc).path.count("/") >= 2
                        and same_site(loc, home.where)]
        dated = []
        for loc, lm in news_entries:
            d = parse_date(lm)
            if d:
                dated.append((d, loc))
        dated.sort(reverse=True)
        for d, loc in dated[:8]:
            slug = urlsplit(loc).path.rstrip("/").rsplit("/", 1)[-1]
            title = re.sub(r"[-_]+", " ", re.sub(r"\.\w+$", "", slug)).strip().capitalize()
            if len(title) < 8:
                continue
            _add_news(cl, site.sitemap, title, loc, d, "last_modified", "sitemap_lastmod",
                      f"<url><loc>{trunc(loc, 150)}</loc>", stats, kw, confidence="low")
    return stats


def _norm_url(u: str) -> str:
    p = urlsplit(u)
    return urlunsplit((p.scheme.lower(), (p.hostname or "").lower(), p.path.rstrip("/"), p.query, ""))


def _add_job(cl: Claims, page: FetchResult, node: dict, idx: int, stats: dict, kw: dict) -> None:
    title = _s(node.get("title")) or _s(node.get("name"))
    if not title:
        return
    url = urljoin(page.where, _s(node.get("url")) or page.where)
    posted = parse_date(_s(node.get("datePosted")))
    until = parse_date(_s(node.get("validThrough")))
    loc = node.get("jobLocation")
    loc_s = None
    if isinstance(loc, list) and loc:
        loc = loc[0]
    if isinstance(loc, dict):
        loc_s = _jsonld_address(loc.get("address")) or _s(loc.get("name"))
    key = f"hiring:job:{short_hash(_norm_url(url) + '|' + fold(title), 16)}"
    if cl.add("hiring", "job_posting",
              {"title": trunc(title, 140), "url": url, "date_posted": posted.isoformat() if posted else None,
               "valid_through": until.isoformat() if until else None, "location": loc_s,
               "employer": _s(node.get("hiringOrganization")), "source": "jobposting_jsonld"},
              fr=page, method="jsonld:JobPosting", locator=f"script[type=ld+json][{idx}]", excerpt=title,
              key=key, period={"date_posted": posted.isoformat() if posted else None,
                               "valid_through": until.isoformat() if until else None}, **kw):
        stats["jobs"] += 1


def _add_news(cl: Claims, page, title, url, d, date_type, method, locator, stats, kw, confidence="high") -> None:
    if stats["news"] >= 25:
        return
    key = f"public_activity:news:{short_hash(_norm_url(url), 16)}"
    if key in cl.items:
        return
    if cl.add("public_activity", "company_news",
              {"date": d.isoformat(), "date_type": date_type, "title": trunc(title, 200), "url": url,
               "source": "company website"},
              fr=page, method=method, locator=locator, excerpt=f"{d.isoformat()} {title}", key=key,
              period={"date": d.isoformat()}, confidence=confidence, **kw):
        stats["news"] += 1


def _feed_items(cl: Claims, feed: FetchResult, stats: dict, kw: dict) -> None:
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(feed.text, "xml")
    except Exception:
        return
    for i, it in enumerate(soup.find_all(["item", "entry"])[:15]):
        t = it.find("title")
        title = t.get_text(" ", strip=True) if t else None
        link = it.find("link")
        url = None
        if link is not None:
            url = link.get("href") or link.get_text(strip=True)
        dnode = it.find(["pubDate", "published", "updated", "date"])
        d = parse_any_datetime(dnode.get_text(strip=True)) if dnode else None
        if title and url and d:
            _add_news(cl, feed, title, urljoin(feed.where, url), d, "published", "rss_feed", f"item[{i}]", stats, kw)
