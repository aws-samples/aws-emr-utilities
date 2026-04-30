"""
EMR Serverless Configuration Advisor Pipeline
=============================================
4-Step PySpark Pipeline for generating EMR Serverless recommendations:

  Step 1: Extract Spark Metrics
    - Reads Spark event logs from S3
    - Extracts metrics (task_stage_summary, spark_config_extract)
    - Outputs JSON files to S3
    - Returns: List of processed job_ids

  Coverage Analysis (runs AFTER Step 1):
    - Compares processed applications vs. all available applications in S3
    - Identifies skipped/missed applications
    - Outputs: coverage_analysis_{timestamp}.json with metrics:
      * Total available applications
      * Total processed applications
      * Coverage percentage
      * List of skipped applications (sample)

  Step 2: Load Metrics to Iceberg (Optional)
    - Reads extracted JSON files from S3
    - Loads data into Iceberg tables (DW VERSION):
      * data_processing.spark_metrics_task_stage_v2
      * data_processing.spark_metrics_config_v2

  Step 3: Generate Recommendations from Iceberg
    - Reads RECENTLY LOADED applications from Iceberg tables
    - Generates cost & performance optimized configurations
    - Outputs: recommendations_cost_optimized.json

  Step 4: Write to serverless_config_advisor Table
    - Reads cost recommendations from Step 3
    - Joins with metrics extracts
    - Writes final recommendations to: data_processing.serverless_config_advisor_v2 (DW VERSION)

Configuration:
  All settings are in lines 34-68 of this file. Update once and all steps use the values.
  Can also override via environment variables (e.g., export S3_BUCKET="...")
"""

import os
import subprocess
import datetime
import shutil
import importlib.util
import sys
from pyspark.sql import SparkSession
from pyspark.conf import SparkConf

# Pre-import dependencies so dynamically loaded modules can find them
# This ensures boto3, zstandard, etc. are in sys.modules before importlib loads sub-modules
try:
    import boto3
    import zstandard
    import pandas
    import pyarrow
except ImportError as e:
    print(f"WARNING: Failed to import dependency: {e}")
    print("Dynamically loaded modules may fail if they need these dependencies")

# Import centralized logging and timestamp utilities
try:
    from logging_config import setup_logging, get_execution_timestamp, get_execution_date
    import logging
    logger = setup_logging(name="SparkMetricsPipeline")
    TIMESTAMP = get_execution_timestamp()
    EXECUTION_DATE = get_execution_date()
except ImportError:
    import logging
    import time
    logging.basicConfig(
        format="%(asctime)s UTC %(levelname)-5s [%(name)s] %(message)s",
        datefmt='%Y-%m-%d %H:%M:%S',
        level=logging.INFO,
    )
    logging.Formatter.converter = time.gmtime
    logger = logging.getLogger("SparkMetricsPipeline")
    TIMESTAMP = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    EXECUTION_DATE = None

################################################################################
# ==================== ENVIRONMENT CONFIGURATION ====================
# UPDATE THESE VALUES FOR YOUR AWS ACCOUNT/ENVIRONMENT
# OR override via environment variables (e.g., export S3_BUCKET="...")
################################################################################

# ========== REQUIRED: AWS S3 Configuration ==========
# Your S3 bucket where Spark event logs are stored
# Format: spark-history-<account-id>-<region>
# Example: "spark-history-123456789012-us-east-1"
S3_BUCKET = os.getenv("S3_BUCKET", "YOUR_S3_BUCKET_HERE")

# S3 prefix where pipeline scripts (this file and dependencies) are stored
# This script is specifically for LARGE event logs (>=1GB)
# Example: "pipeline-files/backlog-enabled-large"
S3_SCRIPTS_PREFIX = os.getenv("S3_SCRIPTS_PREFIX", "pipeline-files/backlog-enabled-large")

# ========== S3 Output Paths ==========
# S3 prefix for output files (metrics and recommendations)
# Example: "target-metrics/recommendations"
S3_PREFIX = os.getenv("S3_PREFIX", "target-metrics/recommendations")

# ========== Iceberg Table Configuration ==========
# Backlog tracking table (must be created beforehand)
BACKLOG_TABLE = os.getenv("BACKLOG_TABLE", "data_processing.backlog_events_log")

# Final recommendations output table
ICEBERG_TABLE = os.getenv("ICEBERG_TABLE", "data_processing.serverless_config_advisor_v2")

# Iceberg warehouse S3 location (auto-derived from S3_BUCKET if not changed)
ICEBERG_WAREHOUSE = os.getenv("ICEBERG_WAREHOUSE", f"s3://{S3_BUCKET}/iceberg/")

# ========== Processing Configuration ==========
# IMPORTANT: This script is for LARGE event logs (>=1GB)
# Use small batch size to avoid memory issues
# Recommended: 5 for large files, 500 for regular files (use other orchestrator)
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))

# Maximum number of local run directories to keep (automatic cleanup)
MAX_RUNS_KEPT = int(os.getenv("MAX_RUNS_KEPT", "24"))

# Processing instance ID (auto-generated with timestamp if not set)
PROCESSING_INSTANCE_ID = os.getenv("PROCESSING_INSTANCE_ID", f"pipeline-{TIMESTAMP}")

# ========== Optional: AWS Settings ==========
# AWS CLI profile name (leave as None to use default credentials or instance role)
AWS_PROFILE = os.getenv("AWS_PROFILE", None)

# ========== Advanced: Processing Filters (Optional) ==========
# Number of recent files to process (0 = use backlog table, N = process last N files)
NUM_RECENT_FILES = int(os.getenv("NUM_RECENT_FILES", "0"))

# Process specific application ID only (leave empty for batch processing)
SPECIFIC_APPLICATION_ID = ""

# ========== Validation ==========
if S3_BUCKET == "YOUR_S3_BUCKET_HERE" or not S3_BUCKET:
    raise ValueError(
        "\n" + "="*80 + "\n"
        "ERROR: S3_BUCKET must be configured!\n"
        "This is the LARGE FILE orchestrator (for event logs >=1GB).\n"
        "Please edit line 95 and set your S3 bucket name.\n"
        "Example: S3_BUCKET = 'spark-history-123456789012-us-east-1'\n"
        "="*80
    )

################################################################################
# END OF USER CONFIGURATION
# Do not modify below this line unless you know what you're doing
################################################################################

# Auto-derived settings
OUTPUT_DIR = os.path.join("output", TIMESTAMP)
CWD = os.getcwd()

# Local output directory (auto-generated with timestamp)
OUTPUT_DIR = os.path.join("output", TIMESTAMP)

# Current working directory (where pipeline files will be downloaded)
CWD = os.getcwd()

# Pipeline files directory (current working directory for downloads)
PIPELINE_FILES_DIR = CWD

################################################################################
# END CONFIGURATION SECTION
################################################################################

# Log execution context
if EXECUTION_DATE:
    logger.info(f"Using Airflow execution date for timestamps: {EXECUTION_DATE}")
logger.info(f"Pipeline timestamp: {TIMESTAMP}")
logger.info(f"Output directory: {OUTPUT_DIR}")


