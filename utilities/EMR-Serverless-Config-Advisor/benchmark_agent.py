#!/usr/bin/env python3
"""
Benchmark Agent CLI — end-to-end orchestrator for Config Advisor validation.

Full cycle: extract event log → generate recommendation → submit benchmark →
poll until complete → extract results → compare against baseline → report.

Usage:
  # Full run with registered workload
  python3 benchmark_agent.py --workload search-health

  # Custom event log
  python3 benchmark_agent.py --workload search-health \
    --event-log s3://bucket/eventlog.zip

  # A/B test with DRA tuning
  python3 benchmark_agent.py --workload search-health \
    --ab-config '{"spark.dynamicAllocation.sustainedSchedulerBacklogTimeout":"15s","spark.dynamicAllocation.executorAllocationRatio":"0.5"}'

  # Dry run (show config without submitting)
  python3 benchmark_agent.py --workload search-health --dry-run
"""
import argparse
import json
import os
import sys
import time
import logging
import tempfile
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s UTC %(levelname)-5s [benchmark-agent]  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("benchmark-agent")

try:
    import boto3
except ImportError:
    log.error("boto3 required: pip install boto3")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

REGION = "us-east-1"
APPLICATION_ID = "00g6iqbn4ke2l109"
EXECUTION_ROLE = "arn:aws:iam::633458367150:role/EMRServerlessJobExecutionRole"
RESULTS_PREFIX = "s3://suthan-event-logs/config-advisor/benchmark-results"
POLL_INTERVAL_SEC = 60
MAX_WAIT_MIN = 360

