"""Static, mobile-friendly profile site: find, compare and verify every fact.

Output layout (works from file:// and any static host):
  site/index.html            directory, search, filters, compare
  site/data/<orgnr>.js       one profile per company, loaded on demand
"""
from __future__ import annotations

import json
from pathlib import Path

from .claims import FAMILIES, FAMILY_LABELS


def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _index_row(e: dict) -> dict:
    li = e.get("legal_identity") or {}
    fields = e.get("fields") or {}
    fin = ((e.get("sections") or {}).get("annual_accounts") or {})
    lf = fin.get("latest_figures") or {}
    rev = (lf.get("revenue") or {}).get("amount")
    muni = li.get("municipality") or {}
    ind = li.get("industry") or {}
    return {
        "o": e.get("organisation_number"),
        "n": li.get("legal_name") or e.get("organisation_number"),
        "b": li.get("public_brand"),
        "m": (muni.get("name") or "").title() if isinstance(muni, dict) else None,
        "i": ind.get("description") if isinstance(ind, dict) else None,
        "s": e.get("status"),
        "f": {f: (fields.get(f) or {}).get("state", "failed") for f in FAMILIES},
        "r": rev,
        "y": fin.get("latest_year"),
        "c": len(e.get("claims") or []),
        "ch": len((e.get("refresh") or {}).get("changes") or []),
        "h": (e.get("synthesis") or {}).get("headline"),
    }


def _profile(e: dict) -> dict:
    p = {k: e.get(k) for k in ("organisation_number", "status", "status_reason", "legal_identity", "fields", "claims",
                                "historical_claims", "stale_claims", "synthesis", "refresh", "run", "errors",
                                "website_resolution")}
    p["source_snapshots"] = [{k: s.get(k) for k in ("snapshot_id", "final_url", "retrieved_at", "content_sha256", "source_class")}
                             for s in e.get("source_snapshots") or []]
    return p


def build_site(envs: list[dict], site_dir: Path, report: dict | None = None) -> None:
    site_dir = Path(site_dir)
    (site_dir / "data").mkdir(parents=True, exist_ok=True)
    rows = []
    for e in envs:
        o = e.get("organisation_number")
        if not o:
            continue
        rows.append(_index_row(e))
        (site_dir / "data" / f"{o}.js").write_text(f"window.SP_LOAD({_js(o)},{_js(_profile(e))});\n", "utf-8")
    meta = {"families": FAMILIES, "labels": FAMILY_LABELS, "report": report or {}}
    html = TEMPLATE.replace("/*__INDEX__*/[]", _js(rows)).replace("/*__META__*/{}", _js(meta))
    (site_dir / "index.html").write_text(html, "utf-8")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signalpost profiles</title>
