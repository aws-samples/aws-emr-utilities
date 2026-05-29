#!/bin/bash
# ============================================================================
# Backfill Pipeline Executor
# ============================================================================
# Runs the EMR Serverless orchestrator for backfill — processes unprocessed
# event logs beyond the normal 2-hour hourly window.
#
# ALL parameters are configurable via environment variables:
#
#   LOOKBACK_HOURS   — How many hours back to scan for unprocessed logs
#                      Default: 6 hours
#                      Example: LOOKBACK_HOURS=48 (2-day backfill)
#
#   MAX_JOBS_PER_RUN — Max jobs to submit in this single run
#                      Default: 5000 (25,000 logs)
#                      Example: MAX_JOBS_PER_RUN=1000
#
# Usage:
#   # Default: 48h lookback, 5000 jobs
#   ./RUN_BACKFILL_PIPELINE.sh
#
#   # 6-hour chunk (recommended for large backfills to avoid slow S3 scan)
#   LOOKBACK_HOURS=6 MAX_JOBS_PER_RUN=2000 ./RUN_BACKFILL_PIPELINE.sh
#
#   # Full 7-day backfill (run multiple times until drained)
#   LOOKBACK_HOURS=168 MAX_JOBS_PER_RUN=5000 ./RUN_BACKFILL_PIPELINE.sh
#
# NOTE: This script runs the orchestrator ONLY (not the S3-to-Iceberg loader).
#       The hourly pipeline continues running independently during backfill.
#       Both pipelines can run simultaneously — no conflicts.
# ============================================================================

set +e  # Don't exit on error — run all steps, capture exit codes

# Capture pipeline start time
PIPELINE_START_TIME=$(date +%s)
PIPELINE_START_TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S %Z')

# Generate timestamp for this pipeline run
RUN_TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
LOG_DIR="$(cd "$(dirname "$0")" && pwd)"

# Master pipeline log
PIPELINE_LOG="${LOG_DIR}/backfill_pipeline_${RUN_TIMESTAMP}.log"

# S3 destination for logs
S3_LOG_BUCKET="${S3_BUCKET}"
S3_LOG_PREFIX="pipeline-files-v1/backlog-scale-dw/backfill_pipeline_logs"
S3_PIPELINE_LOG_DEST="s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/backfill_${RUN_TIMESTAMP}.log"

# Start logging
exec > >(tee -a "${PIPELINE_LOG}") 2>&1

echo "=========================================================================="
echo "BACKFILL PIPELINE EXECUTOR"
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
# Backfill Parameters (configurable via environment variables)
# ============================================================================

# Accept from environment or use defaults
export LOOKBACK_HOURS="${LOOKBACK_HOURS:-6}"         # Default: 6 hours (override with LOOKBACK_HOURS=48)
export MAX_JOBS_PER_RUN="${MAX_JOBS_PER_RUN:-5000}" # Default: 5000 jobs (25,000 logs)

echo "=========================================================================="
echo "BACKFILL PARAMETERS"
echo "=========================================================================="
echo "  LOOKBACK_HOURS   : ${LOOKBACK_HOURS} hours"
echo "  MAX_JOBS_PER_RUN : ${MAX_JOBS_PER_RUN} jobs"
echo "  Logs covered     : ~$((LOOKBACK_HOURS * 3500)) event logs (est. 3500/hr)"
echo "  Jobs needed      : ~$((LOOKBACK_HOURS * 3500 / 5)) jobs (5 logs/job)"
echo "  This run cap     : ${MAX_JOBS_PER_RUN} jobs = $((MAX_JOBS_PER_RUN * 5)) logs max"
echo ""
echo "  To override: LOOKBACK_HOURS=6 MAX_JOBS_PER_RUN=2000 ./RUN_BACKFILL_PIPELINE.sh"
echo "=========================================================================="
echo ""

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

    FAIL_SUBJECT="${email_date}_DW_emr-serverless-advisor-backfill-pipeline" \
    FAIL_REASON="$reason" \
    FAIL_RUN_ID="$RUN_TIMESTAMP" \
    FAIL_START="$PIPELINE_START_TIMESTAMP" \
    FAIL_END="$fail_time" \
    FAIL_LOOKBACK="$LOOKBACK_HOURS" \
    FAIL_MAX_JOBS="$MAX_JOBS_PER_RUN" \
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

