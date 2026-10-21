"""Claim builder: every published fact carries snapshot-backed evidence."""
from __future__ import annotations

from .http import FetchResult
from .util import short_hash, trunc

FAMILIES = [
    "identity",          # legal identity, industry, address, status
    "financials",        # filed annual accounts
    "leadership",        # registered roles (CEO, board, auditor ...)
    "workplaces",        # registered subunits / locations
    "website",           # verified official website
    "social_profiles",   # company-owned profiles linked from the verified site
    "contact",           # registered / site contact points
    "hiring",            # job postings and careers pages
    "public_activity",   # dated announcements, news, press
]

FAMILY_LABELS = {
    "identity": "Legal identity",
    "financials": "Annual accounts",
    "leadership": "Leadership & roles",
    "workplaces": "Registered workplaces",
    "website": "Official website",
    "social_profiles": "Company profiles",
    "contact": "Contact points",
    "hiring": "Hiring",
    "public_activity": "Dated public activity",
}

STATES = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")


class Claims:
    def __init__(self, orgnr: str):
        self.orgnr = orgnr
        self.items: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}

    def register(self, fr: FetchResult | None) -> dict | None:
        if fr is None or not fr.ok or not fr.snapshot:
            return None
        snap = fr.snapshot
        self.snapshots.setdefault(snap["snapshot_id"], snap)
        return snap

    def add(self, family: str, field: str, value, *, fr: FetchResult | None, method: str,
            locator: str | None = None, excerpt: str | None = None, key: str | None = None,
            period: dict | None = None, confidence: str = "high",
            basis: str = "registry_orgnr", relationship: str = "exact",
            extra_evidence: list[FetchResult] | None = None) -> dict | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, (list, dict)) and not value:
            return None
        snap = self.register(fr)
        if snap is None:
            return None  # no snapshot, no claim
        claim_key = key or f"{family}:{field}"
        ev = self._evidence(snap, locator, excerpt, method)
        evs = [ev]
        for other in extra_evidence or []:
            s2 = self.register(other)
            if s2 is not None and s2["snapshot_id"] != snap["snapshot_id"]:
                evs.append(self._evidence(s2, None, None, method))
        existing = self.items.get(claim_key)
        if existing is not None:
            for e in evs:
                if not any(x["snapshot_id"] == e["snapshot_id"] and x["locator"] == e["locator"]
                           for x in existing["evidence"]):
                    existing["evidence"].append(e)
            return existing
        claim = {
            "claim_id": "clm_" + short_hash(f"{self.orgnr}|{claim_key}", 16),
            "claim_key": claim_key,
            "family": family,
            "field": field,
            "value": value,
            "state": "available",
            "reporting_period": period,
            "confidence": confidence,
            "identity_basis": basis,
            "entity_relationship": relationship,
            "evidence": evs,
        }
        self.items[claim_key] = claim
        return claim

    @staticmethod
    def _evidence(snap: dict, locator, excerpt, method) -> dict:
        return {
            "snapshot_id": snap["snapshot_id"],
            "source_url": snap["final_url"],
            "requested_url": snap["requested_url"],
            "source_class": snap["source_class"],
            "retrieved_at": snap["retrieved_at"],
            "content_sha256": snap["content_sha256"],
            "locator": locator,
            "excerpt": trunc(excerpt, 240),
            "extraction_method": method,
        }

    def family(self, family: str) -> list[dict]:
        return [c for c in self.items.values() if c["family"] == family]

    def all(self) -> list[dict]:
        return list(self.items.values())