def get_spark_session(app_name="SparkMetricsPipeline"):
    """
    Initialize and return a SparkSession with S3 configurations.
    """
    conf = SparkConf()

    # Increase RPC message size to handle large serialized tasks (default is 128MB)
    # This must be set before SparkSession creation
    conf.set("spark.rpc.message.maxSize", "512")  # 512 MB

    # S3A Configuration - Fix for "No file: /mnt1/yarn/.../s3ablock-*.tmp" error
    # Use array buffer instead of disk to avoid temp file deletion issues
    conf.set("spark.hadoop.fs.s3a.fast.upload", "true")
    conf.set("spark.hadoop.fs.s3a.fast.upload.buffer", "array")  # Use memory instead of disk
    conf.set("spark.hadoop.fs.s3a.fast.upload.active.blocks", "8")  # Number of blocks for parallel uploads

    # S3A Block size and multipart upload settings
    conf.set("spark.hadoop.fs.s3a.block.size", "128M")
    conf.set("spark.hadoop.fs.s3a.multipart.size", "104857600")  # 100MB per part
    conf.set("spark.hadoop.fs.s3a.multipart.threshold", "52428800")  # 50MB threshold

    # S3A Connection and retry settings
    conf.set("spark.hadoop.fs.s3a.connection.maximum", "100")
    conf.set("spark.hadoop.fs.s3a.threads.max", "50")
    conf.set("spark.hadoop.fs.s3a.connection.establish.timeout", "30000")
    conf.set("spark.hadoop.fs.s3a.connection.timeout", "600000")
    conf.set("spark.hadoop.fs.s3a.attempts.maximum", "20")
    conf.set("spark.hadoop.fs.s3a.retry.limit", "15")
    conf.set("spark.hadoop.fs.s3a.retry.interval", "500ms")

    # S3A credentials provider configuration
    conf.set("spark.hadoop.fs.s3a.aws.credentials.provider",
             "com.amazonaws.auth.InstanceProfileCredentialsProvider,com.amazonaws.auth.DefaultAWSCredentialsProviderChain")

    # Disable S3A filesystem cache to prevent credential issues
    conf.set("spark.hadoop.fs.s3a.impl.disable.cache", "false")

    # Event logging configuration - use array buffer for event logs too
    conf.set("spark.eventLog.enabled", "true")
    conf.set("spark.eventLog.compress", "true")
    conf.set("spark.eventLog.compression.codec", "zstd")

    spark = SparkSession.builder \
        .appName(app_name) \
        .config(conf=conf) \
        .enableHiveSupport() \
        .getOrCreate()

    # Suppress OpenLineage warnings (not critical for our pipeline)
    spark.sparkContext.setLogLevel("WARN")
    spark_logger = spark._jvm.org.apache.log4j.LogManager.getLogger("org.apache.spark.sql.execution.openlineage")
    spark_logger.setLevel(spark._jvm.org.apache.log4j.Level.ERROR)

    return spark


def log(spark, message):
    """
    Log messages with timestamp. Uses both Python logging and Spark's log4j.
    Includes Airflow execution date context when available.
    """
    # Use centralized logger
    logger.info(message)

    # Also log to Spark's log4j logger if available
    try:
        spark.sparkContext.setLocalProperty("callSite.short", message[:50])
        log4j = spark._jvm.org.apache.log4j
        spark_logger = log4j.LogManager.getLogger("SparkMetricsPipeline")

        # Include execution date in Spark logs if available
        if EXECUTION_DATE:
            log_message = f"[ExecutionDate: {EXECUTION_DATE}] {message}"
        else:
            log_message = message
        spark_logger.info(log_message)
    except Exception:
        pass  # Fall back to Python logging only


def run_command(command, step_name, spark=None):
    """
    Execute a shell command and log the results.
    """
    if spark:
        log(spark, f"Starting {step_name}...")
    else:
        logger.info(f"Starting {step_name}...")

    start_time = datetime.datetime.now()
    try:
        subprocess.run(command, check=True, shell=True)
        elapsed = (datetime.datetime.now() - start_time).seconds
        if spark:
            log(spark, f"{step_name} completed in {elapsed}s")
        else:
            logger.info(f"{step_name} completed in {elapsed}s")
    except subprocess.CalledProcessError as e:
        error_msg = f"FATAL: {step_name} failed with exit code {e.returncode}"
        if spark:
            log(spark, error_msg)
        else:
            logger.error(error_msg)
        raise


def download_file_from_s3_spark(spark, s3_path, dest_path):
    """
    Download a file from S3 using Spark's Hadoop filesystem.
    """
    log(spark, f"Downloading {s3_path} to {dest_path} using Spark Hadoop FS...")
    try:
        # Convert s3:// to s3a:// for Spark compatibility
        s3a_path = s3_path.replace("s3://", "s3a://")

        # Use Hadoop FileSystem API to copy file
        hadoop_conf = spark._jsc.hadoopConfiguration()
        fs = spark._jvm.org.apache.hadoop.fs.FileSystem.get(
            spark._jvm.java.net.URI(s3a_path), hadoop_conf
        )

        # Create local file path
        local_path = spark._jvm.org.apache.hadoop.fs.Path(dest_path)
        remote_path = spark._jvm.org.apache.hadoop.fs.Path(s3a_path)

        # Copy from S3 to local
        fs.copyToLocalFile(remote_path, local_path)

        log(spark, f"Downloaded {dest_path}")
    except Exception as e:
        log(spark, f"FATAL: Failed to download {s3_path} - {str(e)}")
        raise


def download_file_from_s3_boto(spark, s3_path, dest_path):
    """
    Download a file from S3 using boto3 (fallback method).
    """
    import boto3

    log(spark, f"Downloading {s3_path} to {dest_path} using boto3...")
    try:
        if not s3_path.startswith("s3://"):
            raise ValueError("Invalid S3 path. It must start with 's3://'.")
        s3_path_parts = s3_path[5:].split("/", 1)
        bucket_name = s3_path_parts[0]
        object_key = s3_path_parts[1]

        s3_client = boto3.client("s3")
        s3_client.download_file(bucket_name, object_key, dest_path)
        log(spark, f"Downloaded {dest_path}")
    except Exception as e:
        log(spark, f"FATAL: Failed to download {s3_path} - {str(e)}")
        raise