<style>
:root{--bg:#f7f7f4;--panel:#fff;--ink:#1c1d1f;--muted:#62666d;--line:#e3e3de;--accent:#123eaa;--accent-soft:#e8edfa;
--ok:#1d7a46;--ok-soft:#e3f3e9;--warn:#9a5b00;--warn-soft:#fbf0dc;--bad:#b3261e;--bad-soft:#fbe5e3;--na:#8a8d93;--na-soft:#efefec}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#131416;--panel:#1c1d20;--ink:#ececec;--muted:#a0a4ab;--line:#2d2f33;
--accent:#8fb0ff;--accent-soft:#1f2a44;--ok:#6fd39a;--ok-soft:#173226;--warn:#f0b65a;--warn-soft:#3a2c14;--bad:#ff8f86;--bad-soft:#3d1c1a;--na:#8a8d93;--na-soft:#26272a}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}button{font:inherit}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line)}
.bar{max-width:1180px;margin:0 auto;padding:10px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:-.01em;text-decoration:none;color:var(--ink);margin-right:6px}
#q{flex:1;min-width:180px;padding:9px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
main{max-width:1180px;margin:0 auto;padding:16px}
.muted{color:var(--muted)}.small{font-size:13px}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:4px 10px;font-size:13px;cursor:pointer;color:var(--ink)}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px}.stat b{font-size:20px;display:block}
.list{display:grid;gap:8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center}
.card h3{margin:0;font-size:16px}.card .sub{font-size:13px;color:var(--muted)}
.dots{display:flex;gap:3px;margin-top:6px}.dot{width:10px;height:10px;border-radius:3px;background:var(--na-soft);border:1px solid var(--line)}
.dot.available{background:var(--ok);border-color:var(--ok)}.dot.ambiguous{background:var(--warn);border-color:var(--warn)}.dot.blocked,.dot.failed{background:var(--bad);border-color:var(--bad)}
.right{text-align:right;font-size:13px}
.st{display:inline-block;font-size:12px;border-radius:6px;padding:1px 7px;font-weight:600}
.st.available{background:var(--ok-soft);color:var(--ok)}.st.not_available,.st.not_applicable{background:var(--na-soft);color:var(--muted)}
.st.ambiguous{background:var(--warn-soft);color:var(--warn)}.st.blocked,.st.failed{background:var(--bad-soft);color:var(--bad)}
.profile-head{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:12px}
.profile-head h1{margin:0 0 4px;font-size:24px;letter-spacing:-.01em}
.grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:12px}
section.box{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:12px}
section.box h2{font-size:15px;margin:0 0 10px;display:flex;justify-content:space-between;gap:8px;align-items:center}
.stmt{margin:0 0 8px}.cite{font-size:11px;vertical-align:super;margin-left:2px;text-decoration:none;border:1px solid var(--line);border-radius:5px;padding:0 4px;background:var(--accent-soft)}
.sig{display:inline-block;border-radius:8px;padding:2px 8px;font-size:12.5px;margin:2px 4px 2px 0}
.sig.risk{background:var(--bad-soft);color:var(--bad)}.sig.growth,.sig.hiring{background:var(--ok-soft);color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12.5px}td.num{text-align:right;font-variant-numeric:tabular-nums}
.claim{padding:8px 0;border-bottom:1px solid var(--line)}.claim:last-child{border-bottom:0}
.claim.hl{background:var(--accent-soft);border-radius:8px;padding:8px}
.src{font-size:12.5px;color:var(--muted)}.src a{word-break:break-all}
details.ev{margin-top:4px}details.ev summary{cursor:pointer;font-size:12.5px;color:var(--accent)}
.evbox{font-size:12.5px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:4px;overflow-wrap:anywhere}
.unk li{margin-bottom:4px}
.tl{list-style:none;padding:0;margin:0}.tl li{padding:6px 0;border-bottom:1px solid var(--line)}.tl time{font-variant-numeric:tabular-nums;color:var(--muted);margin-right:8px;font-size:13px}
.cmpbar{position:sticky;bottom:0;background:var(--panel);border-top:1px solid var(--line);padding:10px 16px;display:none;gap:10px;align-items:center;justify-content:center}
.cmpbar.show{display:flex}.btn{border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:10px;padding:7px 14px;cursor:pointer}
.btn.ghost{background:transparent;color:var(--accent)}
.cmp{overflow-x:auto}.cmp table{min-width:560px}
.back{display:inline-block;margin-bottom:10px}
@media (max-width:820px){.grid{grid-template-columns:1fr}.card{grid-template-columns:auto 1fr}.card .right{grid-column:2;text-align:left}}
</style>
</head>
<body>
<header><div class="bar">
<a class="brand" href="#/">Signalpost</a>
<input id="q" type="search" placeholder="Search name, brand, org. number, municipality…" aria-label="Search companies" autocomplete="off">
<select id="sort" aria-label="Sort"><option value="data">Most verified data</option><option value="name">Name A–Z</option><option value="rev">Revenue</option><option value="chg">Most changes</option></select>
</div>
<div class="bar chips" id="filters" role="group" aria-label="Filter by available information"></div>
</header>
<main id="app" aria-live="polite"></main>
<div class="cmpbar" id="cmpbar"><span id="cmpcount"></span><a class="btn" id="cmpgo" href="#/compare">Compare</a><button class="btn ghost" id="cmpclear">Clear</button></div>
<script>
const INDEX=/*__INDEX__*/[];
const META=/*__META__*/{};
const FAM=META.families, LAB=META.labels;
const PROFILES={}; const WAITERS={};
window.SP_LOAD=(o,p)=>{PROFILES[o]=p;(WAITERS[o]||[]).forEach(f=>f(p));delete WAITERS[o];};
function load(o){return new Promise((res,rej)=>{if(PROFILES[o])return res(PROFILES[o]);(WAITERS[o]=WAITERS[o]||[]).push(res);
 const s=document.createElement('script');s.src='data/'+o+'.js';s.onerror=()=>rej(new Error('missing profile '+o));document.head.appendChild(s);});}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl=u=>{try{const x=new URL(u);return (x.protocol==='https:'||x.protocol==='http:')?x.href:null}catch(e){return null}};
