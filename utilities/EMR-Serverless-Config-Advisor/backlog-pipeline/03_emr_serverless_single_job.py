#!/usr/bin/env python3
"""
EMR Serverless Batch Event Log Processor
==========================================
Processes a batch of event logs (up to 5) through the entire pipeline.
Designed to run as an EMR Serverless Spark job.

Usage:
  spark-submit emr_serverless_single_job.py \
    --log-batch '[{"uuid": "...", "s3path": "s3://...", ...}, ...]' \
    --s3-bucket <bucket-name> \
    --backlog-table <table-name> \
    --advisor-table <table-name>

This script:
  1. Downloads all required pipeline scripts from S3
  2. For each event log in the batch:
     a. Extracts metrics from the event log
     b. Loads metrics to Iceberg tables
     c. Generates recommendations
     d. Writes to S3 with date-hour partitioning (datehour=yyyymmddHH)
  3. Continues processing remaining logs even if one fails

IMPORTANT: Backlog table is NEVER updated
  - Backlog table remains with is_processed='N' forever
  - S3 advisor data (partitioned by datehour) is the source of truth
  - Orchestrator uses anti-join (backlog LEFT ANTI JOIN advisor ON app_id_hash)
  - If a log fails, only that log's S3 entry is skipped = will be retried
"""

import argparse
import os
import sys
import json
import importlib.util
import subprocess
import boto3
from datetime import datetime, timezone
from pyspark.sql import SparkSession

# ============================================================================
# Parse Arguments
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(description='Process batch of event logs')
    parser.add_argument('--log-batch', required=True, help='JSON array of event log dicts')
    parser.add_argument('--s3-bucket', required=True, help='S3 bucket name')
    parser.add_argument('--backlog-table', required=True, help='Backlog table name')
    parser.add_argument('--advisor-table', required=False, help='(Deprecated - not used) Kept for backward compatibility')
    parser.add_argument('--s3-scripts-prefix', default='pipeline-files-v1/backlog-scale-dw',
                        help='S3 prefix for pipeline scripts')
    return parser.parse_args()


# ============================================================================
# Spark Session
# ============================================================================

def get_spark_session(s3_bucket):
    """Initialize Spark session with Iceberg Hive catalog support."""
    print("Initializing Spark session with Iceberg Hive catalog support...")

    iceberg_warehouse = f"s3://{s3_bucket}/iceberg/"

    spark = SparkSession.builder \
        .appName("EMR_Serverless_SingleJob") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.sql.catalog.spark_catalog", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.spark_catalog.type", "hive") \
        .config("spark.sql.catalog.spark_catalog.warehouse", iceberg_warehouse) \
        .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
        .config("spark.rpc.message.maxSize", "512") \
        .enableHiveSupport() \
        .getOrCreate()

    spark.sparkContext.setLogLevel("WARN")

    print(f"✓ Spark session initialized: {spark.version}")
    print(f"   Catalog: Hive Iceberg")
    return spark


# ============================================================================
# Download Scripts from S3
# ============================================================================

def download_scripts_from_s3(s3_bucket, s3_scripts_prefix):
    """Download all required pipeline scripts from S3."""
    import boto3

    print("=" * 80)
    print("DOWNLOADING PIPELINE SCRIPTS FROM S3")
    print("=" * 80)

    local_dir = "/tmp/pipeline_scripts"
    os.makedirs(local_dir, exist_ok=True)

    s3_client = boto3.client('s3')

    required_scripts = [
        "04_spark_extractor.py",
        "06_emr_recommender.py",
        "07_write_to_s3_partitioned.py"  # Writes advisor recommendations to S3 with datehour partitioning
    ]

    downloaded = {}

    for script in required_scripts:
        s3_key = f"{s3_scripts_prefix}/{script}"
        local_path = os.path.join(local_dir, script)

        try:
            print(f"Downloading {script}...")
            s3_client.download_file(s3_bucket, s3_key, local_path)
            downloaded[script] = local_path
            print(f"  ✓ Downloaded to {local_path}")
        except Exception as e:
            print(f"  ✗ Failed to download {script}: {e}")
            raise

    print("=" * 80)
    print(f"✓ Downloaded {len(downloaded)} scripts")
    print("=" * 80)

    return downloaded, local_dir


# ============================================================================
# Pipeline Steps
# ============================================================================

