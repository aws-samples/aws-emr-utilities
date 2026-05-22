#!/usr/bin/env python3
"""
EMR Serverless Job Submission Orchestrator
==========================================
Orchestrates EMR Serverless job submissions for unprocessed event logs.

Workflow:
  1. Query S3 advisor data (partitioned by datehour) to get processed app_id_hash values (last N hours)
  2. Query Iceberg backlog table to get event logs with is_processed='N' (last N hours)
  3. Anti-join: Find logs in backlog that are NOT in S3 advisor
  4. For each unprocessed log, submit ONE EMR Serverless Spark job via API
  5. Each job processes a single event log through the entire pipeline and writes to S3 with datehour partitioning

Dynamic Resource Allocation (Unified Configuration):
  - ALL event logs: min=2, initial=2, max=3 executors
  - Executor size: 2 vCPU + 8GB each (custom configuration)
  - Per job at startup: 1 driver (4 vCPU, 16GB) + 2 executors (4 vCPU, 16GB) = 8 vCPU, 32GB
  - Per job at max: 1 driver (4 vCPU, 16GB) + 3 executors (6 vCPU, 24GB) = 10 vCPU, 40GB

Capacity Planning (Application: CPU=5000, Memory=30000GB, Disk=40000GB):
  - CPU bottleneck: 5000 vCPU / 8 vCPU per job = ~625 concurrent jobs ✅
  - Memory: 30000 GB / 32 GB per job = ~937 concurrent jobs ✅
  - Limiting factor: CPU (625 concurrent jobs)
  - 2000 jobs supported via automatic queuing (no capacity exceeded errors)
  - Custom executor sizing (2 vCPU + 8GB) allows more concurrency vs default (4 vCPU + 16GB)

Configuration:
  - EMR_SERVERLESS_APPLICATION_ID: EMR Serverless application ID
  - EMR_SERVERLESS_EXECUTION_ROLE: IAM role ARN for job execution
  - BACKLOG_TABLE: Iceberg backlog table name
  - S3_ADVISOR_PATH: S3 path for advisor recommendations (partitioned by datehour=yyyymmddHH)
  - LOOKBACK_HOURS: Hours to look back for event logs (default: 2)

Environment Variables:
  - S3_BUCKET: S3 bucket for event logs and outputs
  - AWS_REGION: AWS region (default: us-east-1)
  - AWS_PROFILE: AWS profile for authentication
"""

import os
import sys
import boto3
import time
from datetime import datetime, timezone, timedelta
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, lit

# ============================================================================
# Configuration
# ============================================================================

# AWS Configuration
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_PROFILE = os.getenv("AWS_PROFILE", None)  # None = use IAM instance profile (for EMR instances)
S3_BUCKET = os.getenv("S3_BUCKET", "${S3_BUCKET}")

# EMR Serverless Configuration
# Application: dp-data-processing-demo-emr-serverless-7.13-python11
EMR_SERVERLESS_APPLICATION_ID = os.getenv("EMR_SERVERLESS_APPLICATION_ID", "${EMR_APPLICATION_ID}")
EMR_SERVERLESS_APPLICATION_NAME = os.getenv("EMR_SERVERLESS_APPLICATION_NAME",
    "dp-data-processing-demo-emr-serverless-7.13-python11")
EMR_SERVERLESS_EXECUTION_ROLE = os.getenv("EMR_SERVERLESS_EXECUTION_ROLE",
    "${IAM_EXECUTION_ROLE_ARN}")

# Table Configuration
BACKLOG_TABLE = os.getenv("BACKLOG_TABLE", "${CATALOG_NAMESPACE}.backlog_events_log_v5")
S3_ADVISOR_PATH = os.getenv("S3_ADVISOR_PATH", f"s3://{S3_BUCKET}/emr-serverless-config-advisor/")
ICEBERG_WAREHOUSE = os.getenv("ICEBERG_WAREHOUSE", f"s3://{S3_BUCKET}/iceberg/")

# Discovery Configuration
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "1"))  # Only process logs from last N hours
TEST_LIMIT = int(os.getenv("TEST_LIMIT", "0"))  # Limit number of jobs for testing (0 = no limit)

# S3 Paths
S3_SCRIPTS_PREFIX = os.getenv("S3_SCRIPTS_PREFIX", "pipeline-files-v1/backlog-scale-dw")
S3_ENTRYPOINT_SCRIPT = f"s3://{S3_BUCKET}/{S3_SCRIPTS_PREFIX}/03_run_single_emr_job.py"

# Python Dependencies (Virtual Environment)
# Path updated for EMR 7.13 Python 3.11 compatible venv
S3_DEPENDENCIES_PATH = os.getenv("S3_DEPENDENCIES_PATH",
    f"s3://{S3_BUCKET}/pipeline-files-v1/backlog-scale-dw/dependencies/pyspark_venv.tar.gz")

# Submission Configuration
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "100"))  # Max jobs to submit at once
SUBMISSION_DELAY_SECONDS = int(os.getenv("SUBMISSION_DELAY_SECONDS", "10"))  # Brief API rate-limit pause every 100 submissions
LONG_SLEEP_AFTER_JOBS = int(os.getenv("LONG_SLEEP_AFTER_JOBS", "250"))  # Take long sleep after this many jobs
LONG_SLEEP_SECONDS = int(os.getenv("LONG_SLEEP_SECONDS", "600"))  # Long sleep duration (10 minutes = 600 seconds)