const host=u=>{try{return new URL(u).hostname.replace(/^www\./,'')}catch(e){return u}};
const nf=new Intl.NumberFormat('nb-NO');
function money(v,c){if(v==null)return '—';const a=Math.abs(v);let s;if(a>=1e9)s=(v/1e9).toFixed(1)+' bn';else if(a>=1e6)s=(v/1e6).toFixed(1)+' m';else s=nf.format(Math.round(v));return (c||'NOK')+' '+s;}
const stateChip=s=>`<span class="st ${esc(s)}">${esc((s||'').replace('_',' '))}</span>`;
let filters=new Set(); let cmp=new Set(JSON.parse((()=>{try{return localStorage.getItem('sp_cmp')||'[]'}catch(e){return '[]'}})()));
function saveCmp(){try{localStorage.setItem('sp_cmp',JSON.stringify([...cmp]))}catch(e){};const b=document.getElementById('cmpbar');b.classList.toggle('show',cmp.size>0);document.getElementById('cmpcount').textContent=cmp.size+' selected (max 3)';}

function renderFilters(){const el=document.getElementById('filters');
 el.innerHTML='<span class="small muted">Has:</span>'+FAM.map(f=>`<button class="chip ${filters.has(f)?'on':''}" data-f="${f}" aria-pressed="${filters.has(f)}">${esc(LAB[f])}</button>`).join('');
 el.querySelectorAll('button').forEach(b=>b.onclick=()=>{const f=b.dataset.f;filters.has(f)?filters.delete(f):filters.add(f);renderFilters();route();});}

