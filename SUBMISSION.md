# Submission checklist

Do these on your own machine before emailing submit@builderr.ai.

1. **Match the official output contract.** Download the starter kit and compare its envelope
   format with `DATA_SCHEMA.md`:
   ```bash
   curl -LO https://builderr.ai/signalpost-starter-kit.tar.gz && tar -xzf signalpost-starter-kit.tar.gz
   ```
   Check: the input file format the evaluator passes, the expected output path/filename,
   and the required envelope key names. If they differ, adapt `signalpost/cli.py`
   (`_write_envelopes`) and `pipeline.assemble` — the data is already all there.
2. **Check the NAV feed works:** after a run, `out/run-report.json` → `nav_feed.state` should
   be `ready` with `active_ads_indexed` in the thousands.
3. **Clean-machine test** (matches Builderr's check):
   ```bash
   git clone <repo> /tmp/sp && cd /tmp/sp && git checkout <commit>
   python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
   python -m signalpost sample --universe signalpost-company-universe-2025.jsonl.gz --n 100 --out batch-100.jsonl
   python -m signalpost run --input batch-100.jsonl --output out/envelopes.jsonl --time-budget 1200
   python -m signalpost validate --input batch-100.jsonl --envelopes out/envelopes.jsonl
   ```
4. **Spot-check precision:** open `out/site/index.html`, pick 10 companies with a website and
   confirm each site really belongs to that organisation number.
5. Commit `out/run-report.json`, `out/envelopes.jsonl` (or a link) as the smoke-test result.
6. **Host the profile site.** UX is judged by a person on desktop and mobile, and Builderr only
   hosts entries after they qualify, so give the grader a link now. Copy `out/site/` into a
   `docs/` folder (or a `gh-pages` branch), enable GitHub Pages in the repository settings,
   and put the Pages URL in the submission email. The site is static and works from any host.
7. **Ask Builderr two questions** on Discord or by email before sending the entry: the exact
   time/CPU budget of an official run, and whether they can supply a search API key (Brave or
   Serper) for scoring. The agent reads `SIGNALPOST_BRAVE_API_KEY` / `SIGNALPOST_SERPER_API_KEY`
   and uses results only as website candidates, never as evidence.

## Before you submit: pin the time budget

A timeout means the run is not scored at all, whatever it would have scored. Ask Builderr
(Discord or inquiries@builderr.ai) for the exact wall-clock and CPU budget of an official run
and put about 85% of it into the declared command, e.g. a 60-minute budget becomes
`--time-budget 3000`. The agent finishes every company inside that number by switching to
registry-only mode when time gets short, so a conservative value costs a little depth, never
the run. If they will not state a budget, declare `--time-budget 1500`.

## Email template

```
Subject: Signalpost Round 1 submission

Agent name: <name>
Repository URL: <url>
Exact commit hash: <hash>
100-company smoke-test result or report URL: <url to out/run-report.json + envelopes>
One-command run instruction:
  pip install -r requirements.txt && python -m signalpost run --input <BATCH_FILE> --output out/envelopes.jsonl --time-budget <SECONDS>
Models / APIs / licences: no LLM. Brønnøysundregistrene open APIs (NLOD), NAV Arbeidsplassen public job feed,
  company websites (robots.txt honoured). Code licence: MIT. Optional search API off by default.
Expected cost per official run: USD 0 third-party cost.
Contact for results: <email>
```