# Capacity Management (prevents CPU capacity errors on EMR Serverless)
# Orchestrator checks live running job count before each submission.
# If cluster is full it waits locally — no job is ever rejected for lack of CPU.
CAPACITY_MAX_CONCURRENT = int(os.getenv("CAPACITY_MAX_CONCURRENT", "750"))   # Max concurrent running jobs
CAPACITY_CHECK_INTERVAL = int(os.getenv("CAPACITY_CHECK_INTERVAL", "300"))   # Seconds to wait between rechecks when full
CAPACITY_SAFE_BUFFER = int(os.getenv("CAPACITY_SAFE_BUFFER", "50"))          # Reserved slots below hard cap
MAX_JOBS_PER_RUN = int(os.getenv("MAX_JOBS_PER_RUN", "1200"))               # Max total jobs submitted per orchestrator run

# ============================================================================
# Spark Session Initialization
# ============================================================================

def get_spark_session():
    """Initialize Spark session with Iceberg Hive catalog support."""
    print("Initializing Spark session with Iceberg Hive catalog support...")

    spark = SparkSession.builder \
        .appName("EMR_Serverless_Orchestrator") \
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions") \
        .config("spark.sql.catalog.spark_catalog", "org.apache.iceberg.spark.SparkCatalog") \
        .config("spark.sql.catalog.spark_catalog.type", "hive") \
        .config("spark.sql.catalog.spark_catalog.warehouse", ICEBERG_WAREHOUSE) \
        .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
        .enableHiveSupport() \
        .getOrCreate()

    spark.sparkContext.setLogLevel("WARN")

    print(f"✓ Spark session initialized: {spark.version}")
    print(f"   Catalog: Hive Iceberg")
    return spark


# ============================================================================
# Query Functions
# ============================================================================

def get_processed_hashes_from_s3(s3_advisor_path, lookback_hours):
    """
    Read S3 advisor data from partitions (last N hours) to get processed app_id_hash values.
    Reads from datehour partitions (format: yyyymmddHH) for the lookback window.

    Args:
        s3_advisor_path: S3 path for advisor data (e.g., s3://bucket/emr-serverless-config-advisor/)
        lookback_hours: Hours to look back (match backlog lookback window)

    Returns:
        set: Set of app_id_hash strings that exist in S3 (already processed in last N hours)
    """
    print("=" * 80)
    print(f"SCANNING S3 FOR RECENT RECORDS (LAST {lookback_hours} HOURS)")
    print("=" * 80)
    print(f"S3 Advisor Path: {s3_advisor_path}")
    print(f"Lookback Window: {lookback_hours} hour(s)")
    print(f"Partition Format: datehour=yyyymmddHH")
    print(f"Strategy: Read JSON files from relevant partitions, load to memory")
    print("-" * 80)

    try:
        import boto3
        import json

        # Calculate datehour partitions to scan (last N hours)
        now = datetime.now(timezone.utc)
        partitions_to_scan = []

        for hour_offset in range(lookback_hours + 1):  # +1 to include current hour
            time_point = now - timedelta(hours=hour_offset)
            datehour = int(time_point.strftime("%Y%m%d%H"))
            partitions_to_scan.append(datehour)

        partitions_to_scan = sorted(set(partitions_to_scan))  # Remove duplicates and sort

        print(f"Current time (UTC): {now.isoformat()}")
        print(f"Datehour partitions to scan: {partitions_to_scan}")
        print("-" * 80)

        # Parse S3 path
        s3_path_clean = s3_advisor_path.replace("s3://", "").rstrip("/")
        parts = s3_path_clean.split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""

        s3_client = boto3.client('s3', region_name=AWS_REGION)
        processed_hashes = set()
        files_read = 0
        records_read = 0

        # Read from each partition
        for datehour in partitions_to_scan:
            partition_prefix = f"{prefix}/datehour={datehour}/" if prefix else f"datehour={datehour}/"

            print(f"Scanning partition: datehour={datehour}")
            print(f"  S3 prefix: s3://{bucket}/{partition_prefix}")

            try:
                # List all JSON files in this partition
                paginator = s3_client.get_paginator('list_objects_v2')
                page_iterator = paginator.paginate(Bucket=bucket, Prefix=partition_prefix)

                for page in page_iterator:
                    if 'Contents' not in page:
                        print(f"  No files found in partition datehour={datehour}")
                        continue

                    for obj in page['Contents']:
                        key = obj['Key']
                        if not key.endswith('.json'):
                            continue

                        # Read JSON file
                        try:
                            response = s3_client.get_object(Bucket=bucket, Key=key)
                            content = response['Body'].read().decode('utf-8')

                            # Parse JSON - could be single object or array
                            for line in content.strip().split('\n'):
                                if not line.strip():
                                    continue
                                try:
                                    record = json.loads(line)
                                    app_id_hash = record.get('app_id_hash')
                                    if app_id_hash and app_id_hash.strip():
                                        processed_hashes.add(app_id_hash)
                                        records_read += 1
                                except json.JSONDecodeError:
                                    pass  # Skip malformed lines

                            files_read += 1

                            # Progress indicator
                            if files_read % 500 == 0:
                                print(f"  Read {files_read} files, {records_read:,} records, {len(processed_hashes):,} unique hashes...")

                        except Exception as file_error:
                            print(f"  ⚠ Warning: Could not read {key}: {file_error}")

                print(f"  ✓ Partition datehour={datehour}: {len(processed_hashes):,} unique hashes")

            except Exception as partition_error:
                print(f"  ⚠ Warning: Could not scan partition datehour={datehour}: {partition_error}")

        print("-" * 80)
        print(f"✓ Read {files_read} JSON files from {len(partitions_to_scan)} partitions")
        print(f"✓ Found {records_read:,} total records")
        print(f"✓ Found {len(processed_hashes):,} unique app_id_hash values")
        print(f"✓ Loaded to memory for anti-join")
        print("=" * 80)

        return processed_hashes

    except Exception as e:
        print(f"⚠ Warning: Could not scan S3 advisor data: {e}")
        print(f"  Assuming no logs have been processed yet")
        import traceback
        traceback.print_exc()
        print("=" * 80)
        return set()


