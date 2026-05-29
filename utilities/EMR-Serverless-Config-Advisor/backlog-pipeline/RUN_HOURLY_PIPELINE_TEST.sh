#!/bin/bash
# ============================================================================
# Hourly Pipeline Executor - TEST VERSION (10 jobs)
# ============================================================================
# This is a TEST version that uses RUN_ORCHESTRATOR.sh (10 jobs limit)
# instead of RUN_ORCHESTRATOR_PRODUCTION.sh (500 jobs).
#
# This script runs both the S3-to-Iceberg loader and the orchestrator,
# captures all logs, and uploads them to S3.
#
# Usage:
#   ./RUN_HOURLY_PIPELINE_TEST.sh
# ============================================================================

set +e  # Don't exit on error - we want both jobs to run

# Capture pipeline start time
PIPELINE_START_TIME=$(date +%s)
PIPELINE_START_TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')

# Generate timestamp for this pipeline run
RUN_TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
LOG_DIR="$(cd "$(dirname "$0")" && pwd)"

# Master pipeline log
PIPELINE_LOG="${LOG_DIR}/hourly_pipeline_test_${RUN_TIMESTAMP}.log"

# S3 destination for logs
S3_LOG_BUCKET="${S3_BUCKET}"
S3_LOG_PREFIX="pipeline-files-v1/backlog-scale-dw/hourly_pipeline_logs_test"
S3_PIPELINE_LOG_DEST="s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/pipeline_test_${RUN_TIMESTAMP}.log"

# Start logging
exec > >(tee -a "${PIPELINE_LOG}") 2>&1

echo "=========================================================================="
echo "HOURLY PIPELINE EXECUTOR - TEST VERSION (10 jobs)"
echo "=========================================================================="
echo "Pipeline Start: ${PIPELINE_START_TIMESTAMP}"
echo "Run ID:         ${RUN_TIMESTAMP}"
echo "Log Directory:  ${LOG_DIR}"
echo "Pipeline Log:   ${PIPELINE_LOG}"
echo "=========================================================================="
echo ""

# Clear AWS Profile
unset AWS_PROFILE 2>/dev/null || true
unset AWS_DEFAULT_PROFILE 2>/dev/null || true

# Set AWS Region
export AWS_REGION="us-east-1"

# Change to script directory
cd "${LOG_DIR}"

# ============================================================================
# Email Config (defined early so failure trap can use them)
# ============================================================================

# ============================================================================
# Failure Email Function + Exit Trap
# ============================================================================
PIPELINE_COMPLETED=false
CURRENT_STEP="Initialization"

send_failure_email() {
    local reason="$1"
    local fail_time
    fail_time=$(date '+%Y-%m-%d %H:%M:%S %Z')
    local email_date
    email_date=$(date '+%Y%m%d-%H')

    FAIL_SUBJECT="${email_date}_DW_emr-serverless-advisor-pipeline-metrics" \
    FAIL_REASON="$reason" \
    FAIL_RUN_ID="$RUN_TIMESTAMP" \
    FAIL_START="$PIPELINE_START_TIMESTAMP" \
    FAIL_END="$fail_time" \
    FAIL_TO_1="$EMAIL_TO_1" \
    FAIL_TO_2="$EMAIL_TO_2" \
    FAIL_FROM="$EMAIL_FROM" \
    python3 <<'FAIL_EMAIL'
import smtplib, os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

subject   = os.environ['FAIL_SUBJECT']
from_addr = os.environ['FAIL_FROM']
to_addrs  = [os.environ['FAIL_TO_1'], os.environ['FAIL_TO_2']]
smtp_host = os.environ['FAIL_SMTP_HOST']
smtp_port = int(os.environ['FAIL_SMTP_PORT'])

body = f"""[DW] EMR Serverless Advisor Pipeline - FAILED
==========================================================

Pipeline Run ID : {os.environ['FAIL_RUN_ID']}
Start Time      : {os.environ['FAIL_START']}
Failure Time    : {os.environ['FAIL_END']}

FAILURE REASON
--------------
{os.environ['FAIL_REASON']}

ACTION REQUIRED
---------------
Check the pipeline log for details and re-run manually if needed.

TARGET TABLES
-------------
Iceberg  : ${CATALOG_NAMESPACE}.serverless_config_advisor_v5
DynamoDB : prod-dynamodb-egdataplatform-dw-dataproc-emr-serverless-config-recommander

---
Sent automatically by EMR Serverless Advisor Pipeline
Account: ${AWS_ACCOUNT_ID} | Region: us-east-1
"""

msg = MIMEMultipart()
msg["From"]    = from_addr
msg["To"]      = ", ".join(to_addrs)
msg["Subject"] = subject
msg.attach(MIMEText(body, "plain"))

try:
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        smtp.sendmail(from_addr, to_addrs, msg.as_string())
    print(f"✓ Failure email sent to {', '.join(to_addrs)}")
    print(f"  Subject: {subject}")
except Exception as e:
    print(f"✗ Could not send failure email: {e}")
FAIL_EMAIL
}

