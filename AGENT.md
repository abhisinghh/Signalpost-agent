# Research, identity and abstention policy

## Order of work per company

1. **Identity anchor** — Enhetsregisteret entity record. If the live register is unavailable
   and the supplied batch row carries registry fields, that row is stored as a snapshot
   (`supplied_registry_snapshot`) and used instead. If neither exists, every family is
   `blocked`/`failed` and nothing else is published.
2. **Registry fan-out** (parallel) — roles, subunits, annual accounts (latest and up to two
   earlier years when served), announcements.
3. **Website resolution** — candidates in order of authority, each probed and judged:

   Candidates, in order: register `hjemmeside` → Wikidata item carrying this organisation
   number (P2333 → P856, one batched query per run) → subunit websites → registered email
   domain → name-derived `.no`/`.com` guesses → optional search API.

   | Candidate source | Accepted when | Site facts extracted? |
   | --- | --- | --- |
   | any | organisation number appears on the site (checksum-validated) | yes |
   | any `.no` domain | Norid RDAP record names this organisation number as registrant | yes |
   | register `hjemmeside` / subunit website / Wikidata | the site confirms the name, address or phone; if it shows another org. number **and** the name, it is published as `shared_or_group_site` with nothing extracted; if it shows no trace of the company → `ambiguous` (lapsed domains get re-registered) | yes, when confirmed |
   | registered email domain | site carries the legal name or brand | yes |
   | name-derived guess / search result | full legal name **and** registered address or phone; or brand **and** address **and** phone | yes |
   | anything with only a name match | never — reported as `ambiguous` | no |
   | site showing a different org. number (non-register candidate) | rejected | no |

4. **Company-owned facts** from the verified site only: brand (`og:site_name`/JSON-LD),
   self-description (labelled company copy), social profiles linked from the site, contact
   points on the company's own domain, JSON-LD addresses, careers pages and job postings
   (JSON-LD `JobPosting`, ATS and careers-page links), dated news (JSON-LD articles, dated
   links, RSS/Atom, sitemap `lastmod` labelled as a modification date).
5. **Jobs from NAV** — ads are matched by employer name only to find candidates; an ad is
   published only when its `employer.orgnr` equals the company or one of its registered
   subunits.
6. **Synthesis** — deterministic sentences built from published claims, each citing claim ids;
   signals (bankruptcy, negative equity, losses, growth, hiring); explicit unknowns.
7. **Refresh diff** against the previous run's state.

## Abstention rules

- It is better to miss a fact than to publish it under the wrong company.
- Name similarity alone never publishes anything.
- Search results are candidates only; the fact is published from the re-fetched page.
- Group, parent, franchise and shared sites are labelled, not collapsed.
- Missing values are never converted to zero.
- Personal birth dates from the roles register are never published.

## Budget behaviour

The run has a hard deadline (`--time-budget` minus a safety margin). Each company gets a fair
share of the remaining time; when that share drops below 25 s the company is researched in
registry-only mode. Any company not finished at the deadline still gets an envelope assembled
from what was collected, with unchecked families marked `failed`.