def get_unprocessed_event_logs(spark, backlog_table, s3_advisor_path, lookback_hours, limit=None):
    """
    Find unprocessed event logs from the last N hours.
    OPTIMIZED: Read S3 partitions for last N hours, load to memory, anti-join with backlog.

    Efficient logic:
      1. Read S3 advisor data for last N hours (from datehour partitions)
         - Read: JSON files from partitions matching lookback window
         - Extract: app_id_hash values from records
         - Result: ~100-1000 records loaded to memory
      2. Query Iceberg backlog for last N hours
         - Result: ~100-1000 candidates
      3. Anti-join in memory (both datasets already loaded)

    This reads ONLY relevant S3 partitions (matching the time window)!

    Args:
        spark: SparkSession
        backlog_table: Backlog table name (columns: uuid, app_id_hash, s3path, is_processed, ...)
        s3_advisor_path: S3 path for advisor data (partitioned by datehour)
        lookback_hours: Hours to look back for new logs
        limit: Max number of logs to process (None = no limit)

    Returns:
        list: List of dicts with event log metadata
    """
    print("=" * 80)
    print("FINDING UNPROCESSED EVENT LOGS (OPTIMIZED S3 SCAN)")
    print("=" * 80)
    print(f"Backlog Table: {backlog_table} (Iceberg)")
    print(f"S3 Advisor Path: {s3_advisor_path}")
    print(f"Lookback Window: {lookback_hours} hour(s)")
    print(f"Strategy: Read S3 partitions (last {lookback_hours}h), load to memory, anti-join")
    if limit:
        print(f"Limit: {limit} logs (FOR TESTING)")
    print("-" * 80)

    try:
        # STEP 1: Read S3 advisor data for records from last N hours
        print(f"STEP 1: Reading S3 advisor data for last {lookback_hours} hours...")
        processed_hashes = get_processed_hashes_from_s3(s3_advisor_path, lookback_hours)
        print(f"✓ Loaded {len(processed_hashes):,} processed app_id_hash values to memory")
        print("-" * 80)

        # STEP 2: Query Iceberg backlog table for recent unprocessed logs (last N hours)

        # Calculate time window for discovery
        now = datetime.now(timezone.utc)
        cutoff_time = now - timedelta(hours=lookback_hours)
        cutoff_date = cutoff_time.date()
        cutoff_hour = cutoff_time.hour

        print(f"STEP 2: Querying Iceberg backlog table...")
        print(f"Current time (UTC): {now.isoformat()}")
        print(f"Cutoff time: {cutoff_time.isoformat()}")
        print(f"Filtering for logs discovered after: {cutoff_date} hour {cutoff_hour}")
        print("-" * 80)

        # Query backlog table for recent unprocessed logs
        query = f"""
            SELECT
                uuid,
                application_id,
                app_id_hash,
                s3path,
                file_size,
                discovery_date,
                discovery_hour,
                created_at
            FROM {backlog_table}
            WHERE is_processed = 'N'
              AND app_id_hash IS NOT NULL
              AND (
                  discovery_date > DATE('{cutoff_date}')
                  OR (discovery_date = DATE('{cutoff_date}') AND discovery_hour >= {cutoff_hour})
              )
            ORDER BY discovery_date ASC, discovery_hour ASC, created_at ASC
        """

        df = spark.sql(query)
        backlog_records = df.collect()
        print(f"✓ Found {len(backlog_records):,} candidate logs in backlog (is_processed='N', last {lookback_hours}h)")
        print("-" * 80)

        if not backlog_records:
            print("No candidate logs found in backlog for the specified time window")
            print("=" * 80)
            return []

        # STEP 3: Anti-join in memory to find unprocessed logs
        print(f"STEP 3: Performing anti-join in memory...")
        print(f"  Backlog candidates: {len(backlog_records):,}")
        print(f"  S3 processed: {len(processed_hashes):,}")
        unprocessed_logs = []
        skipped_already_processed = 0

        for row in backlog_records:
            app_id_hash = row.app_id_hash

            # Anti-join: Skip if app_id_hash exists in S3 advisor data
            if app_id_hash in processed_hashes:
                skipped_already_processed += 1
                continue

            unprocessed_logs.append({
                "uuid": row.uuid,
                "application_id": row.application_id,
                "app_id_hash": row.app_id_hash,
                "s3path": row.s3path,
                "file_size": row.file_size,
                "discovery_date": row.discovery_date,
                "discovery_hour": row.discovery_hour,
                "created_at": row.created_at
            })

            # Apply limit if specified (for testing)
            if limit and len(unprocessed_logs) >= limit:
                break

        print("-" * 80)
        print(f"✓ Unprocessed logs (NOT in advisor table): {len(unprocessed_logs):,}")
        print(f"✓ Skipped (already in advisor table): {skipped_already_processed:,}")
        if limit and len(unprocessed_logs) >= limit:
            print(f"⚠ LIMITED to {limit} logs for testing")
        print("=" * 80)

        # Show sample
        if unprocessed_logs:
            print("\nSample unprocessed logs (first 5):")
            for i, log in enumerate(unprocessed_logs[:5], 1):
                size_mb = (log['file_size'] or 0) / (1024 ** 2)
                print(f"  {i}. {log['application_id']}")
                print(f"     UUID: {log['uuid']}")
                print(f"     S3: {log['s3path']}")
                print(f"     Size: {size_mb:.2f} MB")
            print()

        return unprocessed_logs

    except Exception as e:
        print(f"❌ ERROR finding unprocessed logs: {e}")
        import traceback
        traceback.print_exc()
        raise