body = f"""[DW] EMR Serverless Advisor - BACKFILL PIPELINE FAILED
==========================================================

Pipeline Run ID  : {os.environ['FAIL_RUN_ID']}
Start Time       : {os.environ['FAIL_START']}
Failure Time     : {os.environ['FAIL_END']}

BACKFILL PARAMETERS
-------------------
Lookback Hours   : {os.environ['FAIL_LOOKBACK']} hours
Max Jobs Per Run : {os.environ['FAIL_MAX_JOBS']} jobs

FAILURE REASON
--------------
{os.environ['FAIL_REASON']}

ACTION REQUIRED
---------------
Check the backfill pipeline log for details and re-run manually if needed.

Re-run command:
  LOOKBACK_HOURS={os.environ['FAIL_LOOKBACK']} MAX_JOBS_PER_RUN={os.environ['FAIL_MAX_JOBS']} ./RUN_BACKFILL_PIPELINE.sh

TARGET TABLES
-------------
Iceberg  : ${CATALOG_NAMESPACE}.serverless_config_advisor_v5
DynamoDB : prod-dynamodb-egdataplatform-dw-dataproc-emr-serverless-config-recommander

---
Sent automatically by EMR Serverless Advisor Backfill Pipeline
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
        echo "BACKFILL PIPELINE EXITED EARLY (exit code: ${exit_code})"
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
echo "=========================================================================="
CURRENT_STEP="STEP 0: Downloading required scripts from S3"
echo "Start Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

S3_SCRIPTS_BUCKET="${S3_BUCKET}"
S3_SCRIPTS_PREFIX="pipeline-files-v1/backlog-scale-dw"

python3 <<DOWNLOAD_SCRIPTS || { echo "✗ Failed to download required scripts from S3"; echo "  Cannot proceed with backfill execution"; echo "=========================================================================="; exit 1; }
import boto3
import os
import sys

bucket  = "${S3_SCRIPTS_BUCKET}"
prefix  = "${S3_SCRIPTS_PREFIX}"
log_dir = "${LOG_DIR}"
region  = "${AWS_REGION}"

# Backfill only needs the orchestrator (no S3-to-Iceberg loader)
scripts = [
    "RUN_ORCHESTRATOR_PRODUCTION.sh",
]

s3 = boto3.client('s3', region_name=region)
all_ok = True

for script in scripts:
    key     = f"{prefix}/{script}"
    dest    = os.path.join(log_dir, script)
    s3_path = f"s3://{bucket}/{key}"
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
# STEP 1: Run Backfill Orchestrator
# ============================================================================

echo ""
echo "=========================================================================="
echo "STEP 1: BACKFILL ORCHESTRATOR"
echo "=========================================================================="
CURRENT_STEP="STEP 1: Backfill orchestrator"
echo "Start Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  LOOKBACK_HOURS   = ${LOOKBACK_HOURS}"
echo "  MAX_JOBS_PER_RUN = ${MAX_JOBS_PER_RUN}"
echo ""

ORCHESTRATOR_SCRIPT="${LOG_DIR}/RUN_ORCHESTRATOR_PRODUCTION.sh"
ORCHESTRATOR_EXIT_CODE=0

echo "Executing: $ORCHESTRATOR_SCRIPT"
echo ""

# Run orchestrator with backfill parameters
# LOOKBACK_HOURS and MAX_JOBS_PER_RUN are already exported above
bash "$ORCHESTRATOR_SCRIPT"
ORCHESTRATOR_EXIT_CODE=$?

echo ""
echo "--------------------------------------------------------------------------"
if [ $ORCHESTRATOR_EXIT_CODE -eq 0 ]; then
    echo "✓ Backfill orchestrator completed successfully (exit code: 0)"
else
    echo "✗ Backfill orchestrator failed with exit code: $ORCHESTRATOR_EXIT_CODE"
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
# STEP 2: Upload All Logs to S3
# ============================================================================

echo ""
echo "=========================================================================="
echo "STEP 2: UPLOADING LOGS TO S3"
echo "=========================================================================="
CURRENT_STEP="STEP 2: Uploading logs to S3"

# Stop logging to file temporarily so we can upload it
exec > /dev/null 2>&1
sleep 2
sync

# Upload all logs via Python boto3
UPLOAD_RESULTS=$(python3 <<UPLOAD_LOGS
import boto3
import os
import glob
import time

bucket       = "${S3_LOG_BUCKET}"
prefix       = "${S3_LOG_PREFIX}"
log_dir      = "${LOG_DIR}"
pipeline_log = "${PIPELINE_LOG}"
region       = "${AWS_REGION}"

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

# Orchestrator logs generated during this run
orch_logs = glob.glob(os.path.join(log_dir, "serverless_advisor_orchestrator_production_*.log"))
orch_logs = [f for f in orch_logs if (time.time() - os.path.getmtime(f)) < 7200]
if orch_logs:
    print("Orchestrator logs found:")
    for f in orch_logs:
        upload(f, f"{prefix}/orchestrator_{os.path.basename(f)}")
else:
    print("No orchestrator logs found (checked last 2 hours)")
    print()

# Pipeline master log
print("Backfill pipeline master log:")
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

PIPELINE_EXIT_CODE=0
if [ $ORCHESTRATOR_EXIT_CODE -ne 0 ]; then
    PIPELINE_EXIT_CODE=1
fi

echo ""
echo "=========================================================================="
echo "BACKFILL PIPELINE EXECUTION SUMMARY"
echo "=========================================================================="
echo "Pipeline Run ID:          ${RUN_TIMESTAMP}"
echo "Start Time:               ${PIPELINE_START_TIMESTAMP}"
echo "End Time:                 ${PIPELINE_END_TIMESTAMP}"
echo "Total Duration:           ${PIPELINE_DURATION_MIN} minutes ${PIPELINE_DURATION_SEC} seconds"
echo "--------------------------------------------------------------------------"
echo "Backfill Parameters:"
echo "  LOOKBACK_HOURS   :      ${LOOKBACK_HOURS} hours"
echo "  MAX_JOBS_PER_RUN :      ${MAX_JOBS_PER_RUN} jobs"
echo "--------------------------------------------------------------------------"
echo "Step 1 - Backfill Orchestrator: $([ $ORCHESTRATOR_EXIT_CODE -eq 0 ] && echo '✓ Success' || echo '✗ Failed')"
echo "  Exit Code:              $ORCHESTRATOR_EXIT_CODE"
echo "Step 2 - Log Upload:      ✓ Completed"
echo "  Uploaded:               ${UPLOAD_SUCCESS_COUNT} logs"
echo "  Failed:                 ${UPLOAD_FAIL_COUNT} logs"
echo "--------------------------------------------------------------------------"
echo "Overall Status:           $([ $PIPELINE_EXIT_CODE -eq 0 ] && echo '✓ SUCCESS' || echo '✗ FAILED')"
echo "Pipeline Exit Code:       $PIPELINE_EXIT_CODE"
echo "--------------------------------------------------------------------------"
echo "Master Pipeline Log:      ${S3_PIPELINE_LOG_DEST}"
echo "All Backfill Logs:        s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/"
echo "=========================================================================="
echo ""

# List recent backfill runs in S3
echo "Recent backfill runs in S3:"
python3 -c "
import boto3
s3 = boto3.client('s3', region_name='${AWS_REGION}')
resp = s3.list_objects_v2(Bucket='${S3_LOG_BUCKET}', Prefix='${S3_LOG_PREFIX}/')
objs = [o for o in resp.get('Contents', []) if 'backfill_' in o['Key'] and o['Key'].endswith('.log')]
objs.sort(key=lambda x: x['LastModified'], reverse=True)
for o in objs[:5]:
    print(f\"  {o['LastModified'].strftime('%Y-%m-%d %H:%M:%S')}  {o['Key']}\")
" 2>/dev/null || echo "  Unable to list S3 logs"
echo ""

# ============================================================================
# STEP 3: Send Email Notification via SMTP (smtplib)
# ============================================================================

echo ""
echo "=========================================================================="
echo "STEP 3: SENDING EMAIL NOTIFICATION"
echo "=========================================================================="
CURRENT_STEP="STEP 3: Sending email notification"

EMAIL_DATE=$(date '+%Y%m%d-%H')
EMAIL_SUBJECT="${EMAIL_DATE}_DW_emr-serverless-advisor-backfill-pipeline"

echo "Subject: ${EMAIL_SUBJECT}"
echo "To:      ${EMAIL_TO_1}, ${EMAIL_TO_2}"
echo ""

# Collect submitted job run IDs from orchestrator logs
SUBMITTED_JOBS_LIST=""
JOB_COUNT=0

ORCHESTRATOR_LOGS=$(find "${LOG_DIR}" -name "serverless_advisor_orchestrator_production_*.log" -type f -mmin -120 2>/dev/null)

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

# Collect S3 log paths
S3_LOG_PATHS=""
if [ -n "$ORCHESTRATOR_LOGS" ]; then
    for log_file in $ORCHESTRATOR_LOGS; do
        log_name=$(basename "$log_file")
        S3_LOG_PATHS="${S3_LOG_PATHS}  s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/orchestrator_${log_name}"$'\n'
    done
fi
S3_LOG_PATHS="${S3_LOG_PATHS}  ${S3_PIPELINE_LOG_DEST}"$'\n'

# Send email
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

body = """[DW] EMR Serverless Advisor - Backfill Pipeline Execution Report
==========================================================

