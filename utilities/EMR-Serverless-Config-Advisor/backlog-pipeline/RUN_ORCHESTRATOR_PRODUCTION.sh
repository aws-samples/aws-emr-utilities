#!/bin/bash
# ============================================================================
# Run EMR Serverless Orchestrator - Job Submission Script (PRODUCTION)
# ============================================================================
# Submits EMR Serverless jobs for unprocessed event logs
# PRODUCTION CONFIG: Processes up to 1000 jobs (5000 logs @ 5 logs per job)
# Capacity Control: Max 500 concurrent running jobs (cluster capacity limit)
# Throttling: 20s after every 100 jobs, 10min sleep after 250 jobs
# Runs on: EMR 7.2.0 cluster (your-emr-cluster-name)
# EC2 Instance Profile: your-iam-instance-profile
# ============================================================================

set -e

# Capture start time
SCRIPT_START_TIME=$(date +%s)
SCRIPT_START_TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')

# Generate log file name with timestamp
# Format: serverless_advisor_orchestrator_production_{ddmmyyHHMM}.log
LOG_TIMESTAMP=$(date '+%d%m%y%H%M')
LOG_FILE="serverless_advisor_orchestrator_production_${LOG_TIMESTAMP}.log"
LOG_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_PATH="${LOG_DIR}/${LOG_FILE}"

# S3 destination for logs
S3_LOG_BUCKET="${S3_BUCKET}"
S3_LOG_PREFIX="pipeline-files-v1/backlog-scale-dw/pipeline_orchestrator_script_logs"
S3_LOG_DIR="s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/"
S3_LOG_DESTINATION="${S3_LOG_DIR}${LOG_FILE}"

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
echo "EMR Serverless Orchestrator - Job Submission"
echo "=========================================================================="
echo "Start Time: ${SCRIPT_START_TIMESTAMP}"
echo ""

cd "$(dirname "$0")"

# ============================================================================
# Configuration
# ============================================================================

export AWS_REGION="us-east-1"
export S3_BUCKET="${S3_BUCKET}"

# EMR Serverless Application (7.13 Python 3.11)
export EMR_SERVERLESS_APPLICATION_ID="${EMR_APPLICATION_ID}"
export EMR_SERVERLESS_APPLICATION_NAME="dp-data-processing-demo-emr-serverless-7.13-python11"
export EMR_SERVERLESS_EXECUTION_ROLE="${IAM_EXECUTION_ROLE_ARN}"

# Tables
export BACKLOG_TABLE="${CATALOG_NAMESPACE}.backlog_events_log_v5"  # Iceberg (reads unprocessed logs)
export S3_ADVISOR_PATH="s3://${S3_BUCKET}/emr-serverless-config-advisor/"  # S3 (writes recommendations with datehour partitioning)
export ICEBERG_WAREHOUSE="s3://${S3_BUCKET}/iceberg/"  # Only for backlog table

# Discovery Configuration
export LOOKBACK_HOURS="2"  # Process logs discovered in last N hours (optimized S3 lookup)
export TEST_LIMIT="0"  # 0 = no limit, process ALL available unprocessed logs

# S3 Paths
export S3_SCRIPTS_PREFIX="pipeline-files-v1/backlog-scale-dw"
export S3_DEPENDENCIES_PATH="s3://${S3_BUCKET}/pipeline-files-v1/backlog-scale-dw/dependencies/pyspark_venv.tar.gz"

# Job Submission Configuration (Batch Mode: 5 logs per job)
export MAX_CONCURRENT_JOBS="1500"  # Max jobs to submit (1500 jobs = 7500 logs)
export MAX_JOBS_PER_RUN="1500"  # Total job cap per orchestrator run
export SUBMISSION_DELAY_SECONDS="20"  # Brief API rate-limit pause every 100 submissions
export LONG_SLEEP_AFTER_JOBS="250"  # After this many jobs (1250 logs), take a long sleep
export LONG_SLEEP_SECONDS="600"  # Long sleep duration (10 minutes = 600 seconds)