# ============================================================================
# EMR Serverless Job Submission
# ============================================================================

def submit_emr_serverless_job(emr_client, application_id, execution_role, job_name, log_batch):
    """
    Submit a Spark job to EMR Serverless for processing a batch of event logs (up to 5).
    Resource allocation is optimized based on total batch size.

    Args:
        emr_client: Boto3 EMR Serverless client
        application_id: EMR Serverless application ID
        execution_role: IAM role ARN
        job_name: Name for the job
        log_batch: List of event log dicts (up to 5 logs per batch)

    Returns:
        str: Job run ID if successful, None otherwise
    """
    try:
        # Calculate total batch size from all logs
        total_batch_size = sum(log.get('file_size', 0) for log in log_batch)
        batch_size_gb = total_batch_size / (1024 ** 3)
        num_logs = len(log_batch)

        # Dynamic resource allocation based on batch size
        # CRITICAL: Ensure minExecutors <= initialExecutors <= maxExecutors
        #
        # Executor Config: 2 vCPU + 8GB per executor (custom, not default)
        # SMALL batches (< 1 GB): max_executors=3 → 10 vCPU/job max
        # LARGE batches (>= 1 GB): max_executors=10 → 24 vCPU/job max
        #
        # Capacity calculation:
        #   SMALL: 1 driver (4 vCPU, 16GB) + 3 executors (6 vCPU, 24GB) = 10 vCPU, 40GB
        #   LARGE: 1 driver (4 vCPU, 16GB) + 10 executors (20 vCPU, 80GB) = 24 vCPU, 96GB

        # Scale executors based on batch size
        if batch_size_gb < 1.0:
            resource_profile = "SMALL"
            min_executors = 2
            initial_executors = 2
            max_executors = 3
        else:
            resource_profile = "LARGE"
            min_executors = 2
            initial_executors = 2
            max_executors = 10  # Scale up for large batches

        # Validate configuration before building parameters
        if not (min_executors <= initial_executors <= max_executors):
            error_msg = (
                f"ERROR: Invalid executor configuration for {job_name}\n"
                f"  Batch size: {batch_size_gb:.2f} GB ({num_logs} logs)\n"
                f"  Profile: {resource_profile}\n"
                f"  Config: min={min_executors}, initial={initial_executors}, max={max_executors}\n"
                f"  Constraint violated: {min_executors} <= {initial_executors} <= {max_executors}"
            )
            print(error_msg)
            return None

        # Build Spark submit parameters with dynamic resource allocation
        # IMPORTANT: initialExecutors must satisfy: minExecutors <= initialExecutors <= maxExecutors
        spark_submit_parameters = (
            # Iceberg Runtime JAR (pre-installed on EMR Serverless)
            "--conf spark.jars=/usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar "

            # Executor Resource Configuration (Reduced to fit more jobs in CPU capacity)
            # Per executor: 2 vCPU + 8GB (instead of default 4 vCPU + 16GB)
            # Per job CPU: 1 driver (4 vCPU) + 2 executors (4 vCPU) = 8 vCPU at startup
            # Concurrent capacity: 5000 vCPU / 8 vCPU = ~625 concurrent jobs
            "--conf spark.executor.cores=2 "
            "--conf spark.executor.memory=8g "

            # Dynamic Allocation (unified configuration: min=2, initial=2, max=3)
            # CRITICAL: spark.executor.instances should NOT be set when using dynamic allocation
            # Constraint validated above: minExecutors <= initialExecutors <= maxExecutors
            "--conf spark.dynamicAllocation.enabled=true "
            f"--conf spark.dynamicAllocation.minExecutors={min_executors} "
            f"--conf spark.dynamicAllocation.initialExecutors={initial_executors} "
            f"--conf spark.dynamicAllocation.maxExecutors={max_executors} "
            "--conf spark.dynamicAllocation.executorIdleTimeout=60s "

            # Python virtual environment with Python 3.11 (built with GLIBC 2.31 - Ubuntu 20.04)
            # Archive extracts to /home/hadoop/venv/, includes Python 3.11 binary
            f"--conf spark.archives={S3_DEPENDENCIES_PATH}#venv "
            "--conf spark.emr-serverless.driverEnv.PYSPARK_DRIVER_PYTHON=/home/hadoop/venv/bin/python3.11 "
            "--conf spark.emr-serverless.driverEnv.PYSPARK_PYTHON=/home/hadoop/venv/bin/python3.11 "
            "--conf spark.executorEnv.PYSPARK_PYTHON=/home/hadoop/venv/bin/python3.11 "
            "--conf spark.yarn.appMasterEnv.PYSPARK_PYTHON=/home/hadoop/venv/bin/python3.11 "

            # Iceberg catalog configurations
            "--conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions "
            "--conf spark.sql.catalog.iceberg=org.apache.iceberg.spark.SparkCatalog "
            "--conf spark.sql.catalog.spark_catalog=org.apache.iceberg.spark.SparkSessionCatalog "
            "--conf spark.sql.catalog.spark_catalog.type=hive "
            f"--conf spark.sql.catalog.spark_catalog.warehouse=s3://{S3_BUCKET}/iceberg/ "

            # Hadoop S3A configurations (from working config)
            "--conf spark.hadoop.fs.s3a.impl=org.apache.hadoop.fs.s3a.S3AFileSystem "
            "--conf spark.hadoop.fs.s3a.connection.ssl.enabled=true "
            "--conf spark.hadoop.fs.s3a.enableServerSideEncryption=true "
            "--conf spark.hadoop.fs.s3a.server-side-encryption-algorithm=AES256 "
            "--conf spark.hadoop.fs.s3a.retry.limit=10 "
            "--conf spark.hadoop.fs.s3a.retry.interval=1000 "
            "--conf spark.hadoop.fs.s3a.attempts.maximum=10 "
            "--conf spark.hadoop.fs.s3a.threads.max=10 "
            "--conf spark.hadoop.fs.s3a.threads.core=10 "
            "--conf spark.hadoop.fs.s3a.threads.keepalivetime=60 "
            "--conf spark.hadoop.fs.s3a.socket.timeout=60000 "
            "--conf spark.hadoop.fs.s3a.connection.timeout=60000 "
            "--conf spark.hadoop.fs.s3a.connection.maximum=60000 "
            "--conf spark.hadoop.fs.s3a.connection.request.timeout=60000 "
            "--conf spark.hadoop.fs.s3a.connection.establish.timeout=60000 "

            # Hive configurations (from working config)
            "--conf spark.hadoop.hive.exec.dynamic.partition=true "
            "--conf spark.hadoop.hive.exec.dynamic.partition.mode=nonstrict "
            "--conf spark.hadoop.hive.metastore.failure.retries=5 "
            "--conf spark.hadoop.hive.metastore.client.socket.timeout=6000 "
            "--conf spark.hadoop.hive.metastore.client.connect.retry.delay=15 "

            # Arrow optimization (from working config)
            "--conf spark.sql.execution.arrow.pyspark.enabled=true "

            # Other optimizations (from working config)
            "--conf spark.rpc.message.maxSize=256 "
            "--conf spark.sql.session.timeZone=UTC "
            "--conf spark.sql.sources.partitionOverwriteMode=dynamic "

            # Hive support
            "--conf spark.sql.catalogImplementation=hive"
        )

        # Job arguments - pass batch information as JSON
        # Convert batch to JSON string for passing to EMR Serverless job
        import json as json_module
        batch_json = json_module.dumps([{
            'uuid': log['uuid'],
            's3path': log['s3path'],
            'application_id': log['application_id'],
            'app_id_hash': log['app_id_hash'],
            'file_size': log.get('file_size', 0)
        } for log in log_batch])

        job_arguments = [
            "--log-batch", batch_json,
            "--s3-bucket", S3_BUCKET,
            "--backlog-table", BACKLOG_TABLE,
            "--advisor-table", S3_ADVISOR_PATH,
            "--s3-scripts-prefix", S3_SCRIPTS_PREFIX
        ]

        # DEBUG: Print the actual spark submit parameters being sent
        print(f"\n  DEBUG: Spark Submit Parameters for {job_name}:")
        print(f"    Batch size: {batch_size_gb:.3f} GB ({num_logs} logs)")
        print(f"    Profile: {resource_profile}")
        print(f"    min_executors: {min_executors}")
        print(f"    initial_executors: {initial_executors}")
        print(f"    max_executors: {max_executors}")

        # Extract just the dynamic allocation configs from spark_submit_parameters
        import re
        for line in spark_submit_parameters.split("--conf"):
            if "dynamicAllocation" in line:
                print(f"    {line.strip()}")

        # Submit job with AWS_REGION environment variable
        response = emr_client.start_job_run(
            applicationId=application_id,
            executionRoleArn=execution_role,
            name=job_name,
            jobDriver={
                'sparkSubmit': {
                    'entryPoint': S3_ENTRYPOINT_SCRIPT,
                    'entryPointArguments': job_arguments,
                    'sparkSubmitParameters': spark_submit_parameters
                }
            },
            configurationOverrides={
                'applicationConfiguration': [
                    {
                        'classification': 'spark-defaults',
                        'properties': {
                            'spark.executorEnv.AWS_REGION': AWS_REGION,
                            'spark.yarn.appMasterEnv.AWS_REGION': AWS_REGION
                        }
                    }
                ],
                'monitoringConfiguration': {
                    's3MonitoringConfiguration': {
                        'logUri': f"s3://{S3_BUCKET}/emr-serverless-logs/"
                    }
                }
            }
        )

        job_run_id = response['jobRunId']
        print(f"  ✓ Job submitted: {job_run_id}")
        print(f"    Profile: {resource_profile} | Batch: {num_logs} logs, {batch_size_gb:.2f}GB total")
        print(f"    Executors: min={min_executors}, initial={initial_executors}, max={max_executors}")

        return job_run_id

    except Exception as e:
        print(f"  ✗ Failed to submit job: {e}")
        print(f"    Batch: {num_logs} logs")
        return None


