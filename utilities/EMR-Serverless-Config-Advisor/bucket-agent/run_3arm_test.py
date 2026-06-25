#!/usr/bin/env python3
"""
3-Arm A/B/C Test:
  A: General sub-bucket + Mode 1 (no input — generous defaults)
  B: Optimized sub-bucket + Mode 2 (proxy: target_dur + shuffle_gb)
  C: Optimized sub-bucket + Mode 3 (event log: task_hours)

Usage:
  python3 run_3arm_test.py --dry-run
  python3 run_3arm_test.py --submit --app-id 00g68it8007j3d09
  python3 run_3arm_test.py --poll
"""
import argparse, json, os, subprocess, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from emr_s_tshirt_size import select_bucket, WorkloadIntent, _classify_size, _general

ROLE_ARN = "arn:aws:iam::633458367150:role/EMRServerlessJobExecutionRole"
REGION = "us-east-1"
S3_BASE = "s3://suthan-event-logs/synthetic/regression-suite"
MANIFEST = "/tmp/3arm_test_manifest.json"

JOBS = {
    "sup-trvlr-bml": {
        "script": f"{S3_BASE}/sup-trvlr-bml/scripts/query_iceberg.py",
        "args": ["--output", f"{S3_BASE}/sup-trvlr-bml/output-3arm-{{tag}}/"],
        "gb": 5400, "wtype": "join_heavy", "nj": 12, "ltg": 2000, "dur": 171, "shuf": 25900, "fan": None, "th": 139.3,
    },
    "lodging-sort-be": {
        "script": f"{S3_BASE}/lodging_sort_be/scripts/query_iceberg.py",
        "args": ["--output", f"{S3_BASE}/lodging_sort_be/output-3arm-{{tag}}/"],
        "gb": 2.4, "wtype": "aggregation", "nj": 3, "ltg": 1.2, "dur": 71, "shuf": 1189, "fan": 500.0, "th": 152.1,
    },
    "vrbo-new-property": {
        "script": f"{S3_BASE}/vrbo_new_property/scripts/query_iceberg.py",
        "args": ["--output", f"{S3_BASE}/vrbo_new_property/output-3arm-{{tag}}/"],
        "gb": 2500, "wtype": "join_heavy", "nj": 8, "ltg": 1200, "dur": 20, "shuf": 4100, "fan": None, "th": 100.5,
    },
    "clickstream-room-upsell": {
        "script": f"{S3_BASE}/clickstream_be_room_upsell/scripts/query_iceberg.py",
        "args": ["--output", f"{S3_BASE}/clickstream_be_room_upsell/output-3arm-{{tag}}/"],
        "gb": 1100, "wtype": "etl", "nj": 4, "ltg": 600, "dur": 22, "shuf": 32, "fan": None, "th": 160.4,
    },
    "search-health": {
        "script": "s3://suthan-event-logs/synthetic/search-health/scripts/query_iceberg.py",
        "args": ["--output", "s3://suthan-event-logs/synthetic/search-health/output-3arm-{tag}/"],
        "gb": 3000, "wtype": "aggregation", "nj": 15, "ltg": 800, "dur": 78, "shuf": 1500, "fan": None, "th": 405.7,
    },
    "ump-email-clickstream": {
        "script": f"{S3_BASE}/ump-email-clickstream/scripts/query_iceberg.py",
        "args": ["--output", f"{S3_BASE}/ump-email-clickstream/output-3arm-{{tag}}/"],
        "gb": 18800, "wtype": "join_heavy", "nj": 43, "ltg": 5000, "dur": 34, "shuf": 3700, "fan": None, "th": 261.7,
    },
}


def configs_to_params(configs: dict) -> str:
    return " ".join(f"--conf {k}={v}" for k, v in configs.items())