on_exit_handler() {
    local exit_code=$?
    if [ "$PIPELINE_COMPLETED" != "true" ] && [ $exit_code -ne 0 ]; then
        echo ""
        echo "=========================================================================="
        echo "PIPELINE EXITED EARLY (exit code: ${exit_code})"
        echo "Failed at: ${CURRENT_STEP}"
        echo "=========================================================================="
        echo "Sending failure email..."
        send_failure_email "Pipeline exited early at: ${CURRENT_STEP} (exit code: ${exit_code})"
    fi
}

trap on_exit_handler EXIT

# ============================================================================
# STEP 0: Download Required Scripts from S3
# ============================================================================

echo ""
echo "=========================================================================="
echo "STEP 0: DOWNLOADING REQUIRED SCRIPTS FROM S3"
CURRENT_STEP="STEP 0: Downloading required scripts from S3"
echo "=========================================================================="
echo "Start Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

S3_SCRIPTS_BUCKET="${S3_BUCKET}"
S3_SCRIPTS_PREFIX="pipeline-files-v1/backlog-scale-dw"

python3 <<DOWNLOAD_SCRIPTS || { echo "✗ Failed to download required scripts from S3"; echo "  Cannot proceed with pipeline execution"; echo "=========================================================================="; exit 1; }
import boto3
import os
import sys

bucket  = "${S3_SCRIPTS_BUCKET}"
prefix  = "${S3_SCRIPTS_PREFIX}"
log_dir = "${LOG_DIR}"
region  = "${AWS_REGION}"

scripts = [
    "RUN_S3_TO_ICEBERG_DYNAMODB.sh",
    "RUN_ORCHESTRATOR.sh",
]

s3 = boto3.client('s3', region_name=region)
all_ok = True

for script in scripts:
    key       = f"{prefix}/{script}"
    dest      = os.path.join(log_dir, script)
    s3_path   = f"s3://{bucket}/{key}"
    print(f"Downloading: {script}")
    print(f"  From: {s3_path}")
    try:
        s3.download_file(bucket, key, dest)
        os.chmod(dest, 0o755)
        print(f"  Status: ✓ Downloaded and made executable")
    except Exception as e:
        print(f"  Status: ✗ Failed to download: {e}")
        all_ok = False
    print()

if not all_ok:
    sys.exit(1)

print("✓ All required scripts downloaded successfully")
DOWNLOAD_SCRIPTS
echo "=========================================================================="
echo ""

# ============================================================================
# STEP 1: Run S3 to Iceberg/DynamoDB Loader
# ============================================================================

echo ""
echo "=========================================================================="
echo "STEP 1: S3 TO ICEBERG/DYNAMODB LOADER"
CURRENT_STEP="STEP 1: S3 to Iceberg/DynamoDB loader"
echo "=========================================================================="
echo "Start Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

LOADER_SCRIPT="${LOG_DIR}/RUN_S3_TO_ICEBERG_DYNAMODB.sh"
LOADER_EXIT_CODE=0

echo "Executing: $LOADER_SCRIPT"
echo ""

# Run the loader script
bash "$LOADER_SCRIPT"
LOADER_EXIT_CODE=$?

echo ""
echo "--------------------------------------------------------------------------"
if [ $LOADER_EXIT_CODE -eq 0 ]; then
    echo "✓ Loader completed successfully (exit code: 0)"
else
    echo "✗ Loader failed with exit code: $LOADER_EXIT_CODE"
    echo "  Continuing to next step anyway..."
fi
echo "--------------------------------------------------------------------------"
echo ""

# ============================================================================
# STEP 2: Run Orchestrator TEST (10 jobs)
# ============================================================================

echo ""
echo "=========================================================================="
echo "STEP 2: ORCHESTRATOR TEST (10 jobs limit)"
CURRENT_STEP="STEP 2: EMR Serverless orchestrator (TEST)"
echo "=========================================================================="
echo "Start Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