# Global cache for running job count (prevents API rate limit errors)
_JOB_COUNT_CACHE = {
    'count': 0,
    'timestamp': 0,
    'cache_duration': 300  # Cache for 5 minutes (300 seconds)
}


def get_current_running_jobs(emr_client, application_id):
    """
    Return count of RUNNING, PENDING, or SCHEDULED jobs on EMR Serverless.
    Uses 5-minute cache to avoid TooManyRequestsException API rate limits.
    """
    import time

    current_time = time.time()
    cache_age = current_time - _JOB_COUNT_CACHE['timestamp']

    # Return cached value if less than 5 minutes old
    if cache_age < _JOB_COUNT_CACHE['cache_duration']:
        remaining = int(_JOB_COUNT_CACHE['cache_duration'] - cache_age)
        print(f"  📦 Using cached job count: {_JOB_COUNT_CACHE['count']} (cache refreshes in {remaining}s)")
        return _JOB_COUNT_CACHE['count']

    # Cache expired - fetch fresh count from API
    try:
        print(f"  🔄 Fetching current job count from EMR Serverless API...")
        all_jobs = []
        kwargs = {
            'applicationId': application_id,
            'states': ['RUNNING', 'PENDING', 'SCHEDULED']
        }
        while True:
            response = emr_client.list_job_runs(**kwargs)
            all_jobs.extend(response.get('jobRuns', []))
            if 'nextToken' not in response:
                break
            kwargs['nextToken'] = response['nextToken']

        count = len(all_jobs)

        # Update cache
        _JOB_COUNT_CACHE['count'] = count
        _JOB_COUNT_CACHE['timestamp'] = current_time

        print(f"  ✓ Current running jobs: {count} (cached for 5 minutes)")
        return count

    except Exception as e:
        print(f"  ⚠ Could not get running job count: {e}. Using last cached value: {_JOB_COUNT_CACHE['count']}")
        return _JOB_COUNT_CACHE['count']