def preflight_checks(spark):
    """
    Run pre-flight checks and download required files from S3.
    Downloads all pipeline files to current working directory.
    Required files: Python scripts (03_spark_extractor, 04_json_to_iceberg_enhanced, 05_emr_recommender, 06_write_to_iceberg)
    Optional files: CSV/JSON reference data (if not available, pipeline continues)
    """
    log(spark, "Running pre-flight checks...")
    log(spark, f"Pipeline files will be downloaded to: {PIPELINE_FILES_DIR}")
    log(spark, f"S3 scripts location: s3://{S3_BUCKET}/{S3_SCRIPTS_PREFIX}/")

    s3_base_path = f"s3://{S3_BUCKET}/{S3_SCRIPTS_PREFIX}/"
    os.makedirs(PIPELINE_FILES_DIR, exist_ok=True)

    # REQUIRED files - must be downloaded
    required_files = {
        "03_spark_extractor.py": os.path.join(PIPELINE_FILES_DIR, "03_spark_extractor.py"),
        "04_json_to_iceberg_enhanced.py": os.path.join(PIPELINE_FILES_DIR, "04_json_to_iceberg_enhanced.py"),
        "05_emr_recommender.py": os.path.join(PIPELINE_FILES_DIR, "05_emr_recommender.py"),
        "06_write_to_iceberg.py": os.path.join(PIPELINE_FILES_DIR, "06_write_to_iceberg.py"),
    }

    # Download REQUIRED files - try boto3 first (now guaranteed to be installed), fallback to Spark Hadoop FS
    for filename, dest_path in required_files.items():
        # Try boto3 first (auto-installed at module load)
        boto3_success = False
        try:
            import boto3
            log(spark, f"Downloading REQUIRED file: {filename} (using boto3)...")
            s3_client = boto3.client("s3")
            s3_key = f"{S3_SCRIPTS_PREFIX}/{filename}"
            log(spark, f"  S3 path: s3://{S3_BUCKET}/{s3_key}")
            s3_client.download_file(S3_BUCKET, s3_key, dest_path)
            log(spark, f"✓ Downloaded {dest_path} (boto3)")
            boto3_success = True
        except ImportError:
            log(spark, f"boto3 not available, using Spark Hadoop FS as fallback...")
        except Exception as e:
            log(spark, f"boto3 download failed ({str(e)}), trying Spark Hadoop FS as fallback...")

        # Use Spark Hadoop FS if boto3 failed or not available
        if not boto3_success:
            try:
                log(spark, f"Downloading REQUIRED file: {filename} (using Spark Hadoop FS)...")
                download_file_from_s3_spark(spark, f"{s3_base_path}{filename}", dest_path)
                log(spark, f"✓ Downloaded {dest_path} (Spark Hadoop FS)")
            except Exception as e2:
                log(spark, f"FATAL: All download methods failed for REQUIRED file {filename}: {str(e2)}")
                raise

    log(spark, "Pre-flight checks passed.")


def step_1_extract_metrics(spark, input_path, output_path):
    """
    Step 1: Extract Spark Metrics from event logs
    Returns list of job_ids extracted during this run

    Supports:
    - SPECIFIC_APPLICATION_ID: Process a single application (e.g., "application_1774639960824_3221")
    - NUM_RECENT_FILES: Get N most recent files
    - Time-based: Get files from last N hours (default: 1 hour)
    """
    log(spark, "========================================================")
    log(spark, "→ STEP 1 STARTING: Extract Spark Metrics...")
    log(spark, "========================================================")

    extractor_path = os.path.join(PIPELINE_FILES_DIR, "03_spark_extractor.py")
    if not os.path.isfile(extractor_path):
        raise FileNotFoundError(f"03_spark_extractor.py not found at {extractor_path}")

    spec = importlib.util.spec_from_file_location("spark_extractor", extractor_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec from {extractor_path}")
    spark_extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spark_extractor)

    # Determine extraction mode and parameters
    use_single_app = False
    extractor_input_path = input_path
    hours_for_extractor = 1
    max_recent_files_for_extractor = NUM_RECENT_FILES

    if SPECIFIC_APPLICATION_ID:
        log(spark, f"🎯 SPECIFIC APPLICATION MODE: Processing ONLY '{SPECIFIC_APPLICATION_ID}'")
        use_single_app = True
        # Construct S3 path to specific application
        # Format: s3://bucket/logs/application_timestamp_appid/
        extractor_input_path = f"{input_path.rstrip('/')}/{SPECIFIC_APPLICATION_ID}/"
        log(spark, f"   S3 path: {extractor_input_path}")
        log(spark, f"   ⚠️  IMPORTANT: Will only process this single application, NOT scanning other logs")

        # Override time and file count since we're targeting specific app
        hours_for_extractor = 1  # Still need some value, but won't be used with single_app=True
        max_recent_files_for_extractor = 0  # Override to 0 to ensure single_app logic is used
    elif max_recent_files_for_extractor > 0:
        log(spark, f"📊 RECENT FILES MODE: Processing {max_recent_files_for_extractor} most recent files")
        log(spark, f"   Scanning all logs in: {input_path}")
    else:
        log(spark, f"⏰ TIME-BASED MODE: Processing files from last {hours_for_extractor} hour(s)")
        log(spark, f"   Scanning logs in: {input_path}")

    log(spark, f"   Extractor will use single_app={use_single_app}, max_recent_files={max_recent_files_for_extractor}, hours_ago={hours_for_extractor}")

    job_ids = spark_extractor.run_extractor(
        input_path=extractor_input_path,
        output_path=output_path,
        hours_ago=hours_for_extractor,
        max_recent_files=max_recent_files_for_extractor,
        decompress_workers=int(os.getenv("DECOMPRESS_WORKERS", "50")),
        local_decompress=os.getenv("LOCAL_DECOMPRESS", "false").lower() == "true",
        single_app=use_single_app or os.getenv("SINGLE_APP", "false").lower() == "true",
        spark=spark,
    )

    log(spark, "========================================================")
    log(spark, f"✓ STEP 1 COMPLETED: Extract Spark Metrics ({len(job_ids) if job_ids else 0} job_ids)")
    log(spark, "========================================================")

    return job_ids


# ========================================================================
# BACKLOG TABLE FUNCTIONS (NEW)
# ========================================================================