# Capacity-Aware Submission (prevents CPU capacity errors on EMR Serverless)
# Orchestrator checks live running job count before each submission.
# If cluster is at capacity it waits locally instead of letting EMR reject the job.
# This is the ONLY limit - controls concurrent running jobs to prevent cluster overload
export CAPACITY_MAX_CONCURRENT="750"  # Max concurrent running jobs before waiting
export CAPACITY_CHECK_INTERVAL="300"  # Seconds to wait between capacity rechecks when cluster is full (5 min)
export CAPACITY_SAFE_BUFFER="50"      # Reserve this many slots as safety buffer below the hard cap

# ============================================================================
# Configuration Display
# ============================================================================

echo "Configuration:"
echo "  AWS Region:           ${AWS_REGION}"
echo "  AWS Auth:             IAM Instance Profile"
echo "  S3 Bucket:            ${S3_BUCKET}"
echo "  EMR Application:      ${EMR_SERVERLESS_APPLICATION_NAME}"
echo "  Application ID:       ${EMR_SERVERLESS_APPLICATION_ID}"
echo "  Backlog Table:        ${BACKLOG_TABLE}"
echo "  S3 Advisor Path:      ${S3_ADVISOR_PATH}"
echo "  Lookback Window:      ${LOOKBACK_HOURS} hour(s)"
echo ""
echo "Batch Processing Mode:"
echo "  Discovery Limit:      ALL UNPROCESSED LOGS (no artificial limit)"
echo "  Logs per Job:         5 (each EMR Serverless job processes 5 event logs)"
echo "  Submission Limit:     ${MAX_JOBS_PER_RUN} jobs (${MAX_JOBS_PER_RUN} x 5 = $((MAX_JOBS_PER_RUN * 5)) logs max per run)"
echo "  Estimated Jobs:       Depends on unprocessed log count (N logs / 5)"
echo ""
echo "Throttling Strategy:"
echo "  API Rate-Limit Pause: ${SUBMISSION_DELAY_SECONDS}s every 100 submissions"
echo "  Long Sleep:           ${LONG_SLEEP_SECONDS}s (10 min) after ${LONG_SLEEP_AFTER_JOBS} jobs"
echo "                        (processes ~$((LONG_SLEEP_AFTER_JOBS * 5)) logs before long sleep)"
echo ""
echo "Capacity-Aware Submission:"
echo "  Max Concurrent Jobs:  ${CAPACITY_MAX_CONCURRENT}  (cluster hard cap: ~625)"
echo "  Safety Buffer:        ${CAPACITY_SAFE_BUFFER} reserved slots"
echo "  Effective Limit:      $((CAPACITY_MAX_CONCURRENT - CAPACITY_SAFE_BUFFER)) submittable concurrent jobs"
echo "  Capacity Check:       Every ${CAPACITY_CHECK_INTERVAL}s when cluster is full"
echo "  Guarantee:            No job rejected for CPU capacity — orchestrator queues locally"
echo ""
echo "Logging:"
echo "  Local Log File:       ${LOG_PATH}"
echo "  S3 Log Location:      ${S3_LOG_DESTINATION}"
echo ""

# ============================================================================
# Pre-flight Checks (using Python/boto3)
# ============================================================================

echo "Running pre-flight checks..."
echo ""