ORCHESTRATOR_SCRIPT="${LOG_DIR}/RUN_ORCHESTRATOR.sh"
ORCHESTRATOR_EXIT_CODE=0

echo "Executing: $ORCHESTRATOR_SCRIPT"
echo ""

# Run the orchestrator script
bash "$ORCHESTRATOR_SCRIPT"
ORCHESTRATOR_EXIT_CODE=$?

echo ""
echo "--------------------------------------------------------------------------"
if [ $ORCHESTRATOR_EXIT_CODE -eq 0 ]; then
    echo "✓ Orchestrator completed successfully (exit code: 0)"
else
    echo "✗ Orchestrator failed with exit code: $ORCHESTRATOR_EXIT_CODE"
fi
echo "--------------------------------------------------------------------------"
echo ""

# ============================================================================
# Calculate Pipeline Duration
# ============================================================================

PIPELINE_END_TIME=$(date +%s)
PIPELINE_END_TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')
PIPELINE_DURATION=$((PIPELINE_END_TIME - PIPELINE_START_TIME))
PIPELINE_DURATION_MIN=$((PIPELINE_DURATION / 60))
PIPELINE_DURATION_SEC=$((PIPELINE_DURATION % 60))

# ============================================================================
# STEP 3: Upload All Logs to S3
# ============================================================================

echo ""
echo "=========================================================================="
echo "STEP 3: UPLOADING LOGS TO S3"
CURRENT_STEP="STEP 3: Uploading logs to S3"
echo "=========================================================================="

# Stop logging to file temporarily so we can upload it
exec > /dev/null 2>&1
sleep 2
sync

# Upload all logs via Python boto3 (aws CLI has no credentials on this cluster)
UPLOAD_RESULTS=$(python3 <<UPLOAD_LOGS
import boto3
import os
import glob
import time

bucket     = "${S3_LOG_BUCKET}"
prefix     = "${S3_LOG_PREFIX}"
log_dir    = "${LOG_DIR}"
pipeline_log = "${PIPELINE_LOG}"
region     = "${AWS_REGION}"

s3 = boto3.client('s3', region_name=region)

success = 0
failed  = 0

def upload(local_path, s3_key):
    global success, failed
    name = os.path.basename(local_path)
    dest = f"s3://{bucket}/{s3_key}"
    print(f"  Uploading: {name}")
    print(f"  To:        {dest}")
    try:
        s3.upload_file(local_path, bucket, s3_key)
        print(f"  Status:    ✓ Success")
        success += 1
    except Exception as e:
        print(f"  Status:    ✗ Failed: {e}")
        failed += 1
    print()

# Loader logs
loader_logs = glob.glob(os.path.join(log_dir, "s3_to_iceberg_dynamodb_*.log"))
loader_logs = [f for f in loader_logs if (time.time() - os.path.getmtime(f)) < 7200]
if loader_logs:
    print("Loader logs found:")
    for f in loader_logs:
        upload(f, f"{prefix}/loader_{os.path.basename(f)}")
else:
    print("No loader logs found (checked last 2 hours)")
    print()

# Orchestrator TEST logs
orch_logs = glob.glob(os.path.join(log_dir, "serverless_advisor_orchestrator_pipeline_*.log"))
orch_logs = [f for f in orch_logs if (time.time() - os.path.getmtime(f)) < 7200]
if orch_logs:
    print("Orchestrator logs found:")
    for f in orch_logs:
        upload(f, f"{prefix}/orchestrator_{os.path.basename(f)}")
else:
    print("No orchestrator logs found (checked last 2 hours)")
    print()

# Pipeline master log
print("Pipeline master log:")
if os.path.isfile(pipeline_log):
    size = os.path.getsize(pipeline_log)
    print(f"  Size:      {size} bytes")
    s3_key = "${S3_PIPELINE_LOG_DEST}".replace("s3://${S3_LOG_BUCKET}/", "")
    upload(pipeline_log, s3_key)
else:
    print(f"  Status:    ✗ Pipeline log file not found")
    failed += 1

print(f"Log Upload Summary:")
print(f"  Successful: {success}")
print(f"  Failed:     {failed}")

# Export counts for shell
print(f"__UPLOAD_SUCCESS_COUNT__={success}")
print(f"__UPLOAD_FAIL_COUNT__={failed}")
UPLOAD_LOGS
)

echo "$UPLOAD_RESULTS"

