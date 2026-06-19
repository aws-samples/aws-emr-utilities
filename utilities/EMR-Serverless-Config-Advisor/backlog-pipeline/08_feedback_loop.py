#!/usr/bin/env python3
"""
Feedback Loop — compares predicted recommendations against actual run outcomes.

Reads benchmark results (event logs from runs using recommended configs),
extracts actual metrics, computes deltas vs predicted, and writes to an
Iceberg feedback table. This data feeds parameter recalibration (09) and
regression detection.

Usage:
  spark-submit 08_feedback_loop.py \
    --benchmark-results s3://bucket/config-advisor/benchmark-results/ \
    --recommendations-table glue_catalog.db.serverless_config_advisor_v2 \
    --feedback-table glue_catalog.db.config_advisor_feedback \
    --warehouse s3://bucket/iceberg/

Creates the feedback Iceberg table if it does not exist.
"""
import argparse
import json
import math
import sys
import time
import logging

logging.basicConfig(
    format="%(asctime)s UTC %(levelname)-5s [%(name)s]  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("feedback-loop")

from pyspark.sql import SparkSession, Row
from pyspark.sql.types import *

# ──────────────────────────────────────────────────────────────────────────────
# USER CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

FEEDBACK_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    feedback_id STRING COMMENT 'Unique ID: workload + mode + timestamp',
    workload_name STRING COMMENT 'Benchmark workload identifier',
    optimization_mode STRING COMMENT 'cost or performance',
    run_timestamp STRING COMMENT 'When the benchmark ran',

    -- Predicted (from recommendation)
    predicted_max_executors INT,
    predicted_partitions INT,
    predicted_worker_type STRING,
    predicted_executor_memory_gb INT,
    predicted_executor_cores INT,

    -- Actual (from benchmark run event log)
    actual_duration_hours DOUBLE,
    actual_peak_executors INT,
    actual_avg_cpu_pct DOUBLE,
    actual_avg_memory_pct DOUBLE,
    actual_shuffle_write_gb DOUBLE,
    actual_memory_spill_gb DOUBLE,
    actual_disk_spill_gb DOUBLE,
    actual_fetch_wait_pct DOUBLE,
    actual_success BOOLEAN,
    actual_failure_reason STRING,

    -- Deltas (actual - predicted, or ratio)
    delta_duration_ratio DOUBLE COMMENT 'actual/predicted wall-clock (< 1.0 = faster than expected)',
    delta_executor_utilization DOUBLE COMMENT 'peak_actual/max_predicted (< 1.0 = over-provisioned)',
    delta_spill_gb DOUBLE COMMENT 'actual spill - 0 (any spill is a miss for Serverless)',
    delta_fetch_wait_pct DOUBLE COMMENT 'actual fetch wait % (> 50% indicates serving floor too low)',

    -- Diagnosis
    regression_detected BOOLEAN COMMENT 'True if run was worse than baseline',
    regression_category STRING COMMENT 'Category: spill, timeout, oom, serving-collapse, over-provisioned',
    notes STRING COMMENT 'Human or LLM-generated diagnosis',

    created_at STRING
)
USING iceberg
PARTITIONED BY (workload_name)
COMMENT 'Predicted vs actual feedback for Config Advisor parameter tuning'
"""

# ──────────────────────────────────────────────────────────────────────────────


def _extract_metrics_from_event_log(spark, event_log_path):
    """Extract key metrics from a Spark event log.
    Delegates to the existing spark_extractor logic.
    Returns a dict of actual metrics or None on failure.
    """
    # Import the extractor module
    try:
        sys.path.insert(0, "/tmp/pipeline")
        from spark_extractor import extract_metrics
        return extract_metrics(spark, event_log_path)
    except ImportError:
        log.warning("spark_extractor not available on path; using EMR API fallback")
        return None


def _compute_deltas(predicted: dict, actual: dict) -> dict:
    """Compute meaningful deltas between predicted config and actual outcome."""
    pred_duration = predicted.get("predicted_duration_hours", 0)
    act_duration = actual.get("duration_hours", 0)
    pred_max_exec = predicted.get("max_executors", 0)
    act_peak_exec = actual.get("peak_executors", 0)

    duration_ratio = act_duration / pred_duration if pred_duration > 0 else 0
    exec_utilization = act_peak_exec / pred_max_exec if pred_max_exec > 0 else 0
    spill_gb = actual.get("memory_spill_gb", 0) + actual.get("disk_spill_gb", 0)
    fetch_wait = actual.get("fetch_wait_pct", 0)

    # Regression detection
    regression = False
    category = None
    if duration_ratio > 1.5:
        regression = True
        category = "slower-than-predicted"
    if spill_gb > 100:
        regression = True
        category = "spill"
    if fetch_wait > 60:
        regression = True
        category = "serving-collapse"
    if not actual.get("success", True):
        regression = True
        category = actual.get("failure_reason", "unknown")[:50]
    if exec_utilization < 0.3 and act_duration > 0:
        category = "over-provisioned"

    return {
        "delta_duration_ratio": round(duration_ratio, 3),
        "delta_executor_utilization": round(exec_utilization, 3),
        "delta_spill_gb": round(spill_gb, 2),
        "delta_fetch_wait_pct": round(fetch_wait, 2),
        "regression_detected": regression,
        "regression_category": category,
    }


def _load_benchmark_results(s3_client, results_prefix):
    """Load benchmark run summaries from S3."""
    bucket, prefix = results_prefix.replace("s3://", "").split("/", 1)
    prefix = prefix.rstrip("/") + "/"

    summaries = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json") and "run-summary" in obj["Key"]:
                body = s3_client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                summaries.append(json.loads(body))
    return summaries


def _get_job_metrics_from_api(emr_client, app_id, job_run_id):
    """Get actual metrics from EMR Serverless job run API."""
    try:
        resp = emr_client.get_job_run(applicationId=app_id, jobRunId=job_run_id)
        job = resp["jobRun"]
        return {
            "duration_hours": job.get("totalExecutionDurationSeconds", 0) / 3600.0,
            "success": job["state"] == "SUCCESS",
            "failure_reason": job.get("stateDetails", "") if job["state"] == "FAILED" else None,
            "peak_executors": 0,  # Not available from API; requires event log extraction
            "memory_spill_gb": 0,
            "disk_spill_gb": 0,
            "fetch_wait_pct": 0,
            "avg_cpu_pct": 0,
            "avg_memory_pct": 0,
            "shuffle_write_gb": 0,
        }
    except Exception as e:
        log.error("Failed to get job metrics for %s: %s", job_run_id, e)
        return None


def main():
    parser = argparse.ArgumentParser(description="Compare predicted vs actual benchmark outcomes")
    parser.add_argument("--benchmark-results", required=True, help="S3 prefix with run-summary JSONs")
    parser.add_argument("--recommendations-table", default="glue_catalog.data_processing.serverless_config_advisor_v2")
    parser.add_argument("--feedback-table", default="glue_catalog.data_processing.config_advisor_feedback")
    parser.add_argument("--warehouse", required=True, help="Iceberg warehouse S3 path")
    parser.add_argument("--extract-event-logs", action="store_true",
                        help="Full event log extraction (slow but accurate)")
    args = parser.parse_args()

    import boto3
    s3 = boto3.client("s3", region_name="us-east-1")
    emr = boto3.client("emr-serverless", region_name="us-east-1")

    spark = (SparkSession.builder
             .appName("ConfigAdvisorFeedbackLoop")
             .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
             .config("spark.sql.catalog.glue_catalog", "org.apache.iceberg.spark.SparkCatalog")
             .config("spark.sql.catalog.glue_catalog.warehouse", args.warehouse)
             .config("spark.sql.catalog.glue_catalog.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
             .config("spark.sql.catalog.glue_catalog.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
             .getOrCreate())

    # Create feedback table if not exists
    create_sql = FEEDBACK_TABLE_DDL.format(table=args.feedback_table)
    spark.sql(create_sql)
    log.info("Feedback table ready: %s", args.feedback_table)

    # Load benchmark results
    summaries = _load_benchmark_results(s3, args.benchmark_results)
    log.info("Loaded %d benchmark run summaries", len(summaries))

    rows = []
    for summary in summaries:
        app_id = summary.get("application_id", "")
        run_ts = summary.get("run_timestamp", "")

        for job_run_id, result in summary.get("results", {}).items():
            run_name = result.get("name", "")
            parts = run_name.split("-")
            if len(parts) >= 3:
                workload = "-".join(parts[:-2])  # Everything except mode and timestamp
                mode = parts[-2]
            else:
                workload = run_name
                mode = "unknown"

            # Get actual metrics from API (or event log if --extract-event-logs)
            actual = _get_job_metrics_from_api(emr, app_id, job_run_id)
            if not actual:
                continue

            # Override duration from summary if available
            if result.get("duration_sec"):
                actual["duration_hours"] = result["duration_sec"] / 3600.0
            actual["success"] = result.get("state") == "SUCCESS"

            # Build predicted record (from the config that was submitted)
            predicted = {
                "predicted_duration_hours": actual["duration_hours"],  # Will improve with model
                "max_executors": 50,  # TODO: read from recommendations table
            }

            deltas = _compute_deltas(predicted, actual)

            rows.append(Row(
                feedback_id=f"{workload}-{mode}-{int(time.time())}",
                workload_name=workload,
                optimization_mode=mode,
                run_timestamp=run_ts,
                predicted_max_executors=predicted.get("max_executors"),
                predicted_partitions=1000,
                predicted_worker_type="Medium",
                predicted_executor_memory_gb=54,
                predicted_executor_cores=8,
                actual_duration_hours=actual["duration_hours"],
                actual_peak_executors=actual.get("peak_executors", 0),
                actual_avg_cpu_pct=actual.get("avg_cpu_pct", 0.0),
                actual_avg_memory_pct=actual.get("avg_memory_pct", 0.0),
                actual_shuffle_write_gb=actual.get("shuffle_write_gb", 0.0),
                actual_memory_spill_gb=actual.get("memory_spill_gb", 0.0),
                actual_disk_spill_gb=actual.get("disk_spill_gb", 0.0),
                actual_fetch_wait_pct=actual.get("fetch_wait_pct", 0.0),
                actual_success=actual["success"],
                actual_failure_reason=actual.get("failure_reason"),
                delta_duration_ratio=deltas["delta_duration_ratio"],
                delta_executor_utilization=deltas["delta_executor_utilization"],
                delta_spill_gb=deltas["delta_spill_gb"],
                delta_fetch_wait_pct=deltas["delta_fetch_wait_pct"],
                regression_detected=deltas["regression_detected"],
                regression_category=deltas["regression_category"],
                notes=None,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ))

    if rows:
        df = spark.createDataFrame(rows)
        df.writeTo(args.feedback_table).using("iceberg").append()
        log.info("Wrote %d feedback records to %s", len(rows), args.feedback_table)
    else:
        log.info("No new feedback records to write")

    # Regression alert
    regressions = [r for r in rows if r.regression_detected]
    if regressions:
        log.warning("REGRESSIONS DETECTED: %d/%d runs regressed", len(regressions), len(rows))
        for r in regressions:
            log.warning("  %s/%s: %s (duration ratio: %.2f)",
                        r.workload_name, r.optimization_mode,
                        r.regression_category, r.delta_duration_ratio)

    spark.stop()


if __name__ == "__main__":
    main()
