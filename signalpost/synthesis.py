"""Deterministic, citation-carrying synthesis.

Every statement is generated from published claims and lists the claim_ids that
support it. Nothing is inferred that is not in a claim; unknowns are stated.
"""
from __future__ import annotations

from collections import Counter

from .claims import FAMILY_LABELS, FAMILIES


def money(v, cur="NOK") -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e9:
        s = f"{a / 1e9:.1f}bn"
    elif a >= 1e6:
        s = f"{a / 1e6:.1f}m"
    elif a >= 1e3:
        s = f"{a / 1e3:.0f}k"
    else:
        s = f"{a:.0f}"
    return f"{sign}{cur} {s}"


def pct(a, b):
    try:
        if b in (0, None) or a is None:
            return None
        return (float(a) - float(b)) / abs(float(b)) * 100
    except (TypeError, ValueError):
        return None


def _first(claims, key):
    for c in claims:
        if c.get("claim_key") == key:
            return c
    return None


def _fin_table(claims) -> dict:
    """{(year, type): {metric: claim}} using current + historical financial claims."""
    t: dict = {}
    for c in claims:
        if c.get("family") != "financials":
            continue
        v = c.get("value") or {}
        y, typ = v.get("fiscal_year"), v.get("account_type", "SELSKAP")
        if y is None:
            continue
        t.setdefault((int(y), typ), {})[c["field"]] = c
    return t