function listView(){const q=document.getElementById('q').value.trim().toLowerCase();const sort=document.getElementById('sort').value;
 let rows=INDEX.filter(r=>[...filters].every(f=>r.f[f]==='available'));
 if(q){const qd=q.replace(/\s/g,'');rows=rows.filter(r=>(r.n||'').toLowerCase().includes(q)||(r.b||'').toLowerCase().includes(q)||(r.o||'').includes(qd)||(r.m||'').toLowerCase().includes(q)||(r.i||'').toLowerCase().includes(q));}
 const score=r=>FAM.filter(f=>r.f[f]==='available').length*1000+r.c;
 rows.sort(sort==='name'?(a,b)=>a.n.localeCompare(b.n,'nb'):sort==='rev'?(a,b)=>(b.r??-1e18)-(a.r??-1e18):sort==='chg'?(a,b)=>b.ch-a.ch:(a,b)=>score(b)-score(a));
 const rep=META.report||{};const cov=rep.family_coverage_pct||{};
 const all5=INDEX.filter(r=>['website','leadership','financials','hiring','public_activity'].every(f=>r.f[f]==='available')).length;
 let h=`<div class="stats"><div class="stat"><b>${INDEX.length}</b><span class="small muted">companies</span></div>
 <div class="stat"><b>${INDEX.filter(r=>r.f.website==='available').length}</b><span class="small muted">verified websites</span></div>
 <div class="stat"><b>${INDEX.filter(r=>r.f.hiring==='available').length}</b><span class="small muted">with hiring data</span></div>
 <div class="stat"><b>${all5}</b><span class="small muted">with web, leaders, accounts, jobs & activity</span></div>
 <div class="stat"><b>${INDEX.reduce((a,r)=>a+r.c,0)}</b><span class="small muted">source-linked facts</span></div></div>
 <p class="small muted">${rows.length} shown. Every fact links to its source and retrieval time. Grey squares = not found, amber = uncertain match (not published), red = source blocked or failed.</p><div class="list">`;
 for(const r of rows.slice(0,400)){h+=`<div class="card"><input type="checkbox" aria-label="Select ${esc(r.n)} to compare" data-o="${r.o}" ${cmp.has(r.o)?'checked':''}>
 <div><h3><a href="#/c/${r.o}">${esc(r.n)}</a>${r.b?` <span class="muted small">· ${esc(r.b)}</span>`:''}</h3>
 <div class="sub">${esc(r.o)}${r.m?' · '+esc(r.m):''}${r.i?' · '+esc(r.i):''} ${r.s!=='available'?stateChip(r.s):''}</div>
 <div class="dots" title="${FAM.map(f=>LAB[f]+': '+r.f[f]).join('\n')}">${FAM.map(f=>`<span class="dot ${r.f[f]}" aria-label="${esc(LAB[f])}: ${r.f[f]}"></span>`).join('')}</div></div>
 <div class="right">${r.r!=null?`<div>${money(r.r)}</div><div class="muted">revenue ${r.y||''}</div>`:'<span class="muted">no revenue filed</span>'}${r.ch?`<div class="small">${r.ch} change${r.ch>1?'s':''}</div>`:''}</div></div>`;}
 if(rows.length>400)h+=`<p class="muted small">Showing first 400 — refine the search.</p>`;
 h+='</div>';document.getElementById('app').innerHTML=h;
 document.querySelectorAll('.card input[type=checkbox]').forEach(cb=>cb.onchange=()=>{if(cb.checked){if(cmp.size>=3){cb.checked=false;return}cmp.add(cb.dataset.o)}else cmp.delete(cb.dataset.o);saveCmp();});}

function srcLine(c){const e=(c.evidence||[])[0]||{};const u=safeUrl(e.source_url);
 return `<div class="src">Source: ${u?`<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(host(u))}</a>`:esc(e.source_url||'')} · retrieved ${esc((e.retrieved_at||'').slice(0,10))}${c.reporting_period&&(c.reporting_period.fiscal_year||c.reporting_period.as_of||c.reporting_period.date)?' · period '+esc(c.reporting_period.fiscal_year||c.reporting_period.as_of||c.reporting_period.date):''}${c.change&&c.change!=='unchanged'&&c.change!=='initial'?` · <b>${esc(c.change)}</b>`:''}${c.confidence&&c.confidence!=='high'?` · confidence ${esc(c.confidence)}`:''}</div>
 <details class="ev"><summary>Verify</summary><div class="evbox">${(c.evidence||[]).map(e=>`<div><b>${esc(e.extraction_method)}</b> · ${esc(e.source_class)}<br>URL: ${safeUrl(e.source_url)?`<a href="${esc(e.source_url)}" target="_blank" rel="noopener noreferrer">${esc(e.source_url)}</a>`:esc(e.source_url)}<br>Locator: <span class="mono">${esc(e.locator||'—')}</span><br>${e.excerpt?`Excerpt: “${esc(e.excerpt)}”<br>`:''}Retrieved ${esc(e.retrieved_at)} · sha256 <span class="mono">${esc((e.content_sha256||'').slice(0,16))}…</span> · ${esc(e.snapshot_id)}</div>`).join('<hr>')}
 <div>Identity basis: <b>${esc(c.identity_basis)}</b>${c.entity_relationship&&c.entity_relationship!=='exact'?` · relationship: <b>${esc(c.entity_relationship)}</b>`:''} · first seen ${esc((c.first_observed_at||'').slice(0,10))}</div></div></details>`;}

