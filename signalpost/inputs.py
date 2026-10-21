"""Read the supplied company batch in whatever common format it arrives in."""
from __future__ import annotations

import csv
import gzip
import io
import json
import random
import re
from pathlib import Path

from .util import clean_orgnr

ORG_KEYS = (
    "organisation_number", "organisationNumber", "organization_number", "organizationNumber",
    "orgnr", "org_nr", "orgNr", "organisasjonsnummer", "org_number", "orgNumber",
    "company_number", "companyNumber", "company_id", "id",
)


def _open_text(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".gz" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8-sig")


def orgnr_of(rec) -> str | None:
    if isinstance(rec, (str, int)):
        return clean_orgnr(rec)
    if not isinstance(rec, dict):
        return None
    for k in ORG_KEYS:
        if k in rec and rec[k] is not None:
            v = clean_orgnr(rec[k])
            if v:
                return v
    for v in rec.values():
        if isinstance(v, (str, int)) and re.fullmatch(r"\s*\d{3}\s?\d{3}\s?\d{3}\s*", str(v)):
            return clean_orgnr(v)
    return None


def _records(path: Path) -> list:
    text = _open_text(path)
    name = path.name.lower().removesuffix(".gz")
    stripped = text.lstrip()
    if name.endswith(".json") or (stripped.startswith(("[", "{")) and not name.endswith((".jsonl", ".ndjson"))):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None  # probably JSON Lines without the extension
        if obj is not None:
            if isinstance(obj, dict):
                for k in ("companies", "items", "inputs", "batch", "organisation_numbers", "orgnrs", "data", "results"):
                    if isinstance(obj.get(k), list):
                        return obj[k]
                return [obj]
            return obj if isinstance(obj, list) else [obj]
    if name.endswith(".jsonl") or name.endswith(".ndjson") or stripped.startswith("{"):
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append(line)
        return out
    if name.endswith(".csv") or name.endswith(".tsv"):
        dialect = "excel-tab" if name.endswith(".tsv") else "excel"
        return list(csv.DictReader(io.StringIO(text), dialect=dialect))
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def read_inputs(path: str | Path) -> tuple[list[dict], list[dict]]:
    """Return (unique items in input order, rejected rows)."""
    items, rejected, seen = [], [], set()
    for i, rec in enumerate(_records(Path(path))):
        org = orgnr_of(rec)
        if not org:
            rejected.append({"row": i, "record": rec})
            continue
        if org in seen:
            continue
        seen.add(org)
        items.append({"orgnr": org, "index": len(items), "record": rec if isinstance(rec, dict) else {}})
    return items, rejected


def sample(universe: str | Path, n: int, seed: int, out: str | Path) -> int:
    recs = _records(Path(universe))
    rng = random.Random(seed)
    pick = rng.sample(recs, min(n, len(recs)))
    with open(out, "w", encoding="utf-8") as fh:
        for r in pick:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(pick)