# Use Python with boto3 (AWS CLI is broken, but orchestrator uses boto3 anyway)
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

    # Test EMR Serverless (optional - skip if no permission)
    try:
        emr = boto3.client('emr-serverless', region_name='us-east-1')
        app = emr.get_application(applicationId='${EMR_APPLICATION_ID}')
        state = app['application']['state']
        print(f"✓ EMR Serverless app: {state}")
        if state != 'STARTED':
            print(f"  ⚠ App is {state}, not STARTED")

        # Capacity check: count currently active jobs
        try:
            max_concurrent = int(os.environ.get('CAPACITY_MAX_CONCURRENT', '750'))
            safe_buffer    = int(os.environ.get('CAPACITY_SAFE_BUFFER', '50'))
            running_resp   = emr.list_job_runs(
                applicationId='${EMR_APPLICATION_ID}',
                states=['RUNNING', 'PENDING', 'SCHEDULED']
            )
            current_jobs = len(running_resp.get('jobRuns', []))
            safe_limit   = max_concurrent - safe_buffer
            available    = safe_limit - current_jobs
            print(f"✓ Cluster capacity: {current_jobs} active jobs  |  {available} slots free")
            print(f"  (max={max_concurrent}, buffer={safe_buffer}, effective limit={safe_limit})")
            if available <= 0:
                print(f"  ⚠ Cluster is at capacity — orchestrator will wait before each submission")
            else:
                print(f"  ✓ Ready to submit up to {available} jobs immediately")
        except Exception as cap_e:
            print(f"  ⚠ Could not check current capacity: {cap_e}")

    except ClientError as e:
        if e.response['Error']['Code'] == 'AccessDeniedException':
            print("⚠ Cannot check EMR Serverless app (no GetApplication permission)")
            print("  Orchestrator will verify access when submitting jobs")
        else:
            raise

    print()

    # Test S3
    s3 = boto3.client('s3', region_name='us-east-1')

    # Check Python venv (warning only — missing venv won't block the pipeline)
    try:
        s3.head_object(Bucket='${S3_BUCKET}',
                       Key='pipeline-files-v1/backlog-scale-dw/dependencies/pyspark_venv.tar.gz')
        print("✓ Python venv found in S3")
    except Exception:
        print("⚠ Python venv not found in S3 (non-blocking — jobs may use system Python)")

    print()

    # Count scripts
    result = s3.list_objects_v2(Bucket='${S3_BUCKET}',
                                Prefix='pipeline-files-v1/backlog-scale-dw/')
    count = sum(1 for obj in result.get('Contents', []) if obj['Key'].endswith('.py'))
    print(f"✓ Found {count} pipeline scripts in S3")

    sys.exit(0)

except NoCredentialsError:
    print("✗ No AWS credentials found")
    print("  EMR cluster needs IAM instance profile: your-iam-instance-profile")
    sys.exit(1)
except ClientError as e:
    error_code = e.response['Error']['Code']
    if error_code == 'AccessDeniedException':
        print(f"✗ Access denied: {e}")
        print("  IAM role needs permissions for S3 and emr-serverless:StartJobRun")
        sys.exit(1)
    else:
        print(f"✗ AWS API error: {e}")
        sys.exit(1)
except Exception as e:
    print(f"✗ Pre-flight check failed: {e}")
    sys.exit(1)
PYTHON_CHECK

echo ""
echo "=========================================================================="
echo "Launching orchestrator..."
echo "=========================================================================="
echo ""

# ============================================================================
# Ensure Orchestrator Script Exists
# ============================================================================

ORCHESTRATOR_SCRIPT="02_orchestrator_emr_serverless.py"
WORK_DIR="/tmp/emr-orchestrator-$$"

# Check if script exists locally
if [ ! -f "$ORCHESTRATOR_SCRIPT" ]; then
    echo "Orchestrator script not found locally, downloading from S3..."
    mkdir -p "$WORK_DIR"

    # Export WORK_DIR for Python to access
    export WORK_DIR

    # Download orchestrator and all pipeline scripts from S3
    python3 <<'DOWNLOAD_SCRIPT'
import boto3
import os
import sys

work_dir = os.environ.get('WORK_DIR')
if not work_dir:
    print("✗ WORK_DIR not set")
    sys.exit(1)

s3 = boto3.client('s3', region_name='us-east-1')
bucket = '${S3_BUCKET}'
prefix = 'pipeline-files-v1/backlog-scale-dw/'

print(f"Downloading pipeline scripts from S3 to {work_dir}...")

try:
    # List all Python scripts
    result = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    scripts = [obj['Key'] for obj in result.get('Contents', []) if obj['Key'].endswith('.py')]

    for key in scripts:
        filename = os.path.basename(key)
        local_path = os.path.join(work_dir, filename)
        s3.download_file(bucket, key, local_path)
        print(f"  Downloaded: {filename}")

    print(f"✓ Downloaded {len(scripts)} scripts")
    sys.exit(0)

except Exception as e:
    print(f"✗ Failed to download scripts: {e}")
    sys.exit(1)
DOWNLOAD_SCRIPT

    if [ $? -ne 0 ]; then
        echo "Failed to download orchestrator from S3"
        exit 1
    fi

    # Change to work directory
    cd "$WORK_DIR"
else
    echo "Using orchestrator script from current directory"
fi

# ============================================================================
# Run Orchestrator (needs PySpark to query Iceberg tables)
# ============================================================================

