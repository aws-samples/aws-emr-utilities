#!/bin/bash
# ============================================================================
# Load S3 Advisor Data to Iceberg + DynamoDB
# ============================================================================
# Reads the last N partitions from S3 advisor data and writes to:
#   1. Iceberg table: serverless_config_advisor_v5
#   2. DynamoDB table: prod-dynamodb-egdataplatform-dw-dataproc-emr-serverless-config-recommander
#
# This script:
#   - Lists all datehour partitions in S3
#   - Sorts by timestamp (yyyymmddHH) and takes the 5 most recent
#   - Reads JSON files from those partitions
#   - Writes to both Iceberg and DynamoDB
# ============================================================================

set -e

# Capture start time
SCRIPT_START_TIME=$(date +%s)
SCRIPT_START_TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')

# Generate log file name with timestamp
LOG_TIMESTAMP=$(date '+%d%m%y%H%M')
LOG_FILE="s3_to_iceberg_dynamodb_${LOG_TIMESTAMP}.log"
LOG_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_PATH="${LOG_DIR}/${LOG_FILE}"

# S3 destination for logs
S3_LOG_BUCKET="${S3_BUCKET}"
S3_LOG_PREFIX="pipeline-files-v1/backlog-scale-dw/pipeline_loader_logs"
S3_LOG_DESTINATION="s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/${LOG_FILE}"

# Start logging (redirect all output to both console and log file)
exec > >(tee -a "${LOG_PATH}") 2>&1

echo "=========================================================================="
echo "LOGGING ENABLED"
echo "=========================================================================="
echo "Log File: ${LOG_PATH}"
echo "S3 Destination: ${S3_LOG_DESTINATION}"
echo "=========================================================================="
echo ""

# Clear AWS Profile from SSH session
unset AWS_PROFILE 2>/dev/null || true
unset AWS_DEFAULT_PROFILE 2>/dev/null || true

echo "=========================================================================="
echo "S3 TO ICEBERG + DYNAMODB LOADER"
echo "=========================================================================="
echo "Start Time: ${SCRIPT_START_TIMESTAMP}"
echo ""

cd "$(dirname "$0")"

# ============================================================================
# Configuration
# ============================================================================

export AWS_REGION="us-east-1"
export S3_BUCKET="${S3_BUCKET}"

# S3 Advisor Data Location
export S3_ADVISOR_PATH="s3://${S3_BUCKET}/emr-serverless-config-advisor/"

# Target Tables
export ICEBERG_TABLE="${CATALOG_NAMESPACE}.serverless_config_advisor_v5"
export DYNAMODB_TABLE="prod-dynamodb-egdataplatform-dw-dataproc-emr-serverless-config-recommander"
export ICEBERG_WAREHOUSE="s3://${S3_BUCKET}/iceberg/"

# Number of recent partitions to load
export NUM_PARTITIONS="${NUM_PARTITIONS:-1}"  # Default: 1, override with: NUM_PARTITIONS=5 ./script.sh

# ============================================================================
# Configuration Display
# ============================================================================

echo "Configuration:"
echo "  AWS Region:           ${AWS_REGION}"
echo "  S3 Bucket:            ${S3_BUCKET}"
echo "  S3 Advisor Path:      ${S3_ADVISOR_PATH}"
echo "  Iceberg Table:        ${ICEBERG_TABLE}"
echo "  DynamoDB Table:       ${DYNAMODB_TABLE}"
echo "  Iceberg Warehouse:    ${ICEBERG_WAREHOUSE}"
echo "  Number of Partitions: ${NUM_PARTITIONS}"
echo ""

# ============================================================================
# Pre-flight Checks
# ============================================================================

echo "Running pre-flight checks..."
echo ""

python3 <<'PYTHON_CHECK' || { echo ""; echo "Pre-flight checks failed. Cannot proceed."; exit 1; }
import os
import sys

