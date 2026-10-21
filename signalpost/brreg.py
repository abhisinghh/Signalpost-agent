"""Brønnøysundregistrene: identity, roles, subunits, annual accounts, announcements.

Official registry data is the identity anchor: everything here is keyed by the
organisation number, so it cannot be attributed to the wrong company.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from .claims import Claims
from .http import Ctx, Fetcher, FetchResult
from .util import fold

ENHET_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}"
ROLLER_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}/roller"
UNDER_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter"
REGN_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}"
KUNN_URL = "https://w2.brreg.no/kunngjoring/hent_nr.jsp"
JSON = {"Accept": "application/json"}
SRC = "official_register"


async def fetch_enhet(f: Fetcher, ctx: Ctx, orgnr: str) -> FetchResult:
    return await f.get(ENHET_URL.format(orgnr=orgnr), ctx=ctx, source_class=SRC, headers=JSON,
                       check_robots=False)


async def fetch_roller(f: Fetcher, ctx: Ctx, orgnr: str) -> FetchResult:
    return await f.get(ROLLER_URL.format(orgnr=orgnr), ctx=ctx, source_class=SRC, headers=JSON,
                       check_robots=False)


async def fetch_regnskap(f: Fetcher, ctx: Ctx, orgnr: str, year: int | None = None) -> FetchResult:
    params = {"år": str(year)} if year else None
    return await f.get(REGN_URL.format(orgnr=orgnr), ctx=ctx, source_class=SRC, headers=JSON,
                       params=params, check_robots=False)


async def fetch_kunngjoringer(f: Fetcher, ctx: Ctx, orgnr: str) -> FetchResult:
    return await f.get(KUNN_URL, ctx=ctx, source_class=SRC, params={"orgnr": orgnr},
                       check_robots=False, timeout=10)


async def fetch_underenheter(f: Fetcher, ctx: Ctx, orgnr: str) -> tuple[list[tuple[FetchResult, dict, int]], FetchResult | None]:
    """Return ([(page_result, subunit_json, index_on_page)], failing_result_or_None)."""
    out: list[tuple[FetchResult, dict, int]] = []
    page = 0
    while page < 5:
        fr = await f.get(UNDER_URL, ctx=ctx, source_class=SRC, headers=JSON, check_robots=False,
                         params={"overordnetEnhet": orgnr, "size": "200", "page": str(page)})
        if not fr.ok:
            return out, fr
        try:
            d = fr.json()
        except ValueError:
            return out, fr
        items = (d.get("_embedded") or {}).get("underenheter") or []
        for i, item in enumerate(items):
            out.append((fr, item, i))
        total_pages = (d.get("page") or {}).get("totalPages") or 1
        if not items or page + 1 >= total_pages:
            break
        page += 1
    return out, None


# --------------------------------------------------------------------------- identity


def fmt_address(a: dict | None) -> str | None:
    if isinstance(a, str):
        return a.strip() or None
    if not isinstance(a, dict) or not a:
        return None
    lines = [x for x in (a.get("adresse") or []) if x]
    city = " ".join(x for x in (a.get("postnummer"), a.get("poststed")) if x)
    parts = lines + ([city] if city else [])
    if a.get("landkode") and a.get("landkode") != "NO" and a.get("land"):
        parts.append(a["land"])
    return ", ".join(parts) or None


def _code(d: dict | None):
    if isinstance(d, str) and d.strip():
        return {"code": d.strip(), "description": None}
    if not isinstance(d, dict) or not d.get("kode"):
        return None
    return {"code": d.get("kode"), "description": d.get("beskrivelse")}


def identity_claims(cl: Claims, fr: FetchResult, e: dict) -> None:
    def add(field, value, loc, family="identity", **kw):
        cl.add(family, field, value, fr=fr, method="registry_api", locator=loc,
               excerpt=f"{loc}: {value}", **kw)

    add("legal_name", e.get("navn"), "/navn")
    add("legal_form", _code(e.get("organisasjonsform")), "/organisasjonsform")
    for i, k in enumerate(("naeringskode1", "naeringskode2", "naeringskode3")):
        add("industry", _code(e.get(k)), f"/{k}", key=f"identity:industry:{i + 1}")
    add("institutional_sector", _code(e.get("institusjonellSektorkode")), "/institusjonellSektorkode")
    add("business_address", fmt_address(e.get("forretningsadresse")), "/forretningsadresse")
    add("postal_address", fmt_address(e.get("postadresse")), "/postadresse")
    addr = e.get("forretningsadresse") or e.get("postadresse") or {}
    if not isinstance(addr, dict):
        addr = {}
    if addr.get("kommune"):
        add("municipality", {"name": addr.get("kommune"), "number": addr.get("kommunenummer")},
            "/forretningsadresse/kommune")
    add("registered_date", e.get("registreringsdatoEnhetsregisteret"), "/registreringsdatoEnhetsregisteret")
    add("founded_date", e.get("stiftelsesdato"), "/stiftelsesdato")
    if e.get("harRegistrertAntallAnsatte") is not False and e.get("antallAnsatte") is not None:
        add("registered_employees", e.get("antallAnsatte"), "/antallAnsatte",
            period={"as_of": e.get("registreringsdatoAntallAnsatteEnhetsregisteret")
                    or e.get("registreringsdatoAntallAnsatteNAVAaregisteret")})
    purpose = e.get("vedtektsfestetFormaal") or e.get("formaal")
    if isinstance(purpose, list):
        purpose = " ".join(p for p in purpose if p)
    add("statutory_purpose", purpose, "/vedtektsfestetFormaal")
    activity = e.get("aktivitet")
    if isinstance(activity, list):
        activity = " ".join(p for p in activity if p)
    add("registered_activity", activity, "/aktivitet")
    add("latest_filed_accounts_year", e.get("sisteInnsendteAarsregnskap"), "/sisteInnsendteAarsregnskap")
    status = {
        "bankrupt": bool(e.get("konkurs")),
        "bankruptcy_date": e.get("konkursdato"),
        "under_liquidation": bool(e.get("underAvvikling")),
        "liquidation_date": e.get("underAvviklingDato"),
        "forced_dissolution": bool(e.get("underTvangsavviklingEllerTvangsopplosning")),
        "deleted_date": e.get("slettedato"),
        "in_vat_register": e.get("registrertIMvaregisteret"),
        "in_business_register": e.get("registrertIForetaksregisteret"),
        "in_nonprofit_register": e.get("registrertIFrivillighetsregisteret"),
    }
    add("registry_status", {k: v for k, v in status.items() if v is not None}, "/konkurs,/underAvvikling")
    if e.get("overordnetEnhet"):
        add("parent_unit_orgnr", e.get("overordnetEnhet"), "/overordnetEnhet")

    # Registered contact points (company-declared in the official register)
    add("registered_email", e.get("epostadresse"), "/epostadresse", family="contact")
    add("registered_phone", e.get("telefon"), "/telefon", family="contact")
    add("registered_mobile", e.get("mobil"), "/mobil", family="contact")


# --------------------------------------------------------------------------- roles

ROLE_EN = {
    "DAGL": "Managing director / CEO", "LEDE": "Chair of the board", "NEST": "Deputy chair",
    "MEDL": "Board member", "VARA": "Deputy board member", "OBS": "Board observer",
    "KONT": "Contact person", "INNH": "Proprietor", "DTPR": "Partner (full liability)",
    "DTSO": "Partner (shared liability)", "KOMP": "General partner", "REVI": "Auditor",
    "REGN": "Accountant", "FFØR": "Business manager", "BEST": "Managing shipowner",
    "SAM": "Co-owner", "KDEB": "Bankruptcy trustee", "REPR": "Norwegian representative",
    "ADOS": "Administrative unit", "HFOR": "Head office", "STYR": "Board",
}
SERVICE_ROLES = {"REVI", "REGN"}


def role_claims(cl: Claims, fr: FetchResult, data: dict) -> int:
    n = 0
    for gi, g in enumerate(data.get("rollegrupper") or []):
        as_of = g.get("sistEndret")
        for ri, r in enumerate(g.get("roller") or []):
            if r.get("fratraadt") or r.get("avregistrert"):
                continue
            t = r.get("type") or {}
            code = t.get("kode")
            person, enhet = r.get("person"), r.get("enhet")
            value = {"role_code": code, "role": t.get("beskrivelse"), "role_en": ROLE_EN.get(code)}
            if person:
                if person.get("erDoed"):
                    continue
                nm = person.get("navn") or {}
                name = " ".join(x for x in (nm.get("fornavn"), nm.get("mellomnavn"), nm.get("etternavn")) if x)
                value.update(name=name, holder_type="person")  # birth dates deliberately not published
                key = f"leadership:role:{code}:{fold(name)}"
            elif enhet:
                nm = enhet.get("navn")
                name = " ".join(nm) if isinstance(nm, list) else nm
                value.update(name=name, holder_type="organisation", holder_orgnr=enhet.get("organisasjonsnummer"))
                key = f"leadership:role:{code}:{enhet.get('organisasjonsnummer') or fold(name)}"
            else:
                continue
            if not name:
                continue
            field = "service_provider" if code in SERVICE_ROLES else "role"
            if cl.add("leadership", field, value, fr=fr, method="registry_api",
                      locator=f"/rollegrupper/{gi}/roller/{ri}",
                      excerpt=f"{t.get('beskrivelse')}: {name}", key=key, period={"as_of": as_of}):
                n += 1
    return n


# --------------------------------------------------------------------------- subunits


def subunit_claims(cl: Claims, rows: list[tuple[FetchResult, dict, int]]) -> list[dict]:
    kept = []
    for fr, u, i in rows:
        if u.get("slettedato"):
            continue
        orgnr = u.get("organisasjonsnummer")
        addr = u.get("beliggenhetsadresse") or u.get("postadresse") or {}
        value = {
            "orgnr": orgnr,
            "name": u.get("navn"),
            "address": fmt_address(addr),
            "municipality": addr.get("kommune"),
            "employees": u.get("antallAnsatte") if u.get("harRegistrertAntallAnsatte") is not False else None,
            "industry": _code(u.get("naeringskode1")),
            "start_date": u.get("oppstartsdato"),
            "website": u.get("hjemmeside"),
        }
        value = {k: v for k, v in value.items() if v not in (None, "")}
        if cl.add("workplaces", "subunit", value, fr=fr, method="registry_api",
                  locator=f"/_embedded/underenheter/{i}", excerpt=f"{u.get('navn')} {value.get('address', '')}",
                  key=f"workplaces:subunit:{orgnr}"):
            kept.append(u)
    return kept


# --------------------------------------------------------------------------- accounts

METRICS = [
    ("revenue", "sumDriftsinntekter"),
    ("operating_costs", "sumDriftskostnad"),
    ("operating_result", "driftsresultat"),
    ("net_financial_items", "nettoFinans"),
    ("profit_before_tax", "ordinaertResultatFoerSkattekostnad"),
    ("net_income", "aarsresultat"),
    ("total_result", "totalresultat"),
    ("total_assets", "sumEiendeler"),
    ("fixed_assets", "sumAnleggsmidler"),
    ("current_assets", "sumOmloepsmidler"),
    ("equity", "sumEgenkapital"),
    ("total_debt", "sumGjeld"),
    ("long_term_debt", "sumLangsiktigGjeld"),
    ("short_term_debt", "sumKortsiktigGjeld"),
]


def find_numeric(obj, key: str, path: str = ""):
    """Depth-first search for the first numeric value stored under `key`."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}/{k}"
            if k == key and isinstance(v, (int, float)) and not isinstance(v, bool):
                return v, p
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                r = find_numeric(v, key, f"{path}/{k}")
                if r[0] is not None:
                    return r
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            r = find_numeric(v, key, f"{path}/{i}")
            if r[0] is not None:
                return r
    return None, None


