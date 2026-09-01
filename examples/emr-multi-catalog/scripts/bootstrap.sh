#!/usr/bin/env bash
#
# Bootstrap the single-account multi-catalog demo:
#   1. create the AWS Glue database (via the Glue API, so it exists before the
#      demo issues CREATE TABLE),
#   2. upload the PySpark worker (multicatalog_demo.py) to S3,
#   3. report the EMR Serverless application state.
#
# Reads APP_ID / ROLE_ARN / BUCKET / REGION from .env (or pass as flags).
#
# Usage:
#   cp env.template .env      # then edit values
#   ./scripts/bootstrap.sh
#
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for _envf in "./.env" "${SELF_DIR}/../.env" "${SELF_DIR}/.env"; do
  [[ -f "$_envf" ]] && { set -a; . "$_envf"; set +a; break; }
done

DB="${DB:-salesdb}"; REGION="${REGION:-us-east-1}"; TARGET="${TARGET:-ec2}"
CLUSTER_ID="${CLUSTER_ID:-}"; APP_ID="${APP_ID:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)     TARGET="$2"; shift 2;;
    --cluster-id) CLUSTER_ID="$2"; shift 2;;
    --app-id)     APP_ID="$2"; shift 2;;
    --bucket)     BUCKET="$2"; shift 2;;
    --region)     REGION="$2"; shift 2;;
    --db)         DB="$2"; shift 2;;
    *) echo "unknown flag: $1" >&2; exit 2;;
  esac
done

: "${BUCKET:?set BUCKET in .env or pass --bucket}"
PREFIX="s3://${BUCKET}/multicatalog"
WAREHOUSE="${PREFIX}/warehouse"
SCRIPTS="${PREFIX}/scripts"

echo ">> creating Glue database '${DB}' (idempotent)"
aws glue create-database --region "$REGION" \
  --database-input "{\"Name\":\"${DB}\",\"LocationUri\":\"${WAREHOUSE}/${DB}.db\"}" 2>/dev/null \
  || echo "   database '${DB}' already exists"

echo ">> uploading PySpark worker to ${SCRIPTS}/"
aws s3 cp "${SELF_DIR}/multicatalog_demo.py" "${SCRIPTS}/multicatalog_demo.py" --region "$REGION" >/dev/null

if [[ "$TARGET" == "ec2" && -n "$CLUSTER_ID" ]]; then
  ST=$(aws emr describe-cluster --region "$REGION" --cluster-id "$CLUSTER_ID" \
        --query 'Cluster.Status.State' --output text 2>/dev/null || echo "UNKNOWN")
  echo ">> EMR on EC2 cluster ${CLUSTER_ID} state=${ST}"
elif [[ "$TARGET" == "serverless" && -n "$APP_ID" ]]; then
  ST=$(aws emr-serverless get-application --region "$REGION" --application-id "$APP_ID" \
        --query 'application.state' --output text 2>/dev/null || echo "UNKNOWN")
  echo ">> EMR Serverless application ${APP_ID} state=${ST}"
else
  echo ">> (no cluster/app id set; skipping compute check)"
fi

echo ">> bootstrap complete. Next: ./scripts/run_demo.sh --phase setup"