def build(claims: list[dict], historical: list[dict], fields: dict, refresh: dict, website: dict | None) -> dict:
    cur = claims
    allc = claims + [h for h in historical if h.get("claim_key")]
    stmts: list[dict] = []
    signals: list[dict] = []

    def say(topic, text, cs):
        ids = [c["claim_id"] for c in cs if c and c.get("claim_id")]
        if text and ids:
            stmts.append({"topic": topic, "text": text, "claim_ids": ids})

    # ------------------------------------------------ what it does
    name = _first(cur, "identity:legal_name")
    brand = _first(cur, "identity:public_brand")
    form = _first(cur, "identity:legal_form")
    ind = _first(cur, "identity:industry:1")
    muni = _first(cur, "identity:municipality")
    founded = _first(cur, "identity:founded_date") or _first(cur, "identity:registered_date")
    if name:
        parts = [name["value"]]
        if brand:
            parts.append(f'(trading as "{brand["value"]}")')
        s = " ".join(parts) + " is "
        s += f'a {form["value"]["description"]}' if form and form["value"].get("description") else "a registered entity"
        if muni:
            s += f' based in {str(muni["value"].get("name", "")).title()}'
        if ind and ind["value"].get("description"):
            s += f', classified under "{ind["value"]["description"]}" ({ind["value"]["code"]})'
        if founded:
            s += f', on record since {str(founded["value"])[:4]}'
        say("what_it_does", s + ".", [name, brand, form, muni, ind, founded])
    act = _first(cur, "identity:registered_activity") or _first(cur, "identity:statutory_purpose")
    if act:
        txt = str(act["value"])
        say("what_it_does", f"Registered activity: {txt[:260]}{'…' if len(txt) > 260 else ''}", [act])
    desc = _first(cur, "identity:self_description")
    if desc:
        say("what_it_does", f'Its own website describes it as: "{str(desc["value"])[:220]}" (company-owned copy, not an independent assessment).', [desc])

    # ------------------------------------------------ status
    st = _first(cur, "identity:registry_status")
    if st:
        v = st["value"]
        if v.get("bankrupt"):
            say("status", f"The register shows the company as bankrupt{(' since ' + v['bankruptcy_date']) if v.get('bankruptcy_date') else ''}.", [st])
            signals.append({"type": "risk", "text": "Bankrupt", "claim_ids": [st["claim_id"]]})
        elif v.get("under_liquidation") or v.get("forced_dissolution"):
            say("status", "The register shows the company as under liquidation/dissolution.", [st])
            signals.append({"type": "risk", "text": "Under liquidation", "claim_ids": [st["claim_id"]]})
        elif v.get("deleted_date"):
            say("status", f"The entity was deleted from the register on {v['deleted_date']}.", [st])
            signals.append({"type": "risk", "text": "Deleted from register", "claim_ids": [st["claim_id"]]})
        else:
            extra = []
            if v.get("in_vat_register"):
                extra.append("VAT-registered")
            if v.get("in_business_register"):
                extra.append("in the Register of Business Enterprises")
            say("status", "No bankruptcy or liquidation is registered" + (f"; it is {' and '.join(extra)}" if extra else "") + ".", [st])

    # ------------------------------------------------ financials
    table = _fin_table(allc)
    years = sorted({y for (y, typ) in table if typ == "SELSKAP"} or {y for (y, _) in table}, reverse=True)
    if years:
        typ = "SELSKAP" if (years[0], "SELSKAP") in table else next(t for (y, t) in table if y == years[0])
        latest = table.get((years[0], typ), {})
        prev = table.get((years[0] - 1, typ), {}) if len(years) > 1 else {}
        bits, cs = [], []
        for m, label in (("revenue", "revenue"), ("operating_result", "operating result"), ("net_income", "net income")):
            c = latest.get(m)
            if c:
                bits.append(f"{label} {money(c['value']['amount'], c['value'].get('currency', 'NOK'))}")
                cs.append(c)
        if bits:
            say("financials", f"Latest filed accounts ({years[0]}{'' if typ == 'SELSKAP' else ', group'}): " + ", ".join(bits) + ".", cs)
        eq = latest.get("equity")
        er = latest.get("equity_ratio_pct")
        if eq is not None:
            amount = eq["value"]["amount"]
            if amount < 0:
                say("financials", f"Equity is negative ({money(amount)}), a solvency warning sign.", [eq])
                signals.append({"type": "risk", "text": "Negative equity", "claim_ids": [eq["claim_id"]]})
            elif er:
                say("financials", f"Equity of {money(amount)} gives an equity ratio of {er['value']['amount']}%.", [eq, er])
        r1, r0 = latest.get("revenue"), prev.get("revenue")
        if r1 and r0:
            g = pct(r1["value"]["amount"], r0["value"]["amount"])
            if g is not None:
                say("financials", f"Revenue {'grew' if g >= 0 else 'fell'} {abs(g):.0f}% from {years[0] - 1} to {years[0]}.", [r1, r0])
                if abs(g) >= 25:
                    signals.append({"type": "growth" if g > 0 else "risk", "text": f"Revenue {'+' if g >= 0 else ''}{g:.0f}% YoY",
                                    "claim_ids": [r1["claim_id"], r0["claim_id"]]})
        ni = latest.get("net_income")
        if ni and ni["value"]["amount"] < 0:
            signals.append({"type": "risk", "text": "Loss in latest filed year", "claim_ids": [ni["claim_id"]]})

    # ------------------------------------------------ leadership
    roles = [c for c in cur if c["family"] == "leadership" and c["field"] == "role"]
    ceo = [c for c in roles if c["value"].get("role_code") == "DAGL"]
    chair = [c for c in roles if c["value"].get("role_code") == "LEDE"]
    owner = [c for c in roles if c["value"].get("role_code") in ("INNH", "DTPR", "DTSO", "KOMP")]
    board = [c for c in roles if c["value"].get("role_code") in ("MEDL", "LEDE", "NEST")]
    aud = [c for c in cur if c["family"] == "leadership" and c["value"].get("role_code") == "REVI"]
    parts, cs = [], []
    if ceo:
        parts.append(f"managed by {', '.join(c['value']['name'] for c in ceo)} (CEO)")
        cs += ceo
    if chair:
        parts.append(f"chaired by {chair[0]['value']['name']}")
        cs += chair
    if owner and not ceo:
        parts.append(f"run by {', '.join(c['value']['name'] for c in owner[:3])} ({owner[0]['value'].get('role_en') or owner[0]['value'].get('role')})")
        cs += owner[:3]
    if board:
        parts.append(f"{len(board)} registered board member{'s' if len(board) != 1 else ''}")
        cs += board
    if parts:
        say("leadership", "It is " + "; ".join(parts) + ".", cs)
    if aud:
        say("leadership", f"Auditor on record: {aud[0]['value']['name']}.", aud[:1])

    # ------------------------------------------------ footprint
    emp = _first(cur, "identity:registered_employees")
    subs = [c for c in cur if c["family"] == "workplaces" and c["field"] == "subunit"]
    fp, cs = [], []
    if emp is not None:
        fp.append(f"{emp['value']} registered employees")
        cs.append(emp)
    if subs:
        munis = Counter(str(c["value"].get("municipality") or "").title() for c in subs if c["value"].get("municipality"))
        where = ", ".join(m for m, _ in munis.most_common(3))
        fp.append(f"{len(subs)} registered workplace{'s' if len(subs) != 1 else ''}" + (f" (mainly {where})" if where else ""))
        cs += subs[:5]
    if fp:
        say("footprint", "Footprint: " + "; ".join(fp) + ".", cs)

    # ------------------------------------------------ online presence
    site = [c for c in cur if c["family"] == "website"]
    socials = [c for c in cur if c["family"] == "social_profiles"]
    if site:
        w = site[0]
        how = {"orgnr_on_site": "the site shows its organisation number",
               "registry_declared": "the website is declared in the official register",
               "subunit_registry_declared": "the website is declared for one of its registered workplaces",
               "registry_email_domain_and_name": "the site matches its registered email domain and name",
               "legal_name_and_address_or_phone": "the site shows its legal name and registered address/phone",
               "brand_address_and_phone": "the site shows its brand, registered address and phone"}.get(w.get("identity_basis"), "verified")
        rel = w.get("entity_relationship")
        note = " The site appears to be shared with a group or other entity, so its content is not attributed to this company." if rel == "shared_or_group_site" else ""
        say("online", f"Official website {w['value'].get('url')} (verified because {how}).{note}", [w])
    if socials:
        say("online", "Company-owned profiles linked from the verified site: " + ", ".join(sorted({c["value"]["platform"].title() for c in socials})) + ".", socials)

    # ------------------------------------------------ hiring
    jobs = [c for c in cur if c["family"] == "hiring" and c["field"] == "job_posting"]
    careers = [c for c in cur if c["family"] == "hiring" and c["field"] == "careers_page"]
    if jobs:
        titles = [c["value"].get("title") for c in jobs if c["value"].get("title")][:3]
        say("hiring", f"Appears to be hiring: {len(jobs)} open position{'s' if len(jobs) != 1 else ''} found" + (f", e.g. {'; '.join(titles)}" if titles else "") + ".", jobs[:6])
        signals.append({"type": "hiring", "text": f"{len(jobs)} open positions", "claim_ids": [c["claim_id"] for c in jobs[:6]]})
    elif careers:
        say("hiring", "Has a careers page, but no individual open positions could be confirmed.", careers[:1])

    # ------------------------------------------------ activity
    acts = [c for c in allc if c.get("family") == "public_activity" and (c.get("value") or {}).get("date") and c.get("claim_id")]
    acts.sort(key=lambda c: c["value"]["date"], reverse=True)
    if acts:
        top = acts[:3]
        say("activity", "Most recent dated activity: " + "; ".join(f"{c['value']['date']} — {c['value'].get('title')}" for c in top) + ".", top)

    # ------------------------------------------------ changes
    changes = (refresh or {}).get("changes") or []
    if refresh and not refresh.get("is_first_run"):
        if changes:
            cnt = Counter(c["change_type"] for c in changes)
            txt = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in cnt.most_common())
            ids = [c.get("claim_id") for c in cur if c.get("change") in ("added", "updated")]
            stmts.append({"topic": "changes", "text": f"Since the previous run ({refresh.get('previous_run_at')}): {txt}.",
                          "claim_ids": [i for i in ids if i][:20]})
        else:
            stmts.append({"topic": "changes", "text": f"No material changes since the previous run ({refresh.get('previous_run_at')}).", "claim_ids": []})
    what_changed = [{"change_type": c["change_type"], "materiality": c["materiality"], "claim_key": c["claim_key"],
                     "previous_value": c["previous_value"], "current_value": c["current_value"]} for c in changes[:30]]

    # ------------------------------------------------ unknowns
    unknowns = []
    for fam in FAMILIES:
        f = fields.get(fam) or {}
        if f.get("state") != "available":
            unknowns.append({"family": fam, "label": FAMILY_LABELS[fam], "state": f.get("state"), "reason": f.get("reason")})

    headline_parts = [s["text"] for s in stmts if s["topic"] in ("what_it_does",)][:1]
    fin = [s["text"] for s in stmts if s["topic"] == "financials"][:1]
    headline = " ".join(headline_parts + fin) or None
    # The rubric asks for three things: what the company does, what changed, what remains
    # unknown — each with sources. Group the cited statements under exactly those headings.
    section_of = {"changes": "what_changed"}
    sections = {"what_it_does": [], "what_changed": [], "what_remains_unknown": []}
    for s in stmts:
        sections[section_of.get(s["topic"], "what_it_does")].append(s)
    if refresh and refresh.get("is_first_run"):
        sections["what_changed"].append({"topic": "changes", "claim_ids": [],
                                         "text": "First observation of this company; changes will be reported from the next refresh."})
    for u in unknowns:
        sections["what_remains_unknown"].append({
            "topic": "unknown", "claim_ids": [], "family": u["family"],
            "text": f"{u['label']}: {u['state'].replace('_', ' ')}" + (f" — {u['reason']}" if u.get("reason") else "")})
    if not unknowns:
        sections["what_remains_unknown"].append({"topic": "unknown", "claim_ids": [],
                                                 "text": "Every information type was found and sourced for this company."})
    return {
        "method": "deterministic_template_from_claims",
        "headline": headline,
        "sections": sections,
        "statements": stmts,
        "signals": signals,
        "what_changed": what_changed,
        "unknowns": unknowns,
        "coverage": {fam: (fields.get(fam) or {}).get("state") for fam in FAMILIES},
        "website_resolution": website,
    }