def accounts_list(data) -> list[dict]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        emb = data.get("_embedded")
        if isinstance(emb, dict):
            for v in emb.values():
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
        return [data]
    return []


def financial_claims(cl: Claims, fr: FetchResult, data) -> set[int]:
    years: set[int] = set()
    for i, r in enumerate(accounts_list(data)):
        per = r.get("regnskapsperiode") or {}
        to = per.get("tilDato") or ""
        if not re.match(r"^\d{4}", to):
            continue
        year = int(to[:4])
        typ = (r.get("regnskapstype") or "SELSKAP").upper()
        cur = r.get("valuta") or "NOK"
        period = {"from": per.get("fraDato"), "to": to, "fiscal_year": year, "account_type": typ}
        vals = {}
        for metric, k in METRICS:
            v, path = find_numeric(r, k)
            if v is None:
                continue  # missing stays missing, never zero
            vals[metric] = v
            cl.add("financials", metric,
                   {"amount": v, "currency": cur, "fiscal_year": year, "account_type": typ},
                   fr=fr, method="registry_api", locator=f"/{i}{path}", excerpt=f"{k} = {v} {cur} ({year})",
                   key=f"financials:{metric}:{year}:{typ}", period=period)
        if vals.get("total_assets") and "equity" in vals:
            ratio = round(vals["equity"] / vals["total_assets"] * 100, 1)
            cl.add("financials", "equity_ratio_pct",
                   {"amount": ratio, "unit": "%", "fiscal_year": year, "account_type": typ},
                   fr=fr, method="derived:sumEgenkapital/sumEiendeler", locator=f"/{i}",
                   excerpt=f"{vals['equity']} / {vals['total_assets']}",
                   key=f"financials:equity_ratio_pct:{year}:{typ}", period=period)
        rev = r.get("revisjon") or {}
        if isinstance(rev, dict) and rev:
            audit = {
                "audited": None if rev.get("ikkeRevidertAarsregnskap") is None else not rev.get("ikkeRevidertAarsregnskap"),
                "audit_opted_out": rev.get("fravalgRevisjon"),
            }
            audit = {k: v for k, v in audit.items() if v is not None}
            cl.add("financials", "audit", audit, fr=fr, method="registry_api", locator=f"/{i}/revisjon",
                   excerpt=str(rev), key=f"financials:audit:{year}:{typ}", period=period)
        years.add(year)
    return years


# --------------------------------------------------------------------------- announcements

_DMY = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


def parse_kunngjoringer(fr: FetchResult, limit: int = 20) -> list[dict]:
    soup = fr.soup()
    seen, out = set(), []
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(r"hent_en\.jsp", re.I))
        if not a:
            continue
        m = _DMY.search(tr.get_text(" ", strip=True))
        if not m:
            continue
        url = urljoin(fr.where, a.get("href"))
        if url in seen:
            continue
        seen.add(url)
        out.append({"date": f"{m.group(3)}-{m.group(2)}-{m.group(1)}",
                    "title": a.get_text(" ", strip=True), "url": url})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out[:limit]


def announcement_claims(cl: Claims, fr: FetchResult, items: list[dict]) -> int:
    n = 0
    for it in items:
        if cl.add("public_activity", "registry_announcement",
                  {"date": it["date"], "title": it["title"], "url": it["url"], "source": "Brønnøysundregistrene"},
                  fr=fr, method="html_table", locator=f"a[href='{it['url']}']",
                  excerpt=f"{it['date']} {it['title']}", key=f"public_activity:announcement:{it['url']}",
                  period={"date": it["date"]}):
            n += 1
    return n