# Clear profiles
os.environ.pop('AWS_PROFILE', None)
os.environ.pop('AWS_DEFAULT_PROFILE', None)

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    # Test credentials
    sts = boto3.client('sts', region_name='us-east-1')
    identity = sts.get_caller_identity()

    account = identity['Account']
    arn = identity['Arn']

    print(f"✓ AWS credentials valid")
    print(f"  Account: {account}")
    print(f"  ARN: {arn}")

    if account != "${AWS_ACCOUNT_ID}":
        print(f"\n✗ Wrong account! Expected: ${AWS_ACCOUNT_ID}, Got: {account}")
        sys.exit(1)

    print()

    # Test S3 access
    s3 = boto3.client('s3', region_name='us-east-1')

    # List partitions
    response = s3.list_objects_v2(
        Bucket='${S3_BUCKET}',
        Prefix='emr-serverless-config-advisor/',
        Delimiter='/',
        MaxKeys=10
    )

    partition_count = len(response.get('CommonPrefixes', []))
    print(f"✓ Found {partition_count} partitions in S3 advisor path")

    print()

    sys.exit(0)

except NoCredentialsError:
    print("✗ No AWS credentials found")
    sys.exit(1)
except ClientError as e:
    print(f"✗ AWS API error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"✗ Pre-flight check failed: {e}")
    sys.exit(1)
PYTHON_CHECK

echo ""
echo "=========================================================================="
echo "Launching loaders..."
echo "=========================================================================="
echo ""

# ============================================================================
# Download Scripts from S3 (ALWAYS download fresh to avoid cached versions)
# ============================================================================

ADVISOR_LOADER="22_load_advisor_to_iceberg_dynamodb.py"
BULK_METRICS_LOADER="25_bulk_load_metrics_to_iceberg.py"
WORK_DIR="/tmp/iceberg-loader-$$"

echo "Downloading loader scripts from S3..."
mkdir -p "$WORK_DIR"

# Download advisor loader
aws s3 cp "s3://${S3_BUCKET}/pipeline-files-v1/backlog-scale-dw/${ADVISOR_LOADER}" "$WORK_DIR/" --region ${AWS_REGION} \
    || { echo "Failed to download advisor loader script"; exit 1; }

# Download bulk metrics loader (NEW - replaces old 20_load_metrics_to_iceberg.py)
aws s3 cp "s3://${S3_BUCKET}/pipeline-files-v1/backlog-scale-dw/${BULK_METRICS_LOADER}" "$WORK_DIR/" --region ${AWS_REGION} \
    || { echo "Failed to download bulk metrics loader script"; exit 1; }

# DISABLED: Download old metrics loader (20_load_metrics_to_iceberg.py takes too long — commented out)
# aws s3 cp "s3://${S3_BUCKET}/pipeline-files-v1/backlog-scale-dw/${METRICS_LOADER}" "$WORK_DIR/" --region ${AWS_REGION}
# if [ $? -ne 0 ]; then
#     echo "Failed to download metrics loader script"
#     exit 1
# fi

cd "$WORK_DIR"
echo "✓ Downloaded scripts to ${WORK_DIR}"
echo "  - ${ADVISOR_LOADER}"
echo "  - ${BULK_METRICS_LOADER}"
echo ""

# ============================================================================
# STEP 1: Load Metrics to Iceberg — DISABLED (takes too long)
# Uncomment to re-enable: 20_load_metrics_to_iceberg.py
# ============================================================================

# if command -v python3 > /dev/null 2>&1; then
#     echo "=========================================================================="
#     echo "STEP 1: Loading Metrics to Iceberg Tables (PyIceberg)"
#     echo "=========================================================================="
#     echo "Reading from: s3://${S3_BUCKET}/test-target-metrics/{timestamp}/"
#     echo "Target tables:"
#     echo "  - ${CATALOG_NAMESPACE}.spark_metrics_task_stage_v5"
#     echo "  - ${CATALOG_NAMESPACE}.spark_metrics_config_v5"
#     echo "Lookback: Last 1 hour"
#     echo ""
#
#     python3 ${METRICS_LOADER} \
#         --s3-bucket "${S3_BUCKET}" \
#         --lookback-hours 1
#
#     METRICS_EXIT_CODE=$?
#
#     if [ $METRICS_EXIT_CODE -eq 0 ]; then
#         echo ""
#         echo "✓ Step 1 completed: Metrics loaded to Iceberg"
#     else
#         echo ""
#         echo "✗ Step 1 failed with exit code: $METRICS_EXIT_CODE"
#         echo "  Continuing to Step 2 anyway..."
#     fi
#     echo ""

echo "=========================================================================="
echo "STEP 1: Load Metrics to Iceberg — SKIPPED (disabled, takes too long)"
echo "  To re-enable: uncomment STEP 1 block in RUN_S3_TO_ICEBERG_DYNAMODB.sh"
echo "=========================================================================="
echo ""
METRICS_EXIT_CODE=0

if command -v python3 > /dev/null 2>&1; then

    # ============================================================================
    # STEP 2: Load Advisor Data to Iceberg + DynamoDB
    # ============================================================================

    echo "=========================================================================="
    echo "STEP 2: Loading Advisor Data to Iceberg + DynamoDB"
    echo "=========================================================================="
    echo "Reading from: ${S3_ADVISOR_PATH}"
    echo "Target tables:"
    echo "  - ${ICEBERG_TABLE} (Iceberg)"
    echo "  - ${DYNAMODB_TABLE} (DynamoDB)"
    echo "Partitions: ${NUM_PARTITIONS} most recent"
    echo ""

    spark-submit \
        --master local[*] \
        --deploy-mode client \
        --packages org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.6.0 \
        --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions \
        --conf spark.sql.catalog.spark_catalog=org.apache.iceberg.spark.SparkSessionCatalog \
        --conf spark.sql.catalog.spark_catalog.type=hive \
        --conf spark.sql.catalog.spark_catalog.warehouse=${ICEBERG_WAREHOUSE} \
        --conf spark.pyspark.python=python3 \
        --conf spark.pyspark.driver.python=python3 \
        ${ADVISOR_LOADER} \
        --s3-path "${S3_ADVISOR_PATH}" \
        --iceberg-table "${ICEBERG_TABLE}" \
        --dynamodb-table "${DYNAMODB_TABLE}" \
        --num-partitions ${NUM_PARTITIONS} \
        --s3-bucket "${S3_BUCKET}"

    ADVISOR_EXIT_CODE=$?

    if [ $ADVISOR_EXIT_CODE -eq 0 ]; then
        echo ""
        echo "✓ Step 2 completed: Advisor data loaded"
    else
        echo ""
        echo "✗ Step 2 failed with exit code: $ADVISOR_EXIT_CODE"
    fi
    echo ""

    # ============================================================================
    # STEP 3: Bulk Load Metrics to Iceberg (NEW - replaces STEP 1)
    # ============================================================================

    echo "=========================================================================="
    echo "STEP 3: Bulk Loading Metrics to Iceberg Tables (PyIceberg)"
    echo "=========================================================================="
    echo "Reading from: s3://${S3_BUCKET}/test-target-metrics/{timestamp}/"
    echo "Target tables:"
    echo "  - ${CATALOG_NAMESPACE}.spark_metrics_task_stage_v5"
    echo "  - ${CATALOG_NAMESPACE}.spark_metrics_config_v5"
    echo "Lookback: Most recent 1 hour window"
    echo ""

    python3 ${BULK_METRICS_LOADER} \
        --s3-bucket "${S3_BUCKET}" \
        --lookback-hours 1

    BULK_METRICS_EXIT_CODE=$?

    if [ $BULK_METRICS_EXIT_CODE -eq 0 ]; then
        echo ""
        echo "✓ Step 3 completed: Bulk metrics loaded to Iceberg"
    else
        echo ""
        echo "✗ Step 3 failed with exit code: $BULK_METRICS_EXIT_CODE"
    fi
    echo ""

    # Set overall exit code (fail if any step fails)
    if [ $METRICS_EXIT_CODE -eq 0 ] && [ $ADVISOR_EXIT_CODE -eq 0 ] && [ $BULK_METRICS_EXIT_CODE -eq 0 ]; then
        EXIT_CODE=0
    else
        EXIT_CODE=1
    fi
else
    echo "✗ python3 not found"
    echo "  This script requires Python 3 with PyIceberg, pandas, and pyarrow"
    EXIT_CODE=1
    METRICS_EXIT_CODE=1
    ADVISOR_EXIT_CODE=1
    BULK_METRICS_EXIT_CODE=1
fi

# Cleanup temp directory if used
if [ -d "$WORK_DIR" ]; then
    echo "Cleaning up temporary files..."
    rm -rf "$WORK_DIR"
fi

# Calculate script execution duration
SCRIPT_END_TIME=$(date +%s)
SCRIPT_END_TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')
SCRIPT_DURATION=$((SCRIPT_END_TIME - SCRIPT_START_TIME))
SCRIPT_DURATION_MIN=$((SCRIPT_DURATION / 60))
SCRIPT_DURATION_SEC=$((SCRIPT_DURATION % 60))

echo ""
echo "=========================================================================="
echo "EXECUTION SUMMARY"
echo "=========================================================================="
echo "Step 1 - Metrics to Iceberg (OLD): $([ $METRICS_EXIT_CODE -eq 0 ] && echo '✓ Skipped (disabled)' || echo '✗ Skipped (disabled)')"
echo "  Exit Code:                       $METRICS_EXIT_CODE"
echo "  Tables: spark_metrics_task_stage_v5, spark_metrics_config_v5"
echo ""
echo "Step 2 - Advisor to Iceberg+DDB:   $([ $ADVISOR_EXIT_CODE -eq 0 ] && echo '✓ Success' || echo '✗ Failed')"
echo "  Exit Code:                       $ADVISOR_EXIT_CODE"
echo "  Tables: ${ICEBERG_TABLE}, ${DYNAMODB_TABLE}"
echo ""
echo "Step 3 - Bulk Metrics to Iceberg:  $([ $BULK_METRICS_EXIT_CODE -eq 0 ] && echo '✓ Success' || echo '✗ Failed')"
echo "  Exit Code:                       $BULK_METRICS_EXIT_CODE"
echo "  Tables: ${CATALOG_NAMESPACE}.spark_metrics_task_stage_v5, ${CATALOG_NAMESPACE}.spark_metrics_config_v5"
echo ""
echo "Overall Status:                    $([ $EXIT_CODE -eq 0 ] && echo '✓ SUCCESS' || echo '✗ PARTIAL FAILURE')"
echo "=========================================================================="
echo "Execution Timeline:"
echo "  Start Time:     ${SCRIPT_START_TIMESTAMP}"
echo "  End Time:       ${SCRIPT_END_TIMESTAMP}"
echo "  Total Duration: ${SCRIPT_DURATION_MIN} minutes ${SCRIPT_DURATION_SEC} seconds"
echo "=========================================================================="

# ============================================================================
# Upload Log File to S3
# ============================================================================

if [ ! -f "${LOG_PATH}" ]; then
    echo "ERROR: Log file not found at ${LOG_PATH}" >&2
    exit 1
fi

exec > /dev/null 2>&1
sleep 3
sync

LOG_SIZE=$(stat -f%z "${LOG_PATH}" 2>/dev/null || stat -c%s "${LOG_PATH}" 2>/dev/null)
echo ""
echo "=========================================================================="
echo "UPLOADING LOG FILE TO S3"
echo "=========================================================================="
echo "Local Log:      ${LOG_PATH}"
echo "Log Size:       ${LOG_SIZE} bytes"
echo "S3 Destination: ${S3_LOG_DESTINATION}"
echo ""

sync
sleep 1

S3_UPLOAD_SUCCESS=false
if aws s3 cp "${LOG_PATH}" "${S3_LOG_DESTINATION}" --region ${AWS_REGION}; then
    echo "✓ Log file uploaded successfully to S3"
    echo "  S3 Path: ${S3_LOG_DESTINATION}"
    S3_UPLOAD_SUCCESS=true
else
    echo "⚠ Warning: Failed to upload log file to S3"
    echo "  Log file is still available locally: ${LOG_PATH}"
fi
echo "=========================================================================="

echo ""
echo "=========================================================================="
echo "FINAL SUMMARY"
echo "=========================================================================="
echo "Exit Code:   $EXIT_CODE"
echo "Duration:    ${SCRIPT_DURATION_MIN}m ${SCRIPT_DURATION_SEC}s"
echo "Local Log:   ${LOG_PATH}"
if [ "$S3_UPLOAD_SUCCESS" = true ]; then
    echo "S3 Log:      ${S3_LOG_DESTINATION} ✓"
else
    echo "S3 Log:      Upload failed ✗"
fi
echo "=========================================================================="

exit $EXIT_CODE