def wait_for_capacity(emr_client, application_id, max_concurrent, safe_buffer, check_interval):
    """
    Block until there is at least one free job slot on EMR Serverless.
    Safe limit = max_concurrent - safe_buffer.
    Returns (current_running, available_slots) when a slot is free.
    """
    safe_limit = max_concurrent - safe_buffer
    attempts = 0
    while True:
        current   = get_current_running_jobs(emr_client, application_id)
        available = safe_limit - current
        if available > 0:
            if attempts > 0:
                print(f"  ✓ [{datetime.now().strftime('%H:%M:%S')}] Capacity restored: "
                      f"{current} running, {available} slots free. Resuming submissions.")
            return current, available
        attempts += 1
        print(f"  ⏳ [{datetime.now().strftime('%H:%M:%S')}] At capacity: {current}/{safe_limit} active jobs "
              f"(max={max_concurrent}, buffer={safe_buffer}). "
              f"Waiting {check_interval}s... (attempt {attempts})")
        time.sleep(check_interval)


def submit_jobs_for_unprocessed_logs(unprocessed_logs, application_id, execution_role, max_concurrent):
    """
    Submit EMR Serverless jobs for all unprocessed event logs.
    Each job processes a batch of 5 event logs (or fewer for the last batch).

    Args:
        unprocessed_logs: List of event log dicts
        application_id: EMR Serverless application ID
        execution_role: IAM role ARN
        max_concurrent: Maximum number of concurrent job submissions

    Returns:
        dict: Statistics about job submissions
    """
    LOGS_PER_JOB = 5  # Each EMR Serverless job processes 5 event logs

    # Group logs into batches of 5
    log_batches = []
    for i in range(0, len(unprocessed_logs), LOGS_PER_JOB):
        batch = unprocessed_logs[i:i + LOGS_PER_JOB]
        log_batches.append(batch)

    print("=" * 80)
    print("SUBMITTING EMR SERVERLESS JOBS (BATCH MODE)")
    print("=" * 80)
    print(f"Total logs to process: {len(unprocessed_logs):,}")
    print(f"Logs per job: {LOGS_PER_JOB}")
    print(f"Total jobs to submit: {len(log_batches):,}")
    print(f"Max concurrent submissions: {max_concurrent}")
    print(f"Capacity-Aware Throttling: Check live running job count before each submission")
    print(f"  Safe limit: {CAPACITY_MAX_CONCURRENT - CAPACITY_SAFE_BUFFER} concurrent jobs "
          f"(max={CAPACITY_MAX_CONCURRENT}, buffer={CAPACITY_SAFE_BUFFER})")
    print(f"  Wait interval: {CAPACITY_CHECK_INTERVAL}s when cluster is full")
    print(f"  API cache: 5 minutes (prevents TooManyRequestsException)")
    print(f"  API rate-limit pause: {SUBMISSION_DELAY_SECONDS}s every 100 submissions")
    print(f"Application ID: {application_id}")
    print("-" * 80)

    emr_client = boto3.client('emr-serverless', region_name=AWS_REGION)

    # Generate batch name ONCE for all jobs in this orchestrator run
    # Format: serverless_config_advisor_pipeline_{yyyymmddhhmm}
    # Using IST timezone (UTC+5:30) for easy identification in India
    # All jobs in same orchestrator run share this exact base name
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    batch_timestamp = datetime.now(ist_tz).strftime("%Y%m%d%H%M")  # 4-digit year
    batch_name_base = f"serverless_config_advisor_pipeline_{batch_timestamp}"

    print(f"Batch Name: {batch_name_base} (IST)")
    print(f"  All jobs will use this base name with job index suffix")
    print(f"  This helps identify which jobs belong to this orchestrator run")
    print("-" * 80)

    submitted_jobs = []
    failed_submissions = []

    # Track resource profile distribution
    small_jobs_count = 0  # < 1 GB
    large_jobs_count = 0  # >= 1 GB
    total_logs_in_submitted_jobs = 0

    for idx, log_batch in enumerate(log_batches, 1):
        # Use batch name with job index for easy grouping and identification
        # Format: serverless_config_advisor_pipeline_{yyyymmddhhmm}_Job_{number}
        job_name = f"{batch_name_base}_Job_{idx:04d}"

        # Calculate batch info for logging
        batch_size = sum(log.get('file_size', 0) for log in log_batch)
        batch_size_gb = batch_size / (1024 ** 3)
        app_ids = ', '.join([log['application_id'][:20] for log in log_batch])

        # Wait for capacity before submitting — prevents CPU capacity errors on EMR Serverless
        current_running, available_slots = wait_for_capacity(
            emr_client, application_id, CAPACITY_MAX_CONCURRENT, CAPACITY_SAFE_BUFFER, CAPACITY_CHECK_INTERVAL
        )

        print(f"[{idx}/{len(log_batches)}] Submitting job for batch of {len(log_batch)} logs...")
        print(f"                   Job name: {job_name}")
        print(f"                   Batch size: {batch_size_gb:.2f} GB")
        print(f"                   Cluster: {current_running} running, {available_slots} slots free")
        print(f"                   App IDs: {app_ids}...")

        job_run_id = submit_emr_serverless_job(
            emr_client,
            application_id,
            execution_role,
            job_name,
            log_batch
        )

        # Track resource profile distribution
        if batch_size_gb < 1.0:
            small_jobs_count += 1
        else:
            large_jobs_count += 1

        if job_run_id:
            total_logs_in_submitted_jobs += len(log_batch)
            submitted_jobs.append({
                'job_run_id': job_run_id,
                'job_name': job_name,
                'log_batch': log_batch,
                'batch_size': batch_size,
                'num_logs': len(log_batch)
            })
        else:
            failed_submissions.append({
                'job_name': job_name,
                'log_batch': log_batch,
                'num_logs': len(log_batch)
            })

        # Brief API rate-limit pause every 100 submissions
        # (capacity is already managed by wait_for_capacity above — no blind long sleeps needed)
        if len(submitted_jobs) > 0 and len(submitted_jobs) % 100 == 0 and idx < len(log_batches):
            remaining = len(log_batches) - idx
            print(f"\n⏸ API checkpoint: {len(submitted_jobs)} jobs submitted, {remaining} batches remaining")
            print(f"  Brief {SUBMISSION_DELAY_SECONDS}s pause to avoid API rate limits...")
            time.sleep(SUBMISSION_DELAY_SECONDS)
            print(f"  Resuming...\n")

        # Check if we've hit the total jobs per run cap
        if len(submitted_jobs) >= MAX_JOBS_PER_RUN and idx < len(log_batches):
            print(f"\n⚠ Reached MAX_JOBS_PER_RUN limit ({MAX_JOBS_PER_RUN})")
            print(f"  Submitted {len(submitted_jobs)} jobs so far")
            print(f"  Remaining batches: {len(log_batches) - idx}")
            print(f"  To submit more, increase MAX_JOBS_PER_RUN or run again next hour\n")
            break

    # Calculate failed logs
    total_failed_logs = sum(fs['num_logs'] for fs in failed_submissions)

    print("-" * 80)
    print(f"Batch Name: {batch_name_base}")
    print(f"✓ Successfully submitted: {len(submitted_jobs):,} jobs (processing {total_logs_in_submitted_jobs:,} logs)")
    print(f"✗ Failed submissions: {len(failed_submissions):,} jobs ({total_failed_logs:,} logs affected)")
    print(f"\n📊 Batch Size Distribution:")
    print(f"   SMALL batches (< 1 GB):  {small_jobs_count:,} ({small_jobs_count*100/len(log_batches):.1f}%)")
    print(f"   LARGE batches (>= 1 GB): {large_jobs_count:,} ({large_jobs_count*100/len(log_batches):.1f}%)")
    print(f"\n📦 Batching Strategy:")
    print(f"   Logs per job: {LOGS_PER_JOB}")
    print(f"   Total logs: {len(unprocessed_logs):,}")
    print(f"   Total jobs: {len(log_batches):,}")
    print(f"   Processing capacity: {len(log_batches) * LOGS_PER_JOB:,} logs (max)")
    print(f"\n💡 Resource Configuration (Unified for All Jobs):")
    print(f"   Application: CPU=5000, Memory=30000GB, Disk=40000GB")
    print(f"   Executor size: 2 vCPU + 8GB (custom)")
    print(f"   Per job config: min=2, initial=2, max=3 executors")
    print(f"   Per job at startup: 8 vCPU + 32 GB (1 driver + 2 executors)")
    print(f"   Estimated concurrent capacity: ~625 jobs (5000 vCPU / 8 vCPU per job)")
    print(f"   With {LOGS_PER_JOB} logs/job: ~{625 * LOGS_PER_JOB} logs processing concurrently")
    if len(submitted_jobs) > 0:
        print(f"\n💡 To track these jobs in EMR Console:")
        print(f"   Filter by job name prefix: {batch_name_base}")
        print(f"   All jobs: {batch_name_base}_Job_0001 to {batch_name_base}_Job_{len(submitted_jobs):04d}")
    print("=" * 80)

    return {
        'submitted': len(submitted_jobs),
        'failed': len(failed_submissions),
        'total_logs_submitted': total_logs_in_submitted_jobs,
        'total_logs_failed': total_failed_logs,
        'submitted_jobs': submitted_jobs,
        'failed_submissions': failed_submissions,
        'batch_name': batch_name_base,
        'small_jobs': small_jobs_count,
        'large_jobs': large_jobs_count,
        'logs_per_job': LOGS_PER_JOB
    }