function val(c){const v=c.value;if(v==null)return '';if(typeof v!=='object')return esc(v);
 if(c.family==='financials')return v.unit==='%'?esc(v.amount)+' %':money(v.amount,v.currency);
 if(v.url&&v.title)return `<a href="${esc(safeUrl(v.url)||'#')}" target="_blank" rel="noopener noreferrer">${esc(v.title)}</a>${v.date?` <span class="muted small">${esc(v.date)}</span>`:''}${v.date_posted?` <span class="muted small">posted ${esc(v.date_posted)}</span>`:''}${v.location?` <span class="muted small">· ${esc(v.location)}</span>`:''}`;
 if(v.platform)return `<a href="${esc(safeUrl(v.url)||'#')}" target="_blank" rel="noopener noreferrer">${esc(v.platform)} · ${esc(v.handle)}</a>`;
 if(v.url)return `<a href="${esc(safeUrl(v.url)||'#')}" target="_blank" rel="noopener noreferrer">${esc(v.url)}</a>`;
 if(v.role||v.role_en)return `<b>${esc(v.name)}</b> — ${esc(v.role_en||v.role)}${v.holder_orgnr?` <span class="muted small">(${esc(v.holder_orgnr)})</span>`:''}`;
 if(v.orgnr&&v.name)return `<b>${esc(v.name)}</b> <span class="muted small">${esc(v.orgnr)}</span><br>${esc(v.address||'')}${v.employees!=null?` · ${esc(v.employees)} employees`:''}`;
 if(v.description)return esc(v.description)+(v.code?` <span class="muted small">(${esc(v.code)})</span>`:'');
 if(v.name&&v.number)return esc(v.name);
 return Object.entries(v).filter(([k,x])=>x!==null&&x!==''&&typeof x!=='object').map(([k,x])=>`${esc(k.replace(/_/g,' '))}: ${esc(x)}`).join(' · ');}

const FIELD_LABEL={legal_name:'Legal name',public_brand:'Public brand',legal_form:'Legal form',industry:'Industry',institutional_sector:'Sector',business_address:'Business address',postal_address:'Postal address',municipality:'Municipality',registered_date:'Registered',founded_date:'Founded',registered_employees:'Registered employees',statutory_purpose:'Statutory purpose',registered_activity:'Registered activity',latest_filed_accounts_year:'Latest filed accounts',registry_status:'Register status',parent_unit_orgnr:'Parent unit',self_description:'Self-description (company copy)',registered_email:'Registered email',registered_phone:'Registered phone',registered_mobile:'Registered mobile',site_phone:'Phone (website)',site_email:'Email (website)',official_website:'Official website',careers_page:'Careers page',job_posting:'Job posting',registry_announcement:'Register announcement',company_news:'Company news',subunit:'Registered workplace',site_location:'Location (website)',role:'Role',service_provider:'Service provider'};
function claimList(cs){if(!cs.length)return '';return cs.map(c=>`<div class="claim" id="${c.claim_id}"><div><span class="muted small">${esc(FIELD_LABEL[c.field]||c.field)}</span><br>${val(c)}</div>${srcLine(c)}</div>`).join('');}

function finTable(p){const all=(p.claims||[]).concat(p.historical_claims||[]).filter(c=>c.family==='financials'&&c.value&&c.value.fiscal_year);
 if(!all.length)return '';const years=[...new Set(all.map(c=>c.value.fiscal_year))].sort((a,b)=>b-a).slice(0,4);
 const rows=['revenue','operating_result','net_income','total_assets','equity','total_debt','equity_ratio_pct'];
 const L={revenue:'Revenue',operating_result:'Operating result',net_income:'Net income',total_assets:'Total assets',equity:'Equity',total_debt:'Total debt',equity_ratio_pct:'Equity ratio'};
 let h='<div class="cmp"><table><thead><tr><th></th>'+years.map(y=>`<th class="num">${y}</th>`).join('')+'</tr></thead><tbody>';
 for(const r of rows){const cells=years.map(y=>{const c=all.find(c=>c.field===r&&c.value.fiscal_year===y&&(c.value.account_type||'SELSKAP')==='SELSKAP')||all.find(c=>c.field===r&&c.value.fiscal_year===y);
  if(!c)return '<td class="num muted">—</td>';const e=(c.evidence||[])[0]||{};const u=safeUrl(e.source_url);
  return `<td class="num">${c.value.unit==='%'?esc(c.value.amount)+' %':money(c.value.amount,'')}${u?` <a class="cite" href="${esc(u)}" target="_blank" rel="noopener noreferrer" title="${esc(e.excerpt||'')}">src</a>`:''}</td>`}).join('');
  if(cells.includes('class="num">'))h+=`<tr><td>${L[r]}</td>${cells}</tr>`;}
 return h+'</tbody></table></div><p class="small muted">Amounts in NOK as filed in Regnskapsregisteret. “—” means not filed or not available, never zero.</p>';}