UPLOAD_SUCCESS_COUNT=$(echo "$UPLOAD_RESULTS" | grep "__UPLOAD_SUCCESS_COUNT__=" | cut -d= -f2)
UPLOAD_FAIL_COUNT=$(echo "$UPLOAD_RESULTS"    | grep "__UPLOAD_FAIL_COUNT__="    | cut -d= -f2)
UPLOAD_SUCCESS_COUNT=${UPLOAD_SUCCESS_COUNT:-0}
UPLOAD_FAIL_COUNT=${UPLOAD_FAIL_COUNT:-0}

echo "=========================================================================="

# ============================================================================
# Final Summary
# ============================================================================

# Determine overall pipeline status
PIPELINE_EXIT_CODE=0

if [ $LOADER_EXIT_CODE -ne 0 ] || [ $ORCHESTRATOR_EXIT_CODE -ne 0 ]; then
    PIPELINE_EXIT_CODE=1
fi

echo ""
echo "=========================================================================="
echo "PIPELINE EXECUTION SUMMARY - TEST VERSION"
echo "=========================================================================="
echo "Pipeline Run ID:          ${RUN_TIMESTAMP}"
echo "Start Time:               ${PIPELINE_START_TIMESTAMP}"
echo "End Time:                 ${PIPELINE_END_TIMESTAMP}"
echo "Total Duration:           ${PIPELINE_DURATION_MIN} minutes ${PIPELINE_DURATION_SEC} seconds"
echo "--------------------------------------------------------------------------"
echo "Step 1 - S3 Loader:       $([ $LOADER_EXIT_CODE -eq 0 ] && echo '✓ Success' || echo '✗ Failed')"
echo "  Exit Code:              $LOADER_EXIT_CODE"
echo "Step 2 - Orchestrator:    $([ $ORCHESTRATOR_EXIT_CODE -eq 0 ] && echo '✓ Success' || echo '✗ Failed')"
echo "  Exit Code:              $ORCHESTRATOR_EXIT_CODE"
echo "  Jobs Submitted:         ~10 (TEST limit)"
echo "Step 3 - Log Upload:      ✓ Completed"
echo "  Uploaded:               ${UPLOAD_SUCCESS_COUNT} logs"
echo "  Failed:                 ${UPLOAD_FAIL_COUNT} logs"
echo "--------------------------------------------------------------------------"
echo "Overall Pipeline Status:  $([ $PIPELINE_EXIT_CODE -eq 0 ] && echo '✓ SUCCESS' || echo '✗ PARTIAL FAILURE')"
echo "Pipeline Exit Code:       $PIPELINE_EXIT_CODE"
echo "--------------------------------------------------------------------------"
echo "Master Pipeline Log:      ${S3_PIPELINE_LOG_DEST}"
echo "All Logs Location:        s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/"
echo "=========================================================================="
echo ""

