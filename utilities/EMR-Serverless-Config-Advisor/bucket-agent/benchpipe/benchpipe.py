#!/usr/bin/env python3
"""benchpipe — reusable EMR Serverless benchmark pipeline.

Runs registered replicated workloads against a target release (and optional
feature confs), collects billed + event-log metrics, stores structured
results, and generates a baseline-vs-candidate comparison report.

Usage:
  # Run all workloads on a candidate release (creates app if needed):
  python3 benchpipe.py run --release emr-7.14.0

  # Run a subset, with feature flags under test:
  python3 benchpipe.py run --release emr-7.13.0 \
      --workloads emc-window-matcher,vrbo-moderate-join \
      --conf spark.membrain.spill.enabled=true --label membrain-on

  # Compare two result sets:
  python3 benchpipe.py compare --baseline emr-7.13.0 --candidate emr-7.14.0
  python3 benchpipe.py compare --baseline emr-7.13.0/default \
      --candidate emr-7.13.0/membrain-on --threshold 10

Design decisions (from hard-won lessons in this repo's benchmark history):
  - SEQUENTIAL execution only: concurrent submissions contaminate results
    (2026-06-29 finding) and can starve each other on shared app capacity.
  - One app per release, created on demand, generous caps so the app is
    never the bottleneck being measured; apps auto-stop (idle = $0).
  - Results keyed (workload_id, release, label, timestamp) in JSONL —
    grep/pandas-friendly, no infra dependency.
  - Billed utilization is the cost ground truth (event-log estimates run
    2-19% low); event log supplies stage-level metrics.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, "workloads.yaml")
RESULTS = os.path.join(HERE, "results", "runs.jsonl")

# Worker shapes must fit the app caps; sized so 400x4c workloads fit.
APP_CAPS = {"cpu": "1700 vCPU", "memory": "7000 GB", "disk": "60000 GB"}


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def aws(args, parse=True):
    p = sh(["aws"] + args)
    if p.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:3])}...: {p.stderr[:400]}")
    return json.loads(p.stdout) if parse and p.stdout.strip() else None


def load_registry():
    with open(REGISTRY) as f:
        return yaml.safe_load(f)


def ensure_app(release, region):
    """Find-or-create the benchpipe app for a release. Idle apps cost $0."""
    name = f"benchpipe-{release.replace('.', '-')}"
    apps = aws(["emr-serverless", "list-applications", "--region", region,
                "--query", f"applications[?name=='{name}']"])
    if apps:
        return apps[0]["id"]
    app = aws(["emr-serverless", "create-application",
               "--name", name, "--type", "Spark",
               "--release-label", release,
               "--maximum-capacity", json.dumps(APP_CAPS),
               "--region", region])
    print(f"[benchpipe] created app {app['applicationId']} ({release})")
    return app["applicationId"]


def submit_and_wait(app_id, wl_id, wl, extra_conf, defaults, label):
    conf = dict(wl.get("spark_conf") or {})
    conf.update(extra_conf)
    params = " ".join(f"--conf {k}={v}" for k, v in conf.items())
    driver = {"sparkSubmit": {
        "entryPoint": wl["script"],
        "entryPointArguments": wl.get("args") or [],
        "sparkSubmitParameters": params,
    }}
    overrides = {"monitoringConfiguration": {"s3MonitoringConfiguration": {
        "logUri": defaults["log_uri"]}}}
    jid = aws(["emr-serverless", "start-job-run",
               "--application-id", app_id,
               "--execution-role-arn", defaults["execution_role"],
               "--name", f"bp-{label}-{wl_id}"[:64],
               "--job-driver", json.dumps(driver),
               "--configuration-overrides", json.dumps(overrides),
               "--region", defaults["region"]])["jobRunId"]
    print(f"[benchpipe] {wl_id} -> {jid}")
    deadline = time.time() + defaults.get("timeout_minutes", 75) * 60
    while True:
        time.sleep(60)
        jr = aws(["emr-serverless", "get-job-run", "--application-id", app_id,
                  "--job-run-id", jid, "--region", defaults["region"]])["jobRun"]
        state = jr["state"]
        print(f"  {datetime.now().strftime('%H:%M:%S')} {wl_id} {state}")
        if state in ("SUCCESS", "FAILED", "CANCELLED"):
            return jid, jr
        if time.time() > deadline:
            print(f"  !! timeout — cancelling {jid}")
            aws(["emr-serverless", "cancel-job-run", "--application-id", app_id,
                 "--job-run-id", jid, "--region", defaults["region"]], parse=False)


def extract_metrics(jr):
    b = jr.get("billedResourceUtilization") or {}
    created = jr.get("createdAt"); updated = jr.get("updatedAt")
    dur = None
    if created and updated:
        dur = round((datetime.fromisoformat(str(updated)) -
                     datetime.fromisoformat(str(created))).total_seconds() / 60, 1)
    # us-east-1 x86 rates; override via env for other regions/arch
    cost = (b.get("vCPUHour", 0) * float(os.environ.get("RATE_VCPU", "0.052624"))
            + b.get("memoryGBHour", 0) * float(os.environ.get("RATE_MEM", "0.0057785"))
            + b.get("storageGBHour", 0) * float(os.environ.get("RATE_STO", "0.000111")))
    return {
        "state": jr["state"],
        "duration_min": dur,
        "vcpu_hours": b.get("vCPUHour"),
        "memory_gb_hours": b.get("memoryGBHour"),
        "storage_gb_hours": b.get("storageGBHour"),
        "cost_usd": round(cost, 2) if b else None,
    }


def cmd_run(args):
    reg = load_registry()
    defaults = reg["defaults"]
    wanted = args.workloads.split(",") if args.workloads else list(reg["workloads"])
    extra_conf = dict(kv.split("=", 1) for kv in (args.conf or []))
    label = args.label or "default"
    app_id = ensure_app(args.release, defaults["region"])
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    for wl_id in wanted:
        wl = reg["workloads"].get(wl_id)
        if not wl:
            print(f"[benchpipe] unknown workload {wl_id}, skipping"); continue
        if wl.get("baseline") is None and not args.include_unbaselined:
            print(f"[benchpipe] {wl_id} has no baseline — first run establishes it")
        jid, jr = submit_and_wait(app_id, wl_id, wl, extra_conf, defaults, label)
        rec = {
            "workload_id": wl_id, "release": args.release, "label": label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job_run_id": jid, "application_id": app_id,
            "extra_conf": extra_conf,
            **extract_metrics(jr),
        }
        with open(RESULTS, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[benchpipe] recorded: {wl_id} {rec['state']} "
              f"{rec['duration_min']}min ${rec['cost_usd']}")
    print(f"[benchpipe] done — results in {RESULTS}")


def latest_runs(release, label):
    """Most recent result per workload for a (release, label) pair."""
    if not os.path.exists(RESULTS):
        return {}
    out = {}
    with open(RESULTS) as f:
        for line in f:
            r = json.loads(line)
            if r["release"] == release and r.get("label", "default") == label:
                out[r["workload_id"]] = r  # later lines overwrite = latest
    return out


def parse_target(s):
    return (s.split("/", 1) + ["default"])[:2] if "/" in s else (s, "default")


def cmd_compare(args):
    (b_rel, b_label), (c_rel, c_label) = parse_target(args.baseline), parse_target(args.candidate)
    base, cand = latest_runs(b_rel, b_label), latest_runs(c_rel, c_label)
    reg = load_registry()["workloads"]
    thr = args.threshold
    METRICS = ["duration_min", "vcpu_hours", "memory_gb_hours", "cost_usd"]
    print(f"\n=== benchpipe compare: {args.baseline} -> {args.candidate} "
          f"(regression threshold {thr}%) ===\n")
    print(f"{'workload':26s} {'metric':16s} {'baseline':>10s} {'candidate':>10s} {'delta%':>8s}  status")
    verdict_fail = []
    for wl_id in sorted(set(base) | set(cand)):
        b, c = base.get(wl_id), cand.get(wl_id)
        if not b or not c:
            src = "baseline" if not b else "candidate"
            # fall back to registry baseline for missing baseline side
            if not b and reg.get(wl_id, {}).get("baseline"):
                b = reg[wl_id]["baseline"]
            else:
                print(f"{wl_id:26s} {'—':16s} {'MISSING in ' + src:>21s}")
                continue
        if c.get("state") == "FAILED":
            print(f"{wl_id:26s} {'—':16s} {'':>10s} {'FAILED':>10s} {'':>8s}  REGRESS (job failed)")
            verdict_fail.append(wl_id); continue
        for m in METRICS:
            bv, cv = b.get(m), c.get(m)
            if bv in (None, 0) or cv is None:
                continue
            delta = (cv - bv) / bv * 100
            status = "ok"
            if delta > thr: status = "REGRESS"; verdict_fail.append(wl_id)
            elif delta < -thr: status = "improve"
            print(f"{wl_id:26s} {m:16s} {bv:10.1f} {cv:10.1f} {delta:+7.1f}%  {status}")
        print()
    if verdict_fail:
        print(f"VERDICT: NO-GO — regressions in: {', '.join(sorted(set(verdict_fail)))}")
        return 1
    print("VERDICT: GO — no metric regressed beyond threshold")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="benchpipe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run workloads on a release")
    r.add_argument("--release", required=True)
    r.add_argument("--workloads", help="comma-separated subset (default: all)")
    r.add_argument("--conf", action="append", help="extra spark conf k=v (repeatable)")
    r.add_argument("--label", help="tag for this variant (default: 'default')")
    r.add_argument("--include-unbaselined", action="store_true")
    c = sub.add_parser("compare", help="compare two result sets")
    c.add_argument("--baseline", required=True, help="release[/label]")
    c.add_argument("--candidate", required=True, help="release[/label]")
    c.add_argument("--threshold", type=float, default=10.0)
    args = ap.parse_args()
    sys.exit(cmd_run(args) if args.cmd == "run" else cmd_compare(args))


if __name__ == "__main__":
    main()