Pipeline Run ID  : ${RUN_TIMESTAMP}
Start Time       : ${PIPELINE_START_TIMESTAMP}
End Time         : ${PIPELINE_END_TIMESTAMP}
Duration         : ${PIPELINE_DURATION_MIN}m ${PIPELINE_DURATION_SEC}s

BACKFILL PARAMETERS
-------------------
Lookback Hours   : ${LOOKBACK_HOURS} hours
Max Jobs Per Run : ${MAX_JOBS_PER_RUN} jobs

PIPELINE STATUS
---------------
Step 1 - Backfill Orchestrator : $([ $ORCHESTRATOR_EXIT_CODE -eq 0 ] && echo "SUCCESS" || echo "FAILED (exit code: $ORCHESTRATOR_EXIT_CODE)")
Overall Status                  : $([ $PIPELINE_EXIT_CODE -eq 0 ] && echo "SUCCESS" || echo "FAILED")

EMR SERVERLESS JOBS SUBMITTED (${JOB_COUNT} jobs)
----------------------------------------------------------
${SUBMITTED_JOBS_LIST}
S3 LOG FILE LOCATIONS
---------------------
${S3_LOG_PATHS}
All backfill logs:
  s3://${S3_LOG_BUCKET}/${S3_LOG_PREFIX}/

TARGET TABLES
-------------
Iceberg  : ${CATALOG_NAMESPACE}.serverless_config_advisor_v5
DynamoDB : prod-dynamodb-egdataplatform-dw-dataproc-emr-serverless-config-recommander

RE-RUN COMMAND (if needed)
--------------------------
  LOOKBACK_HOURS=${LOOKBACK_HOURS} MAX_JOBS_PER_RUN=${MAX_JOBS_PER_RUN} ./RUN_BACKFILL_PIPELINE.sh

---
Sent automatically by EMR Serverless Advisor Backfill Pipeline
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
