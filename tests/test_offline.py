"""Offline tests: no network. Run with `python -m pytest -q`."""
import json
from pathlib import Path

from signalpost.brreg import financial_claims, find_numeric, parse_kunngjoringer, role_claims
from signalpost.claims import Claims
from signalpost.http import FetchResult, safe_host
from signalpost.identity import WebsiteResolver, normalize_site_url, registrable
from signalpost.inputs import read_inputs
from signalpost.refresh import apply_refresh
from signalpost.store import SnapshotStore
from signalpost.util import find_orgnrs, parse_date, slug_variants, strip_legal_form, valid_orgnr
from signalpost.web import canon_social


def fr_json(store, url, obj):
    body = json.dumps(obj).encode()
    fr = FetchResult(url=url, ok=True, status=200, final_url=url, content=body, content_type="application/json")
    fr.snapshot = store.put(content=body, requested_url=url, final_url=url, status=200,
                            content_type="application/json", redirects=[], truncated=False, source_class="official_register")
    return fr


def test_orgnr_checksum():
    assert valid_orgnr("923609016")  # Equinor ASA
    assert valid_orgnr("923 609 016")
    assert not valid_orgnr("923609017")
    assert not valid_orgnr("12345678")


def test_find_orgnrs_context():
    hits = find_orgnrs("Kontakt oss · Org.nr: 923 609 016 MVA · tlf 22 33 44 55")
    assert hits and hits[0]["orgnr"] == "923609016" and hits[0]["context"]


def test_names_and_slugs():
    assert strip_legal_form("Bergen Rør & Varme AS") == "Bergen Rør & Varme"
    s = slug_variants("Bergen Rør & Varme")
    assert "bergenrorogvarme" in s and "bergen-ror-og-varme" in s


def test_dates():
    assert str(parse_date("Publisert 12. mars 2024")) == "2024-03-12"
    assert str(parse_date("2023-11-05T10:00")) == "2023-11-05"
    assert str(parse_date("05.11.2023")) == "2023-11-05"


def test_urls_and_hosts():
    assert normalize_site_url("www.Firma.no/") == "https://www.firma.no"
    assert registrable("www.shop.firma.no") == "firma.no"
    assert not safe_host("127.0.0.1") and not safe_host("localhost") and safe_host("firma.no")


def test_social_canon():
    assert canon_social("https://no.linkedin.com/company/acme-as/?trk=x")[1] == "https://www.linkedin.com/company/acme-as"
    assert canon_social("https://www.facebook.com/sharer.php?u=x") is None
    assert canon_social("https://twitter.com/intent/tweet") is None


def test_financials_never_zero_fill(tmp_path):
    store = SnapshotStore(tmp_path)
    data = [{"regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "regnskapstype": "SELSKAP",
             "valuta": "NOK",
             "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 1000},
                                                             "driftsresultat": -50}, "aarsresultat": -40},
             "eiendeler": {"sumEiendeler": 2000}, "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 500}}}]
    cl = Claims("923609016")
    years = financial_claims(cl, fr_json(store, "https://x.test/r", data), data)
    assert years == {2025}
    keys = set(cl.items)
    assert "financials:revenue:2025:SELSKAP" in keys
    assert "financials:operating_result:2025:SELSKAP" in keys
    assert cl.items["financials:operating_result:2025:SELSKAP"]["value"]["amount"] == -50
    assert "financials:total_debt:2025:SELSKAP" not in keys  # absent stays absent
    assert cl.items["financials:equity_ratio_pct:2025:SELSKAP"]["value"]["amount"] == 25.0
    assert find_numeric({"a": {"b": 3}}, "b")[0] == 3


def test_roles(tmp_path):
    store = SnapshotStore(tmp_path)
    data = {"rollegrupper": [{"sistEndret": "2025-01-01", "roller": [
        {"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "person": {"navn": {"fornavn": "Kari", "etternavn": "Nordmann"}, "fodselsdato": "1980-01-01"}},
        {"type": {"kode": "MEDL", "beskrivelse": "Styremedlem"}, "person": {"navn": {"fornavn": "Ola", "etternavn": "N"}}, "fratraadt": True},
    ]}]}
    cl = Claims("923609016")
    assert role_claims(cl, fr_json(store, "https://x.test/roller", data), data) == 1
    c = next(iter(cl.items.values()))
    assert c["value"]["name"] == "Kari Nordmann" and "fodselsdato" not in json.dumps(c)


def test_refresh_idempotent_and_removal(tmp_path):
    store = SnapshotStore(tmp_path)
    fr = fr_json(store, "https://x.test/a", {"a": 1})
    cl = Claims("923609016")
    cl.add("leadership", "role", {"name": "Kari"}, fr=fr, method="t", key="leadership:role:DAGL:kari")
    fam = {"leadership": {"source_checked": True}}
    b1, s1, _ = apply_refresh(None, cl.all(), fam, "r1", "2026-01-01T00:00:00Z")
    assert b1["is_first_run"] and not b1["changes"]
    b2, s2, _ = apply_refresh(s1, cl.all(), fam, "r2", "2026-01-02T00:00:00Z")
    assert b2["changes"] == []  # same snapshot -> no false change
    assert s2["claims"]["leadership:role:DAGL:kari"]["first_observed_at"] == "2026-01-01T00:00:00Z"
    b3, s3, carried = apply_refresh(s2, [], {"leadership": {"source_checked": False}}, "r3", "2026-01-03T00:00:00Z")
    assert not b3["changes"] and carried  # failed source: preserve last known
    b4, s4, _ = apply_refresh(s2, [], fam, "r4", "2026-01-04T00:00:00Z")
    assert b4["changes"][0]["change_type"] == "ended_role"


def test_kunngjoringer_parse():
    html = """<table><tr><td>12.03.2025</td><td><a href="hent_en.jsp?kid=1">Endring av styre</a></td></tr>
              <tr><td>01.02.2024</td><td><a href="hent_en.jsp?kid=2">Nyregistrering</a></td></tr></table>"""
    fr = FetchResult(url="https://w2.brreg.no/kunngjoring/hent_nr.jsp", ok=True, status=200,
                     final_url="https://w2.brreg.no/kunngjoring/hent_nr.jsp", content=html.encode(), content_type="text/html")
    items = parse_kunngjoringer(fr)
    assert items[0]["date"] == "2025-03-12" and items[0]["url"].endswith("kid=1")


def test_inputs_formats(tmp_path):
    p = tmp_path / "b.jsonl"
    p.write_text('{"organisasjonsnummer": "923609016", "navn": "X"}\n{"orgnr": "923609016"}\n{"x": 1}\n')
    items, rej = read_inputs(p)
    assert [i["orgnr"] for i in items] == ["923609016"] and len(rej) == 1
    p2 = tmp_path / "b.txt"
    p2.write_text("923609016\n914778271\n")
    assert len(read_inputs(p2)[0]) == 2


def test_wikidata_candidates_rank_after_register():
    r = WebsiteResolver(fetcher=None)
    cands = r.candidates({"hjemmeside": "acme.no"}, [], "Acme AS", guesses=False, wikidata_urls=["https://acme.com"])
    assert [c.source for c in cands] == ["registry_website", "wikidata_orgnr"]


def test_candidates_skip_free_mail():
    r = WebsiteResolver(fetcher=None)
    cands = r.candidates({"hjemmeside": "www.acme.no", "epostadresse": "kari@gmail.com"}, [], "Acme Rør AS", guesses=True)
    assert cands[0].url == "https://www.acme.no" and cands[0].source == "registry_website"
    assert all("gmail" not in c.url for c in cands)