# ============================================================================
# Main Execution
# ============================================================================

def main():
    """Main orchestrator execution."""
    start_time = datetime.now(timezone.utc)

    print("\n" + "=" * 80)
    print("EMR SERVERLESS JOB SUBMISSION ORCHESTRATOR")
    print("=" * 80)
    print(f"Start Time: {start_time.isoformat()}")
    print(f"AWS Region: {AWS_REGION}")
    print(f"S3 Bucket: {S3_BUCKET}")
    print(f"EMR Serverless Application: {EMR_SERVERLESS_APPLICATION_NAME}")
    print(f"Application ID: {EMR_SERVERLESS_APPLICATION_ID}")
    print(f"Python Venv: {S3_DEPENDENCIES_PATH}")
    print(f"Backlog Table: {BACKLOG_TABLE}")
    print(f"S3 Advisor Path: {S3_ADVISOR_PATH}")
    print(f"Lookback Window: {LOOKBACK_HOURS} hour(s)")
    print("=" * 80)

    spark = None

    try:
        # Step 1: Initialize Spark
        spark = get_spark_session()

        # Step 2: Find unprocessed event logs
        limit = TEST_LIMIT if TEST_LIMIT > 0 else None
        unprocessed_logs = get_unprocessed_event_logs(
            spark,
            BACKLOG_TABLE,
            S3_ADVISOR_PATH,
            LOOKBACK_HOURS,
            limit=limit
        )

        if not unprocessed_logs:
            print("\n✓ No unprocessed event logs found")
            print("  All recent logs have already been processed")
            print("=" * 80)
            return 0

        # Step 3: Submit EMR Serverless jobs
        submission_stats = submit_jobs_for_unprocessed_logs(
            unprocessed_logs,
            EMR_SERVERLESS_APPLICATION_ID,
            EMR_SERVERLESS_EXECUTION_ROLE,
            MAX_CONCURRENT_JOBS
        )

        # Calculate duration
        end_time = datetime.now(timezone.utc)
        duration = (end_time - start_time).total_seconds()

        print("\n" + "=" * 80)
        print("✅ ORCHESTRATOR COMPLETED SUCCESSFULLY")
        print("=" * 80)
        print(f"Start Time:         {start_time.isoformat()}")
        print(f"End Time:           {end_time.isoformat()}")
        print(f"Duration:           {duration:.1f} seconds")
        print(f"Batch Name:         {submission_stats.get('batch_name', 'N/A')}")
        print(f"Unprocessed Logs:   {len(unprocessed_logs):,}")
        print(f"Jobs Submitted:     {submission_stats['submitted']:,}")
        print(f"Failed Submissions: {submission_stats['failed']:,}")
        if submission_stats['submitted'] > 0:
            print(f"\n💡 Track jobs in EMR Console using batch name:")
            print(f"   {submission_stats.get('batch_name', 'N/A')}_Job_****")
        print("=" * 80)

        # Display all submitted jobs in a table format
        submitted_jobs = submission_stats.get('submitted_jobs', [])
        if submitted_jobs:
            print("\n" + "=" * 80)
            print("SUBMITTED JOBS SUMMARY (BATCH MODE)")
            print("=" * 80)
            print(f"{'#':<4} {'Job Name':<50} {'Logs':<6} {'Batch Size':<12}")
            print("-" * 80)
            for idx, job in enumerate(submitted_jobs, 1):
                job_name = job['job_name'][:48] + '..' if len(job['job_name']) > 50 else job['job_name']
                num_logs = job.get('num_logs', 0)
                batch_size_gb = job.get('batch_size', 0) / (1024 ** 3)
                print(f"{idx:<4} {job_name:<50} {num_logs:<6} {batch_size_gb:<10.2f} GB")

                # Show the app IDs in this batch
                if idx <= 5:  # Show first 5 jobs with details
                    log_batch = job.get('log_batch', [])
                    for log_idx, log in enumerate(log_batch[:3], 1):  # Show first 3 logs per batch
                        app_id = log.get('application_id', 'unknown')[:40]
                        print(f"       └─ Log {log_idx}: {app_id}")
                    if len(log_batch) > 3:
                        print(f"       └─ ... and {len(log_batch) - 3} more log(s)")

            if len(submitted_jobs) > 5:
                print(f"     ... and {len(submitted_jobs) - 5} more job(s)")

            print("=" * 80)
            print(f"Total Submitted Jobs: {len(submitted_jobs)}")
            print(f"Total Logs Processing: {submission_stats.get('total_logs_submitted', 0)}")
            print("=" * 80)

        # Display failed submissions if any
        failed_submissions = submission_stats.get('failed_submissions', [])
        if failed_submissions:
            print("\n" + "=" * 80)
            print("FAILED SUBMISSIONS (BATCH MODE)")
            print("=" * 80)
            for idx, fail in enumerate(failed_submissions, 1):
                job_name = fail.get('job_name', 'unknown')
                num_logs = fail.get('num_logs', 0)
                print(f"{idx}. Job Name: {job_name}")
                print(f"   Logs in batch: {num_logs}")

                # Show the logs in failed batch
                log_batch = fail.get('log_batch', [])
                for log_idx, log in enumerate(log_batch[:5], 1):
                    app_id = log.get('application_id', 'unknown')
                    print(f"     {log_idx}. {app_id}")
                if len(log_batch) > 5:
                    print(f"     ... and {len(log_batch) - 5} more log(s)")
            print("=" * 80)

        return 0

    except Exception as e:
        print("\n" + "=" * 80)
        print("❌ ORCHESTRATOR FAILED")
        print("=" * 80)
        print(f"Error: {e}")
        print("=" * 80)
        import traceback
        traceback.print_exc()
        return 1

    finally:
        if spark:
            print("\nStopping Spark session...")
            spark.stop()
            print("✓ Spark session stopped")


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
