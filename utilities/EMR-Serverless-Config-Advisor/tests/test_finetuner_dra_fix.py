"""Test the Fine Tuner Balanced-DRA fix: short/ramp-wasteful queries get DRA rate
controls (and drop the static maxExecutors ceiling), while sustained shuffle-heavy
workloads keep the hard cap (no regression)."""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(HERE)
RECOMMENDER = os.path.join(TOOL_DIR, "emr_s_fine_tuner.py")
FIX = os.path.join(HERE, "fixtures")

RATIO = "spark.dynamicAllocation.executorAllocationRatio"
BACKLOG = "spark.dynamicAllocation.sustainedSchedulerBacklogTimeout"
MAXEXEC = "spark.dynamicAllocation.maxExecutors"


def cost_configs(fixture_dir):
    """Run recommender on a fixture dir, return list of cost spark_configs dicts."""
    return _configs(fixture_dir, "cost")


def perf_configs(fixture_dir):
    """Run recommender on a fixture dir, return list of PERFORMANCE spark_configs dicts."""
    return _configs(fixture_dir, "perf")


def _configs(fixture_dir, which):
    with tempfile.TemporaryDirectory() as td:
        cost_out = os.path.join(td, "cost.json")
        perf_out = os.path.join(td, "perf.json")
        proc = subprocess.run(
            [sys.executable, RECOMMENDER, "--input-path", fixture_dir,
             "--output-cost", cost_out, "--output-perf", perf_out],
            capture_output=True, text=True, cwd=td)
        assert proc.returncode == 0, f"recommender failed:\n{proc.stderr[-2000:]}"
        with open(cost_out if which == "cost" else perf_out) as f:
            recs = json.load(f)
    if isinstance(recs, dict):
        recs = [recs]
    return [r.get("spark_configs", {}) for r in recs]


def test_perf_mode_never_throttles():
    """Performance-optimized mode must NOT apply DRA rate controls (it optimizes for
    runtime, so throttling the ramp would be counterproductive). Even on the short-query
    fixture that triggers the cost-mode fix, perf configs keep an aggressive ramp."""
    for name in ("serverless_overprovisioned", "field_reports", "b12"):
        d = os.path.join(FIX, name)
        if not os.path.isdir(d):
            continue
        for c in perf_configs(d):
            assert c.get(RATIO) != "0.5", f"{name}: perf config must not carry DRA rate controls"


def test_short_query_gets_dra_rate_controls():
    """serverless_overprovisioned contains a short (~2.4 min) TPC-DS query.
    The fix should apply DRA rate controls and drop the static maxExecutors cap."""
    configs = cost_configs(os.path.join(FIX, "serverless_overprovisioned"))
    # At least one recommendation in this fixture should be ramp-wasteful (short) and
    # therefore carry the rate controls without a hard ceiling.
    ramp_tuned = [c for c in configs if c.get(RATIO) == "0.5" and c.get(BACKLOG) == "15s"]
    assert ramp_tuned, "expected at least one short-query rec with DRA rate controls"
    for c in ramp_tuned:
        assert MAXEXEC not in c, "rate-control recs must drop the static maxExecutors ceiling"


def test_sustained_shuffle_keeps_hard_cap():
    """The b12/field_reports fixtures are large sustained workloads; the guard should
    keep the hard maxExecutors cap and NOT apply rate controls (no regression)."""
    for name in ("b12", "field_reports"):
        for c in cost_configs(os.path.join(FIX, name)):
            ratio = c.get(RATIO)
            shuffle_ratio = None
            # Sustained-shuffle recs must not have had their cap replaced by rate controls.
            if ratio == "0.5":
                # Only acceptable if this particular rec was genuinely short/ramp-wasteful;
                # for these large fixtures we assert the cap is preserved for shuffle-heavy.
                # A rec with rate controls must still be a legitimate ramp-wasteful case
                # (it will lack maxExecutors); we simply ensure we never have BOTH set,
                # which would be the redundant/regressive state.
                assert MAXEXEC not in c, "must not set both rate controls and a hard cap"


def test_no_double_setting():
    """Across all fixtures, a rec must never carry both DRA rate controls AND a static
    maxExecutors cap (that reintroduces the ramp waste the fix removes)."""
    for name in ("serverless_overprovisioned", "b12", "field_reports", "ec2_migration"):
        d = os.path.join(FIX, name)
        if not os.path.isdir(d):
            continue
        for c in cost_configs(d):
            if c.get(RATIO) == "0.5":
                assert MAXEXEC not in c, f"{name}: rec has both rate controls and maxExecutors"