WORKLOAD_REGISTRY = {
    "search-health": {
        "event_log": "s3://suthan-event-logs/config-advisor-test/eventLogs-search-health-impressions-success-00g62e96m053ng0b.zip",
        "data_path": "s3://suthan-event-logs/synthetic/search-health/data-v2/",
        "query_script": "s3://suthan-event-logs/synthetic/search-health/scripts/query.py",
        "prod_duration_hours": 1.30,
        "prod_shuffle_write_gb": 4463,
        "prod_spill_gb": 9569,
    },
    "sup-trvlr-bml": {
        "event_log": "",
        "data_path": "s3://suthan-event-logs/synthetic/regression-suite/sup_trvlr_bml/data-fullscale/",
        "query_script": "s3://suthan-event-logs/synthetic/regression-suite/sup_trvlr_bml/scripts/query.py",
        "prod_duration_hours": 0.42,
        "prod_shuffle_write_gb": 15126,
        "prod_spill_gb": 2603,
    },
    "vrbo-new-property": {
        "event_log": "s3://suthan-event-logs/config-advisor-test/eventLogs-vrbo-new-property-msr-v2-success-00g62e98i2qspg0b.zip",
        "data_path": "s3://suthan-event-logs/synthetic/regression-suite/vrbo_new_property/data-fullscale/",
        "query_script": "s3://suthan-event-logs/synthetic/regression-suite/vrbo_new_property/scripts/query.py",
        "prod_duration_hours": 0.0,
        "prod_shuffle_write_gb": 0,
        "prod_spill_gb": 0,
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 1: EXTRACT
# ──────────────────────────────────────────────────────────────────────────────

def extract_event_log(event_log_path, output_dir):
    """Extract metrics from a Spark event log zip."""
    log.info("Step 1: Extracting event log → %s", output_dir)

    sys.path.insert(0, str(Path(__file__).parent))
    import python_extractor as pe

    os.makedirs(f"{output_dir}/task_stage_summary", exist_ok=True)

    if event_log_path.startswith("s3://"):
        parts = event_log_path.replace("s3://", "").split("/", 1)
        s3 = boto3.client("s3", region_name=REGION)
        body = s3.get_object(Bucket=parts[0], Key=parts[1])["Body"].read()
    else:
        with open(event_log_path, "rb") as f:
            body = f.read()

    filename = os.path.basename(event_log_path)
    lines = pe.extract_from_zip(body, filename)
    events = pe.parse_events(lines)

    result = {}
    result["application_info"] = pe.extract_app_info(events)
    result["spark_config"] = pe.extract_spark_config(events)
    result["io_summary"] = pe.extract_io_summary(events)
    result["executor_summary"] = pe.extract_executor_summary(events)
    result["spill_summary"] = pe.extract_spill_summary(events)
    result["stage_summary"] = pe.extract_stage_summary(events)
    result["task_summary"] = pe.extract_task_summary(events)
    result["application_id"] = result["application_info"].get("app_id", "")

    # Fix sql_executions format for recommender compatibility
    sql_execs = pe.extract_sql_execution_plans(events)
    if isinstance(sql_execs, dict):
        result["sql_executions"] = sql_execs.get("execution_plans", [])
    else:
        result["sql_executions"] = sql_execs

    out_file = f"{output_dir}/task_stage_summary/{filename.replace('.zip', '')}.json"
    with open(out_file, "w") as f:
        json.dump(result, f)

    io = result["io_summary"].get("application_level", {})
    log.info("  Input: %.1f GB | Shuffle W: %.1f GB | Spill: %.1f GB | Duration: %.2f h",
             io.get("total_input_gb", 0), io.get("total_shuffle_write_gb", 0),
             result["spill_summary"].get("total_memory_spilled_gb", 0),
             result["application_info"].get("total_run_duration_hours", 0))

    return output_dir, result


# ──────────────────────────────────────────────────────────────────────────────
# STEP 2: RECOMMEND
# ──────────────────────────────────────────────────────────────────────────────

def generate_recommendation(extract_dir):
    """Run the recommender on extracted metrics."""
    log.info("Step 2: Generating recommendations")

    sys.path.insert(0, str(Path(__file__).parent))
    from emr_recommender import generate_dual_recommendations

    cost_recs, perf_recs = generate_dual_recommendations(extract_dir)

    if not cost_recs:
        log.error("  No recommendations generated!")
        return None, None

    cost = cost_recs[0]
    perf = perf_recs[0] if perf_recs else None

    cost_cfg = cost.get("spark_configs", {})
    log.info("  COST: %s %sc/%sg | maxExec=%s | partitions=%s",
             cost.get("worker_type", "?"),
             cost_cfg.get("spark.executor.cores"),
             cost_cfg.get("spark.executor.memory"),
             cost_cfg.get("spark.dynamicAllocation.maxExecutors"),
             cost_cfg.get("spark.sql.shuffle.partitions"))

    if perf:
        perf_cfg = perf.get("spark_configs", {})
        log.info("  PERF: %s %sc/%s | maxExec=%s | partitions=%s",
                 perf.get("worker_type", "?"),
                 perf_cfg.get("spark.executor.cores"),
                 perf_cfg.get("spark.executor.memory"),
                 perf_cfg.get("spark.dynamicAllocation.maxExecutors"),
                 perf_cfg.get("spark.sql.shuffle.partitions"))

    return cost, perf


# ──────────────────────────────────────────────────────────────────────────────
# STEP 3: SUBMIT
# ──────────────────────────────────────────────────────────────────────────────

def build_spark_params(configs):
    """Convert spark_configs dict to sparkSubmitParameters string."""
    params = ""
    for k, v in configs.items():
        if v and k.startswith("spark."):
            params += f" --conf {k}={v}"
    return params.strip()


def submit_job(emr, workload_name, mode, query_script, data_path, spark_configs,
               app_id=APPLICATION_ID):
    """Submit a benchmark job to EMR Serverless."""
    run_tag = f"{workload_name}-{mode}-{int(time.time()) % 100000}"
    output_path = f"{RESULTS_PREFIX}/{workload_name}/{run_tag}/"
    spark_params = build_spark_params(spark_configs)

    resp = emr.start_job_run(
        applicationId=app_id,
        executionRoleArn=EXECUTION_ROLE,
        name=f"bench-{run_tag}",
        jobDriver={
            "sparkSubmit": {
                "entryPoint": query_script,
                "entryPointArguments": ["--input", data_path, "--output", output_path],
                "sparkSubmitParameters": spark_params,
            }
        },
        configurationOverrides={
            "monitoringConfiguration": {
                "s3MonitoringConfiguration": {
                    "logUri": f"{RESULTS_PREFIX}/{workload_name}/logs/{run_tag}/"
                },
                "managedPersistenceMonitoringConfiguration": {"enabled": True},
            }
        },
    )
    job_run_id = resp["jobRunId"]
    log.info("  Submitted %s: %s", run_tag, job_run_id)
    return job_run_id, run_tag


# ──────────────────────────────────────────────────────────────────────────────
# STEP 4: POLL
# ──────────────────────────────────────────────────────────────────────────────

def poll_jobs(emr, app_id, jobs):
    """Poll until all jobs complete. Returns {job_id: result_dict}."""
    log.info("Step 4: Polling %d jobs...", len(jobs))
    pending = set(jobs.keys())
    results = {}
    elapsed = 0

    while pending and elapsed < MAX_WAIT_MIN * 60:
        time.sleep(POLL_INTERVAL_SEC)
        elapsed += POLL_INTERVAL_SEC

        for jid in list(pending):
            resp = emr.get_job_run(applicationId=app_id, jobRunId=jid)
            job = resp["jobRun"]
            state = job["state"]
            if state in ("SUCCESS", "FAILED", "CANCELLED"):
                duration_sec = job.get("totalExecutionDurationSeconds", 0) or 0
                results[jid] = {
                    "state": state,
                    "name": jobs[jid],
                    "duration_sec": duration_sec,
                    "duration_hours": duration_sec / 3600.0,
                }
                pending.discard(jid)
                log.info("  %s → %s (%.1f min)", jobs[jid], state, duration_sec / 60)

        if pending:
            log.info("  [%d min] %d/%d still running...",
                     elapsed // 60, len(pending), len(jobs))

    for jid in pending:
        results[jid] = {"state": "TIMEOUT", "name": jobs[jid]}
    return results


# ──────────────────────────────────────────────────────────────────────────────
# STEP 5: COMPARE
# ──────────────────────────────────────────────────────────────────────────────

def compare_results(results, workload_cfg):
    """Compare benchmark results against production baseline."""
    log.info("Step 5: Comparing results")
    prod_duration = workload_cfg.get("prod_duration_hours", 0)

    report = []
    all_pass = True

    for jid, result in results.items():
        name = result["name"]
        state = result["state"]
        entry = {"name": name, "state": state, "pass": False, "issues": []}

        if state != "SUCCESS":
            entry["issues"].append(f"Job {state}")
            all_pass = False
            report.append(entry)
            continue

        duration_h = result.get("duration_hours", 0)
        entry["duration_hours"] = round(duration_h, 2)

        # Duration check (allow up to 1.5x production for same-scale data)
        if prod_duration > 0:
            ratio = duration_h / prod_duration
            entry["duration_ratio"] = round(ratio, 2)
            if ratio > 1.5:
                entry["issues"].append(f"Duration {ratio:.1f}x production (>{1.5}x threshold)")
                all_pass = False

        if not entry["issues"]:
            entry["pass"] = True

        report.append(entry)

    return report, all_pass


# ──────────────────────────────────────────────────────────────────────────────
# STEP 6: REPORT
# ──────────────────────────────────────────────────────────────────────────────

def print_report(workload_name, report, all_pass):
    """Print human-readable report."""
    print("\n" + "=" * 70)
    print(f"  BENCHMARK REPORT: {workload_name}")
    print("=" * 70)

    for entry in report:
        status = "PASS" if entry["pass"] else "FAIL"
        print(f"\n  [{status}] {entry['name']}")
        print(f"         State: {entry['state']}")
        if "duration_hours" in entry:
            print(f"         Duration: {entry['duration_hours']:.2f} h")
        if "duration_ratio" in entry:
            print(f"         vs Production: {entry['duration_ratio']:.2f}x")
        for issue in entry.get("issues", []):
            print(f"         ISSUE: {issue}")

    print("\n" + "-" * 70)
    verdict = "ALL PASS" if all_pass else "REGRESSION DETECTED"
    print(f"  Verdict: {verdict}")
    print("=" * 70 + "\n")

    return 0 if all_pass else 1


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="End-to-end benchmark orchestrator for EMR Config Advisor")
    parser.add_argument("--workload", required=True,
                        choices=list(WORKLOAD_REGISTRY.keys()),
                        help="Workload name from registry")
    parser.add_argument("--event-log", help="Override event log path (S3 or local)")
    parser.add_argument("--data-path", help="Override synthetic data path")
    parser.add_argument("--mode", default="cost",
                        choices=["cost", "performance", "both"],
                        help="Optimization mode(s) to benchmark")
    parser.add_argument("--application-id", default=APPLICATION_ID)
    parser.add_argument("--ab-config", help="JSON string of additional Spark configs for A/B test")
    parser.add_argument("--ab-name", default="ab-variant",
                        help="Name for the A/B variant (default: ab-variant)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show config without submitting")
    parser.add_argument("--skip-extract", action="store_true",
                        help="Skip extraction (use existing extract at --extract-dir)")
    parser.add_argument("--extract-dir",
                        help="Path to existing extraction directory")
    parser.add_argument("--no-poll", action="store_true",
                        help="Submit and exit without waiting")
    args = parser.parse_args()

    workload_cfg = WORKLOAD_REGISTRY[args.workload]
    event_log = args.event_log or workload_cfg["event_log"]
    data_path = args.data_path or workload_cfg["data_path"]
    query_script = workload_cfg["query_script"]

    # Step 1: Extract
    if args.skip_extract and args.extract_dir:
        extract_dir = args.extract_dir
        log.info("Step 1: Skipping extraction (using %s)", extract_dir)
    elif not event_log:
        log.error("No event log specified and workload has no registered default")
        sys.exit(1)
    else:
        extract_dir = tempfile.mkdtemp(prefix=f"bench_{args.workload}_")
        extract_dir, extract_data = extract_event_log(event_log, extract_dir)

    # Step 2: Recommend
    cost_rec, perf_rec = generate_recommendation(extract_dir)
    if not cost_rec:
        sys.exit(1)

    # Build job list
    jobs_to_submit = []
    if args.mode in ("cost", "both"):
        jobs_to_submit.append(("cost", cost_rec.get("spark_configs", {})))
    if args.mode in ("performance", "both") and perf_rec:
        jobs_to_submit.append(("perf", perf_rec.get("spark_configs", {})))

    # A/B variant
    if args.ab_config:
        ab_overrides = json.loads(args.ab_config)
        base_cfg = cost_rec.get("spark_configs", {}).copy()
        base_cfg.update(ab_overrides)
        jobs_to_submit.append((args.ab_name, base_cfg))
        log.info("  A/B variant '%s': +%s", args.ab_name, ab_overrides)

    if args.dry_run:
        print("\n[DRY RUN] Would submit:")
        for mode, cfg in jobs_to_submit:
            print(f"\n  --- {mode} ---")
            for k in sorted(cfg):
                if k.startswith("spark."):
                    print(f"    {k} = {cfg[k]}")
        return 0

    # Step 3: Submit
    log.info("Step 3: Submitting %d jobs", len(jobs_to_submit))
    emr = boto3.client("emr-serverless", region_name=REGION)
    submitted = {}

    for mode, cfg in jobs_to_submit:
        jid, name = submit_job(emr, args.workload, mode, query_script, data_path, cfg,
                               app_id=args.application_id)
        submitted[jid] = name

    if args.no_poll:
        print("\nJobs submitted (--no-poll). Check status with:")
        for jid, name in submitted.items():
            print(f"  aws emr-serverless get-job-run --application-id {args.application_id} --job-run-id {jid}")
        return 0

    # Step 4: Poll
    results = poll_jobs(emr, args.application_id, submitted)

    # Step 5: Compare
    report, all_pass = compare_results(results, workload_cfg)

    # Step 6: Report
    exit_code = print_report(args.workload, report, all_pass)

    # Save results JSON
    results_file = f"/tmp/bench_{args.workload}_{int(time.time())}.json"
    with open(results_file, "w") as f:
        json.dump({"workload": args.workload, "report": report, "all_pass": all_pass}, f, indent=2)
    log.info("Results saved: %s", results_file)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