def step_1_extract_metrics(spark, s3path, uuid, output_path, scripts):
    """Extract metrics from single event log."""
    print("=" * 80)
    print("→ STEP 1: Extract Metrics")
    print("=" * 80)

    extractor_path = scripts["04_spark_extractor.py"]

    spec = importlib.util.spec_from_file_location("spark_extractor", extractor_path)
    spark_extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spark_extractor)

    # Extract metrics for single event log
    job_ids = spark_extractor.run_extractor(
        input_path=s3path,
        output_path=output_path,
        hours_ago=1,
        max_recent_files=0,
        decompress_workers=50,
        local_decompress=False,
        single_app=True,
        spark=spark
    )

    print(f"✓ Step 1 completed: Extracted {len(job_ids) if job_ids else 0} job_ids")
    return job_ids


def step_2_generate_recommendations(spark, output_path, scripts):
    """Generate recommendations from JSON files."""
    print("=" * 80)
    print("→ STEP 3: Generate Recommendations")
    print("=" * 80)

    recommender_path = scripts["06_emr_recommender.py"]

    spec = importlib.util.spec_from_file_location("emr_recommender", recommender_path)
    recommender_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recommender_module)

    # GitHub version of emr_recommender uses generate_dual_recommendations(input_path, ...)
    # Pass the output_path directly - it will load the metrics from task_stage_summary/
    print(f"Generating recommendations from: {output_path}")

    try:
        # Generate recommendations using the path (GitHub version)
        cost_recs, perf_recs = recommender_module.generate_dual_recommendations(
            input_path=output_path,
            limit=100,  # Process all available files
            target_partition_size_mib=1024,
            serverless_storage=False
        )
    except Exception as e:
        print(f"⚠ Error generating recommendations: {e}")
        print("Creating empty recommendations as fallback")
        cost_recs = []
        perf_recs = []

    if not cost_recs:
        print("⚠ No recommendations generated")
        # Create empty files for compatibility
        cost_recs = []
        perf_recs = []

    # Write to local files
    import json
    local_output = "/tmp/recommendations"
    os.makedirs(local_output, exist_ok=True)

    cost_file = os.path.join(local_output, "recommendations_cost_optimized.json")
    perf_file = os.path.join(local_output, "recommendations_performance_optimized.json")

    with open(cost_file, 'w') as f:
        json.dump(cost_recs, f, indent=2)

    with open(perf_file, 'w') as f:
        json.dump(perf_recs, f, indent=2)

    print(f"✓ Step 3 completed: Generated {len(cost_recs)} cost recommendations, {len(perf_recs)} perf recommendations")
    return cost_file, perf_file


def read_metrics_from_json(spark, extract_path):
    """Read metrics from JSON files."""
    import json
    import boto3

    metrics_list = []

    s3_path = extract_path.replace("s3://", "")
    parts = s3_path.split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") + "/task_stage_summary/"

    s3 = boto3.client("s3")
    paginator = s3.get_paginator('list_objects_v2')

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        if 'Contents' not in page:
            continue

        for obj in page['Contents']:
            key = obj['Key']
            if key.endswith('.json'):
                try:
                    response = s3.get_object(Bucket=bucket, Key=key)
                    content = response['Body'].read().decode('utf-8')
                    data = json.loads(content)
                    metrics_list.append(data)
                except Exception as e:
                    print(f"⚠ Error reading {key}: {e}")

    return metrics_list


def step_3_write_to_advisor(spark, extract_path, cost_file, perf_file, s3_bucket, scripts):
    """Write recommendations to S3 with date-hour partitioning."""
    print("=" * 80)
    print("→ STEP 3: Write to S3 with Date-Hour Partitioning")
    print("=" * 80)

    writer_path = scripts["07_write_to_s3_partitioned.py"]

    spec = importlib.util.spec_from_file_location("write_to_s3_partitioned", writer_path)
    writer_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer_module)

    # S3 output path for advisor data
    s3_output_path = f"s3://{s3_bucket}/emr-serverless-config-advisor/"

    rows_written = writer_module.write_to_s3_partitioned(
        rec_path=cost_file,
        perf_rec_path=perf_file,
        extract_path=extract_path,
        s3_output_path=s3_output_path,
        spark=spark
    )

    print(f"✓ Step 4 completed: Wrote {rows_written} rows to S3")
    return rows_written


def update_backlog_status(spark, backlog_table, uuid, status):
    """
    NOTE: This function is NO LONGER USED.

    We do NOT update the backlog table.
    The backlog table remains with is_processed='N' forever.
    The advisor table is the source of truth (via anti-join on app_id_hash).
    """
    print(f"ℹ️  Backlog table NOT updated (by design)")
    print(f"   The advisor table contains is_processed='Y' as source of truth")
    pass  # No-op


# ============================================================================
# Main
# ============================================================================

