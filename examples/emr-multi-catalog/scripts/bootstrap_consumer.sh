#!/usr/bin/env bash
#
# Cross-account demo bootstrap — CONSUMER side.
# Run this with the CONSUMER account's credentials. It:
#   1. creates an S3 bucket for scripts/warehouse/logs (if needed),
#   2. creates an EMR Serverless execution role with the permissions the demo needs
#      (Glue + S3 on this account, Lake Formation GetDataAccess, and — if
#      --producer-account is given — cross-account Glue/S3 read),
#   3. creates an EMR 8.1 Serverless application,
#   and prints the --app-id / --role-arn / --bucket to pass to run_demo.sh.
#
# NOTE: requires the public EMR 8.1 release label to be available in your region.
# Until EMR 8.1 is GA you can pass --release / --endpoint to target a preview.
#
# Usage:
#   ./bootstrap_consumer.sh [--region us-east-1] [--bucket <bucket>]
#       [--role-name EMRMultiCatalogDemoRole] [--app-name multicatalog-demo]
#       [--producer-account 111122223333] [--producer-bucket <producer-bucket>]
#       [--release emr-8.1.0] [--endpoint <emr-serverless-endpoint>]
set -euo pipefail

REGION="us-east-1" BUCKET="" ROLE_NAME="EMRMultiCatalogDemoRole"
APP_NAME="multicatalog-demo" PRODUCER_ACCOUNT="" PRODUCER_BUCKET=""
RELEASE="emr-8.1.0" ENDPOINT="" DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)            REGION="$2"; shift 2;;
    --bucket)            BUCKET="$2"; shift 2;;
    --role-name)         ROLE_NAME="$2"; shift 2;;
    --app-name)          APP_NAME="$2"; shift 2;;
    --producer-account)  PRODUCER_ACCOUNT="$2"; shift 2;;
    --producer-bucket)   PRODUCER_BUCKET="$2"; shift 2;;
    --release)           RELEASE="$2"; shift 2;;
    --endpoint)          ENDPOINT="$2"; shift 2;;
    --dry-run)           DRY_RUN=true; shift;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done
EP_FLAG=""; [[ -n "$ENDPOINT" ]] && EP_FLAG="--endpoint-url $ENDPOINT"
$DRY_RUN && echo ">> DRY RUN — no resources will be created (read-only calls still run)"

# run a MUTATING command, or just print it under --dry-run
run() { if $DRY_RUN; then echo "  [dry-run] $*"; else "$@"; fi; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
if [[ -z "$ACCOUNT" ]]; then
  $DRY_RUN && ACCOUNT="<account-id>" || { echo "cannot determine caller account (invalid/expired creds?)" >&2; exit 1; }
fi
BUCKET="${BUCKET:-multicatalog-demo-${ACCOUNT}-${REGION//-/}}"
echo ">> consumer account=${ACCOUNT} region=${REGION} bucket=${BUCKET}"

# 1. bucket -------------------------------------------------------------------
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [[ "$REGION" == "us-east-1" ]]; then
    run aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    run aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"
  fi
  echo "   created bucket ${BUCKET}"
fi

# 2. execution role -----------------------------------------------------------
cat > /tmp/trust.json <<'EOF'
{ "Version": "2012-10-17", "Statement": [{
  "Effect": "Allow",
  "Principal": {"Service": "emr-serverless.amazonaws.com"},
  "Action": "sts:AssumeRole"
}]}
EOF
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  run aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document file:///tmp/trust.json
  echo "   created role ${ROLE_NAME}"
fi
ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null \
           || echo "arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}")

# base permissions: own Glue catalog + this bucket + Lake Formation data access
XACCT_GLUE=""; XACCT_S3=""
if [[ -n "$PRODUCER_ACCOUNT" ]]; then
  XACCT_GLUE=",\"arn:aws:glue:${REGION}:${PRODUCER_ACCOUNT}:catalog\",\"arn:aws:glue:${REGION}:${PRODUCER_ACCOUNT}:database/*\",\"arn:aws:glue:${REGION}:${PRODUCER_ACCOUNT}:table/*/*\""
  if [[ -n "$PRODUCER_BUCKET" ]]; then
    XACCT_S3=",\"arn:aws:s3:::${PRODUCER_BUCKET}\",\"arn:aws:s3:::${PRODUCER_BUCKET}/*\""
  fi
fi
cat > /tmp/exec-policy.json <<EOF
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "GlueAccess", "Effect": "Allow",
    "Action": ["glue:Get*","glue:BatchGet*","glue:CreateDatabase","glue:CreateTable",
               "glue:UpdateTable","glue:DeleteTable","glue:UpdateDatabase"],
    "Resource": ["arn:aws:glue:${REGION}:${ACCOUNT}:catalog",
                 "arn:aws:glue:${REGION}:${ACCOUNT}:database/*",
                 "arn:aws:glue:${REGION}:${ACCOUNT}:table/*/*"${XACCT_GLUE}] },
  { "Sid": "S3Access", "Effect": "Allow",
    "Action": ["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"],
    "Resource": ["arn:aws:s3:::${BUCKET}","arn:aws:s3:::${BUCKET}/*"${XACCT_S3}] },
  { "Sid": "LakeFormation", "Effect": "Allow",
    "Action": ["lakeformation:GetDataAccess"], "Resource": "*" }
] }
EOF
run aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name multicatalog-demo --policy-document file:///tmp/exec-policy.json
echo "   attached inline policy to ${ROLE_NAME}"
[[ -n "$PRODUCER_ACCOUNT" && -z "$PRODUCER_BUCKET" ]] && \
  echo "   NOTE: pass --producer-bucket to allow cross-account S3 data reads."

# 3. EMR Serverless application ----------------------------------------------
APP_ID=$(aws emr-serverless list-applications $EP_FLAG --region "$REGION" \
          --query "applications[?name=='${APP_NAME}'].id | [0]" --output text 2>/dev/null || echo "None")
if [[ "$APP_ID" == "None" || -z "$APP_ID" ]]; then
  if $DRY_RUN; then
    echo "  [dry-run] aws emr-serverless create-application --name ${APP_NAME} --release-label ${RELEASE} --type SPARK"
    APP_ID="<new-app-id>"
  else
    APP_ID=$(aws emr-serverless create-application $EP_FLAG --region "$REGION" \
              --name "$APP_NAME" --release-label "$RELEASE" --type SPARK \
              --query 'applicationId' --output text)
    echo "   created EMR Serverless app ${APP_NAME} (${RELEASE})"
  fi
fi

echo ""
echo ">> DONE. Consumer setup complete."
echo "   --app-id   ${APP_ID}"
echo "   --role-arn ${ROLE_ARN}"
echo "   --bucket   ${BUCKET}"
echo "   Run the demo:"
echo "     ./run_demo.sh --phase all --app-id ${APP_ID} --role-arn ${ROLE_ARN} --bucket ${BUCKET} --region ${REGION} \\"
echo "        ${PRODUCER_ACCOUNT:+--producer-account ${PRODUCER_ACCOUNT} --producer-db salesdb --producer-table fulfillment}"