def pull_batch_from_backlog(spark, backlog_table, batch_size, instance_id):
    """
    Pull a batch of pending event logs from backlog table.
    Marks them as 'IP' (In Progress) to prevent other instances from processing.

    Args:
        spark: SparkSession
        backlog_table: Full table name (e.g., data_processing.backlog_events_log)
        batch_size: Number of logs to pull
        instance_id: Identifier for this pipeline instance

    Returns:
        list: List of dicts with event log metadata
    """
    log(spark, "========================================================")
    log(spark, f"→ PULLING BATCH FROM BACKLOG TABLE")
    log(spark, "========================================================")
    log(spark, f"Table: {backlog_table}")
    log(spark, f"Batch Size: {batch_size}")
    log(spark, f"Instance ID: {instance_id}")
    log(spark, "-" * 60)

    try:
        # Check total pending count
        pending_count_df = spark.sql(f"""
            SELECT COUNT(*) as cnt
            FROM {backlog_table}
            WHERE is_processed = 'N'
        """)
        pending_count = pending_count_df.collect()[0].cnt
        log(spark, f"Total pending logs in backlog: {pending_count:,}")

        if pending_count == 0:
            log(spark, "✓ No pending logs to process")
            log(spark, "========================================================")
            return []

        # Check how many logs are >= 1GB
        eligible_count_df = spark.sql(f"""
            SELECT COUNT(*) as cnt
            FROM {backlog_table}
            WHERE is_processed = 'N'
              AND s3_file_size >= 1073741824
        """)
        eligible_count = eligible_count_df.collect()[0].cnt
        skipped_small = pending_count - eligible_count
        log(spark, f"Eligible logs (pending & >= 1GB): {eligible_count:,}")
        if skipped_small > 0:
            log(spark, f"Skipped logs (< 1GB): {skipped_small:,}")

        if eligible_count == 0:
            log(spark, "✓ No pending logs >= 1GB available for processing")
            log(spark, "========================================================")
            return []

        # ATOMIC CLAIM: First, claim records by setting instance_id
        # This prevents other instances from grabbing the same records
        # Retry with 5-minute wait on Iceberg conflicts
        max_retries = 3
        retry_wait_seconds = 300  # 5 minutes
        claim_successful = False

        claim_query = f"""
            UPDATE {backlog_table}
            SET
                processing_instance_id = '{instance_id}',
                processing_started_at = current_timestamp(),
                processing_attempt_count = processing_attempt_count + 1,
                updated_at = current_timestamp()
            WHERE event_log_id IN (
                SELECT event_log_id
                FROM {backlog_table}
                WHERE is_processed = 'N'
                  AND (processing_instance_id IS NULL OR processing_instance_id = '')
                  AND s3_file_size >= 1073741824
                ORDER BY created_at ASC
                LIMIT {batch_size}
            )
        """

        for attempt in range(1, max_retries + 1):
            try:
                log(spark, f"Attempt {attempt}/{max_retries}: Claiming up to {batch_size} pending logs (file size >= 1GB)...")
                spark.sql(claim_query)
                log(spark, f"✓ Claim UPDATE executed on attempt {attempt}")

                # CRITICAL: Verify we actually claimed records
                # Check how many records have our instance_id
                verify_df = spark.sql(f"""
                    SELECT COUNT(*) as cnt
                    FROM {backlog_table}
                    WHERE is_processed = 'N'
                      AND processing_instance_id = '{instance_id}'
                """)
                claimed_count = verify_df.collect()[0].cnt

                if claimed_count > 0:
                    log(spark, f"✓ Successfully claimed {claimed_count} records on attempt {attempt}")
                    claim_successful = True
                    break
                else:
                    # UPDATE succeeded but we claimed 0 records (race condition)
                    log(spark, f"⚠ Attempt {attempt}/{max_retries}: UPDATE succeeded but claimed 0 records (concurrent access)")
                    if attempt < max_retries:
                        log(spark, f"   Waiting {retry_wait_seconds} seconds before retry...")
                        import time
                        time.sleep(retry_wait_seconds)
                    else:
                        log(spark, f"❌ All {max_retries} attempts resulted in 0 claimed records")

            except Exception as e:
                error_msg = str(e)
                is_conflict = "ValidationException" in error_msg or "conflicting files" in error_msg

                if is_conflict:
                    log(spark, f"⚠ Attempt {attempt}/{max_retries}: Iceberg conflict detected")
                    if attempt < max_retries:
                        log(spark, f"   Waiting {retry_wait_seconds} seconds before retry...")
                        import time
                        time.sleep(retry_wait_seconds)
                    else:
                        log(spark, f"❌ All {max_retries} attempts failed due to conflicts")
                        log(spark, f"   Error: {error_msg[:500]}")
                else:
                    # Non-conflict error - don't retry
                    log(spark, f"❌ Claim query failed with non-conflict error: {error_msg[:500]}")
                    break

        if not claim_successful:
            log(spark, "✗ Unable to claim logs after retries, returning empty batch")
            log(spark, "========================================================")
            return []

        # Now SELECT only the records WE claimed
        batch_df = spark.sql(f"""
            SELECT
                event_log_id,
                application_id,
                s3_full_path,
                s3_file_size,
                created_at
            FROM {backlog_table}
            WHERE is_processed = 'N'
              AND processing_instance_id = '{instance_id}'
            ORDER BY created_at ASC
            LIMIT {batch_size}
        """)

        batch_records = batch_df.collect()
        batch_count = len(batch_records)

        if batch_count == 0:
            log(spark, "⚠ Warning: Verification showed records but SELECT returned 0 (possible race condition)")
            log(spark, "========================================================")
            return []

        log(spark, f"✓ Successfully claimed {batch_count:,} logs for processing")

        # Now mark claimed records as 'IP' (In Progress) with retry logic
        update_query = f"""
            UPDATE {backlog_table}
            SET
                is_processed = 'IP',
                updated_at = current_timestamp()
            WHERE is_processed = 'N'
              AND processing_instance_id = '{instance_id}'
        """

        update_successful = False
        for attempt in range(1, max_retries + 1):
            try:
                log(spark, f"Attempt {attempt}/{max_retries}: Marking {batch_count:,} claimed logs as 'IP'...")
                spark.sql(update_query)
                update_successful = True
                log(spark, f"✓ Updated status to 'IP' for {batch_count:,} logs on attempt {attempt}")
                break
            except Exception as e:
                error_msg = str(e)
                is_conflict = "ValidationException" in error_msg or "conflicting files" in error_msg

                if is_conflict:
                    log(spark, f"⚠ Attempt {attempt}/{max_retries}: Iceberg conflict during status update")
                    if attempt < max_retries:
                        log(spark, f"   Waiting {retry_wait_seconds} seconds before retry...")
                        import time
                        time.sleep(retry_wait_seconds)
                    else:
                        log(spark, f"❌ All {max_retries} attempts failed to update status")
                        log(spark, f"   Error: {error_msg[:500]}")
                else:
                    log(spark, f"❌ Status update failed with non-conflict error: {error_msg[:500]}")
                    break

        if not update_successful:
            log(spark, "⚠ Warning: Could not mark records as 'IP', but will proceed with claimed batch")
            # Don't return empty - we already claimed the records, so process them anyway

        # Convert to list of dicts
        batch_list = [row.asDict() for row in batch_records]

        # Calculate file size stats
        file_sizes = [r.get('s3_file_size', 0) or 0 for r in batch_list]
        if file_sizes:
            min_size_gb = min(file_sizes) / (1024**3)
            max_size_gb = max(file_sizes) / (1024**3)
            avg_size_gb = sum(file_sizes) / len(file_sizes) / (1024**3)
            log(spark, f"\nFile size stats: min={min_size_gb:.3f} GB, max={max_size_gb:.3f} GB, avg={avg_size_gb:.3f} GB")

        # Show sample
        log(spark, f"\nSample logs to process (first 5):")
        for i, record in enumerate(batch_list[:5], 1):
            size_gb = (record.get('s3_file_size', 0) or 0) / (1024**3)
            log(spark, f"  {i}. {record['application_id']} (size: {size_gb:.3f} GB)")

        log(spark, "========================================================")
        return batch_list

    except Exception as e:
        log(spark, f"❌ ERROR pulling from backlog: {e}")
        import traceback
        traceback.print_exc()
        raise


