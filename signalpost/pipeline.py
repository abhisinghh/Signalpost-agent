"""Batch runner: one terminal envelope per input, inside a fixed time budget."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import statistics
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import AGENT_NAME, __version__, brreg, web, wikidata
from .claims import FAMILIES, FAMILY_LABELS, Claims
from .http import Ctx, Fetcher, FetchResult
from .identity import WebsiteResolver
from .jobs import NavJobIndex
from .refresh import apply_refresh
from .search import SearchClient
from .store import SnapshotStore, StateStore
from .synthesis import build as build_synthesis
from .util import now_iso, short_hash, valid_orgnr

SCHEMA_VERSION = "signalpost.envelope.v1"


@dataclass
class Config:
    concurrency: int = 24
    time_budget_s: float = 3300.0       # whole run
    per_company_s: float = 75.0         # upper bound for one company
    min_full_s: float = 25.0            # below this, run registry-only
    safety_margin_s: float = 45.0       # reserved for writing outputs
    nav_budget_fraction: float = 0.15
    state_dir: Path = Path(".signalpost-state")
    respect_robots: bool = True
    contact: str = os.environ.get("SIGNALPOST_CONTACT", "see repository README")

    @property
    def user_agent(self) -> str:
        return f"SignalpostResearchBot/{__version__} (+{AGENT_NAME}; contact: {self.contact})"


class FamilyStates:
    def __init__(self):
        self.s = {f: {"state": "not_available", "reason": "not checked", "source_checked": False} for f in FAMILIES}

    def set(self, fam: str, state: str, reason: str | None = None, checked: bool | None = None):
        d = self.s[fam]
        d["state"] = state
        d["reason"] = reason
        if checked is not None:
            d["source_checked"] = checked

    def failed_if_unchecked(self, reason: str):
        for d in self.s.values():
            if d["reason"] == "not checked":
                d["state"], d["reason"] = "failed", reason


@dataclass
class Work:
    orgnr: str
    index: int
    record: dict
    ctx: Ctx
    cl: Claims
    fs: FamilyStates = field(default_factory=FamilyStates)
    enhet: dict | None = None
    identity_status: str = "failed"
    status_reason: str | None = None
    website: dict | None = None
    started_at: str = field(default_factory=now_iso)
    finished: bool = False
    checked_sources: set = field(default_factory=set)
    tasks: list = field(default_factory=list)


class Runtime:
    def __init__(self, cfg: Config, input_path: str):
        self.cfg = cfg
        self.input_path = input_path
        self.run_id = "run_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "_" + uuid.uuid4().hex[:6]
        self.run_at = now_iso()
        self.t0 = time.monotonic()
        self.hard_deadline = self.t0 + cfg.time_budget_s - cfg.safety_margin_s
        self.snapshots = SnapshotStore(cfg.state_dir / "snapshots")
        self.states = StateStore(cfg.state_dir)
        self.fetcher = Fetcher(self.snapshots, user_agent=cfg.user_agent, ua_token="SignalpostResearchBot",
                               global_concurrency=max(8, cfg.concurrency * 2), respect_robots=cfg.respect_robots,
                               global_deadline=self.hard_deadline)
        self.search = SearchClient.from_env()
        self.resolver = WebsiteResolver(self.fetcher, search=self.search)
        self.nav = NavJobIndex(self.fetcher)
        self.wikidata_sites: dict[str, list[str]] = {}
        self.durations: list[float] = []
        self.remaining_companies = 0

    async def aclose(self):
        await self.fetcher.aclose()
        if self.search:
            await self.search.aclose()

    def company_budget(self) -> tuple[float, bool]:
        left = self.hard_deadline - time.monotonic()
        n = max(1, self.remaining_companies)
        fair = left * self.cfg.concurrency / n
        budget = min(self.cfg.per_company_s, max(8.0, fair * 0.9))
        return budget, budget < self.cfg.min_full_s


# ---------------------------------------------------------------------------- research


def _input_snapshot(rt: Runtime, w: Work) -> FetchResult | None:
    """Preserve the Builderr-supplied input record as a source snapshot (identity fallback)."""
    if not w.record:
        return None
    body = json.dumps(w.record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fr = FetchResult(url=f"input:{Path(rt.input_path).name}#{w.orgnr}", ok=True, status=200,
                     final_url=f"input:{Path(rt.input_path).name}#{w.orgnr}", content=body,
                     content_type="application/json")
    fr.snapshot = rt.snapshots.put(content=body, requested_url=fr.url, final_url=fr.url, status=200,
                                   content_type=fr.content_type, redirects=[], truncated=False,
                                   source_class="supplied_registry_snapshot")
    return fr


async def research(rt: Runtime, w: Work) -> None:
    f, ctx, cl, fs, orgnr = rt.fetcher, w.ctx, w.cl, w.fs, w.orgnr

    # 1. Identity anchor -------------------------------------------------------
    efr = await brreg.fetch_enhet(f, ctx, orgnr)
    enhet = None
    if efr.ok:
        try:
            enhet = efr.json()
        except ValueError:
            enhet = None
    if enhet and enhet.get("organisasjonsnummer"):
        w.enhet = enhet
        w.checked_sources.add("official_register")
        brreg.identity_claims(cl, efr, enhet)
        fs.set("identity", "available", None, True)
        w.identity_status = "available"
    elif efr.error == "not_found":
        w.checked_sources.add("official_register")
        fs.set("identity", "not_available", "organisation number not found (or deleted) in Enhetsregisteret", True)
        w.identity_status, w.status_reason = "not_available", "not found in Enhetsregisteret"
    else:
        seed = w.record if (w.record or {}).get("navn") else None
        sfr = _input_snapshot(rt, w) if seed else None
        if seed and sfr:
            w.enhet = dict(seed, organisasjonsnummer=orgnr)
            w.checked_sources.add("supplied_registry_snapshot")
            brreg.identity_claims(cl, sfr, w.enhet)
            fs.set("identity", "available", "live register unavailable; used the supplied registry snapshot", True)
            w.identity_status = "available"
        else:
            state = "blocked" if efr.error == "blocked" else "failed"
            fs.set("identity", state, f"Enhetsregisteret request failed: {efr.error} {efr.status or ''}".strip(), False)
            w.identity_status, w.status_reason = state, f"register lookup {efr.error}"
            ctx.error("identity", efr.error or "error", efr.detail or "", efr.url)
    if w.enhet is None:
        for fam in FAMILIES:
            if fam != "identity" and fs.s[fam]["reason"] == "not checked":
                fs.set(fam, "not_applicable" if w.identity_status == "not_available" else w.identity_status,
                       "no verified legal identity to research", False)
        return
    enhet = w.enhet

    # 2. Registry fan-out ---------------------------------------------------------
    roller_t = asyncio.create_task(brreg.fetch_roller(f, ctx, orgnr))
    under_t = asyncio.create_task(brreg.fetch_underenheter(f, ctx, orgnr))
    regn_t = asyncio.create_task(brreg.fetch_regnskap(f, ctx, orgnr))
    kunn_t = asyncio.create_task(brreg.fetch_kunngjoringer(f, ctx, orgnr))
    w.tasks += [roller_t, under_t, regn_t, kunn_t]  # cancelled by the runner on timeout

    rfr = await roller_t
    if rfr.ok:
        try:
            n = brreg.role_claims(cl, rfr, rfr.json())
            fs.set("leadership", "available" if n else "not_available",
                   None if n else "no active roles registered", True)
        except ValueError:
            fs.set("leadership", "failed", "roles response was not JSON", False)
    elif rfr.error == "not_found":
        fs.set("leadership", "not_available", "no roles registered", True)
    else:
        fs.set("leadership", "blocked" if rfr.error == "blocked" else "failed", f"roles request {rfr.error}", False)

    rows, ufail = await under_t
    subunits = brreg.subunit_claims(cl, rows)
    if ufail is not None and not rows:
        fs.set("workplaces", "blocked" if ufail.error == "blocked" else "failed", f"subunit request {ufail.error}", False)
    elif subunits:
        fs.set("workplaces", "available", None, ufail is None)
    else:
        fs.set("workplaces", "not_available", "no registered subunits (workplaces)", ufail is None)

    gfr = await regn_t
    years: set[int] = set()
    if gfr.ok:
        try:
            years = brreg.financial_claims(cl, gfr, gfr.json())
        except ValueError:
            pass
        fs.set("financials", "available" if years else "not_available",
               None if years else "no figures in the accounts register response", True)
    elif gfr.error == "not_found":
        fs.set("financials", "not_available", "no annual accounts in Regnskapsregisteret", True)
    else:
        fs.set("financials", "blocked" if gfr.error == "blocked" else "failed", f"accounts request {gfr.error}", False)
    # earlier years, when the API serves them
    if years and not ctx.expired(20.0):
        latest = max(years)
        hist = await asyncio.gather(*(brreg.fetch_regnskap(f, ctx, orgnr, y) for y in (latest - 1, latest - 2)))
        for hfr in hist:
            if hfr.ok:
                try:
                    brreg.financial_claims(cl, hfr, hfr.json())
                except ValueError:
                    pass

    kfr = await kunn_t
    n_ann = 0
    if kfr.ok:
        try:
            n_ann = brreg.announcement_claims(cl, kfr, brreg.parse_kunngjoringer(kfr))
        except Exception as e:
            ctx.error("announcements", "parse_error", str(e), kfr.url)
    act_checked = kfr.ok

    # Registered contact points
    if cl.family("contact"):
        fs.set("contact", "available", None, True)
    else:
        fs.set("contact", "not_available", "no contact points registered", True)

    # 3. Website identity + company-owned facts -----------------------------------
    site_stats = None
    if ctx.expired(6.0):
        fs.set("website", "failed", "time budget exhausted before website check", False)
    else:
        res = await rt.resolver.resolve(ctx, orgnr, enhet, subunits, wikidata_urls=rt.wikidata_sites.get(orgnr))
        w.website = {"state": res.state, "reason": res.reason, "candidates_tried": res.tried}
        if res.state == "available":
            v = res.verdict
            cl.add("website", "official_website",
                   {"url": v.home.where, "domain": v.home.where.split("/")[2], "declared_url": v.candidate.url,
                    "candidate_source": v.candidate.source},
                   fr=v.proof or v.home, method=f"identity:{v.basis}", locator="orgnr" if v.basis == "orgnr_on_site" else "homepage",
                   excerpt=v.proof_excerpt or v.reason, key="website:official_website",
                   basis=v.basis, relationship=v.relationship,
                   extra_evidence=[efr] if v.candidate.source in ("registry_website", "registry_email_domain") else None)
            fs.set("website", "available", v.reason, True)
            if v.extract_site_facts and not ctx.expired(10.0):
                try:
                    site = await web.crawl(f, ctx, v)
                    site_stats = web.site_claims(cl, site, enhet)
                except Exception as e:
                    ctx.error("site", "crawl_error", f"{type(e).__name__}: {e}")
        else:
            fs.set("website", res.state, res.reason, res.checked)
            if res.checked:
                # site provably no longer attributable: website-family evidence may be retired
                w.checked_sources |= {"company_site", "domain_registry", "wikidata"}

    site_ok = site_stats is not None
    if site_ok:
        w.checked_sources |= {"company_site", "domain_registry", "wikidata"}
    wstate = fs.s["website"]["state"]
    if cl.family("social_profiles"):
        fs.set("social_profiles", "available", None, True)
    elif site_ok:
        fs.set("social_profiles", "not_available", "verified site links no company profiles", True)
    elif wstate == "available":
        fs.set("social_profiles", "ambiguous" if fs.s["website"]["reason"] and "group" in fs.s["website"]["reason"] else "not_available",
               "site not attributable or could not be crawled; profiles not extracted", False)
    else:
        fs.set("social_profiles", wstate if wstate in ("ambiguous", "blocked") else "not_available",
               "no verified website to find company-owned profiles from", wstate == "not_available")
    if cl.family("contact"):
        fs.set("contact", "available", None, True)

    # 4. Hiring ---------------------------------------------------------------------
    nav_checked = False
    if not ctx.expired(5.0):
        try:
            await asyncio.wait_for(rt.nav.ready.wait(), timeout=max(0.1, min(ctx.remaining() - 5.0, 60)))
        except asyncio.TimeoutError:
            pass
        if rt.nav.ready.is_set():
            names = [enhet.get("navn") or ""]
            brand = cl.items.get("identity:public_brand")
            if brand:
                names.append(brand["value"])
            names += [u.get("navn") or "" for u in subunits[:20]]
            sub_orgs = {u.get("organisasjonsnummer") for u in subunits if u.get("organisasjonsnummer")}
            try:
                _, nav_checked = await rt.nav.company_jobs(ctx, cl, orgnr, names, sub_orgs)
            except Exception as e:
                ctx.error("hiring", "nav_error", f"{type(e).__name__}: {e}")
    if nav_checked:
        w.checked_sources.add("public_job_feed")
    if cl.family("hiring"):
        fs.set("hiring", "available", None, True)
    else:
        checked = nav_checked or site_ok
        why = []
        if nav_checked:
            why.append("no NAV job ads under this organisation number")
        if site_ok:
            why.append("no open positions on the verified website")
        if not why:
            why.append("no job source could be checked")
        fs.set("hiring", "not_available" if checked else "failed" if ctx.expired(5.0) else "not_available",
               "; ".join(why), checked)

    # 5. Activity -------------------------------------------------------------------
    if cl.family("public_activity"):
        fs.set("public_activity", "available", None, True)
    else:
        fs.set("public_activity", "not_available" if act_checked else ("blocked" if kfr.error == "blocked" else "failed"),
               "no dated announcements or news found" if act_checked else f"announcement register {kfr.error}",
               act_checked)
    w.finished = True


# ---------------------------------------------------------------------------- envelope


def _section(fs: FamilyStates, cl: Claims, fam: str) -> dict:
    d = fs.s[fam]
    cs = cl.family(fam)
    return {"state": "available" if cs else d["state"], "reason": d["reason"],
            "source_checked": d["source_checked"], "claim_ids": [c["claim_id"] for c in cs]}


def assemble(rt: Runtime, w: Work) -> dict:
    cl, fs = w.cl, w.fs
    if not w.finished:
        fs.failed_if_unchecked("time budget exhausted before this source was checked")
    for fam in FAMILIES:  # a family with published claims is available by definition
        if cl.family(fam):
            fs.s[fam]["state"] = "available"
    claims = cl.all()
    fields = {fam: {**fs.s[fam], "claim_count": len(cl.family(fam))} for fam in FAMILIES}

    prev = rt.states.load(w.orgnr)
    refresh_block, new_state, carried = apply_refresh(prev, claims, fields, rt.run_id, rt.run_at,
                                                      checked_sources=set(w.checked_sources))
    for c in carried:
        c["claim_id"] = "clm_" + short_hash(f"{w.orgnr}|{c['claim_key']}", 16)
    historical = [c for c in carried if c.get("historical")]
    stale = [c for c in carried if not c.get("historical")]
    if w.identity_status in ("available",) or prev is not None:
        try:
            rt.states.save(w.orgnr, new_state)
        except OSError as e:
            w.ctx.error("refresh", "state_write_failed", str(e))

    synthesis = build_synthesis(claims, historical + stale, fields, refresh_block, w.website)

    def first_value(key):
        c = cl.items.get(key)
        return c["value"] if c else None

    fin_years = sorted({c["value"]["fiscal_year"] for c in claims + historical if c["family"] == "financials"
                        and isinstance(c.get("value"), dict) and c["value"].get("fiscal_year")}, reverse=True)
    latest_fig = {}
    if fin_years:
        for c in claims + historical:
            v = c.get("value") or {}
            if c["family"] == "financials" and v.get("fiscal_year") == fin_years[0] and v.get("account_type", "SELSKAP") == "SELSKAP":
                latest_fig[c["field"]] = {"amount": v.get("amount"), "currency": v.get("currency"), "claim_id": c["claim_id"]}
    website_claim = cl.items.get("website:official_website")
    overall = w.identity_status
    duration = time.monotonic() - w.ctx.started

    env = {
        "schema_version": SCHEMA_VERSION,
        "organisation_number": w.orgnr,
        "input_index": w.index,
        "status": overall,
        "status_reason": w.status_reason,
        "legal_identity": {
            "state": fs.s["identity"]["state"],
            "organisation_number": w.orgnr,
            "organisation_number_checksum_valid": valid_orgnr(w.orgnr),
            "legal_name": first_value("identity:legal_name"),
            "public_brand": first_value("identity:public_brand"),
            "legal_form": first_value("identity:legal_form"),
            "industry": first_value("identity:industry:1"),
            "business_address": first_value("identity:business_address"),
            "municipality": first_value("identity:municipality"),
            "registry_status": first_value("identity:registry_status"),
            "claim_ids": [c["claim_id"] for c in cl.family("identity")],
        },
        "sections": {
            "legal_identity_and_brand": _section(fs, cl, "identity"),
            "annual_accounts": {**_section(fs, cl, "financials"), "fiscal_years": fin_years,
                                "latest_year": fin_years[0] if fin_years else None, "latest_figures": latest_fig,
                                "historical_claim_ids": [c["claim_id"] for c in historical if c["family"] == "financials"]},
            "leadership_and_workplaces": {"leadership": _section(fs, cl, "leadership"),
                                          "workplaces": _section(fs, cl, "workplaces")},
            "website_and_profiles": {"website": {**_section(fs, cl, "website"),
                                                 "url": website_claim["value"]["url"] if website_claim else None,
                                                 "identity_basis": website_claim["identity_basis"] if website_claim else None,
                                                 "entity_relationship": website_claim["entity_relationship"] if website_claim else None},
                                     "social_profiles": _section(fs, cl, "social_profiles"),
                                     "contact": _section(fs, cl, "contact")},
            "hiring_and_activity": {"hiring": _section(fs, cl, "hiring"),
                                    "public_activity": _section(fs, cl, "public_activity")},
        },
        "fields": fields,
        "claims": claims,
        "historical_claims": historical,
        "stale_claims": stale,
        "synthesis": synthesis,
        "website_resolution": w.website,
        "source_snapshots": sorted(cl.snapshots.values(), key=lambda s: s["snapshot_id"]),
        "refresh": {k: v for k, v in refresh_block.items() if k != "carried_forward"},
        "run": {
            "run_id": rt.run_id, "agent": AGENT_NAME, "agent_version": __version__,
            "started_at": w.started_at, "finished_at": now_iso(), "duration_s": round(duration, 2),
            "requests": w.ctx.requests, "mode": "registry_only" if w.ctx.registry_only else "full",
            "third_party_cost_usd": 0.0,
        },
        "errors": w.ctx.errors,
    }
    return env


def failed_envelope(rt: Runtime, orgnr: str, index: int, reason: str) -> dict:
    fields = {f: {"state": "failed", "reason": reason, "source_checked": False, "claim_count": 0} for f in FAMILIES}
    return {
        "schema_version": SCHEMA_VERSION, "organisation_number": orgnr, "input_index": index,
        "status": "failed", "status_reason": reason,
        "legal_identity": {"state": "failed", "organisation_number": orgnr,
                           "organisation_number_checksum_valid": valid_orgnr(orgnr), "claim_ids": []},
        "sections": {}, "fields": fields, "claims": [], "historical_claims": [], "stale_claims": [],
        "synthesis": {"method": "deterministic_template_from_claims", "headline": None, "statements": [],
                      "sections": {"what_it_does": [], "what_changed": [],
                                   "what_remains_unknown": [{"topic": "unknown", "claim_ids": [], "family": f,
                                                             "text": f"{FAMILY_LABELS[f]}: failed — {reason}"} for f in FAMILIES]},
                      "signals": [], "what_changed": [],
                      "unknowns": [{"family": f, "state": "failed", "reason": reason} for f in FAMILIES]},
        "source_snapshots": [], "refresh": {"is_first_run": None, "changes": []},
        "run": {"run_id": rt.run_id, "agent": AGENT_NAME, "agent_version": __version__, "finished_at": now_iso()},
        "errors": [{"stage": "run", "kind": "failed", "message": reason, "at": now_iso()}],
    }


# ---------------------------------------------------------------------------- batch


async def run_batch(cfg: Config, items: list[dict], input_path: str, partial_path: Path | None = None) -> tuple[list[dict], dict]:
    rt = Runtime(cfg, input_path)
    rt.remaining_companies = len(items)
    works: dict[str, Work] = {}
    results: dict[str, dict] = {}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    await rt.resolver.calibrate_dns()
    nav_task = asyncio.create_task(rt.nav.build(max(30.0, cfg.time_budget_s * cfg.nav_budget_fraction)))
    try:
        rt.wikidata_sites = await asyncio.wait_for(
            wikidata.website_candidates(rt.fetcher, [it["orgnr"] for it in items]), timeout=90.0)
    except Exception:  # optional source; never fatal
        rt.wikidata_sites = {}
    sem = asyncio.Semaphore(cfg.concurrency)
    partial = open(partial_path, "a", encoding="utf-8") if partial_path else None

    async def one(item):
        orgnr, idx = item["orgnr"], item["index"]
        async with sem:
            if stop.is_set():
                return
            budget, registry_only = rt.company_budget()
            ctx = Ctx(orgnr, time.monotonic() + budget, registry_only=registry_only)
            w = Work(orgnr=orgnr, index=idx, record=item.get("record") or {}, ctx=ctx, cl=Claims(orgnr))
            works[orgnr] = w
            try:
                await asyncio.wait_for(research(rt, w), timeout=budget + 5.0)
            except asyncio.TimeoutError:
                ctx.error("run", "company_timeout", f"company budget {budget:.0f}s exhausted")
            except asyncio.CancelledError:
                ctx.error("run", "cancelled", "run stopped before this company finished")
                raise
            except Exception as e:
                ctx.error("run", "exception", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
            finally:
                rt.remaining_companies -= 1
                for t in w.tasks:
                    if not t.done():
                        t.cancel()
                if w.tasks:
                    await asyncio.gather(*w.tasks, return_exceptions=True)
            try:
                env = assemble(rt, w)
            except Exception as e:
                env = failed_envelope(rt, orgnr, idx, f"assembly error: {type(e).__name__}: {e}")
            results[orgnr] = env
            rt.durations.append(time.monotonic() - ctx.started)
            if partial:
                partial.write(json.dumps(env, ensure_ascii=False) + "\n")
                partial.flush()

    tasks = [asyncio.create_task(one(it)) for it in items]
    stopper = asyncio.create_task(stop.wait())
    # Wait until all company tasks finish, the run deadline passes, or a stop signal arrives.
    while not stop.is_set() and any(not t.done() for t in tasks) and time.monotonic() < rt.hard_deadline:
        await asyncio.wait([t for t in tasks if not t.done()] + [stopper], timeout=max(0.5, rt.hard_deadline - time.monotonic()),
                           return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        if not t.done():
            t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    stopper.cancel()
    nav_task.cancel()
    await asyncio.gather(nav_task, stopper, return_exceptions=True)

    out: list[dict] = []
    for it in items:
        orgnr = it["orgnr"]
        env = results.get(orgnr)
        if env is None and orgnr in works:
            try:
                env = assemble(rt, works[orgnr])
                env["errors"].append({"stage": "run", "kind": "time_budget", "message": "partial result: run budget ended", "at": now_iso()})
            except Exception as e:
                env = failed_envelope(rt, orgnr, it["index"], f"partial assembly error: {e}")
        if env is None:
            env = failed_envelope(rt, orgnr, it["index"], "run time budget exhausted before this company was started")
        out.append(env)
    if partial:
        partial.close()

    report = make_report(rt, out)
    await rt.aclose()
    return out, report


def make_report(rt: Runtime, envs: list[dict]) -> dict:
    from collections import Counter

    status = Counter(e["status"] for e in envs)
    fam = {f: Counter((e.get("fields") or {}).get(f, {}).get("state", "failed") for e in envs) for f in FAMILIES}
    durs = sorted(rt.durations)

    def q(p):
        if not durs:
            return None
        return round(durs[min(len(durs) - 1, int(p * len(durs)))], 2)

    claims = sum(len(e.get("claims") or []) for e in envs)
    bases = Counter((e.get("sections", {}).get("website_and_profiles", {}).get("website", {}) or {}).get("identity_basis")
                    for e in envs)
    return {
        "run_id": rt.run_id, "agent": AGENT_NAME, "agent_version": __version__,
        "started_at": rt.run_at, "finished_at": now_iso(),
        "elapsed_s": round(time.monotonic() - rt.t0, 1),
        "inputs": len(envs), "envelopes": len(envs),
        "one_envelope_per_input": len({e["organisation_number"] for e in envs}) == len(envs),
        "status_counts": dict(status),
        "family_state_counts": {f: dict(c) for f, c in fam.items()},
        "family_coverage_pct": {f: round(100 * c.get("available", 0) / max(1, len(envs)), 1) for f, c in fam.items()},
        "claims_total": claims,
        "claims_per_company": round(claims / max(1, len(envs)), 1),
        "website_identity_basis": {str(k): v for k, v in bases.items() if k},
        "requests_total": rt.fetcher.request_count,
        "bytes_downloaded": rt.fetcher.bytes_in,
        "runtime_p50_s": q(0.5), "runtime_p95_s": q(0.95),
        "runtime_mean_s": round(statistics.mean(durs), 2) if durs else None,
        "nav_feed": {"state": rt.nav.state, "pages": rt.nav.pages, "active_ads_indexed": len(rt.nav.meta),
                     "reached_feed_end": rt.nav.complete, "error": rt.nav.error},
        "wikidata_website_candidates": len(rt.wikidata_sites),
        "search_provider": rt.search.provider if rt.search else None,
        "search_queries": rt.search.queries if rt.search else 0,
        "third_party_cost_usd": 0.0 if not rt.search else None,
        "dns_precheck": rt.resolver.dns_enabled,
    }