def get_3_configs(name, j):
    """Generate configs for all 3 arms."""
    # A: General + Mode 1
    size = _classify_size(WorkloadIntent(input_size_gb=j["gb"], workload_type=j["wtype"]))
    a = _general(size, WorkloadIntent(input_size_gb=j["gb"], workload_type=j["wtype"], num_joins=j["nj"], largest_table_gb=j["ltg"]))

    # B: Optimized + Mode 2
    b = select_bucket(WorkloadIntent(
        input_size_gb=j["gb"], workload_type=j["wtype"], num_joins=j["nj"], largest_table_gb=j["ltg"],
        target_duration_minutes=j["dur"], shuffle_write_gb=j["shuf"], fan_out_factor=j["fan"]
    ))

    # C: Optimized + Mode 3
    c = select_bucket(WorkloadIntent(
        input_size_gb=j["gb"], workload_type=j["wtype"], num_joins=j["nj"], largest_table_gb=j["ltg"],
        target_duration_minutes=j["dur"], shuffle_write_gb=j["shuf"], fan_out_factor=j["fan"], task_hours=j["th"]
    ))

    return {"A": a, "B": b, "C": c}


def submit(app_id, name, script, args, params):
    cmd = [
        "aws", "emr-serverless", "start-job-run",
        "--application-id", app_id,
        "--execution-role-arn", ROLE_ARN,
        "--name", name, "--region", REGION,
        "--job-driver", json.dumps({"sparkSubmit": {"entryPoint": script, "entryPointArguments": args, "sparkSubmitParameters": params}}),
        "--query", "jobRunId", "--output", "text",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None, r.stderr.strip()[:100]
    return r.stdout.strip(), None


def poll(app_id, manifest):
    print(f"\n  {'Name':<55} {'State':<10} {'Dur(min)':<10} {'vCPU-hr':<9} {'GB-hr':<9} {'$est'}")
    print(f"  {'-'*110}")
    for job in manifest["jobs"]:
        cmd = ["aws", "emr-serverless", "get-job-run", "--application-id", app_id, "--job-run-id", job["job_id"], "--region", REGION]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print(f"  {job['name']:<55} {'ERROR':<10}")
            continue
        d = json.loads(r.stdout)["jobRun"]
        state = d["state"]
        billed = d.get("billedResourceUtilization", {})
        vcpu = billed.get("vCPUHour", 0)
        mem = billed.get("memoryGBHour", 0)
        dur = d.get("totalExecutionDurationSeconds", 0) / 60
        cost = vcpu * 0.052624 + mem * 0.0057785
        print(f"  {job['name']:<55} {state:<10} {dur:<10.1f} {vcpu:<9.1f} {mem:<9.1f} ${cost:.2f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--app-id", default="00g68it8007j3d09")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--submit", action="store_true")
    p.add_argument("--poll", action="store_true")
    args = p.parse_args()

    if args.poll:
        m = json.load(open(MANIFEST))
        poll(m["app_id"], m)
        return

    ts = datetime.now().strftime("%m%d-%H%M")
    manifest = {"app_id": args.app_id, "timestamp": ts, "jobs": []}

    print(f"\n{'='*110}")
    print(f"  3-ARM TEST | App: {args.app_id} | {ts}")
    print(f"{'='*110}")

    for wl_name, j in JOBS.items():
        configs = get_3_configs(wl_name, j)
        print(f"\n  {wl_name}:")

        for arm, result in configs.items():
            tag = f"{arm}-{ts}"
            job_name = f"3arm-{arm}-{wl_name}-{ts}"
            params = configs_to_params(result.configs)
            resolved_args = [a.replace("{tag}", tag) for a in j["args"]]
            me = result.configs.get("spark.dynamicAllocation.maxExecutors", "?")
            w = f"{result.configs.get('spark.executor.cores','?')}c"
            sub = result.sub_bucket if hasattr(result, 'sub_bucket') else 'General'

            if args.submit:
                jid, err = submit(args.app_id, job_name, j["script"], resolved_args, params)
                if jid:
                    manifest["jobs"].append({"name": job_name, "job_id": jid, "arm": arm, "workload": wl_name})
                    print(f"    {arm}: {sub:<22} {w}/{me}exec → {jid}")
                else:
                    print(f"    {arm}: {sub:<22} {w}/{me}exec → FAILED: {err}")
                time.sleep(0.5)
            else:
                print(f"    {arm}: {sub:<22} {w}/{me}exec  (dry-run)")

    if args.submit:
        with open(MANIFEST, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"\n  Submitted {len(manifest['jobs'])} jobs. Manifest: {MANIFEST}")
        print(f"  Poll: python3 run_3arm_test.py --poll")


if __name__ == "__main__":
    main()
