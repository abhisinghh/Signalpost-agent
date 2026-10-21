# Envelope schema (`signalpost.envelope.v1`)

One JSON object per input company. Top-level keys:

| Key | Content |
| --- | --- |
| `organisation_number`, `input_index` | the input |
| `status`, `status_reason` | `available` \| `not_available` \| `blocked` \| `not_applicable` \| `ambiguous` \| `failed` |
| `legal_identity` | legal name, public brand, legal form, industry, address, municipality, register status, claim ids |
| `sections` | the seven required sections, each with `state`, `reason`, `source_checked`, `claim_ids`: `legal_identity_and_brand`, `annual_accounts` (+ `fiscal_years`, `latest_figures`), `leadership_and_workplaces`, `website_and_profiles`, `hiring_and_activity` |
| `fields` | per field family: `state`, `reason`, `source_checked`, `claim_count` |
| `claims` | published facts (below) |
| `historical_claims` | earlier filed periods / dated items kept from previous runs |
| `stale_claims` | last-known values kept because their source could not be re-checked this run |
| `synthesis` | `headline`, `statements[{topic,text,claim_ids}]`, `signals`, `what_changed`, `unknowns` |
| `website_resolution` | every website candidate tried, with decision and reason |
| `source_snapshots` | raw snapshot metadata: requested/final URL, redirects, status, retrieved_at, sha256, content type, source class |
| `refresh` | `is_first_run`, `previous_run_id/at`, `changes[]`, `material_changes[]`, `unchanged_claims` |
| `run` | run id, agent version, timings, request count, mode, third-party cost |
| `errors` | stage, kind, message, url, time |

Field families: `identity`, `financials`, `leadership`, `workplaces`, `website`,
`social_profiles`, `contact`, `hiring`, `public_activity`.

## Claim

```json
{
  "claim_id": "clm_…",
  "claim_key": "financials:revenue:2025:SELSKAP",
  "family": "financials",
  "field": "revenue",
  "value": {"amount": 12345000, "currency": "NOK", "fiscal_year": 2025, "account_type": "SELSKAP"},
  "state": "available",
  "reporting_period": {"from": "2025-01-01", "to": "2025-12-31", "fiscal_year": 2025, "account_type": "SELSKAP"},
  "confidence": "high",
  "identity_basis": "registry_orgnr",
  "entity_relationship": "exact",
  "change": "initial | unchanged | added | updated",
  "first_observed_at": "…", "last_observed_at": "…",
  "evidence": [{
    "snapshot_id": "snap_…", "source_url": "https://data.brreg.no/…", "requested_url": "…",
    "source_class": "official_register", "retrieved_at": "2026-10-21T08:00:00Z",
    "content_sha256": "…", "locator": "/0/resultatregnskapResultat/…/sumDriftsinntekter",
    "excerpt": "sumDriftsinntekter = 12345000 NOK (2025)", "extraction_method": "registry_api"
  }]
}
```

`claim_key` is stable across runs and drives the refresh diff. Change types include
`new_role`, `ended_role`, `new_job`, `closed_job`, `new_activity`, `new_workplace`,
`closed_workplace`, `new_financial_figure`, `website_found`, `website_lost`, `value_updated`.

Identity bases: `registry_orgnr`, `orgnr_in_source`, `orgnr_on_site`, `domain_registrant_orgnr`,
`registry_declared`, `wikidata_orgnr_link`, `subunit_registry_declared`, `registry_email_domain_and_name`,
`legal_name_and_address_or_phone`, `brand_address_and_phone`.
Entity relationships: `exact`, `shared_or_group_site`, `registry_declared_unconfirmed`.
