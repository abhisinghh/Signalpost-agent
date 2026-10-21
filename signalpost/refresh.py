"""Refresh as a diff: stable claim keys, preserved history, no false changes."""
from __future__ import annotations

import copy

from .util import value_hash

MATERIALITY = {
    "identity": "high", "financials": "high", "leadership": "high", "website": "high",
    "workplaces": "medium", "contact": "medium", "hiring": "medium",
    "social_profiles": "low", "public_activity": "low",
}

CHANGE_NAMES = {
    ("leadership", "added"): "new_role",
    ("leadership", "removed"): "ended_role",
    ("hiring", "added"): "new_job",
    ("hiring", "removed"): "closed_job",
    ("public_activity", "added"): "new_activity",
    ("workplaces", "added"): "new_workplace",
    ("workplaces", "removed"): "closed_workplace",
    ("financials", "added"): "new_financial_figure",
    ("website", "added"): "website_found",
    ("website", "removed"): "website_lost",
    ("social_profiles", "added"): "new_profile",
    ("social_profiles", "removed"): "profile_removed",
}

MAX_HISTORY = 10

# Families whose older records stay true after a source stops listing them.
APPEND_ONLY = {"financials", "public_activity"}


def _slim_evidence(evs: list[dict]) -> list[dict]:
    keep = ("snapshot_id", "source_url", "source_class", "retrieved_at", "content_sha256", "locator",
            "extraction_method")
    return [{k: e.get(k) for k in keep} for e in evs or []]


def apply_refresh(prev: dict | None, claims: list[dict], family_states: dict,
                  run_id: str, run_at: str, checked_sources: set[str] | None = None) -> tuple[dict, dict, list[dict]]:
    """Return (refresh_block, new_state, carried_forward_claims).

    * unchanged value  -> keeps first_observed_at, no change record
    * new key          -> 'added' change (only when a previous run exists)
    * different value  -> 'updated' change with both sides' evidence
    * missing key      -> 'removed' only when that family's source was checked
                          successfully this run; otherwise the last known value is
                          carried forward and marked stale.
    """
    prev_claims: dict = (prev or {}).get("claims", {})
    changes: list[dict] = []
    new_claims: dict = {}
    seen = set()

    for c in claims:
        key = c["claim_key"]
        seen.add(key)
        vh = value_hash(c["value"])
        p = prev_claims.get(key)
        rec = {
            "family": c["family"], "field": c["field"], "value": c["value"], "value_hash": vh,
            "first_observed_at": run_at, "last_observed_at": run_at, "status": "current",
            "evidence": _slim_evidence(c["evidence"]), "history": [],
        }
        if p is not None:
            rec["history"] = list(p.get("history", []))[-MAX_HISTORY:]
        if p is not None and p.get("status") == "current" and p.get("value_hash") == vh:
            rec["first_observed_at"] = p.get("first_observed_at", run_at)
            c["change"] = "unchanged"
        elif p is not None and p.get("status") == "current":
            rec["history"].append({
                "value": p.get("value"), "observed_from": p.get("first_observed_at"),
                "observed_to": p.get("last_observed_at"), "evidence": p.get("evidence", []),
            })
            rec["history"] = rec["history"][-MAX_HISTORY:]
            c["change"] = "updated"
            changes.append(_change(c["family"], c["field"], key, "updated", p.get("value"), c["value"],
                                   p.get("evidence", []), rec["evidence"], run_at))
        elif prev is not None:
            c["change"] = "added"
            changes.append(_change(c["family"], c["field"], key, "added", None, c["value"],
                                   [], rec["evidence"], run_at))
        else:
            c["change"] = "initial"
        c["first_observed_at"] = rec["first_observed_at"]
        c["last_observed_at"] = run_at
        new_claims[key] = rec

    carried: list[dict] = []
    for key, p in prev_claims.items():
        if key in seen:
            continue
        if p.get("status") != "current":
            new_claims[key] = p  # keep removed records for audit history
            continue
        fam = p.get("family")
        fstate = family_states.get(fam, {})
        if fam in APPEND_ONLY:
            # Older filed periods and older dated items do not disappear from the world when
            # a source stops listing them; keep them as history, never as a removal.
            new_claims[key] = p
            carried.append({
                "claim_key": key, "family": fam, "field": p.get("field"), "value": p.get("value"),
                "first_observed_at": p.get("first_observed_at"),
                "last_observed_at": p.get("last_observed_at"),
                "evidence": p.get("evidence", []), "historical": True, "stale_reason": None,
            })
            continue
        src = ((p.get("evidence") or [{}])[0] or {}).get("source_class")
        source_ok = checked_sources is None or src in checked_sources
        if fstate.get("source_checked") and source_ok:
            removed = copy.deepcopy(p)
            removed["status"] = "removed"
            removed["removed_at"] = run_at
            new_claims[key] = removed
            changes.append(_change(fam, p.get("field"), key, "removed", p.get("value"), None,
                                   p.get("evidence", []), [], run_at))
        else:
            # Source failed or was skipped: preserve last known value, never erase it.
            new_claims[key] = p
            carried.append({
                "claim_key": key, "family": fam, "field": p.get("field"), "value": p.get("value"),
                "first_observed_at": p.get("first_observed_at"),
                "last_observed_at": p.get("last_observed_at"),
                "evidence": p.get("evidence", []),
                "stale_reason": fstate.get("reason") or "source not re-checked this run",
            })

    runs = list((prev or {}).get("runs", []))
    if not runs or runs[-1].get("run_id") != run_id:
        runs.append({"run_id": run_id, "run_at": run_at})
    runs = runs[-20:]
    new_state = {"runs": runs, "claims": new_claims}
    block = {
        "is_first_run": prev is None,
        "previous_run_id": (prev or {}).get("runs", [{}])[-1].get("run_id") if prev and prev.get("runs") else None,
        "previous_run_at": (prev or {}).get("runs", [{}])[-1].get("run_at") if prev and prev.get("runs") else None,
        "changes": changes,
        "material_changes": [c for c in changes if c["materiality"] == "high"],
        "unchanged_claims": sum(1 for c in claims if c.get("change") == "unchanged"),
        "carried_forward": carried,
    }
    return block, new_state, carried


def _change(family, field, key, kind, before, after, ev_before, ev_after, at) -> dict:
    return {
        "claim_key": key,
        "family": family,
        "field": field,
        "change_type": CHANGE_NAMES.get((family, kind), f"value_{kind}"),
        "kind": kind,
        "materiality": MATERIALITY.get(family, "low"),
        "previous_value": before,
        "current_value": after,
        "previous_evidence": ev_before,
        "current_evidence": ev_after,
        "detected_at": at,
    }