def main():
    start_time = datetime.now(timezone.utc)

    # Parse arguments
    args = parse_args()

    # Parse log batch from JSON
    log_batch = json.loads(args.log_batch)

    print("\n" + "=" * 80)
    print("EMR SERVERLESS BATCH JOB PROCESSOR")
    print("=" * 80)
    print(f"Batch Size:     {len(log_batch)} logs")
    print(f"S3 Bucket:      {args.s3_bucket}")
    print(f"Backlog Table:  {args.backlog_table}")
    print(f"Advisor Table:  {args.advisor_table}")
    print(f"Start Time:     {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 80)
    print("\nLogs in batch:")
    for i, log in enumerate(log_batch, 1):
        print(f"  {i}. UUID: {log['uuid']}")
        print(f"     S3 Path: {log['s3path']}")
        print(f"     App ID: {log['application_id']}")
    print("=" * 80)

    spark = None

    try:
        # Track step timings
        step_times = {}

        # Initialize Spark
        step_start = datetime.now(timezone.utc)
        spark = get_spark_session(args.s3_bucket)
        step_times['spark_init'] = (datetime.now(timezone.utc) - step_start).total_seconds()

        # Download scripts
        step_start = datetime.now(timezone.utc)
        scripts, script_dir = download_scripts_from_s3(args.s3_bucket, args.s3_scripts_prefix)
        step_times['download_scripts'] = (datetime.now(timezone.utc) - step_start).total_seconds()

        # Add script directory to Python path
        sys.path.insert(0, script_dir)

        # ============================================================================
        # BATCH PROCESSING: Process each event log individually
        # ============================================================================
        batch_start_time = datetime.now(timezone.utc)

        successful_logs = []
        failed_logs = []
        log_timings = []

        print("\n" + "=" * 80)
        print("🚀 BATCH PROCESSING STARTED")
        print("=" * 80)
        print(f"Processing {len(log_batch)} event logs")
        print(f"Start Time: {batch_start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 80 + "\n")

        for log_idx, log_info in enumerate(log_batch, 1):
            uuid = log_info['uuid']
            s3path = log_info['s3path']
            application_id = log_info['application_id']
            app_id_hash = log_info['app_id_hash']

            log_start_time = datetime.now(timezone.utc)

            print("\n" + "=" * 80)
            print(f"📝 PROCESSING LOG {log_idx}/{len(log_batch)}")
            print("=" * 80)
            print(f"UUID:           {uuid}")
            print(f"Application ID: {application_id}")
            print(f"S3 Path:        {s3path}")
            print(f"Start Time:     {log_start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print("=" * 80)

            try:
                # Output path for this specific log
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                output_path = f"s3://{args.s3_bucket}/test-target-metrics/{timestamp}_{uuid[:8]}/"

                # Step 1: Extract metrics
                step_start = datetime.now(timezone.utc)
                job_ids = step_1_extract_metrics(spark, s3path, uuid, output_path, scripts)
                extract_time = (datetime.now(timezone.utc) - step_start).total_seconds()

                if not job_ids:
                    raise Exception("No metrics extracted from event log")

                # Step 2: Generate recommendations (directly from Step 1 JSON output)
                step_start = datetime.now(timezone.utc)
                cost_file, perf_file = step_2_generate_recommendations(spark, output_path, scripts)
                reco_time = (datetime.now(timezone.utc) - step_start).total_seconds()

                # Step 3: Write to S3 with date-hour partitioning
                step_start = datetime.now(timezone.utc)

                s3_write_count = step_3_write_to_advisor(
                    spark=spark,
                    extract_path=output_path,
                    cost_file=cost_file,
                    perf_file=perf_file,
                    s3_bucket=args.s3_bucket,
                    scripts=scripts
                )
                s3_time = (datetime.now(timezone.utc) - step_start).total_seconds()

                log_end_time = datetime.now(timezone.utc)
                log_duration = (log_end_time - log_start_time).total_seconds()

                print("\n" + "-" * 80)
                print(f"✅ LOG {log_idx}/{len(log_batch)} COMPLETED")
                print("-" * 80)
                print(f"Duration: {log_duration:.1f}s")
                print(f"  Extract: {extract_time:.1f}s | Recommendations: {reco_time:.1f}s")
                print(f"  S3 Advisor Write: {s3_time:.1f}s")
                print(f"  S3 records written: {s3_write_count}")
                print("-" * 80)

                successful_logs.append({
                    'uuid': uuid,
                    'application_id': application_id,
                    'app_id_hash': app_id_hash,
                    's3path': s3path,
                    'duration': log_duration,
                    's3_records': s3_write_count
                })

                log_timings.append({
                    'uuid': uuid,
                    'extract': extract_time,
                    'recommendations': reco_time,
                    's3_advisor_write': s3_time,
                    'total': log_duration
                })

            except Exception as log_error:
                log_end_time = datetime.now(timezone.utc)
                log_duration = (log_end_time - log_start_time).total_seconds()

                print("\n" + "-" * 80)
                print(f"❌ LOG {log_idx}/{len(log_batch)} FAILED")
                print("-" * 80)
                print(f"Error: {log_error}")
                print(f"Duration before failure: {log_duration:.1f}s")
                print("-" * 80)
                import traceback
                traceback.print_exc()
                print("-" * 80)
                print("⚠️  Continuing with next log in batch...")
                print("-" * 80)

                failed_logs.append({
                    'uuid': uuid,
                    'application_id': application_id,
                    'app_id_hash': app_id_hash,
                    's3path': s3path,
                    'error': str(log_error),
                    'duration': log_duration
                })

        # ============================================================================
        # All logs processed - S3 writes happened immediately after each log
        # ============================================================================

        # ============================================================================
        # BATCH PROCESSING COMPLETE
        # ============================================================================
        batch_end_time = datetime.now(timezone.utc)
        batch_duration = (batch_end_time - batch_start_time).total_seconds()
        batch_duration_min = int(batch_duration / 60)
        batch_duration_sec = int(batch_duration % 60)

        # Calculate total job duration
        end_time = datetime.now(timezone.utc)
        total_duration = (end_time - start_time).total_seconds()
        total_duration_min = int(total_duration / 60)
        total_duration_sec = int(total_duration % 60)

        print("\n" + "=" * 80)
        print("🎯 BATCH PROCESSING SUMMARY")
        print("=" * 80)
        print(f"Total Logs:          {len(log_batch)}")
        print(f"✅ Successful:       {len(successful_logs)}")
        print(f"❌ Failed:           {len(failed_logs)}")
        print(f"Success Rate:        {len(successful_logs)*100/len(log_batch):.1f}%")
        print(f"Batch Duration:      {batch_duration_min}m {batch_duration_sec}s ({batch_duration:.1f}s)")
        print(f"Avg per log:         {batch_duration/len(log_batch):.1f}s")
        print("=" * 80)

        if successful_logs:
            print("\n✅ Successfully Processed Logs:")
            total_s3_records = sum(log.get('s3_records', 0) for log in successful_logs)
            for i, log in enumerate(successful_logs, 1):
                print(f"  {i}. {log['application_id']} ({log['duration']:.1f}s, {log.get('s3_records', 0)} S3 records)")
            print(f"\nTotal S3 records written: {total_s3_records}")

        if failed_logs:
            print("\n❌ Failed Logs:")
            for i, log in enumerate(failed_logs, 1):
                print(f"  {i}. {log['application_id']}")
                print(f"     Error: {log['error']}")
                print(f"     Duration: {log['duration']:.1f}s")

        print("\n" + "=" * 80)
        print("✅ EMR SERVERLESS BATCH JOB COMPLETED")
        print("=" * 80)
        print(f"Job Start:       {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"Job End:         {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"Total Duration:  {total_duration_min}m {total_duration_sec}s ({total_duration:.1f}s)")
        print(f"\n⏱️  BREAKDOWN:")
        print(f"  Job Overhead:    {step_times.get('spark_init', 0) + step_times.get('download_scripts', 0):.1f}s")
        print(f"    - Spark Init:  {step_times.get('spark_init', 0):.1f}s")
        print(f"    - Scripts DL:  {step_times.get('download_scripts', 0):.1f}s")
        total_s3_records = sum(log.get('s3_records', 0) for log in successful_logs)
        print(f"  Batch Process:   {batch_duration:.1f}s")
        print(f"    - {len(log_batch)} logs processed")
        print(f"    - {total_s3_records} records written to S3")
        print(f"\n💡 WRITE STRATEGY:")
        print(f"   Each log writes to S3 IMMEDIATELY after processing")
        print(f"   Partitioned by datehour (yyyymmddHH) - all jobs in same hour write to same partition")
        print("=" * 80)

        # Return 0 if at least one log succeeded, 1 if all failed
        return 0 if successful_logs else 1

    except Exception as e:
        # Catastrophic failure (Spark init, script download, etc.)
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()
        duration_min = int(duration / 60)
        duration_sec = int(duration % 60)

        print("\n" + "=" * 80)
        print("❌ BATCH JOB FAILED (CRITICAL ERROR)")
        print("=" * 80)
        print(f"Start Time:  {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"Failed At:   {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"Duration:    {duration_min}m {duration_sec}s ({duration:.1f}s)")
        print(f"Error:       {e}")
        print("=" * 80)
        import traceback
        traceback.print_exc()
        print("=" * 80)
        print("\nℹ️  All logs in this batch will be retried on next orchestrator run")

        return 1

    finally:
        if spark:
            spark.stop()


if __name__ == "__main__":
    sys.exit(main())
