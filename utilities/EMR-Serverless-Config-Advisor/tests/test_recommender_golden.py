#!/usr/bin/env python3
"""Golden-fixture regression tests for emr_recommender.py.

Runs the recommender against extracted event-log fixtures (12 production
jobs + 7 EC2 migration jobs) and diffs the output against a committed golden
baseline. Any change to recommendation output fails the test unless the
change is explicitly allow-listed or the baseline is regenerated.

Usage:
  python3 tests/test_recommender_golden.py                  # run regression check
  python3 tests/test_recommender_golden.py --update-golden  # regenerate baseline
  python3 tests/test_recommender_golden.py --update-golden --reason "PR #164 wave cap"

Exit codes: 0 = pass, 1 = regression detected, 2 = harness error.

Design (from INTERNAL_KNOWLEDGE.md "Regression Test Suite" TODO):
  - golden fixtures = extracted metrics JSONs (tests/fixtures/{b12,ec2_migration}/)
  - run recommender on fixtures before every PR merge
  - diff output against golden baseline — fail on unexpected changes
  - allow-list for intentional changes (reviewed and approved):
    changes recorded in golden_baseline.json's "history" with --reason
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)
RECOMMENDER = os.path.join(TOOL_DIR, "emr_s_fine_tuner.py")
FIXTURES = [os.path.join(HERE, "fixtures", "b12"), os.path.join(HERE, "fixtures", "ec2_migration"),
            os.path.join(HERE, "fixtures", "serverless_overprovisioned"),
            os.path.join(HERE, "fixtures", "field_reports")]
GOLDEN = os.path.join(HERE, "golden_baseline.json")

# The fields that constitute the recommendation "contract". Anything else in
# the output JSON (metrics echoes, narrative text) can change freely.
CONTRACT_FIELDS = [
    ("worker", "type"),
    ("worker", "vcpu"),
    ("worker", "memory_gb"),
    ("worker", "max_executors"),
    ("worker", "min_executors"),
    ("spark_configs", "spark.sql.shuffle.partitions"),
    ("spark_configs", "spark.dynamicAllocation.maxExecutors"),
    ("spark_configs", "spark.dynamicAllocation.minExecutors"),
    ("spark_configs", "spark.emr-serverless.executor.disk"),
    ("spark_configs", "spark.emr-serverless.executor.disk.type"),
    ("spark_configs", "spark.sql.adaptive.advisoryPartitionSizeInBytes"),
    ("spark_configs", "spark.sql.autoBroadcastJoinThreshold"),
    ("spark_configs", "spark.sql.optimizer.excludedRules"),
    ("spark_configs", "spark.io.compression.codec"),
]


def check_fixtures_parse(fixture_dir):
    """Fail loudly on unparseable fixture JSONs. The recommender's loader
    silently skips files that fail json.load, so a corrupt fixture degrades
    coverage without failing anything (found: one fixture with an invalid
    escape sequence contributed nothing for weeks while looking protective)."""
    import glob as _glob
    bad = []
    for f in _glob.glob(os.path.join(fixture_dir, "task_stage_summary", "*.json")):
        try:
            with open(f) as fh:
                json.load(fh)
        except Exception as e:
            bad.append("%s: %s" % (os.path.basename(f), e))
    if bad:
        raise RuntimeError("unparseable fixture(s) in %s:\n  %s"
                           % (fixture_dir, "\n  ".join(bad)))


def run_recommender(fixture_dir):
    """Run the recommender on one fixture dir, return (cost_recs, perf_recs)."""
    with tempfile.TemporaryDirectory() as td:
        cost_out = os.path.join(td, "cost.json")
        perf_out = os.path.join(td, "perf.json")
        proc = subprocess.run(
            [sys.executable, RECOMMENDER, "--input-path", fixture_dir,
             "--output-cost", cost_out, "--output-perf", perf_out],
            capture_output=True, text=True, cwd=td)
        if proc.returncode != 0:
            raise RuntimeError("recommender failed on %s:\n%s" % (fixture_dir, proc.stderr[-3000:]))
        with open(cost_out) as f:
            cost = json.load(f)
        with open(perf_out) as f:
            perf = json.load(f)
    return cost, perf


def extract_contract(rec):
    """Project one recommendation onto the contract fields."""
    out = {}
    for section, field in CONTRACT_FIELDS:
        val = (rec.get(section) or {}).get(field)
        if val is not None:
            out["%s.%s" % (section, field)] = val
    return out


def build_snapshot():
    """Run recommender over all fixture dirs → {fixture_set: {app_id: {mode: contract}}}."""
    snapshot = {}
    for fdir in FIXTURES:
        # fixture dirs hold task_stage_summary/ + spark_config_extract/
        if not os.path.isdir(os.path.join(fdir, "task_stage_summary")):
            print("WARN: skipping %s (no task_stage_summary)" % fdir, file=sys.stderr)
            continue
        set_name = os.path.basename(fdir)
        check_fixtures_parse(fdir)
        cost, perf = run_recommender(fdir)
        apps = {}
        for mode, recs in (("cost", cost), ("performance", perf)):
            for rec in recs:
                app = rec["application_id"]
                apps.setdefault(app, {})[mode] = extract_contract(rec)
        snapshot[set_name] = apps
    return snapshot


def diff_snapshots(golden, current):
    """Return list of human-readable differences."""
    diffs = []
    sets = sorted(set(golden) | set(current))
    for s in sets:
        g_apps, c_apps = golden.get(s, {}), current.get(s, {})
        for app in sorted(set(g_apps) | set(c_apps)):
            if app not in c_apps:
                diffs.append("%s/%s: MISSING from current output" % (s, app))
                continue
            if app not in g_apps:
                diffs.append("%s/%s: NEW app not in golden baseline" % (s, app))
                continue
            for mode in ("cost", "performance"):
                g, c = g_apps[app].get(mode, {}), c_apps[app].get(mode, {})
                for key in sorted(set(g) | set(c)):
                    gv, cv = g.get(key), c.get(key)
                    if gv != cv:
                        diffs.append("%s/%s [%s] %s: golden=%r current=%r"
                                     % (s, app, mode, key, gv, cv))
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-golden", action="store_true",
                    help="Regenerate the golden baseline from current recommender output")
    ap.add_argument("--reason", default="",
                    help="Why the baseline is changing (recorded in history)")
    args = ap.parse_args()

    try:
        current = build_snapshot()
    except Exception as e:
        print("HARNESS ERROR: %s" % e, file=sys.stderr)
        return 2

    n_apps = sum(len(a) for a in current.values())
    if n_apps == 0:
        print("HARNESS ERROR: no fixtures produced recommendations", file=sys.stderr)
        return 2

    if args.update_golden:
        history = []
        if os.path.exists(GOLDEN):
            with open(GOLDEN) as f:
                old = json.load(f)
            history = old.get("history", [])
            old_snapshot = old.get("snapshot", {})
            changes = diff_snapshots(old_snapshot, current)
            entry = {"reason": args.reason or "(no reason given)",
                     "changed_lines": len(changes)}
            if changes:
                entry["changes"] = changes[:200]
            history.append(entry)
        with open(GOLDEN, "w") as f:
            json.dump({"snapshot": current, "history": history}, f, indent=1, sort_keys=True)
        print("Golden baseline updated: %d apps across %d fixture sets"
              % (n_apps, len(current)))
        return 0

    if not os.path.exists(GOLDEN):
        print("HARNESS ERROR: no golden baseline at %s — run with --update-golden first"
              % GOLDEN, file=sys.stderr)
        return 2

    with open(GOLDEN) as f:
        golden = json.load(f)["snapshot"]
    diffs = diff_snapshots(golden, current)
    if diffs:
        print("REGRESSION: %d unexpected recommendation change(s):\n" % len(diffs))
        for d in diffs:
            print("  " + d)
        print("\nIf these changes are intentional, regenerate the baseline:")
        print("  python3 tests/test_recommender_golden.py --update-golden --reason \"<why>\"")
        return 1
    print("PASS: %d apps, %d fixture sets — recommendations match golden baseline"
          % (n_apps, len(current)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