function sec(title,state,body,id){return `<section class="box" id="${id||''}"><h2><span>${esc(title)}</span>${stateChip(state)}</h2>${body||`<p class="muted small">${esc((title))}: nothing published.</p>`}</section>`;}

async function profileView(o){const app=document.getElementById('app');app.innerHTML='<p class="muted">Loading…</p>';
 let p;try{p=await load(o)}catch(e){app.innerHTML=`<p>Profile ${esc(o)} not found.</p>`;return}
 const li=p.legal_identity||{},F=p.fields||{},S=p.synthesis||{};const cs=f=>(p.claims||[]).filter(c=>c.family===f);
 const site=cs('website')[0];const st=li.registry_status||{};
 let h=`<a class="back" href="#/">← All companies</a><div class="profile-head"><h1>${esc(li.legal_name||o)}</h1>
 <div class="muted">${li.public_brand?esc(li.public_brand)+' · ':''}Org. no. <span class="mono">${esc(o)}</span>${li.legal_form?' · '+esc(li.legal_form.description||li.legal_form.code):''}${li.municipality?' · '+esc((li.municipality.name||'').toString()):''}</div>
 <div style="margin-top:8px">${stateChip(p.status)} ${st.bankrupt?'<span class="sig risk">Bankrupt</span>':''}${st.under_liquidation?'<span class="sig risk">Under liquidation</span>':''}
 ${site?` <a href="${esc(safeUrl(site.value.url)||'#')}" target="_blank" rel="noopener noreferrer">${esc(host(site.value.url))}</a>`:''}
 <label class="small" style="margin-left:8px"><input type="checkbox" id="cmpme" ${cmp.has(o)?'checked':''}> compare</label></div>
 <div class="dots" style="margin-top:8px">${FAM.map(f=>`<span class="dot ${(F[f]||{}).state}" title="${esc(LAB[f])}: ${esc((F[f]||{}).state)}"></span>`).join('')}</div></div>
 <div class="grid"><div>`;
 const cite=ids=>ids.map((id,i)=>`<a class="cite" href="#/c/${o}/${id}" data-claim="${id}">${i+1}</a>`).join('');
 const SEC=S.sections||{what_it_does:S.statements||[],what_changed:[],what_remains_unknown:[]};
 const para=list=>list.map(s=>`<p class="stmt">${esc(s.text)}${cite(s.claim_ids||[])}</p>`).join('');
 h+=`<section class="box"><h2><span>Summary</span><span class="small muted">every sentence cites its facts</span></h2>
 ${(S.signals||[]).map(s=>`<span class="sig ${esc(s.type)}">${esc(s.text)}</span>`).join('')}
 <h3 class="small muted" style="margin:10px 0 4px">What the company does</h3>${para(SEC.what_it_does||[])||'<p class="muted">No verified facts to summarise.</p>'}
 <h3 class="small muted" style="margin:10px 0 4px">What changed</h3>${para(SEC.what_changed||[])||'<p class="muted small">Nothing to report.</p>'}
 <h3 class="small muted" style="margin:10px 0 4px">What remains unknown</h3>${para(SEC.what_remains_unknown||[])||'<p class="muted small">Nothing missing.</p>'}</section>`;
 h+=sec('Annual accounts',(F.financials||{}).state,finTable(p),'fin');
 h+=sec('Leadership & roles',(F.leadership||{}).state,claimList(cs('leadership')),'lead');
 h+=sec('Hiring',(F.hiring||{}).state,claimList(cs('hiring')),'hire');
 const acts=cs('public_activity').concat((p.historical_claims||[]).filter(c=>c.family==='public_activity')).filter(c=>c.value&&c.value.date).sort((a,b)=>b.value.date.localeCompare(a.value.date));
 h+=sec('Dated public activity',(F.public_activity||{}).state,acts.length?`<ul class="tl">${acts.slice(0,25).map(c=>`<li id="${c.claim_id}"><time>${esc(c.value.date)}</time>${val(c)}${c.evidence?srcLine(c):''}</li>`).join('')}</ul>`:'','act');
 h+=sec('Registered workplaces',(F.workplaces||{}).state,claimList(cs('workplaces').slice(0,40)),'work');
 h+=`</div><div>`;
 h+=sec('Legal identity',(F.identity||{}).state,claimList(cs('identity')),'id');
 h+=sec('Website & company profiles',(F.website||{}).state,claimList(cs('website').concat(cs('social_profiles'))),'web');
 h+=sec('Contact points',(F.contact||{}).state,claimList(cs('contact')),'contact');
 const ch=(p.refresh||{}).changes||[];
 h+=`<section class="box"><h2><span>What changed</span></h2>${(p.refresh||{}).is_first_run?'<p class="small muted">First observation of this company; changes appear from the next refresh.</p>':
  ch.length?`<ul class="tl">${ch.map(c=>`<li><b>${esc(c.change_type.replace(/_/g,' '))}</b> <span class="muted small">${esc(c.materiality)}</span><br><span class="small">${c.previous_value!=null?'was: '+esc(JSON.stringify(c.previous_value)).slice(0,160)+'<br>':''}${c.current_value!=null?'now: '+esc(JSON.stringify(c.current_value)).slice(0,160):''}</span></li>`).join('')}</ul>`:'<p class="small muted">No changes since the previous run.</p>'}
  ${(p.stale_claims||[]).length?`<p class="small">${p.stale_claims.length} earlier fact(s) kept from the last successful check because a source could not be re-checked.</p>`:''}</section>`;
 h+=`<section class="box unk"><h2><span>What we could not find</span></h2><ul>${(S.unknowns||[]).map(u=>`<li><b>${esc(u.label||u.family)}</b> ${stateChip(u.state)}<br><span class="small muted">${esc(u.reason||'')}</span></li>`).join('')||'<li class="muted">Nothing missing.</li>'}</ul></section>`;
 const wr=p.website_resolution;if(wr&&wr.candidates_tried&&wr.candidates_tried.length)h+=`<section class="box"><h2><span>How the website was matched</span></h2><ul class="small">${wr.candidates_tried.map(t=>`<li><span class="mono">${esc(host(t.final_url||t.url))}</span> — ${esc(t.decision)} (${esc(t.source)}): ${esc(t.reason)}</li>`).join('')}</ul></section>`;
 h+=`<p class="small muted">Run ${esc((p.run||{}).run_id)} · ${esc((p.run||{}).finished_at)} · ${(p.source_snapshots||[]).length} source snapshots</p></div></div>`;
 app.innerHTML=h;
 document.getElementById('cmpme').onchange=e=>{if(e.target.checked){if(cmp.size>=3){e.target.checked=false;return}cmp.add(o)}else cmp.delete(o);saveCmp();};
 app.querySelectorAll('a.cite[data-claim]').forEach(a=>a.onclick=ev=>{ev.preventDefault();highlight(a.dataset.claim)});
 const sub=location.hash.split('/')[3];if(sub)highlight(sub);else window.scrollTo(0,0);}