def update_backlog_status(spark, backlog_table, event_log_id, status, job_id=None, error_message=None, processing_started_at=None):
    """
    Update backlog table after processing an event log.
    Includes retry logic for Iceberg conflicts.

    Args:
        spark: SparkSession
        backlog_table: Table name
        event_log_id: Unique ID of event log
        status: 'Y' (success) or 'Y-F' (failure)
        job_id: Job ID extracted from event log (optional)
        error_message: Error message if failed (optional)
        processing_started_at: Start time for duration calculation (optional)
    """
    max_retries = 3
    retry_wait_seconds = 300  # 5 minutes

    try:
        # Escape single quotes in error message
        if error_message:
            error_message = error_message.replace("'", "''")[:1000]  # Limit to 1000 chars

        # Build UPDATE query
        update_fields = [
            f"is_processed = '{status}'",
            "processing_completed_at = current_timestamp()",
            "updated_at = current_timestamp()"
        ]

        # Calculate duration if we have start time
        if processing_started_at and status == 'Y':
            update_fields.append(f"processing_duration_seconds = CAST((unix_timestamp(current_timestamp()) - unix_timestamp('{processing_started_at}')) AS BIGINT)")

        if job_id:
            update_fields.append(f"job_id = '{job_id}'")

        if error_message:
            update_fields.append(f"error_message = '{error_message}'")
            update_fields.append("error_timestamp = current_timestamp()")

        update_query = f"""
            UPDATE {backlog_table}
            SET {', '.join(update_fields)}
            WHERE event_log_id = '{event_log_id}'
        """

        # Retry loop for Iceberg conflicts
        for attempt in range(1, max_retries + 1):
            try:
                spark.sql(update_query)
                if attempt > 1:
                    log(spark, f"✓ Status update successful for {event_log_id} on attempt {attempt}")
                break
            except Exception as e:
                error_msg = str(e)
                is_conflict = "ValidationException" in error_msg or "conflicting files" in error_msg

                if is_conflict and attempt < max_retries:
                    log(spark, f"⚠ Iceberg conflict updating {event_log_id} (attempt {attempt}/{max_retries}), waiting {retry_wait_seconds}s...")
                    import time
                    time.sleep(retry_wait_seconds)
                else:
                    # Last attempt or non-conflict error
                    if attempt == max_retries:
                        log(spark, f"⚠ WARNING: Failed to update {event_log_id} after {max_retries} attempts: {error_msg[:200]}")
                    else:
                        log(spark, f"⚠ WARNING: Failed to update backlog status for {event_log_id}: {error_msg[:200]}")
                    break

    except Exception as e:
        log(spark, f"⚠ WARNING: Unexpected error updating backlog status for {event_log_id}: {e}")
        # Don't raise - this is non-critical


def update_backlog_batch_status(spark, backlog_table, success_jobs, failed_jobs):
    """
    Batch update backlog table for multiple jobs.

    Args:
        spark: SparkSession
        backlog_table: Table name
        success_jobs: List of (event_log_id, job_id, processing_started_at) tuples
        failed_jobs: List of (event_log_id, error_message, processing_started_at) tuples
    """
    log(spark, "========================================================")
    log(spark, "→ UPDATING BACKLOG TABLE STATUS")
    log(spark, "========================================================")

    try:
        # Update successful jobs
        if success_jobs:
            log(spark, f"Updating {len(success_jobs):,} successful jobs to 'Y'...")
            for event_log_id, job_id, processing_started_at in success_jobs:
                update_backlog_status(spark, backlog_table, event_log_id, 'Y',
                                    job_id=job_id, processing_started_at=processing_started_at)
            log(spark, f"✓ Updated {len(success_jobs):,} jobs to status 'Y'")

        # Update failed jobs
        if failed_jobs:
            log(spark, f"Updating {len(failed_jobs):,} failed jobs to 'Y-F'...")
            for event_log_id, error_message, processing_started_at in failed_jobs:
                update_backlog_status(spark, backlog_table, event_log_id, 'Y-F',
                                    error_message=error_message, processing_started_at=processing_started_at)
            log(spark, f"✓ Updated {len(failed_jobs):,} jobs to status 'Y-F'")

        # Show updated stats
        log(spark, "\nBacklog table status after update:")
        status_df = spark.sql(f"""
            SELECT is_processed, COUNT(*) as count
            FROM {backlog_table}
            GROUP BY is_processed
            ORDER BY is_processed
        """)
        status_df.show()

        log(spark, "========================================================")

    except Exception as e:
        log(spark, f"⚠ WARNING: Failed to update backlog batch: {e}")
        # Don't raise - processing already completed


def step_1_extract_metrics_from_backlog(spark, backlog_table, batch_size, instance_id):
    """
    Step 1: Extract metrics from event logs in backlog table.

    This is the NEW version of Step 1 that reads from backlog table instead of scanning S3 directly.

    Workflow:
    1. Pull batch of pending logs from backlog (status='N')
    2. Mark them as 'IP' (In Progress)
    3. Process each log using 03_spark_extractor
    4. Track success/failure for each log

    Returns:
        tuple: (job_ids, success_jobs, failed_jobs)
    """
    log(spark, "========================================================")
    log(spark, "→ STEP 1 STARTING: Extract Metrics from Backlog")
    log(spark, "========================================================")

    from datetime import datetime, timezone

    # Pull batch from backlog
    batch_records = pull_batch_from_backlog(spark, backlog_table, batch_size, instance_id)

    if not batch_records:
        log(spark, "No logs to process from backlog")
        log(spark, "========================================================")
        return [], [], []

    log(spark, f"Processing {len(batch_records):,} event logs from backlog...")

    # Load spark_extractor dynamically
    extractor_path = os.path.join(PIPELINE_FILES_DIR, "03_spark_extractor.py")
    if not os.path.isfile(extractor_path):
        raise FileNotFoundError(f"03_spark_extractor.py not found at {extractor_path}")

    spec = importlib.util.spec_from_file_location("spark_extractor", extractor_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec from {extractor_path}")
    spark_extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spark_extractor)

    # Process each event log
    job_ids = []
    success_jobs = []  # (event_log_id, job_id, processing_started_at)
    failed_jobs = []   # (event_log_id, error_message, processing_started_at)

    output_path = f"s3://{S3_BUCKET}/dw-target-metrics/{TIMESTAMP}/"

    for idx, record in enumerate(batch_records, 1):
        event_log_id = record['event_log_id']
        application_id = record['application_id']
        s3_full_path = record['s3_full_path']

        processing_start_time = datetime.now(timezone.utc)
        processing_start_str = processing_start_time.strftime("%Y-%m-%d %H:%M:%S")

        log(spark, f"  [{idx}/{len(batch_records)}] Processing {application_id}...")

        try:
            # Call spark_extractor for single app
            # Use single_app=True mode and pass application_id to get specific job_id
            app_job_ids = spark_extractor.run_extractor(
                input_path=s3_full_path,
                output_path=output_path,
                hours_ago=1,  # Not used in single_app mode
                max_recent_files=0,
                decompress_workers=50,
                local_decompress=False,
                single_app=True,  # Process only this app
                spark=spark,
                application_id=application_id  # Pass application_id to extract job_id from specific file
            )

            # Extract job_id from result
            if app_job_ids and len(app_job_ids) > 0:
                job_id = app_job_ids[0]
                job_ids.append(job_id)
                success_jobs.append((event_log_id, job_id, processing_start_str))
                log(spark, f"  ✓ {application_id}: Success (job_id={job_id})")
            else:
                # No job_id returned - treat as failed
                error_msg = "No data extracted from event log"
                failed_jobs.append((event_log_id, error_msg, processing_start_str))
                log(spark, f"  ⚠ {application_id}: No data extracted")

        except Exception as e:
            error_msg = str(e)[:1000]  # Limit error message length
            failed_jobs.append((event_log_id, error_msg, processing_start_str))
            log(spark, f"  ✗ {application_id}: Failed - {str(e)[:100]}")

    log(spark, "========================================================")
    log(spark, f"✓ STEP 1 COMPLETED:")
    log(spark, f"  - Total processed: {len(batch_records):,}")
    log(spark, f"  - Successful: {len(success_jobs):,}")
    log(spark, f"  - Failed: {len(failed_jobs):,}")
    log(spark, f"  - Job IDs extracted: {len(job_ids):,}")
    log(spark, "========================================================")

    return job_ids, success_jobs, failed_jobs


