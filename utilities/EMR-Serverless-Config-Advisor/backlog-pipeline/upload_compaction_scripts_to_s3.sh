#!/bin/bash
##############################################################################
# Upload Compaction Scripts to S3
##############################################################################
# Uploads all compaction scripts to the pipeline S3 location
#
# Usage:
#   # With default AWS profile
#   ./upload_compaction_scripts_to_s3.sh
#
#   # With specific AWS profile
#   AWS_PROFILE=data-test ./upload_compaction_scripts_to_s3.sh
#
#   # With custom S3 path
#   S3_BUCKET=my-bucket S3_PREFIX=my-prefix ./upload_compaction_scripts_to_s3.sh
##############################################################################

set -e

# Configuration
S3_BUCKET="${S3_BUCKET:-${S3_BUCKET}}"
S3_PREFIX="${S3_PREFIX:-pipeline-files-v1/backlog-scale-dw}"
AWS_REGION="${AWS_REGION:-us-east-1}"

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=============================================================================="
echo "UPLOAD COMPACTION SCRIPTS TO S3"
echo "=============================================================================="
echo "Source:      ${SCRIPT_DIR}"
echo "Destination: s3://${S3_BUCKET}/${S3_PREFIX}/"
echo "Region:      ${AWS_REGION}"
if [ -n "$AWS_PROFILE" ]; then
    echo "AWS Profile: ${AWS_PROFILE}"
fi
echo "=============================================================================="
echo ""

# Files to upload
SCRIPTS=(
    "30_compact_all_iceberg_tables.py"
    "31_submit_compaction_emr_serverless.sh"
    "32_run_compaction_local.sh"
    "33_setup_compaction_cron.sh"
    "34_check_compaction_status.sh"
)

# Check if files exist
echo "Checking files..."
MISSING=0
for script in "${SCRIPTS[@]}"; do
    if [ ! -f "${SCRIPT_DIR}/${script}" ]; then
        echo "  ✗ Missing: ${script}"
        MISSING=1
    else
        echo "  ✓ Found:   ${script}"
    fi
done

if [ $MISSING -eq 1 ]; then
    echo ""
    echo "ERROR: Some files are missing. Please ensure all compaction scripts are present."
    exit 1
fi

echo ""
echo "Uploading files..."
echo ""

# Upload each file
SUCCESS=0
FAILED=0

for script in "${SCRIPTS[@]}"; do
    echo -n "  Uploading ${script}... "
    if aws s3 cp "${SCRIPT_DIR}/${script}" "s3://${S3_BUCKET}/${S3_PREFIX}/${script}" \
        --region "${AWS_REGION}" ${AWS_PROFILE:+--profile $AWS_PROFILE} 2>&1 | grep -q "upload:"; then
        echo "✓"
        ((SUCCESS++))
    else
        echo "✗ FAILED"
        ((FAILED++))
    fi
done

echo ""
echo "=============================================================================="
echo "UPLOAD SUMMARY"
echo "=============================================================================="
echo "Total files:    ${#SCRIPTS[@]}"
echo "Uploaded:       ${SUCCESS}"
echo "Failed:         ${FAILED}"
echo ""

if [ $FAILED -eq 0 ]; then
    echo "✓ All files uploaded successfully!"
    echo ""
    echo "Files are now available at:"
    echo "  s3://${S3_BUCKET}/${S3_PREFIX}/"
    echo ""
    echo "To list uploaded files:"
    echo "  aws s3 ls s3://${S3_BUCKET}/${S3_PREFIX}/ --region ${AWS_REGION}"
    echo ""
    echo "To verify compaction script:"
    echo "  aws s3 ls s3://${S3_BUCKET}/${S3_PREFIX}/30_compact_all_iceberg_tables.py"
    echo "=============================================================================="
    exit 0
else
    echo "✗ Some uploads failed. Please check:"
    echo "  1. AWS credentials are configured"
    echo "  2. IAM permissions for s3:PutObject on bucket ${S3_BUCKET}"
    echo "  3. Bucket exists and is in region ${AWS_REGION}"
    echo ""
    echo "To test permissions:"
    echo "  aws s3 ls s3://${S3_BUCKET}/ --region ${AWS_REGION}"
    echo "=============================================================================="
    exit 1
fi