# List recent pipeline runs in S3
echo "Recent TEST pipeline runs in S3:"
python3 -c "
import boto3
s3 = boto3.client('s3', region_name='${AWS_REGION}')
resp = s3.list_objects_v2(Bucket='${S3_LOG_BUCKET}', Prefix='${S3_LOG_PREFIX}/')
objs = [o for o in resp.get('Contents', []) if 'pipeline_test' in o['Key'] and o['Key'].endswith('.log')]
objs.sort(key=lambda x: x['LastModified'], reverse=True)
for o in objs[:5]:
    print(f\"  {o['LastModified'].strftime('%Y-%m-%d %H:%M:%S')}  {o['Key']}\")
" 2>/dev/null || echo "  Unable to list S3 logs"
echo ""

# ============================================================================
# STEP 4: Send Email Notification via SMTP (smtplib)
# ============================================================================

echo ""
echo "=========================================================================="
echo "STEP 4: SENDING EMAIL NOTIFICATION"
CURRENT_STEP="STEP 4: Sending email notification"
echo "=========================================================================="

EMAIL_DATE=$(date '+%Y%m%d-%H')
EMAIL_SUBJECT="${EMAIL_DATE}_DW_emr-serverless-advisor-pipeline-metrics"

echo "Subject: ${EMAIL_SUBJECT}"
echo "To:      ${EMAIL_TO_1}, ${EMAIL_TO_2}"
echo ""

# Collect submitted EMR Serverless job run IDs from orchestrator logs
SUBMITTED_JOBS_LIST=""
JOB_COUNT=0

if [ -n "$ORCHESTRATOR_LOGS" ]; then
    for log_file in $ORCHESTRATOR_LOGS; do
        JOBS=$(grep -oP "(?<=Job submitted: )[A-Za-z0-9]+" "$log_file" 2>/dev/null | sort -u)
        if [ -n "$JOBS" ]; then
            while IFS= read -r job_id; do
                [ -n "$job_id" ] || continue
                SUBMITTED_JOBS_LIST="${SUBMITTED_JOBS_LIST}  - ${job_id}"$'\n'
                JOB_COUNT=$((JOB_COUNT + 1))
            done <<< "$JOBS"
        fi
    done
fi

if [ $JOB_COUNT -eq 0 ]; then
    SUBMITTED_JOBS_LIST="  No job run IDs found in logs (check orchestrator log for details)"
fi

# Collect S3 log paths for the email
S3_LOG_PATHS=""
if [ -n "$ORCHESTRATOR_LOGS" ]; then
    for log_file in $ORCHESTRATOR_LOGS; do
        log_name=$(basename "$log_file")
        S3_LOG_PATHS="${S3_LOG_PATHS}  s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/orchestrator_${log_name}"$'\n'
    done
fi
if [ -n "$LOADER_LOGS" ]; then
    for log_file in $LOADER_LOGS; do
        log_name=$(basename "$log_file")
        S3_LOG_PATHS="${S3_LOG_PATHS}  s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/loader_${log_name}"$'\n'
    done
fi
S3_LOG_PATHS="${S3_LOG_PATHS}  ${S3_PIPELINE_LOG_DEST}"$'\n'

# Send email via Python smtplib (no AWS permissions required)
python3 - <<PYEOF
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

subject   = "${EMAIL_SUBJECT}"
from_addr = "${EMAIL_FROM}"
to_addrs  = ["${EMAIL_TO_1}", "${EMAIL_TO_2}"]
smtp_host = "${SMTP_HOST}"
smtp_port = int("${SMTP_PORT}")

body = """[DW] EMR Serverless Advisor Pipeline - Execution Report
==========================================================

Pipeline Run ID : ${RUN_TIMESTAMP}
Start Time      : ${PIPELINE_START_TIMESTAMP}
End Time        : ${PIPELINE_END_TIMESTAMP}
Duration        : ${PIPELINE_DURATION_MIN}m ${PIPELINE_DURATION_SEC}s

PIPELINE STATUS
---------------
Step 1 - S3 to Iceberg/DynamoDB      : $([ $LOADER_EXIT_CODE -eq 0 ] && echo "SUCCESS" || echo "FAILED (exit code: $LOADER_EXIT_CODE)")
Step 2 - EMR Serverless Orchestrator : $([ $ORCHESTRATOR_EXIT_CODE -eq 0 ] && echo "SUCCESS" || echo "FAILED (exit code: $ORCHESTRATOR_EXIT_CODE)")
Overall Status                        : $([ $PIPELINE_EXIT_CODE -eq 0 ] && echo "SUCCESS" || echo "PARTIAL FAILURE")

EMR SERVERLESS APPLICATIONS SUBMITTED (${JOB_COUNT} jobs)
----------------------------------------------------------
${SUBMITTED_JOBS_LIST}
S3 LOG FILE LOCATIONS
---------------------
Logs for this run:
${S3_LOG_PATHS}
All pipeline logs:
  s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/

TARGET TABLES
-------------
Iceberg  : ${CATALOG_NAMESPACE}.serverless_config_advisor_v5
DynamoDB : prod-dynamodb-egdataplatform-dw-dataproc-emr-serverless-config-recommander

---
Sent automatically by EMR Serverless Advisor Pipeline
Account: ${AWS_ACCOUNT_ID} | Region: us-east-1
"""

msg = MIMEMultipart()
msg["From"]    = from_addr
msg["To"]      = ", ".join(to_addrs)
msg["Subject"] = subject
msg.attach(MIMEText(body, "plain"))

try:
    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        smtp.sendmail(from_addr, to_addrs, msg.as_string())
    print(f"✓ Email sent successfully via {smtp_host}:{smtp_port}")
    print(f"  To: {', '.join(to_addrs)}")
    print(f"  Subject: {subject}")
except Exception as e:
    print(f"✗ Failed to send email: {e}")
    sys.exit(1)
PYEOF

EMAIL_EXIT_CODE=$?

if [ $EMAIL_EXIT_CODE -eq 0 ]; then
    echo "✓ Email notification sent"
else
    echo "⚠ Email notification failed (non-blocking)"
fi
echo "=========================================================================="

PIPELINE_COMPLETED=true
exit $PIPELINE_EXIT_CODE