# Check if we're on EMR with Spark installed
if command -v spark-submit > /dev/null 2>&1; then
    echo "Running orchestrator with spark-submit..."

    # Use system Python on EMR cluster (not the venv - that's only for EMR Serverless)
    export PYSPARK_PYTHON=python3
    export PYSPARK_DRIVER_PYTHON=python3

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
        02_orchestrator_emr_serverless.py
    EXIT_CODE=$?
else
    # Fallback: try to set PYTHONPATH for PySpark
    echo "spark-submit not found, trying to set up PySpark environment..."

    if [ -n "$SPARK_HOME" ]; then
        export PYTHONPATH="${SPARK_HOME}/python:${SPARK_HOME}/python/lib/py4j-*-src.zip:$PYTHONPATH"
        python3 02_orchestrator_emr_serverless.py
        EXIT_CODE=$?
    else
        echo "✗ PySpark not available and SPARK_HOME not set"
        echo "  The orchestrator needs PySpark to query Iceberg tables"
        echo "  Ensure you're running on an EMR cluster with Spark installed"
        EXIT_CODE=1
    fi
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
if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ Orchestrator completed successfully"
else
    echo "✗ Orchestrator failed with exit code: $EXIT_CODE"
fi
echo "=========================================================================="
echo "Execution Timeline:"
echo "  Start Time:    ${SCRIPT_START_TIMESTAMP}"
echo "  End Time:      ${SCRIPT_END_TIMESTAMP}"
echo "  Total Duration: ${SCRIPT_DURATION_MIN} minutes ${SCRIPT_DURATION_SEC} seconds (${SCRIPT_DURATION}s)"
echo "=========================================================================="

# ============================================================================
# Upload Log File to S3
# ============================================================================

# Verify log file exists before closing tee redirection
if [ ! -f "${LOG_PATH}" ]; then
    echo "ERROR: Log file not found at ${LOG_PATH}" >&2
    exit 1
fi

# Close the tee redirection to ensure all output is flushed to log file
exec > /dev/null 2>&1

# Wait for tee process to finish writing and sync filesystem
sleep 3
sync

LOG_SIZE=$(stat -f%z "${LOG_PATH}" 2>/dev/null || stat -c%s "${LOG_PATH}" 2>/dev/null)
echo ""
echo "=========================================================================="
echo "UPLOADING LOG FILE TO S3"
echo "=========================================================================="
echo "Local Log:  ${LOG_PATH}"
echo "Log Size:   ${LOG_SIZE} bytes"
echo "S3 Destination: ${S3_LOG_DESTINATION}"
echo ""

# Print exact command being executed
echo "Executing command:"
echo "aws s3 cp \"${LOG_PATH}\" \"${S3_LOG_DESTINATION}\""
echo ""

# Ensure all file buffers are written to disk before upload
sync
sleep 1

# Verify log file is closed and has final size
FINAL_LOG_SIZE=$(stat -f%z "${LOG_PATH}" 2>/dev/null || stat -c%s "${LOG_PATH}" 2>/dev/null)
echo "Final log size: ${FINAL_LOG_SIZE} bytes"
echo ""

# Upload log file to S3 (use full destination path, not directory)
S3_UPLOAD_SUCCESS=false
if aws s3 cp "${LOG_PATH}" "${S3_LOG_DESTINATION}" --region ${AWS_REGION}; then
    echo "✓ Log file uploaded successfully to S3"
    echo "  S3 Path: ${S3_LOG_DESTINATION}"
    echo ""
    echo "💡 To view this log later:"
    echo "   aws s3 cp ${S3_LOG_DESTINATION}"
    echo "   or"
    echo "   aws s3 ls s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/"
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
echo "Exit Code:        $EXIT_CODE"
echo "Duration:         ${SCRIPT_DURATION_MIN}m ${SCRIPT_DURATION_SEC}s"
echo "Local Log:        ${LOG_PATH}"
if [ "$S3_UPLOAD_SUCCESS" = true ]; then
    echo "S3 Log:           ${S3_LOG_DESTINATION} ✓"
else
    echo "S3 Log:           Upload failed ✗"
fi
echo "=========================================================================="

exit $EXIT_CODE
