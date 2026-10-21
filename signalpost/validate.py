"""Self-check of an output file against the official-run requirements."""
from __future__ import annotations

import json
from collections import Counter

from .claims import FAMILIES, STATES
from .inputs import read_inputs


def _load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        txt = fh.read()
    if txt.lstrip().startswith("["):
        return json.loads(txt)
    return [json.loads(l) for l in txt.splitlines() if l.strip()]


def validate(input_path: str, env_path: str) -> tuple[bool, list[str]]:
    items, rejected = read_inputs(input_path)
    envs = _load(env_path)
    msgs, ok = [], True
    want = [i["orgnr"] for i in items]
    got = Counter(e.get("organisation_number") for e in envs if e.get("organisation_number"))
    missing = [o for o in want if got.get(o, 0) == 0]
    dup = [o for o, n in got.items() if n > 1]
    extra = [o for o in got if o not in set(want)]
    if missing:
        ok = False
        msgs.append(f"MISSING envelopes for {len(missing)} inputs, e.g. {missing[:5]}")
    if dup:
        ok = False
        msgs.append(f"DUPLICATE envelopes for {dup[:5]}")
    if extra:
        ok = False
        msgs.append(f"EXTRA envelopes not in input: {extra[:5]}")
    bad_state = 0
    no_evidence = 0
    zero_fill = 0
    for e in envs:
        if e.get("status") not in STATES:
            bad_state += 1
        for f in FAMILIES:
            st = ((e.get("fields") or {}).get(f) or {}).get("state")
            if e.get("fields") and st not in STATES:
                bad_state += 1
        for c in e.get("claims") or []:
            evs = c.get("evidence") or []
            if not evs or not all(x.get("source_url") and x.get("retrieved_at") and x.get("content_sha256") for x in evs):
                no_evidence += 1
            if c["family"] == "financials" and isinstance(c.get("value"), dict) and c["value"].get("amount") is None:
                zero_fill += 1
    if bad_state:
        ok = False
        msgs.append(f"{bad_state} invalid state values")
    if no_evidence:
        ok = False
        msgs.append(f"{no_evidence} claims without complete evidence")
    if zero_fill:
        ok = False
        msgs.append(f"{zero_fill} financial claims without an amount")
    msgs.append(f"inputs={len(want)} envelopes={len(envs)} unreadable_rows={len(rejected)} "
                f"status={dict(Counter(e.get('status') for e in envs))}")
    return ok, msgs