def step_2_load_metrics_to_iceberg(spark, extract_path):
    """
    Step 2: Load extracted JSON metrics to Iceberg tables
    Reads task_stage_summary and spark_config_extract from S3, flattens, and writes to Iceberg
    Returns list of job_ids extracted from the loaded files
    """
    log(spark, "Starting Step 2: Load Metrics to Iceberg...")

    writer_path = os.path.join(PIPELINE_FILES_DIR, "04_json_to_iceberg_enhanced.py")
    if not os.path.isfile(writer_path):
        raise FileNotFoundError(f"04_json_to_iceberg_enhanced.py not found at {writer_path}")

    spec = importlib.util.spec_from_file_location("json_to_iceberg_enhanced", writer_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec from {writer_path}")
    json_to_iceberg_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(json_to_iceberg_module)

    # Call the main function from json_to_iceberg_enhanced with TIMESTAMP
    # This ensures we only load the recently extracted applications (not all historical data)
    # Returns job_ids extracted from the loaded files
    job_ids_from_iceberg = json_to_iceberg_module.main(timestamp=TIMESTAMP)

    log(spark, "========================================================")
    log(spark, f"✓ STEP 2 COMPLETED: Load Metrics to Iceberg ({len(job_ids_from_iceberg) if job_ids_from_iceberg else 0} job_ids)")
    log(spark, "========================================================")

    return job_ids_from_iceberg


def read_metrics_from_iceberg(spark, job_ids=None):
    """
    Read metrics from Iceberg table and convert to JSON format for 05_emr_recommender.
    Filters for recently extracted applications (by job_ids).
    Returns list of metric dictionaries in the format expected by generate_dual_recommendations().

    Args:
        spark: Spark session
        job_ids: List of job_ids to filter for (e.g., ["job_id_1", "job_id_2"]).
                If provided, reads only records with these job_ids.
    """
    log(spark, f"Reading metrics from Iceberg table (filtering by {len(job_ids) if job_ids else 0} job_ids)...")

    # Iceberg table configuration
    iceberg_namespace = "data_processing"
    iceberg_table = "spark_metrics_task_stage_v2"

    try:
        # First, check total records in table
        count_query = f"SELECT COUNT(*) as total_count FROM {iceberg_namespace}.{iceberg_table}"
        count_df = spark.sql(count_query)
        total_count = count_df.collect()[0].total_count
        log(spark, f"DEBUG: Total records in {iceberg_namespace}.{iceberg_table}: {total_count}")

        # Build query with job_id filter
        # IMPORTANT: If no job_ids, return 0 records (not all records!)
        where_clause = ""
        if job_ids is None or (isinstance(job_ids, list) and len(job_ids) == 0):
            # No job_ids found - return 0 records using WHERE 1=0
            where_clause = "WHERE 1=0"
            log(spark, f"⚠ NO job_ids found - filtering to return ZERO records (WHERE 1=0)")
            log(spark, f"DEBUG: WHERE clause = {where_clause}")
        else:
            # Build IN clause with job_ids
            job_ids_str = "', '".join([str(jid).replace("'", "''") for jid in job_ids])
            where_clause = f"WHERE job_id IN ('{job_ids_str}')"
            log(spark, f"Filtering for job_ids: {len(job_ids)} jobs")
            log(spark, f"DEBUG: WHERE clause = {where_clause}")
            log(spark, f"DEBUG: Job IDs to filter: {job_ids}")

        # Read from Iceberg using Spark SQL - only recently extracted applications
        query = f"""
        SELECT
            application_id,
            application_name,
            job_id,
            total_run_duration_hours,
            task_total_tasks,
            task_completed_tasks,
            task_failed_tasks,
            task_killed_tasks,
            task_success_rate_percent,
            stage_total_stages,
            stage_completed_stages,
            executor_total_executors,
            executor_avg_memory_utilization_percent,
            executor_avg_cpu_utilization_percent,
            executor_total_cost_factor,
            io_total_input_gb,
            io_total_shuffle_read_gb,
            io_total_shuffle_write_gb,
            io_input_per_task_avg_gb,
            io_shuffle_read_per_task_avg_gb,
            io_shuffle_write_per_task_avg_gb,
            spill_total_memory_spilled_gb,
            job_total_jobs,
            job_successful_jobs,
            job_failed_jobs
        FROM {iceberg_namespace}.{iceberg_table}
        {where_clause}
        """

        log(spark, f"DEBUG: Executing query:\n{query}")
        df = spark.sql(query)
        records = df.collect()
        log(spark, f"Read {len(records)} records from {iceberg_namespace}.{iceberg_table}")

        # Debug: Show distinct job_ids found
        if records:
            distinct_job_ids = list(set([r.job_id for r in records]))
            log(spark, f"DEBUG: Found {len(distinct_job_ids)} distinct job_ids in results:")
            for jid in distinct_job_ids:
                log(spark, f"  - {jid}")
        else:
            log(spark, f"DEBUG: No records returned from query")

        # Convert Spark rows to dictionaries in the format expected by emr_recommender
        metrics_list = []
        for row in records:
            metric_dict = {
                "application_id": row.application_id,
                "application_name": row.application_name,
                "job_id": row.job_id,
                "application_info": {
                    "job_id": row.job_id,
                    "total_run_duration_hours": row.total_run_duration_hours,
                },
                "task_summary": {
                    "total_tasks": row.task_total_tasks,
                    "completed_tasks": row.task_completed_tasks,
                    "failed_tasks": row.task_failed_tasks,
                    "killed_tasks": row.task_killed_tasks,
                    "success_rate_percent": row.task_success_rate_percent,
                },
                "stage_summary": {
                    "total_stages": row.stage_total_stages,
                    "completed_stages": row.stage_completed_stages,
                },
                "executor_summary": {
                    "total_executors": row.executor_total_executors,
                    "avg_memory_utilization_percent": row.executor_avg_memory_utilization_percent,
                    "avg_cpu_utilization_percent": row.executor_avg_cpu_utilization_percent,
                    "total_cost_factor": row.executor_total_cost_factor,
                },
                "io_summary": {
                    "application_level": {
                        "total_input_gb": row.io_total_input_gb,
                        "total_shuffle_read_gb": row.io_total_shuffle_read_gb,
                        "total_shuffle_write_gb": row.io_total_shuffle_write_gb,
                        "input_per_task_avg_gb": row.io_input_per_task_avg_gb,
                        "shuffle_read_per_task_avg_gb": row.io_shuffle_read_per_task_avg_gb,
                        "shuffle_write_per_task_avg_gb": row.io_shuffle_write_per_task_avg_gb,
                    }
                },
                "spill_summary": {
                    "total_memory_spilled_gb": row.spill_total_memory_spilled_gb,
                },
                "job_details": {
                    "summary": {
                        "total_jobs": row.job_total_jobs,
                        "successful_jobs": row.job_successful_jobs,
                        "failed_jobs": row.job_failed_jobs,
                    }
                },
            }
            metrics_list.append(metric_dict)

        return metrics_list

    except Exception as e:
        log(spark, f"Error reading metrics from Iceberg: {str(e)}")
        raise


def step_3_emr_recommender(spark, extract_path, output_dir, job_ids=None):
    """
    Step 3: Generate Recommendations from Iceberg
    Reads metrics from Iceberg tables for the provided job_ids.
    """
    log(spark, "========================================================")
    log(spark, "→ STEP 3 STARTING: Generate Recommendations from Iceberg...")
    log(spark, "========================================================")

    recommender_path = os.path.join(PIPELINE_FILES_DIR, "05_emr_recommender.py")
    if not os.path.isfile(recommender_path):
        raise FileNotFoundError(f"05_emr_recommender.py not found at {recommender_path}")

    spec = importlib.util.spec_from_file_location("emr_recommender", recommender_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec from {recommender_path}")
    recommender_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recommender_module)

    # Read metrics from Iceberg - filter by job_ids from Step 1
    log(spark, f"Filtering Iceberg table for {len(job_ids) if job_ids else 0} recent job_ids...")
    if job_ids:
        for i, jid in enumerate(job_ids, 1):
            log(spark, f"  {i}. {jid}")

    metrics_list = read_metrics_from_iceberg(spark, job_ids=job_ids)

    if not metrics_list:
        log(spark, "⚠ No metrics found in Iceberg table. Skipping recommendations generation.")
        log(spark, "========================================================")
        log(spark, "✓ STEP 3 COMPLETED: Generate Recommendations (no data)")
        log(spark, "========================================================")
        return

    log(spark, f"✓ Retrieved {len(metrics_list)} application records from Iceberg (filtered by job_ids)")
    log(spark, f"Generating recommendations from {len(metrics_list)} applications...")

    # Generate recommendations using the imported module
    cost_recs, perf_recs = recommender_module.generate_dual_recommendations_from_data(
        metrics_list,
        limit=None,
        target_partition_size_mib=1024,
        serverless_storage=False
    )

    # Write output files
    cost_output = os.path.join(output_dir, "recommendations_cost_optimized.json")
    perf_output = os.path.join(output_dir, "recommendations_performance_optimized.json")

    os.makedirs(output_dir, exist_ok=True)

    import json
    with open(cost_output, 'w') as f:
        json.dump(cost_recs, f, indent=2)
    log(spark, f"✓ Cost-optimized recommendations written to {cost_output}")

    with open(perf_output, 'w') as f:
        json.dump(perf_recs, f, indent=2)
    log(spark, f"✓ Performance-optimized recommendations written to {perf_output}")

    log(spark, "========================================================")
    log(spark, f"✓ STEP 3 COMPLETED: Generate Recommendations ({len(cost_recs)} recommendations)")
    log(spark, "========================================================")


def step_4_write_recommendations_to_iceberg(spark, extract_path, output_dir):
    """
    Step 4: Write cost-optimized recommendations to serverless_config_advisor table
    Reads the cost recommendations JSON and writes to final Iceberg table
    """
    log(spark, "========================================================")
    log(spark, "→ STEP 4 STARTING: Write Cost Recommendations to Iceberg...")
    log(spark, "========================================================")

    writer_path = os.path.join(PIPELINE_FILES_DIR, "06_write_to_iceberg.py")
    if not os.path.isfile(writer_path):
        raise FileNotFoundError(f"06_write_to_iceberg.py not found at {writer_path}")

    spec = importlib.util.spec_from_file_location("write_to_iceberg", writer_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec from {writer_path}")
    writer_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer_module)

    # Use cost-optimized recommendations as primary, with perf recommendations as secondary
    cost_output = os.path.join(output_dir, "recommendations_cost_optimized.json")
    perf_output = os.path.join(output_dir, "recommendations_performance_optimized.json")

    # Check if recommendations file exists before writing
    if not os.path.isfile(cost_output):
        log(spark, "⚠ No cost recommendations file found. Skipping write to Iceberg.")
        log(spark, "========================================================")
        log(spark, "✓ STEP 4 COMPLETED: Write Recommendations (no data)")
        log(spark, "========================================================")
        return

    log(spark, f"Writing recommendations to {ICEBERG_TABLE}...")
    log(spark, f"  Cost recommendations: {cost_output}")
    if os.path.isfile(perf_output):
        log(spark, f"  Perf recommendations: {perf_output}")
    log(spark, f"  Reading extracts from: {extract_path}")

    rows_written = writer_module.write_to_iceberg(
        rec_path=cost_output,
        perf_rec_path=perf_output if os.path.isfile(perf_output) else None,
        extract_path=extract_path,
        table_name=ICEBERG_TABLE,
        warehouse=ICEBERG_WAREHOUSE,
        spark=spark,
    )

    log(spark, "========================================================")
    log(spark, f"✓ STEP 4 COMPLETED: Write Recommendations to Iceberg")
    log(spark, f"  Rows written to {ICEBERG_TABLE}: {rows_written}")
    log(spark, "========================================================")


def upload_to_s3_spark(spark, output_dir, timestamp):
    """
    Upload results to S3 (placeholder - not uploading CSV/JSON files).
    """
    log(spark, "S3 upload skipped (no files to upload)")



def analyze_application_coverage(spark, processed_job_ids, input_path):
    """
    Analyze application coverage: compare processed vs. available applications.
    This runs AFTER spark_extractor to identify skipped/missed applications.

    Args:
        spark: SparkSession
        processed_job_ids: List of job IDs that were processed by extractor
        input_path: S3 path where event logs are stored

    Returns:
        dict: Analysis metrics
    """
    log(spark, "========================================================")
    log(spark, "→ ANALYZING APPLICATION COVERAGE...")
    log(spark, "========================================================")

    try:
        # Import boto3 (already loaded at module level)
        import boto3
        from datetime import datetime, timezone

        # Parse S3 path
        s3_path_clean = input_path.replace("s3://", "").rstrip("/")
        parts = s3_path_clean.split("/", 1)
        bucket = parts[0]
        prefix = parts[1] + "/" if len(parts) > 1 else ""

        log(spark, f"Scanning S3 bucket: s3://{bucket}/{prefix}")

        # Discover all available applications in S3
        s3 = boto3.client("s3", region_name="us-east-1")
        paginator = s3.get_paginator('list_objects_v2')

        all_available_apps = set()
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            if 'Contents' not in page:
                continue

            for obj in page['Contents']:
                key = obj['Key']

                # Skip .inprogress files
                if '.inprogress' in key:
                    continue

                # Extract application folder name
                # Expected format: logs/application_id/ or logs/uuid/
                key_parts = key.split('/')
                if len(key_parts) >= 2:
                    app_folder = key_parts[1]
                    if app_folder:
                        all_available_apps.add(app_folder)

        # Calculate metrics
        processed_set = set(processed_job_ids) if processed_job_ids else set()
        total_available = len(all_available_apps)
        total_processed = len(processed_set)

        # Find skipped apps (available but not processed)
        skipped_apps = all_available_apps - processed_set
        total_skipped = len(skipped_apps)

        # Find missing apps (processed but not in S3 - shouldn't happen)
        missing_from_s3 = processed_set - all_available_apps

        coverage_percent = (total_processed / total_available * 100) if total_available > 0 else 0

        # Log summary
        log(spark, f"✓ Available applications in S3: {total_available}")
        log(spark, f"✓ Processed applications:       {total_processed}")
        log(spark, f"✓ Skipped applications:         {total_skipped}")
        log(spark, f"✓ Coverage:                     {coverage_percent:.2f}%")

        if missing_from_s3:
            log(spark, f"⚠ WARNING: {len(missing_from_s3)} processed job IDs not found in S3")

        # Save detailed metrics to JSON
        analysis_report = {
            'analysis_metadata': {
                'analysis_timestamp_utc': datetime.now(timezone.utc).isoformat(),
                'analysis_date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
                'analysis_time': datetime.now(timezone.utc).strftime('%H:%M:%S'),
                'analysis_hour': datetime.now(timezone.utc).hour,
                'pipeline_timestamp': TIMESTAMP
            },
            'summary': {
                'total_available': total_available,
                'total_processed': total_processed,
                'total_skipped': total_skipped,
                'missing_from_s3': len(missing_from_s3),
                'coverage_percent': round(coverage_percent, 2)
            },
            'details': {
                'skipped_apps_sample': sorted(list(skipped_apps))[:50],  # First 50
                'skipped_apps_count': total_skipped,
                'missing_from_s3': sorted(list(missing_from_s3)) if missing_from_s3 else []
            }
        }

        # Save to output directory
        coverage_report_path = os.path.join(OUTPUT_DIR, f"coverage_analysis_{TIMESTAMP}.json")
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        import json
        with open(coverage_report_path, 'w') as f:
            json.dump(analysis_report, f, indent=2)

        log(spark, f"✓ Coverage analysis saved to: {coverage_report_path}")
        log(spark, "========================================================")

        return analysis_report

    except Exception as e:
        log(spark, f"⚠ Coverage analysis failed: {str(e)}")
        log(spark, "Pipeline will continue without coverage metrics")
        log(spark, "========================================================")
        return None


def cleanup_old_outputs(spark, max_runs_kept):
    """
    Clean up old output directories, keeping only the most recent ones.
    """
    log(spark, f"Cleaning up old output directories (keeping last {max_runs_kept})...")
    output_base = "output"

    if os.path.isdir(output_base):
        dirs = sorted([
            os.path.join(output_base, d)
            for d in os.listdir(output_base)
            if os.path.isdir(os.path.join(output_base, d))
        ])

        if len(dirs) > max_runs_kept:
            for dir_to_remove in dirs[:-max_runs_kept]:
                log(spark, f"Removing: {dir_to_remove}")
                shutil.rmtree(dir_to_remove)
        else:
            log(spark, "Nothing to clean.")


def run_pipeline():
    """
    Main pipeline execution using PySpark.
    """
    spark = None
    try:
        # Initialize Spark session
        spark = get_spark_session(f"SparkMetricsPipeline_{TIMESTAMP}")

        log(spark, "================================================================")
        log(spark, f"SPARK METRICS PIPELINE (BACKLOG-ENABLED) — {TIMESTAMP}")
        log(spark, f"Processing Instance: {PROCESSING_INSTANCE_ID}")
        log(spark, f"Batch Size: {BATCH_SIZE}")
        log(spark, f"Backlog Table: {BACKLOG_TABLE}")
        log(spark, "================================================================")

        # Run pre-flight checks
        preflight_checks(spark)

        # Create output directory
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # Step 1: Process batch from backlog table (NEW)
        output_path = f"s3://{S3_BUCKET}/dw-target-metrics/{TIMESTAMP}/"

        job_ids, success_jobs, failed_jobs = step_1_extract_metrics_from_backlog(
            spark,
            BACKLOG_TABLE,
            BATCH_SIZE,
            PROCESSING_INSTANCE_ID
        )

        # Update backlog table with results
        update_backlog_batch_status(spark, BACKLOG_TABLE, success_jobs, failed_jobs)

        # Print job_ids successfully processed
        if job_ids:
            log(spark, "========================================================")
            log(spark, f"JOB IDs SUCCESSFULLY PROCESSED ({len(job_ids)} total):")
            for i, job_id in enumerate(job_ids, 1):
                log(spark, f"  {i}. {job_id}")
            log(spark, "========================================================")
        else:
            log(spark, "========================================================")
            log(spark, "⚠ No job_ids extracted from backlog batch")
            log(spark, "========================================================")

        # Note: Coverage analysis is no longer needed - backlog table tracks everything

        # Step 2 is optional - skip if HMS/Iceberg is not available
        # Step 2 also extracts job_ids from the files it loads - use those for downstream filtering
        job_ids_from_step2 = None
        try:
            job_ids_from_step2 = step_2_load_metrics_to_iceberg(spark, output_path)
            # Use job_ids from Step 2 (from Iceberg load) as they are definitive
            if job_ids_from_step2:
                log(spark, f"Using job_ids from Step 2 Iceberg load ({len(job_ids_from_step2)} jobs)")
                job_ids = job_ids_from_step2
            else:
                log(spark, f"Step 2 returned no job_ids, using Step 1 job_ids ({len(job_ids) if job_ids else 0} jobs)")
        except Exception as e:
            log(spark, "========================================================")
            log(spark, f"⚠ STEP 2 SKIPPED: Load to Iceberg")
            log(spark, f"  Reason: {str(e)}")
            log(spark, f"  Pipeline will continue with Step 3 using Step 1 job_ids ({len(job_ids) if job_ids else 0})")
            log(spark, "========================================================")

        # Step 3: Generate recommendations (only if job_ids exist)
        if job_ids:
            step_3_emr_recommender(spark, output_path, OUTPUT_DIR, job_ids=job_ids)

            # Step 4: Write recommendations to final table
            step_4_write_recommendations_to_iceberg(spark, output_path, OUTPUT_DIR)
        else:
            log(spark, "⚠ Skipping Steps 3 & 4 (no job_ids to process)")

        # Upload results to S3
        upload_to_s3_spark(spark, OUTPUT_DIR, TIMESTAMP)

        # Cleanup old outputs
        cleanup_old_outputs(spark, MAX_RUNS_KEPT)

        log(spark, "========================================================")
        log(spark, "✓✓✓ PIPELINE COMPLETED SUCCESSFULLY ✓✓✓")
        log(spark, "========================================================")
        log(spark, f"Instance ID: {PROCESSING_INSTANCE_ID}")
        log(spark, f"Batch Size: {BATCH_SIZE}")
        log(spark, f"Processed: {len(job_ids)} jobs")
        log(spark, f"Failed: {len(failed_jobs)} jobs")
        log(spark, "========================================================")

    except Exception as e:
        if spark:
            log(spark, f"Pipeline failed with error: {str(e)}")
        raise
    finally:
        if spark:
            spark.stop()


if __name__ == "__main__":
    run_pipeline()

