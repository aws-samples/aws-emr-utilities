#!/usr/bin/env python3
"""Build the EMR Test Drive example report from fixtures.

    python3 generate.py                       # write out/report.html + out/report.json
    python3 generate.py --open                # and open it
    python3 generate.py --fail-on new_failure,correctness,regression

--fail-on demonstrates the CI gate: exits 1 if any selected category fires on a
comparison whose intent is an upgrade or patch validation.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Import the package from the repo root without requiring an install, so the
# example runs straight from a fresh clone.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from etd.compare import build_all_pairs, load_run                  # noqa: E402
from etd.report import render_html, render_json              # noqa: E402

HERE = Path(__file__).parent
GATED_INTENTS = {"upgrade_regression", "patch_validation"}


def gate(results: list[dict], categories: set[str]) -> list[str]:
    problems = []
    for r in results:
        if r["comparison"]["intent"] not in GATED_INTENTS or not r["comparison"].get("declared"):
            continue
        cid = r["comparison"]["comparison_id"]
        fc = (r["functional"] or {}).get("counts", {})
        if "new_failure" in categories and fc.get("NEW_FAILURE"):
            problems.append(f"{cid}: {fc['NEW_FAILURE']} new functional failure(s)")
        if "correctness" in categories:
            new = [f for f in r["correctness"]
                   if not f["pre_existing"] and not f.get("resolved")]
            if new:
                problems.append(f"{cid}: {len(new)} new correctness finding(s) "
                                f"({', '.join(sorted({f['category'] for f in new}))})")
        perf = r["performance"]
        if perf and r["match"]["perf_verdict_valid"]:
            if "regression" in categories and perf["counts"].get("REGRESSION"):
                problems.append(f"{cid}: {perf['counts']['REGRESSION']} query regression(s)")
            if "timeout" in categories and perf["counts"].get("NEW_TIMEOUT"):
                problems.append(f"{cid}: {perf['counts']['NEW_TIMEOUT']} new timeout(s)")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(HERE / "fixtures"))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--open", action="store_true", dest="do_open")
    ap.add_argument("--fail-on", default="",
                    help="comma list of new_failure,correctness,regression,timeout")
    args = ap.parse_args()

    run = load_run(Path(args.fixtures))
    if not run.units:
        print("No unit fixtures found. Run:  python3 make_fixtures.py", file=sys.stderr)
        return 2

    results = build_all_pairs(run)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    html_path, json_path = out / "report.html", out / "report.json"
    html_path.write_text(render_html(run, results))
    json_path.write_text(render_json(run, results))

    print(f"run        {run.manifest['run_id']}  ({run.manifest.get('data_class', 'REAL')} data)")
    print(f"variants   {len(run.manifest['variants'])}   "
          f"workloads {len(run.manifest['workloads'])}   comparisons {len(results)}")
    for r in results:
        if not r["comparison"].get("declared"):
            continue
        fc = (r["functional"] or {}).get("counts", {})
        pc = (r["performance"] or {}).get("counts", {})
        newc = len([f for f in r["correctness"] if not f["pre_existing"] and not f.get("resolved")])
        fixc = len([f for f in r["correctness"] if f.get("resolved")])
        print(f"  [{r['verdict']['level']:13s}] {r['comparison']['title'][:50]:52s} "
              f"match={r['match']['status']:19s} "
              f"newfail={fc.get('NEW_FAILURE', 0)} fixed={fc.get('FIXED_BY_RELEASE', 0) + fc.get('FIXED', 0)} "
              f"corr_new={newc} corr_fixed={fixc} "
              f"regr={pc.get('REGRESSION', 0)} overhead={pc.get('OVERHEAD', 0)} "
              f"timeout={pc.get('NEW_TIMEOUT', 0)}")
    print(f"\nhtml       {html_path}\njson       {json_path}")

    if args.do_open:
        subprocess.run(["open", str(html_path)], check=False)

    if args.fail_on:
        cats = {c.strip() for c in args.fail_on.split(",") if c.strip()}
        problems = gate(results, cats)
        if problems:
            print(f"\nFAIL --fail-on={','.join(sorted(cats))}", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print(f"\nPASS --fail-on={','.join(sorted(cats))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
