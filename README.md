# Signalpost company research agent

Give it a batch of Norwegian organisation numbers. For every company it returns exactly one
terminal result envelope with source-linked facts, honest availability states, a cited summary,
and a refresh diff against the previous run. It also writes a static, mobile-friendly site to
find, compare and verify every fact.

- **Identity first.** Every fact is anchored to the organisation number. Websites are published
  only when tied back to the exact entity (organisation number on the site, declared in the
  official register, or legal name plus registered address/phone). Uncertain matches are
  reported as `ambiguous`, never published.
- **Evidence for every claim.** Source URL, retrieval time, content SHA-256, snapshot id,
  locator, excerpt and extraction method; reporting period where relevant.
- **Never zero-fill.** Missing is `not_available`; blocked is `blocked`; failures are `failed`.
- **Refresh is a diff.** Stable claim keys, preserved history, no false changes on an
  unchanged snapshot, and last-known values kept when a source fails.

## Install (clean machine, Python 3.12+)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## Run (the one command)

```bash
python -m signalpost run --input BATCH_FILE --output out/envelopes.jsonl
```

`BATCH_FILE` may be `.jsonl`, `.json`, `.csv` or `.txt` (optionally `.gz`). Organisation numbers
are read from `organisation_number`, `orgnr`, `organisasjonsnummer` and similar keys, or one
per line. Duplicates are researched once; unreadable rows still get a `failed` envelope.

Outputs:

| File | What |
| --- | --- |
| `out/envelopes.jsonl` | one envelope per input company, in input order (`.json` gives an array) |
| `out/run-report.json` | coverage per field family, status counts, p50/p95 runtime, requests, cost |
| `out/site/index.html` | profile site: search, filters, compare up to 3, verify any fact |
| `.signalpost-state/` | raw snapshots (gzip, content-addressed) + last-known claims for refresh |

Re-running the same command later refreshes the profiles and reports what changed.

### Options and environment

| Flag / variable | Default | Meaning |
| --- | --- | --- |
| `--time-budget` / `SIGNALPOST_TIME_BUDGET_S` | 3300 | Whole-run budget in seconds. Results are written before it ends; when time gets short the agent switches to registry-only mode so every company still gets an envelope. **Set this to the official budget.** |
| `--concurrency` / `SIGNALPOST_CONCURRENCY` | 24 | Companies researched in parallel |
| `--per-company` / `SIGNALPOST_PER_COMPANY_S` | 75 | Upper bound per company |
| `--state-dir` / `SIGNALPOST_STATE_DIR` | `.signalpost-state` | Snapshots and refresh state |
| `SIGNALPOST_CONTACT` | — | Contact shown in the crawler's User-Agent |
| `SIGNALPOST_NAV=0` | on | Disable the NAV job-feed connector |
| `SIGNALPOST_NAV_MAX_PAGES` | 400 | Feed pages read (newest first) when building the job index |
| `SIGNALPOST_WIKIDATA=0` | on | Disable the Wikidata website-candidate lookup |
| `SIGNALPOST_BRAVE_API_KEY` or `SIGNALPOST_SERPER_API_KEY` | off | Optional search API for website *candidates* (never evidence) |

No secret is required. Any key is read server-side from the environment only and is never
written to outputs or logs.

## Local tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q                       # offline unit tests

# 100-company smoke test from the public universe
curl -LO https://builderr.ai/signalpost-company-universe-2025.jsonl.gz
python -m signalpost sample --universe signalpost-company-universe-2025.jsonl.gz --n 100 --seed 7 --out batch-100.jsonl
python -m signalpost run --input batch-100.jsonl --output out/envelopes.jsonl --time-budget 1200
python -m signalpost validate --input batch-100.jsonl --envelopes out/envelopes.jsonl
```

## Sources

| Family | Source | How it is tied to the company |
| --- | --- | --- |
| Legal identity, industry, address, employees, status, registered contact | Enhetsregisteret API | organisation number |
| Leadership, board, auditor | Enhetsregisteret roles API | organisation number |
| Registered workplaces | Enhetsregisteret subunits (`overordnetEnhet`) | parent organisation number |
| Annual accounts (+ earlier years when served) | Regnskapsregisteret API | organisation number |
| Dated registry announcements | Brønnøysund kunngjøringer | organisation number |
| Official website | register `hjemmeside`, Wikidata (org. number → website), subunit websites, registered email domain, name-derived domains, optional search | org. number on the site, Norid RDAP registrant org. number, confirmed register/Wikidata declaration, or legal name + registered address/phone |
| Company-owned profiles, contact, news, careers, job postings | the verified website (links, JSON-LD, RSS, sitemap) | only extracted from sites attributed to the exact entity |
| Job postings | NAV Arbeidsplassen public feed | employer organisation number in the ad (company or its subunit) |

See `AGENT.md`, `DATA_SCHEMA.md` and `LIMITATIONS.md` for policies, schema and known gaps.