function highlight(id){document.querySelectorAll('.hl').forEach(x=>x.classList.remove('hl'));const el=document.getElementById(id);
 if(el){el.classList.add('hl');const d=el.querySelector('details');if(d)d.open=true;el.scrollIntoView({behavior:'smooth',block:'center'});}
 else{alert('That fact is shown in the financial table or timeline above.');}}

async function compareView(){const app=document.getElementById('app');const ids=[...cmp];
 if(!ids.length){app.innerHTML='<p>Select up to three companies with the checkboxes to compare them.</p><a href="#/">← Back</a>';return}
 const ps=await Promise.all(ids.map(o=>load(o).catch(()=>null)));
 const latest=(p,m)=>{const all=(p.claims||[]).concat(p.historical_claims||[]).filter(c=>c.family==='financials'&&c.field===m&&(c.value.account_type||'SELSKAP')==='SELSKAP');all.sort((a,b)=>b.value.fiscal_year-a.value.fiscal_year);return all[0]};
 const cell=c=>{if(!c)return '<td class="muted">—</td>';const e=(c.evidence||[])[0]||{};const u=safeUrl(e.source_url);return `<td>${val(c)}${c.value&&c.value.fiscal_year?` <span class="muted small">${c.value.fiscal_year}</span>`:''}${u?` <a class="cite" href="${esc(u)}" target="_blank" rel="noopener noreferrer">src</a>`:''}</td>`};
 const one=(p,k)=>(p.claims||[]).find(c=>c.claim_key===k);
 const rows=[['Legal form',p=>cell(one(p,'identity:legal_form'))],['Industry',p=>cell(one(p,'identity:industry:1'))],['Municipality',p=>cell(one(p,'identity:municipality'))],
 ['Employees',p=>cell(one(p,'identity:registered_employees'))],['Revenue',p=>cell(latest(p,'revenue'))],['Operating result',p=>cell(latest(p,'operating_result'))],
 ['Net income',p=>cell(latest(p,'net_income'))],['Equity ratio',p=>cell(latest(p,'equity_ratio_pct'))],
 ['CEO',p=>cell((p.claims||[]).find(c=>c.family==='leadership'&&c.value.role_code==='DAGL'))],['Chair',p=>cell((p.claims||[]).find(c=>c.family==='leadership'&&c.value.role_code==='LEDE'))],
 ['Website',p=>cell(one(p,'website:official_website'))],['Open positions',p=>`<td>${(p.claims||[]).filter(c=>c.field==='job_posting').length||'<span class="muted">none found</span>'}</td>`],
 ['Workplaces',p=>`<td>${(p.claims||[]).filter(c=>c.field==='subunit').length||'<span class="muted">none</span>'}</td>`],
 ['Latest activity',p=>{const a=(p.claims||[]).filter(c=>c.family==='public_activity').sort((a,b)=>b.value.date.localeCompare(a.value.date))[0];return cell(a)}],
 ['Coverage',p=>`<td><div class="dots">${FAM.map(f=>`<span class="dot ${(p.fields[f]||{}).state}" title="${esc(LAB[f])}"></span>`).join('')}</div></td>`]];
 let h=`<a class="back" href="#/">← All companies</a><section class="box cmp"><h2><span>Compare</span></h2><table><thead><tr><th></th>${ps.map((p,i)=>p?`<th><a href="#/c/${ids[i]}">${esc(p.legal_identity.legal_name)}</a><br><span class="mono muted">${ids[i]}</span></th>`:'<th>missing</th>').join('')}</tr></thead><tbody>`;
 for(const [l,f] of rows)h+=`<tr><td><b>${l}</b></td>${ps.map(p=>p?f(p):'<td></td>').join('')}</tr>`;
 app.innerHTML=h+'</tbody></table></section>';}

function route(){const h=location.hash||'#/';const parts=h.slice(2).split('/');
 if(parts[0]==='c'&&parts[1])profileView(parts[1]);else if(parts[0]==='compare')compareView();else listView();}
document.getElementById('q').addEventListener('input',()=>{if(location.hash.startsWith('#/c')||location.hash.startsWith('#/compare'))location.hash='#/';else listView();});
document.getElementById('sort').onchange=()=>listView();
document.getElementById('cmpclear').onclick=()=>{cmp.clear();saveCmp();route();};
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();document.getElementById('q').focus();}});
window.addEventListener('hashchange',route);renderFilters();saveCmp();route();
</script>
</body>
</html>
"""
