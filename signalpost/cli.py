"""Command line entry point.

    python -m signalpost run --input BATCH --output out/envelopes.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .inputs import read_inputs, sample
from .pipeline import Config, run_batch


def _write_envelopes(envs: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        if output.suffix == ".json":
            json.dump(envs, fh, ensure_ascii=False)
        else:
            for e in envs:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, output)


def cmd_run(a) -> int:
    items, rejected = read_inputs(a.input)
    cfg = Config(
        concurrency=a.concurrency,
        time_budget_s=a.time_budget,
        per_company_s=a.per_company,
        state_dir=Path(a.state_dir),
        respect_robots=not a.ignore_robots,
    )
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.stem + ".partial.jsonl")
    if partial.exists():
        partial.unlink()
    print(f"signalpost: {len(items)} companies from {a.input} "
          f"({len(rejected)} unreadable rows) budget={cfg.time_budget_s:.0f}s concurrency={cfg.concurrency}",
          file=sys.stderr)
    envs, report = asyncio.run(run_batch(cfg, items, str(a.input), partial))
    # Unreadable input rows still get a terminal envelope so nothing is silently dropped.
    for r in rejected:
        envs.append({
            "schema_version": "signalpost.envelope.v1", "organisation_number": None, "input_row": r["row"],
            "status": "failed", "status_reason": "input row has no readable organisation number",
            "input_record": r["record"], "claims": [], "errors": [{"stage": "input", "kind": "unreadable"}],
        })
    _write_envelopes(envs, out)
    report["input_file"] = str(a.input)
    report["output_file"] = str(out)
    report["unreadable_input_rows"] = len(rejected)
    rep_path = Path(a.report) if a.report else out.with_name("run-report.json")
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    if not a.no_site:
        from .ui import build_site

        site_dir = Path(a.site_dir) if a.site_dir else out.parent / "site"
        build_site(envs, site_dir, report)
        print(f"signalpost: profile site -> {site_dir / 'index.html'}", file=sys.stderr)
    if partial.exists():
        partial.unlink()
    print(json.dumps({k: report[k] for k in ("inputs", "envelopes", "one_envelope_per_input", "status_counts",
                                              "family_coverage_pct", "elapsed_s")}, indent=2))
    return 0


def cmd_sample(a) -> int:
    n = sample(a.universe, a.n, a.seed, a.out)
    print(f"wrote {n} companies to {a.out}")
    return 0


def cmd_site(a) -> int:
    from .ui import build_site

    envs = []
    with open(a.envelopes, encoding="utf-8") as fh:
        txt = fh.read()
    if txt.lstrip().startswith("["):
        envs = json.loads(txt)
    else:
        envs = [json.loads(l) for l in txt.splitlines() if l.strip()]
    rep = None
    rp = Path(a.envelopes).with_name("run-report.json")
    if rp.exists():
        rep = json.loads(rp.read_text("utf-8"))
    build_site(envs, Path(a.site_dir), rep)
    print(f"site -> {Path(a.site_dir) / 'index.html'}")
    return 0


def cmd_validate(a) -> int:
    from .validate import validate

    ok, msgs = validate(a.input, a.envelopes)
    for m in msgs:
        print(m)
    print("VALID" if ok else "INVALID")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="signalpost", description="Signalpost company research agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="research every company in a batch file")
    r.add_argument("--input", "-i", required=True, help="batch file (.jsonl/.json/.csv/.txt, optionally .gz)")
    r.add_argument("--output", "-o", default="out/envelopes.jsonl", help=".jsonl (default) or .json array")
    r.add_argument("--report", default=None)
    r.add_argument("--site-dir", default=None)
    r.add_argument("--no-site", action="store_true")
    r.add_argument("--state-dir", default=os.environ.get("SIGNALPOST_STATE_DIR", ".signalpost-state"))
    r.add_argument("--concurrency", type=int, default=int(os.environ.get("SIGNALPOST_CONCURRENCY", "24")))
    r.add_argument("--time-budget", type=float, default=float(os.environ.get("SIGNALPOST_TIME_BUDGET_S", "3300")),
                   help="seconds for the whole run (results are always written before this)")
    r.add_argument("--per-company", type=float, default=float(os.environ.get("SIGNALPOST_PER_COMPANY_S", "75")))
    r.add_argument("--ignore-robots", action="store_true", help=argparse.SUPPRESS)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("sample", help="draw a random local test batch from the public universe")
    s.add_argument("--universe", required=True)
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out", default="batch-100.jsonl")
    s.set_defaults(fn=cmd_sample)

    w = sub.add_parser("site", help="rebuild the HTML profile site from an envelopes file")
    w.add_argument("--envelopes", required=True)
    w.add_argument("--site-dir", default="out/site")
    w.set_defaults(fn=cmd_site)

    v = sub.add_parser("validate", help="check one terminal envelope per input and evidence completeness")
    v.add_argument("--input", required=True)
    v.add_argument("--envelopes", required=True)
    v.set_defaults(fn=cmd_validate)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
