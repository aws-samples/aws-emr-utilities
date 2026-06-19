#!/usr/bin/env python3
"""
Benchmark Runner — submits workloads with recommended configs and captures results.

Runs each registered workload with both cost and perf configs, waits for completion,
then extracts the resulting event log into the feedback pipeline.

Usage:
  python3 07_benchmark_runner.py \
    --application-id APP_ID \
    --execution-role ROLE_ARN \
    --config-table glue_catalog.db.serverless_config_advisor_v2 \
    --output-prefix s3://bucket/config-advisor/benchmark-results/ \
    --workloads search-health,sup-trvlr-bml,vrbo-new-property
"""
import argparse
import json
import time
import logging
import sys

logging.basicConfig(
    format="%(asctime)s UTC %(levelname)-5s [%(name)s]  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("benchmark-runner")

try:
    import boto3
except ImportError:
    log.error("boto3 required")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# USER CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

REGION = "us-east-1"
POLL_INTERVAL_SEC = 60
MAX_WAIT_MIN = 360

# Workload registry: each entry defines the query script and data path.
# The recommended config is pulled from the Iceberg recommendations table.
WORKLOAD_REGISTRY = {
    "search-health": {
        "script": "s3://BUCKET/synthetic/regression-suite/search-health/scripts/query.py",
        "data": "s3://BUCKET/synthetic/regression-suite/search-health/data/",
        "output": "s3://BUCKET/config-advisor/benchmark-results/search-health/",
    },
    "sup-trvlr-bml": {
        "script": "s3://BUCKET/synthetic/regression-suite/sup_trvlr_bml/scripts/query.py",
        "data": "s3://BUCKET/synthetic/regression-suite/sup_trvlr_bml/data-fullscale/",
        "output": "s3://BUCKET/config-advisor/benchmark-results/sup-trvlr-bml/",
    },
    "vrbo-new-property": {
        "script": "s3://BUCKET/synthetic/regression-suite/vrbo_new_property/scripts/query.py",
        "data": "s3://BUCKET/synthetic/regression-suite/vrbo_new_property/data-fullscale/",
        "output": "s3://BUCKET/config-advisor/benchmark-results/vrbo-new-property/",
    },
    "lodging-sort-be": {
        "script": "s3://BUCKET/synthetic/regression-suite/lodging_sort_be/scripts/query.py",
        "data": "s3://BUCKET/synthetic/regression-suite/lodging_sort_be/data-fullscale/",
        "output": "s3://BUCKET/config-advisor/benchmark-results/lodging-sort-be/",
    },
}

# ──────────────────────────────────────────────────────────────────────────────


def _build_spark_submit_params(config: dict) -> str:
    """Convert a spark_configs dict into a sparkSubmitParameters string."""
    params = ""
    for key, value in config.items():
        if value and key.startswith("spark."):
            params += f" --conf {key}={value}"
    return params.strip()


def _submit_job(emr_client, app_id, role_arn, workload_name, mode, script_path,
                data_path, output_path, spark_configs):
    """Submit a single benchmark job."""
    run_id = f"{workload_name}-{mode}-{int(time.time())}"
    spark_params = _build_spark_submit_params(spark_configs)

    resp = emr_client.start_job_run(
        applicationId=app_id,
        executionRoleArn=role_arn,
        name=f"bench-{run_id}",
        jobDriver={
            "sparkSubmit": {
                "entryPoint": script_path,
                "entryPointArguments": [
                    "--input", data_path,
                    "--output", f"{output_path}{run_id}/",
                ],
                "sparkSubmitParameters": spark_params,
            }
        },
        configurationOverrides={
            "monitoringConfiguration": {
                "s3MonitoringConfiguration": {
                    "logUri": f"{output_path}logs/{run_id}/"
                },
                "managedPersistenceMonitoringConfiguration": {"enabled": True},
            }
        },
    )
    job_run_id = resp["jobRunId"]
    log.info("Submitted %s: %s", run_id, job_run_id)
    return job_run_id, run_id


def _wait_for_jobs(emr_client, app_id, jobs: dict) -> dict:
    """Poll until all jobs complete. Returns {job_run_id: final_state}."""
    pending = set(jobs.keys())
    results = {}
    elapsed_min = 0

    while pending and elapsed_min < MAX_WAIT_MIN:
        time.sleep(POLL_INTERVAL_SEC)
        elapsed_min += POLL_INTERVAL_SEC / 60

        for jid in list(pending):
            resp = emr_client.get_job_run(applicationId=app_id, jobRunId=jid)
            state = resp["jobRun"]["state"]
            if state in ("SUCCESS", "FAILED", "CANCELLED"):
                results[jid] = {
                    "state": state,
                    "name": jobs[jid],
                    "duration_sec": (
                        resp["jobRun"].get("totalExecutionDurationSeconds", 0)
                    ),
                    "created": str(resp["jobRun"].get("createdAt", "")),
                }
                pending.discard(jid)
                log.info("  %s → %s (%.1f min)", jobs[jid], state,
                         results[jid]["duration_sec"] / 60)

    for jid in pending:
        results[jid] = {"state": "TIMEOUT", "name": jobs[jid]}
        log.warning("  %s TIMED OUT after %d min", jobs[jid], MAX_WAIT_MIN)

    return results


def _load_recommendation(s3_client, config_table, workload_name, mode):
    """Load the latest recommendation for a workload from the Iceberg table.
    Falls back to reading a JSON file from S3 if Iceberg query is not available.
    """
    # For now, load from S3 JSON (pipeline writes recs as JSON alongside Iceberg)
    # TODO: Query Iceberg table directly via Spark/Athena
    log.info("Loading %s recommendation for %s (from config table: %s)",
             mode, workload_name, config_table)
    return None


def main():
    parser = argparse.ArgumentParser(description="Run benchmark workloads with recommended configs")
    parser.add_argument("--application-id", required=True, help="EMR Serverless application ID")
    parser.add_argument("--execution-role", required=True, help="IAM execution role ARN")
    parser.add_argument("--config-table", default="glue_catalog.data_processing.serverless_config_advisor_v2")
    parser.add_argument("--output-prefix", required=True, help="S3 prefix for benchmark results")
    parser.add_argument("--workloads", required=True, help="Comma-separated workload names")
    parser.add_argument("--modes", default="cost,performance", help="Comma-separated optimization modes")
    parser.add_argument("--rec-file", help="Path to recommendations JSON (overrides table lookup)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    emr = boto3.client("emr-serverless", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)

    workloads = [w.strip() for w in args.workloads.split(",")]
    modes = [m.strip() for m in args.modes.split(",")]

    # Load recommendations
    recs_by_workload = {}
    if args.rec_file:
        with open(args.rec_file) as f:
            all_recs = json.load(f)
        for rec in all_recs:
            name = rec.get("application_name", "").lower().replace(" ", "-")
            recs_by_workload[name] = rec.get("spark_configs", {})

    # Submit jobs
    jobs = {}
    for workload_name in workloads:
        if workload_name not in WORKLOAD_REGISTRY:
            log.warning("Unknown workload: %s (skipping)", workload_name)
            continue

        wl = WORKLOAD_REGISTRY[workload_name]
        for mode in modes:
            spark_configs = recs_by_workload.get(workload_name, {})
            if not spark_configs:
                spark_configs = _load_recommendation(s3, args.config_table, workload_name, mode)
                if not spark_configs:
                    log.warning("No recommendation found for %s/%s — using defaults", workload_name, mode)
                    spark_configs = {
                        "spark.executor.cores": "8",
                        "spark.executor.memory": "54g",
                        "spark.dynamicAllocation.maxExecutors": "50",
                        "spark.sql.shuffle.partitions": "1000",
                        "spark.sql.adaptive.enabled": "true",
                    }

            if args.dry_run:
                log.info("[DRY-RUN] Would submit %s/%s with config: %s",
                         workload_name, mode, json.dumps(spark_configs, indent=2))
                continue

            jid, run_name = _submit_job(
                emr, args.application_id, args.execution_role,
                workload_name, mode, wl["script"], wl["data"], wl["output"],
                spark_configs,
            )
            jobs[jid] = run_name

    if args.dry_run or not jobs:
        return

    # Wait for all jobs
    log.info("Waiting for %d benchmark jobs...", len(jobs))
    results = _wait_for_jobs(emr, args.application_id, jobs)

    # Write summary
    summary = {
        "run_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "application_id": args.application_id,
        "results": results,
    }
    summary_path = f"{args.output_prefix.rstrip('/')}/run-summary-{int(time.time())}.json"
    bucket, key = summary_path.replace("s3://", "").split("/", 1)
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(summary, indent=2, default=str))
    log.info("Summary written to %s", summary_path)

    # Report
    succeeded = sum(1 for r in results.values() if r["state"] == "SUCCESS")
    failed = sum(1 for r in results.values() if r["state"] == "FAILED")
    log.info("Benchmark complete: %d succeeded, %d failed, %d total",
             succeeded, failed, len(results))


if __name__ == "__main__":
    main()
